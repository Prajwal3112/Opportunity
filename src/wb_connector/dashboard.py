"""A filter-driven dashboard over live World Bank procurement notices.

Query-on-demand, not a mirror. Each search term costs exactly one API request, because
every product term's whole population fits inside one ``rows=1000`` page -- SIEM 213,
DLP 276, cybersecurity 476. A twelve-term filter is twelve requests against the live
service, which is both cheaper and fresher than re-crawling 417,948 notices to serve a
dozen rows. Nothing is replicated locally except what the user actually engages with.

Run it::

    python -m wb_connector.dashboard          # then open http://127.0.0.1:8000

State lives in a single SQLite file next to the repo (``dashboard.db``): the saved
filter, which notices have been seen, and which have been pursued or dismissed. The
notices themselves are not stored -- they are re-fetched, because there is no date
parameter on this API and therefore no such thing as a cheap delta.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import quote

from opportunity_engine.biddability import assess
from opportunity_engine.domain.adapters import candidate_input_from_procurement
from opportunity_engine.domain.models import CapabilityProfile
from opportunity_engine.domain.taxonomy import TermTier, load_taxonomy
from opportunity_engine.matching.engine import CandidateEngine
from opportunity_engine.matching.scoring import CandidateScoringConfig
from opportunity_engine.matching.spans import contains_any, find_spans
from opportunity_engine.matching.textclean import strip_html

from . import catalogue, dossier
from .client import WorldBankApiClient
from .config import get_settings
from .connectors import ProcurementConnector
from .errors import WorldBankConnectorError, WorldBankResponseError

# Anchored to the repository, NOT to the working directory. Task Scheduler defaults its
# "Start in" to the system directory and cron defaults to $HOME; a CWD-relative path made
# a scheduled digest silently open a different, empty store.
DB_PATH = Path(os.environ.get("WB_DASHBOARD_DB") or Path(__file__).resolve().parents[2] / "dashboard.db")
TAXONOMY_PATH = Path(
    os.environ.get("WB_TAXONOMY")
    or Path(__file__).resolve().parents[2] / "profiles" / "cybersecurity.json"
)
PATH = "/api/procnotices"

# How many terms the first-run seed may contain. Every term is one HTTP request, so this
# bounds a page load against a public service. The user edits the list in the page.
MAX_SEED_TERMS = int(os.environ.get("WB_MAX_SEED_TERMS", "24"))
DEFAULT_HIDE: list[str] = []


def default_find() -> list[str]:
    """Seed the filter from the taxonomy, not from a hard-coded list.

    The seed is the RETRIEVAL layer and the taxonomy was only the FILTER layer, so 55 of
    the 74 EXCLUSIVE/STRONG terms were never searched for -- they could never match
    anything, however good the taxonomy was. Deriving the seed from the profile is what
    makes "swap the JSON, swap the domain" true for recall and not just for filtering.

    EXCLUSIVE terms come first: they are the ones that can stand alone as evidence.
    """
    try:
        taxonomy = load_taxonomy(TAXONOMY_PATH, strict=True)
    except (OSError, ValueError):
        return ["cybersecurity"]
    ordered: list[str] = []
    for tier in (TermTier.EXCLUSIVE, TermTier.STRONG):
        for term in taxonomy.terms:
            if term.tier is tier and term.text not in ordered:
                ordered.append(term.text)
    return ordered[:MAX_SEED_TERMS] or ["cybersecurity"]

# One request is issued per term at this page size. A term whose total exceeds it would
# be silently truncated -- and the returned window is NOT date-ordered (a real page spans
# 2006-2025), so truncation discards an arbitrary slice in which currently-open notices
# are a small minority. So the guard is the page size, not an arbitrary larger number.
logger = logging.getLogger(__name__)

# Where a PDF-only document is saved for the user's own OCR step.
DOCUMENT_DIR = Path(os.environ.get("WB_DOCUMENT_DIR") or Path(DB_PATH).parent / "documents")

PAGE_ROWS = 1000
TOO_BROAD = PAGE_ROWS


class TermList(StrEnum):
    """Which list a term belongs to.

    Was a bare string with `key = "find" if kind == "find" else "hide"`, so any value
    other than "find" silently wrote to the exclude list -- a request with kind="evil"
    added an exclusion and returned 303 as though it had worked.
    """

    FIND = "find"
    HIDE = "hide"
    REGIONS = "regions"


class TriageState(StrEnum):
    """Closed set. /mark previously accepted and stored any string."""

    PURSUING = "PURSUING"
    DISMISSED = "DISMISSED"


# --------------------------------------------------------------------------------- db
def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS filters (
            name TEXT PRIMARY KEY, find TEXT, hide TEXT,
            only_biddable INT DEFAULT 1, firms_only INT DEFAULT 1, updated_at TEXT);
        CREATE TABLE IF NOT EXISTS seen (
            notice_id TEXT PRIMARY KEY, first_seen_at TEXT);
        CREATE TABLE IF NOT EXISTS notice_state (
            notice_id TEXT PRIMARY KEY, state TEXT, note TEXT,
            raw_payload TEXT, updated_at TEXT);
        -- Distinct from `seen`. `seen` records what a process FETCHED; this records what
        -- a human was actually TOLD. Novelty is measured against this one, so a notice
        -- stays eligible until it has genuinely been delivered.
        CREATE TABLE IF NOT EXISTS delivered (
            notice_id TEXT PRIMARY KEY, delivered_at TEXT, channel TEXT);
        -- Transient: replaced on every search. Exists so /mark can capture the record the
        -- user was actually looking at. Durable copies live in notice_state.raw_payload.
        CREATE TABLE IF NOT EXISTS notice_cache (
            notice_id TEXT PRIMARY KEY, raw_payload TEXT, cached_at TEXT);
        -- Fetch caches. A notice body is 96.7% of its weight, so the body is stored apart
        -- from the rest and kept far longer; `term_cache` remembers which notices a term
        -- returned, so re-filtering needs no network.
        CREATE TABLE IF NOT EXISTS term_cache (
            term TEXT PRIMARY KEY, total INT, notice_ids TEXT, fetched_at TEXT);
        CREATE TABLE IF NOT EXISTS light_cache (
            notice_id TEXT PRIMARY KEY, payload TEXT, cached_at TEXT);
        CREATE TABLE IF NOT EXISTS body_cache (
            notice_id TEXT PRIMARY KEY, notice_text TEXT, cached_at TEXT);
        -- The engine verdict for a notice cannot change unless the catalogue changes, so
        -- it is keyed by taxonomy_version and reused. Re-running the matcher over every
        -- body on every page load was the dominant cost of a warm page.
        CREATE TABLE IF NOT EXISTS match_cache (
            notice_id TEXT NOT NULL, taxonomy_version TEXT NOT NULL,
            matched INT NOT NULL, why TEXT, decided_at TEXT,
            PRIMARY KEY (notice_id, taxonomy_version));
        """
    )
    _migrate(conn)
    return conn


# Ordered, append-only. `CREATE TABLE IF NOT EXISTS` cannot add a column to an existing
# file, so without this any new column breaks a user who already has a dashboard.db.
_MIGRATIONS: list[str] = [
    # Countries and regions the user will actually bid in. Added after launch, so it has
    # to be a migration: CREATE TABLE IF NOT EXISTS cannot add a column to a file that
    # already exists, and anyone already running the dashboard has one.
    "ALTER TABLE filters ADD COLUMN regions TEXT DEFAULT '[]'",
]


def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for index, statement in enumerate(_MIGRATIONS[version:], start=version):
        conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {index + 1}")
    conn.commit()


def load_filter(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM filters WHERE name='default'").fetchone()
    if row is None:
        save_filter(conn, default_find(), DEFAULT_HIDE, True, True, [])
        row = conn.execute("SELECT * FROM filters WHERE name='default'").fetchone()
    try:
        regions = json.loads(row["regions"] or "[]")
    except (ValueError, TypeError, IndexError, KeyError):
        regions = []
    return {
        "find": json.loads(row["find"]), "hide": json.loads(row["hide"]),
        "only_biddable": bool(row["only_biddable"]), "firms_only": bool(row["firms_only"]),
        # Empty means everywhere. An empty market list is the sane default: a filter
        # nobody set should never be the reason a tender is invisible.
        "regions": regions,
    }


def save_filter(conn: sqlite3.Connection, find: list[str], hide: list[str],
                only_biddable: bool, firms_only: bool,
                regions: list[str] | None = None) -> None:
    conn.execute(
        "INSERT INTO filters(name,find,hide,only_biddable,firms_only,regions,updated_at) "
        "VALUES('default',?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
        "find=excluded.find, hide=excluded.hide, only_biddable=excluded.only_biddable, "
        "firms_only=excluded.firms_only, regions=excluded.regions, "
        "updated_at=excluded.updated_at",
        (json.dumps(find), json.dumps(hide), int(only_biddable), int(firms_only),
         json.dumps(regions or []), datetime.now(UTC).isoformat()),
    )
    conn.commit()


def mark_seen(conn: sqlite3.Connection, ids: list[str]) -> set[str]:
    """Record ids and return which were new. Without this, 'new since Monday' is
    unanswerable -- the API has no date filter, so novelty exists only as the difference
    between what we fetched and what we remember fetching."""
    known = {r["notice_id"] for r in conn.execute("SELECT notice_id FROM seen")}
    fresh = [i for i in ids if i not in known]
    now = datetime.now(UTC).isoformat()
    conn.executemany("INSERT OR IGNORE INTO seen VALUES(?,?)", [(i, now) for i in fresh])
    conn.commit()
    return set(fresh)


def delivered_ids(conn: sqlite3.Connection) -> set[str]:
    return {row["notice_id"] for row in conn.execute("SELECT notice_id FROM delivered")}


def mark_delivered(conn: sqlite3.Connection, ids: list[str], channel: str) -> None:
    """Record that a human was told. Called ONLY after a channel confirms delivery."""
    now = datetime.now(UTC).isoformat()
    conn.executemany("INSERT OR IGNORE INTO delivered VALUES(?,?,?)",
                     [(i, now, channel) for i in ids])
    conn.commit()


def recently_seen(conn: sqlite3.Connection, within_hours: int = 72) -> set[str]:
    """Ids first fetched within the window.

    The NEW badge is derived from this rather than from the current request's fresh set,
    so a page refresh or a redirect after clicking an action no longer erases the badge
    on every other row.
    """
    cutoff = (datetime.now(UTC) - timedelta(hours=within_hours)).isoformat()
    return {row["notice_id"] for row in
            conn.execute("SELECT notice_id FROM seen WHERE first_seen_at >= ?", (cutoff,))}


def states(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["notice_id"]: r["state"] for r in conn.execute("SELECT notice_id,state FROM notice_state")}


def cache_records(conn: sqlite3.Connection, records: dict[str, dict]) -> None:
    """Replace the on-screen cache. Only rows the user can actually see are kept."""
    now = datetime.now(UTC).isoformat()
    conn.execute("DELETE FROM notice_cache")
    conn.executemany("INSERT OR REPLACE INTO notice_cache VALUES(?,?,?)",
                     [(i, json.dumps(r), now) for i, r in records.items()])
    conn.commit()


def cached_record(conn: sqlite3.Connection, notice_id: str) -> dict | None:
    row = conn.execute("SELECT raw_payload FROM notice_cache WHERE notice_id=?",
                       (notice_id,)).fetchone()
    if row is None or not row["raw_payload"]:
        return None
    try:
        return json.loads(row["raw_payload"])
    except ValueError:
        return None


def set_state(conn: sqlite3.Connection, notice_id: str, state: str, raw: dict | None) -> None:
    """Record a triage decision, capturing the notice itself when one is available.

    COALESCE is deliberate: a later update that carries no payload must never erase a
    payload already stored. Without the stored copy, a PURSUING notice's deadline reminder
    stops the moment the user edits a filter term -- the bid being actively worked was the
    least durable thing in the store.
    """
    conn.execute(
        "INSERT INTO notice_state(notice_id,state,raw_payload,updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(notice_id) DO UPDATE SET state=excluded.state, "
        "raw_payload=COALESCE(excluded.raw_payload, notice_state.raw_payload), "
        "updated_at=excluded.updated_at",
        (notice_id, state, json.dumps(raw) if raw else None, datetime.now(UTC).isoformat()),
    )
    conn.commit()


# ------------------------------------------------------------------------- fetch tuning
# Every field the product reads, except the body. `fl` is honoured by /api/procnotices
# though it is documented only for WDS -- verified live: 486 records fell from 7.66 MB to
# 0.27 MB, and 2.6s to 1.0s. Keep this in step with what `normalize_record` consumes; a
# field omitted here arrives as None, which is a silent data loss, not an error.
LIGHT_FIELDS = (
    "id,bid_description,bid_reference_no,notice_type,notice_status,noticedate,"
    "submission_date,submission_deadline_date,submission_deadline_time,"
    "procurement_method_code,procurement_method_name,procurement_group,"
    "project_id,project_name,project_ctry_name,notice_lang_name,"
    "contact_organization,contact_name,contact_email,contact_phone_no,"
    "contact_address,contact_web_url,contact_ctry_name"
)
BODY_FIELDS = "id,notice_text"

# Concurrent requests. Phase one is now a tenth of the bytes, so more of them in flight
# is no longer rude, and it is what turns a six-wave fetch into two.
FETCH_CONCURRENCY = 8
# Body requests are ~25 KB each rather than ~7 MB, so more can be in flight at once.
BODY_CONCURRENCY = 16
# A ceiling on bodies per load. Reached only when the filters are turned off and the
# terms are broad; past this the honest answer is to say so and ask for a narrower term,
# not to spend two minutes fetching text nobody will read.
MAX_BODY_FETCH = 400

# How long a term's result set stays reusable. Notices are published on a daily cadence,
# so minutes of staleness cannot cost an opportunity, and the Refresh control forces a
# refetch whenever the user wants certainty.
TERM_CACHE_TTL = timedelta(minutes=15)
# Bodies are immutable in practice and expensive, so they outlive the term cache.
BODY_CACHE_TTL = timedelta(days=3)


def cached_term_results(conn: sqlite3.Connection, terms: list[str]) -> dict[str, tuple[int | None, list[str]]]:
    """Fresh per-term result sets. A stale row is treated as absent, not as data."""
    if not terms:
        return {}
    cutoff = (datetime.now(UTC) - TERM_CACHE_TTL).isoformat()
    marks = ",".join("?" * len(terms))
    rows = conn.execute(
        # `marks` is a run of literal "?" placeholders, never user text.
        f"SELECT term, total, notice_ids FROM term_cache "
        f"WHERE term IN ({marks}) AND fetched_at >= ?",
        (*terms, cutoff),
    ).fetchall()
    out: dict[str, tuple[int | None, list[str]]] = {}
    for row in rows:
        try:
            out[row["term"]] = (row["total"], json.loads(row["notice_ids"]))
        except (ValueError, TypeError):
            continue
    return out


def store_term_results(conn: sqlite3.Connection, results: dict[str, tuple[int | None, list[str]]]) -> None:
    now = datetime.now(UTC).isoformat()
    conn.executemany(
        """INSERT INTO term_cache (term, total, notice_ids, fetched_at) VALUES (?,?,?,?)
           ON CONFLICT(term) DO UPDATE SET total=excluded.total,
               notice_ids=excluded.notice_ids, fetched_at=excluded.fetched_at""",
        [(term, total, json.dumps(ids), now) for term, (total, ids) in results.items()],
    )
    conn.commit()


def light_records(conn: sqlite3.Connection, notice_ids: set[str]) -> dict[str, dict]:
    """Body-less records for cached terms, so a cache hit needs no network at all."""
    if not notice_ids:
        return {}
    out: dict[str, dict] = {}
    ids = sorted(notice_ids)
    for chunk in (ids[i:i + 500] for i in range(0, len(ids), 500)):
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"SELECT notice_id, payload FROM light_cache WHERE notice_id IN ({marks})", chunk
        ):
            try:
                out[row["notice_id"]] = json.loads(row["payload"])
            except (ValueError, TypeError):
                continue
    return out


def store_light_records(conn: sqlite3.Connection, records: dict[str, dict]) -> None:
    if not records:
        return
    now = datetime.now(UTC).isoformat()
    conn.executemany(
        """INSERT INTO light_cache (notice_id, payload, cached_at) VALUES (?,?,?)
           ON CONFLICT(notice_id) DO UPDATE SET payload=excluded.payload,
               cached_at=excluded.cached_at""",
        # The body is stored separately and is far larger; keeping it out of this row
        # means a light record stays a few hundred bytes.
        [(i, json.dumps({k: v for k, v in r.items() if k != "notice_text"}), now)
         for i, r in records.items()],
    )
    conn.commit()


def cached_bodies(conn: sqlite3.Connection, notice_ids: set[str]) -> dict[str, str]:
    if not notice_ids:
        return {}
    cutoff = (datetime.now(UTC) - BODY_CACHE_TTL).isoformat()
    out: dict[str, str] = {}
    ids = sorted(notice_ids)
    for chunk in (ids[i:i + 500] for i in range(0, len(ids), 500)):
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"SELECT notice_id, notice_text FROM body_cache "
            f"WHERE notice_id IN ({marks}) AND cached_at >= ?", (*chunk, cutoff)
        ):
            out[row["notice_id"]] = row["notice_text"] or ""
    return out


def cached_verdicts(conn: sqlite3.Connection, notice_ids: list[str], version: str) -> dict[str, tuple[bool, str]]:
    """Remembered engine verdicts for this exact catalogue version.

    Keyed by version so that editing the product catalogue invalidates every verdict at
    once rather than leaving stale decisions attributed to rules that no longer exist.
    """
    if not notice_ids:
        return {}
    out: dict[str, tuple[bool, str]] = {}
    ids = sorted(notice_ids)
    for chunk in (ids[i:i + 500] for i in range(0, len(ids), 500)):
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"SELECT notice_id, matched, why FROM match_cache "
            f"WHERE taxonomy_version = ? AND notice_id IN ({marks})", (version, *chunk)
        ):
            out[row["notice_id"]] = (bool(row["matched"]), row["why"] or "")
    return out


def store_verdicts(conn: sqlite3.Connection, verdicts: dict[str, tuple[bool, str]], version: str) -> None:
    if not verdicts:
        return
    now = datetime.now(UTC).isoformat()
    conn.executemany(
        """INSERT INTO match_cache (notice_id, taxonomy_version, matched, why, decided_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(notice_id, taxonomy_version) DO UPDATE SET
               matched=excluded.matched, why=excluded.why, decided_at=excluded.decided_at""",
        [(i, version, int(matched), why, now) for i, (matched, why) in verdicts.items()],
    )
    conn.commit()


def store_bodies(conn: sqlite3.Connection, bodies: dict[str, str]) -> None:
    if not bodies:
        return
    now = datetime.now(UTC).isoformat()
    conn.executemany(
        """INSERT INTO body_cache (notice_id, notice_text, cached_at) VALUES (?,?,?)
           ON CONFLICT(notice_id) DO UPDATE SET notice_text=excluded.notice_text,
               cached_at=excluded.cached_at""",
        [(i, text, now) for i, text in bodies.items()],
    )
    conn.commit()


def _cheap_date(value: Any) -> date | None:
    """Parse a deadline straight off the raw record, without building a model."""
    if not value:
        return None
    # The service emits ISO timestamps; the date is always the leading ten characters,
    # so there is no need to parse the time or the zone designator at all.
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _in_chosen_markets(record: dict, regions: tuple[str, ...]) -> bool:
    """Whether this notice sits in a market the organisation will bid in.

    Empty means everywhere, so a filter nobody set can never hide a tender.

    Substring matching, deliberately unlike the body exclusions, which must match whole
    words. The field here is one short country or region name, not a 900 KB document, so
    the failure mode that made substrings dangerous there does not exist -- and substrings
    are what makes this useful: typing "Africa" should catch both "South Africa" and the
    World Bank's own "Eastern and Southern Africa", which is how a third of these notices
    name their location.
    """
    if not regions:
        return True
    where = str(record.get("project_ctry_name") or "").casefold()
    return any(r in where for r in regions)


def _survives_cheap_filters(record: dict, *, only_biddable: bool, firms_only: bool,
                            as_of: date, regions: tuple[str, ...] = ()) -> bool:
    """The filters that need no body, decided on the raw record.

    Deliberately does **not** build a ``ProcurementNotice``. Validating a Pydantic model
    for every record was the dominant cost of a warm page -- about 3,000 records parsed to
    keep two -- and these four fields are all the decision needs. The verdict still comes
    from ``biddability.assess``, the single shared owner, so this cannot drift from what
    the rest of the system believes; only the plumbing is cheaper.
    """
    if not _in_chosen_markets(record, regions):
        return False
    if only_biddable:
        verdict = assess(
            notice_type=record.get("notice_type"),
            notice_status=record.get("notice_status"),
            procurement_method_name=record.get("procurement_method_name"),
            submission_deadline_date=_cheap_date(record.get("submission_deadline_date")),
            as_of=as_of,
        )
        if not verdict.is_open:
            return False
    return not (
        firms_only
        and str(record.get("procurement_method_name") or "").strip().casefold()
        == "individual consultant selection"
    )


async def fetch_bodies(client, notice_ids: list[str], warnings: list[str]) -> dict[str, str]:
    """Bodies for specific notices, one request each via ``id=``.

    Undocumented but verified: ``id=<one>`` returns exactly one record, and ``fl`` works
    alongside it, so a body costs about 25 KB instead of the 7.66 MB page it came on.
    Comma-separated ids return nothing, so there is no batching to be had.

    There is deliberately no bulk fallback. An earlier version switched to one wide page
    above a threshold, on the reasoning that many small requests must cost more -- they do
    not. A live default filter leaves 262 survivors out of 1,640 notices; fetching those
    262 bodies individually is about 6 MB, where re-requesting the 24 terms with bodies
    included would be roughly 180 MB. The fallback was also simply broken: it queried
    ``qterm=" "``, which returns nothing, so no body ever loaded.
    """
    out: dict[str, str] = {}
    if not notice_ids:
        return out

    if len(notice_ids) > MAX_BODY_FETCH:
        warnings.append(
            f"{len(notice_ids):,} notices match your terms — too many to read in full. "
            f"The {MAX_BODY_FETCH} closest to their deadline were loaded completely; "
            "narrow a term, or turn on 'only what I can bid on', to see the rest."
        )
        notice_ids = notice_ids[:MAX_BODY_FETCH]

    semaphore = asyncio.Semaphore(BODY_CONCURRENCY)
    failures: list[str] = []

    async def one(notice_id: str) -> None:
        async with semaphore:
            try:
                payload, _ = await client.get_json(
                    PATH, {"id": notice_id, "rows": 1, "os": 0, "fl": BODY_FIELDS})
            except WorldBankConnectorError:
                failures.append(notice_id)
                return
        for record in payload.get("procnotices") or []:
            # Strip here, once. The markup is never wanted again, and stripping it on
            # every page load was costing more than the download it came from.
            out[notice_id] = strip_html(record.get("notice_text") or "")
            return
        # A notice with no body is legitimate (3.6% carry none). Record the empty result
        # so the cache does not re-ask for it on every load.
        out[notice_id] = ""

    await asyncio.gather(*(one(i) for i in notice_ids))
    if failures:
        warnings.append(
            f"Could not load the full text of {len(failures)} notice(s); they are matched "
            "on their title alone.")
    return out


# ------------------------------------------------------------------------------ search
class Result:
    """One row. `is_new` is mutable: it is refined after the fetch ledger is written."""

    __slots__ = ("days", "docs", "is_new", "notice", "raw", "state", "why")

    def __init__(self, notice, raw, why, days, is_new, state, docs=()):
        self.notice, self.raw, self.why = notice, raw, why
        self.days, self.is_new, self.state = days, is_new, state
        # The project's published documents. Attached after the search, because a
        # document lookup must never be able to cost the user a tender.
        self.docs = list(docs)


async def search(find: list[str], hide: list[str], *, only_biddable: bool, firms_only: bool,
                 record_seen: bool = True, fresh: bool = False,
                 regions: list[str] | None = None,
                 ) -> tuple[list[Result], dict[str, int | None], list[str]]:
    """Fetch in two phases, because a notice body is 96.7% of its weight.

    Measured on a live search for ``cybersecurity``: 486 notices, 7.66 MB, and
    ``notice_text`` alone accounted for 7.19 MB of it. Of those 486, **six** survived the
    biddability and firms-only filters. Downloading 480 bodies to discard them took about
    two and a half seconds per term on a phone hotspot, and the terms multiply it.

    So: phase one asks for every field *except* the body (``fl`` is honoured by
    ``/api/procnotices``, undocumented, like ``qterm``), which is 0.27 MB instead of
    7.66 MB. The cheap filters run on that. Phase two then fetches bodies only for the
    survivors, one notice at a time by ``id`` -- also undocumented, verified to return
    exactly one record. Bodies are cached, so a survivor already seen costs nothing.

    Above ``BULK_BODY_THRESHOLD`` survivors the per-notice fetches would cost more than
    one bulk page, so the bulk path is used instead. That case arises when the user turns
    the biddability filter off, and is the only regime where the old behaviour was right.

    ``record_seen`` controls the fetch ledger. The dashboard records (it is showing the
    rows to a human); the digest passes False, because for the digest "delivered" is the
    ledger that matters and it must not be written until a channel confirms.

    ``fresh`` bypasses the per-term cache. Without it, a term whose result set was
    fetched within ``TERM_CACHE_TTL`` is reused, which is what makes removing a term or
    flipping a switch cost no requests at all.
    """
    settings = get_settings()
    # strict: the linter is what catches a phrase that cannot match its own text. Such a
    # phrase loses recall in total silence -- exactly the failure this product exists to
    # prevent -- so the filter path refuses to run on a taxonomy that fails its own lint.
    taxonomy = load_taxonomy(TAXONOMY_PATH, strict=True)
    engine = CandidateEngine(CandidateScoringConfig(), taxonomy)
    profile = CapabilityProfile(profile_id="org", version="1", organization_name="Organisation")
    totals: dict[str, int | None] = {}
    warnings: list[str] = []
    raw_by_id: dict[str, dict] = {}

    # Reuse whole term result sets that were fetched recently. This is the single change
    # that makes the page feel instant: removing a term, adding an excluded word or
    # flipping a switch needs no new data at all, and previously refetched everything.
    cached_terms: dict[str, tuple[int | None, list[str]]] = {}
    if not fresh:
        with closing(connect()) as conn:
            cached_terms = cached_term_results(conn, find)

    to_fetch = [t for t in find if t not in cached_terms]
    fetched_terms: dict[str, tuple[int | None, list[str]]] = {}

    async with WorldBankApiClient(settings) as client:
        async def one(term: str) -> None:
            """Phase one: everything but the body."""
            try:
                payload, _ = await client.get_json(
                    PATH, {"qterm": term, "rows": PAGE_ROWS, "os": 0, "fl": LIGHT_FIELDS})
            except WorldBankConnectorError as exc:
                totals[term] = None
                warnings.append(f"{term}: {exc}")
                return
            try:
                total = int(str(payload.get("total")))
            except (TypeError, ValueError):
                total = None
            totals[term] = total
            if total is not None and total > TOO_BROAD:
                warnings.append(
                    f"'{term}' matches {total:,} notices - more than one page "
                    f"({PAGE_ROWS}), so it would be silently truncated. Add a word to "
                    "narrow it."
                )
                return
            ids: list[str] = []
            for record in payload.get("procnotices") or []:
                if isinstance(record, dict) and record.get("id"):
                    notice_id = str(record["id"])
                    raw_by_id.setdefault(notice_id, record)
                    ids.append(notice_id)
            fetched_terms[term] = (total, ids)

        semaphore = asyncio.Semaphore(FETCH_CONCURRENCY)

        async def guarded(term: str) -> None:
            async with semaphore:
                await one(term)

        await asyncio.gather(*(guarded(t) for t in to_fetch))

        # Cached terms contribute their records from the light-record store. A term whose
        # records have gone missing -- the two tables can be pruned independently -- is
        # refetched rather than quietly contributing fewer notices than it found, which
        # would look exactly like the market having gone quiet.
        if cached_terms:
            with closing(connect()) as conn:
                wanted = {i for _total, ids in cached_terms.values() for i in ids}
                stored = light_records(conn, wanted)
            incomplete: list[str] = []
            for term, (total, ids) in cached_terms.items():
                if ids and sum(1 for i in ids if i in stored) < len(ids):
                    incomplete.append(term)
                    continue
                totals[term] = total
                for notice_id in ids:
                    raw_by_id.setdefault(notice_id, stored[notice_id])
            if incomplete:
                logger.info("refetching %d term(s) with incomplete cached records", len(incomplete))
                await asyncio.gather(*(guarded(t) for t in incomplete))

        today = datetime.now(UTC).date()

        # Phase two: bodies, only for what survives the cheap filters.
        chosen = tuple(r.strip().casefold() for r in (regions or []) if r.strip())
        survivors = [
            notice_id for notice_id, record in raw_by_id.items()
            if _survives_cheap_filters(record, only_biddable=only_biddable,
                                       firms_only=firms_only, as_of=today, regions=chosen)
        ]
        # Soonest deadline first, so that if the body budget is reached it is spent on
        # the tenders closest to closing rather than on an arbitrary slice.
        survivors.sort(key=lambda i: _cheap_date(
            raw_by_id[i].get("submission_deadline_date")) or date.max)
        with closing(connect()) as conn:
            bodies = cached_bodies(conn, set(survivors))
        missing = [i for i in survivors if i not in bodies]
        if missing:
            bodies.update(await fetch_bodies(client, missing, warnings))

    # Bodies are cached already stripped: the HTML is only ever needed once, and
    # re-stripping 8 MB of markup on every page load was measurable.
    for notice_id, body in bodies.items():
        if notice_id in raw_by_id:
            raw_by_id[notice_id]["notice_text"] = body

    with closing(connect()) as conn:
        if fetched_terms:
            store_term_results(conn, fetched_terms)
            store_light_records(conn, {i: raw_by_id[i] for _t, ids in fetched_terms.values()
                                      for i in ids if i in raw_by_id})
        if bodies:
            store_bodies(conn, bodies)
        marked = states(conn)
        already_new = recently_seen(conn)

    with closing(connect()) as conn:
        verdicts = cached_verdicts(conn, survivors, taxonomy.taxonomy_version)
    fresh_verdicts: dict[str, tuple[bool, str]] = {}

    rows: list[Result] = []
    retrieved_at = datetime.now(UTC)
    hide_terms = tuple(h.strip() for h in hide if h.strip())
    excluded = 0
    # Full models for survivors only -- a few dozen records rather than a few thousand.
    # The body was merged into the record above, so it is present on the model too.
    for notice_id in survivors:
        notice = ProcurementConnector.normalize_record(
            raw_by_id[notice_id], source_url=PATH, retrieved_at=retrieved_at)
        if notice is None:
            continue
        record = raw_by_id[notice_id]
        # Already plain text: stripped once when it was fetched and cached that way.
        body = notice.notice_text or ""
        blob = f"{notice.bid_description or ''} {body}"
        # Whole words, not substrings. A plain `in` test made every short exclusion
        # catastrophic: typing "x" removed all 18 tenders, because "x" appears inside
        # some word of practically every notice, and the page then said "nothing open"
        # with no hint that an exclusion had done it. `contains_any` is the same
        # boundary-aware matcher the engine uses, so "Siem Reap" excludes the province
        # without also excluding SIEM.
        if hide_terms and contains_any(blob, hide_terms):
            excluded += 1
            continue

        # The engine runs as a silent noise filter -- it is what stops Siem Reap,
        # Defect Liability Period and "prices soar" reaching the list. Its score is
        # never shown; the list is short enough to read. Its verdict is remembered per
        # catalogue version, because re-deciding 262 bodies on every page load was the
        # single largest cost of a warm page.
        remembered = verdicts.get(notice_id)
        if remembered is not None:
            matched, why = remembered
            if not matched:
                continue
        else:
            candidate = candidate_input_from_procurement(notice)
            candidate = candidate.model_copy(update={"notice_text": body or None})
            verdict = engine.evaluate(candidate, profile)
            why = ""
            if verdict.tier.value != "NO_MATCH":
                for match in verdict.positive_matches:
                    if find_spans(notice.bid_description or "", match.term,
                                  case_sensitive=match.term.isupper()):
                        why = match.term
                        break
                if not why and verdict.positive_matches:
                    why = verdict.positive_matches[0].term
            matched = verdict.tier.value != "NO_MATCH"
            fresh_verdicts[notice_id] = (matched, why)
            if not matched:
                continue

        deadline = notice.submission_deadline_date
        rows.append(Result(
            notice=notice, raw=record, why=why,
            days=(deadline - today).days if deadline else None,
            is_new=notice_id in already_new, state=marked.get(notice_id, ""),
        ))

    if excluded:
        listed = ", ".join(repr(h) for h in hide_terms[:4])
        warnings.append(
            f"{excluded} tender(s) matched your terms but were removed by an excluded "
            f"word ({listed}). Remove it from 'Not interested in' to see them."
        )

    if fresh_verdicts:
        with closing(connect()) as conn:
            store_verdicts(conn, fresh_verdicts, taxonomy.taxonomy_version)

    rows.sort(key=lambda r: (r.days if r.days is not None else 9999,
                             -(r.notice.notice_date or date.min).toordinal()))

    # Mark seen only what SURVIVED the filters, and only when the caller wants a fetch
    # ledger at all. Marking every fetched id here (before the filters above) meant a
    # notice rejected today -- no deadline published yet, hidden by a term since removed,
    # or NO_MATCH under an older taxonomy -- could never be reported once it qualified.
    if record_seen and rows:
        surviving = sorted(r.notice.external_id for r in rows)
        with closing(connect()) as conn:
            fresh = mark_seen(conn, surviving)
            cache_records(conn, {r.notice.external_id: r.raw for r in rows})
        for row in rows:
            if row.notice.external_id in fresh:
                row.is_new = True
    return rows, totals, warnings


async def attach_dossiers(rows: list[Result]) -> None:
    """Attach each tender's project documents, in place.

    Deliberately separate from ``search``: it is enrichment, and a failure here must
    degrade to "no documents shown" rather than to a shorter list of opportunities.
    One request per distinct project, cached for a week, so a reload costs nothing.
    """
    if not rows:
        return
    projects = {r.notice.project_id for r in rows if r.notice.project_id}
    try:
        async with WorldBankApiClient(get_settings()) as client:
            found = await dossier.attach(client, projects, connect=connect)
    except (WorldBankConnectorError, OSError) as exc:
        logger.warning("dossier attachment skipped: %s", exc)
        return
    for row in rows:
        row.docs = found.get(row.notice.project_id or "", [])


# ------------------------------------------------------------------------------ render
def esc(value: Any) -> str:
    return (str(value or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def catalogue_entries() -> list[Any]:
    """The product catalogue for display, or an empty list if the profile is unreadable.

    A broken profile must not blank the whole page: the tenders already fetched are
    still worth showing, and the warning belongs where the user can act on it."""
    try:
        return catalogue.entries(load_taxonomy(TAXONOMY_PATH))
    except (OSError, ValueError) as exc:
        logger.warning("catalogue unavailable: %s", exc)
        return []


def render(rows: list[Result], totals: dict[str, int | None], warnings: list[str],
           flt: dict[str, Any], elapsed: float, *, cached: bool = False) -> str:
    from .dashboard_template import (
        PAGE,
        markets_hint,
        render_blank,
        render_catalogue,
        render_groups,
        render_markets,
        render_reach,
        render_switches,
        render_terms,
    )

    # Counted from what is on screen, so the hint names real options rather than a
    # static list of every country the World Bank lends to.
    places = Counter(r.notice.project_country_name for r in rows if r.notice.project_country_name)
    available = places.most_common()

    fresh = sum(1 for r in rows if r.is_new)
    chasing = sum(1 for r in rows if r.state == "PURSUING")
    tally = f"<b>{len(rows)}</b> open"
    if fresh:
        tally += f", <b>{fresh}</b> new since you last looked"
    if chasing:
        tally += f", <b>{chasing}</b> you are pursuing"

    # Say plainly whether this came off the wire or out of the cache. A page that
    # silently shows minutes-old data in a business where deadlines matter has to be
    # honest about it, and the Refresh control has to be next to the claim.
    source = ("Reusing results fetched in the last 15 minutes"
              if cached else "Fetched live from search.worldbank.org")
    footer = (
        f"{source} in {elapsed:.1f}s at {datetime.now(UTC).strftime('%H:%M')} UTC. "
        "Nothing is stored except your terms, the tenders you mark, and a short-lived "
        "copy of what was fetched. "
        "Relevance is deterministic and comes entirely from the taxonomy file — "
        "there is no scoring model and no win-probability estimate here."
    )
    return PAGE.format(
        tally=tally,
        find_terms=render_terms(flt["find"], "find"),
        hide_terms=render_terms(flt["hide"], "hide"),
        switches=render_switches(flt["only_biddable"]),
        reach=render_reach(totals),
        catalogue=render_catalogue(catalogue_entries()),
        regions=render_markets(flt["regions"], available),
        markets_hint=markets_hint(flt["regions"], available),
        warnings="".join(f'<div class="notice">{esc(w)}</div>' for w in warnings),
        groups=render_groups(rows) if rows else render_blank(totals),
        footer=footer,
    )


def allowed_hosts() -> list[str]:
    """Extra hostnames the app will answer to, beyond loopback and private IPs.

    Set ``WB_ALLOWED_HOSTS`` to a comma-separated list. Needed only for a *name* --
    a DNS record pointing at the machine, which is the case once this is behind a
    real hostname on a server.
    """
    return [h.strip() for h in os.environ.get("WB_ALLOWED_HOSTS", "").split(",") if h.strip()]


def host_is_allowed(host_header: str | None) -> bool:
    """Whether to answer a request carrying this Host header.

    Two rules, and the split between them is what makes the check both safe and usable:

    * A **literal private, loopback or link-local IP** is always accepted. DNS rebinding
      works by having a *name* the attacker controls re-resolve to a private address, so
      the Host header in that attack is always the attacker's hostname, never a bare IP.
      Accepting bare private IPs therefore costs nothing and means the page keeps working
      when the machine's address changes -- switching from office wifi to a phone hotspot
      reassigns it, and an allowlist frozen at startup would answer 400 after the switch.
    * A **hostname** must be named explicitly: `localhost`, or something in
      ``WB_ALLOWED_HOSTS``. This is the half that stops rebinding.

    A public IP is refused, so exposing this directly to the internet by address does not
    work by accident -- put it behind a reverse proxy with a real hostname instead.
    """
    import ipaddress

    if not host_header:
        return False
    host = host_header.strip()
    if host.startswith("["):
        # Bracketed IPv6, e.g. "[::1]:8000". Everything after the bracket is the port.
        host = host[1 : host.index("]")] if "]" in host else host[1:]
    elif host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    host = host.strip().casefold()
    if not host:
        return False
    if host == "localhost" or host in {h.casefold() for h in allowed_hosts()}:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False  # A hostname that was not explicitly allowed.
    return address.is_loopback or address.is_private or address.is_link_local


def build_app():
    from fastapi import FastAPI, Form
    from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

    app = FastAPI(title="Opportunity dashboard")

    @app.middleware("http")
    async def check_host(request, call_next):
        """Replaces TrustedHostMiddleware, which can only match a fixed list and so
        broke whenever the machine's address changed."""
        if not host_is_allowed(request.headers.get("host")):
            return PlainTextResponse(
                "This dashboard answers only to localhost and private network "
                "addresses. Set WB_ALLOWED_HOSTS to serve it under a hostname.\n",
                status_code=400)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    async def index(fresh: int = 0, problem: str = "") -> str:
        with closing(connect()) as conn:
            flt = load_filter(conn)
        started = datetime.now(UTC)
        rows, totals, warnings = await search(
            flt["find"], flt["hide"],
            only_biddable=flt["only_biddable"], firms_only=flt["firms_only"],
            fresh=bool(fresh), regions=flt["regions"])
        await attach_dossiers(rows)
        elapsed = (datetime.now(UTC) - started).total_seconds()
        if problem:
            warnings = [problem[:300], *warnings]
        return render(rows, totals, warnings, flt, elapsed, cached=not fresh)

    @app.post("/refresh")
    async def refresh():
        """Drop the term cache, so the next load asks the service again."""
        with closing(connect()) as conn:
            conn.execute("DELETE FROM term_cache")
            conn.commit()
        return RedirectResponse("/?fresh=1", status_code=303)

    @app.post("/catalogue/add")
    async def catalogue_add(text: str = Form("")):
        """Add a product to the catalogue, and search for it.

        Two stores, deliberately: the catalogue decides *relevance*, the filter terms
        decide *retrieval*. A product added only to the catalogue would never be fetched,
        so it is added to both -- otherwise the screen would accept a new product and
        then never show anything for it, which reads as the tool being broken.
        """
        try:
            catalogue.add_product(Path(TAXONOMY_PATH), text)
        except catalogue.CatalogueError as exc:
            return RedirectResponse(f"/?problem={quote(str(exc))}", status_code=303)
        with closing(connect()) as conn:
            flt = load_filter(conn)
            cleaned = " ".join(text.split())
            if cleaned not in flt["find"]:
                flt["find"].append(cleaned)
            save_filter(conn, flt["find"], flt["hide"], flt["only_biddable"], flt["firms_only"],
                        flt["regions"])
        return RedirectResponse("/", status_code=303)

    @app.post("/catalogue/remove")
    async def catalogue_remove(term_id: str = Form(...)):
        """Remove a product, and stop searching for it.

        Symmetric with the add: adding a product starts searching for it, so removing it
        must stop, or the user is left with results for a product they just deleted and
        no obvious way to connect the two.
        """
        try:
            removed = catalogue.remove_product(Path(TAXONOMY_PATH), term_id)
        except catalogue.CatalogueError as exc:
            return RedirectResponse(f"/?problem={quote(str(exc))}", status_code=303)
        with closing(connect()) as conn:
            flt = load_filter(conn)
            remaining = [t for t in flt["find"] if t.casefold() != removed.casefold()]
            # Never leave the retriever with nothing to ask for: an empty find list
            # fetches nothing at all, which reads as the tool having broken.
            if remaining:
                save_filter(conn, remaining, flt["hide"], flt["only_biddable"], flt["firms_only"],
                        flt["regions"])
        return RedirectResponse("/", status_code=303)

    @app.get("/document")
    async def document(url: str):
        """Fetch a published document as text, for reading or feeding to OCR.

        Where the service publishes a ``txturl`` this returns already-extracted text, so
        OCR is unnecessary. Where only a PDF exists the bytes are saved and the path is
        returned, because OCR belongs in the user's own pipeline, not in a web handler.
        The URL is not trusted: ``get_bytes`` re-checks the host allowlist on every
        redirect hop, so a document link cannot be used to reach an arbitrary host.
        """
        from fastapi.responses import PlainTextResponse
        settings = get_settings()
        try:
            async with WorldBankApiClient(settings) as client:
                content, final = await client.get_bytes(url)
        except WorldBankResponseError as exc:
            # A refused host, scheme or port is the caller's fault. Answering 502 here
            # blamed the World Bank for a link this app declined to follow.
            return PlainTextResponse(f"That document link is not allowed.\n\n{exc}", status_code=400)
        except WorldBankConnectorError as exc:
            return PlainTextResponse(f"Could not fetch that document.\n\n{exc}", status_code=502)
        if final.endswith(".pdf") or content[:5] == b"%PDF-":
            DOCUMENT_DIR.mkdir(parents=True, exist_ok=True)
            target = DOCUMENT_DIR / (final.rsplit("/", 1)[-1] or "document.pdf")
            target.write_bytes(content)
            return PlainTextResponse(
                f"This document is published only as a PDF, so it needs OCR.\n"
                f"Saved to: {target}\n")
        return PlainTextResponse(content.decode("utf-8", errors="replace"))

    @app.post("/add")
    async def add(kind: TermList = Form(...), term: str = Form(...)):  # noqa: B008
        with closing(connect()) as conn:
            flt = load_filter(conn)
            key = kind.value
            cleaned = " ".join(term.split())
            if cleaned and cleaned not in flt[key]:
                flt[key].append(cleaned)
            save_filter(conn, flt["find"], flt["hide"], flt["only_biddable"], flt["firms_only"],
                        flt["regions"])
        return RedirectResponse("/", status_code=303)

    @app.post("/toggle")
    async def toggle(field: str = Form(...)):
        """The two switches were rendered `disabled` with no endpoint -- presented to the
        user as settings that nothing could change except hand-editing SQLite."""
        # firms_only is not a switch any more -- biddability already subsumes it, so
        # toggling it could not change the list. It stays True: the user is a firm.
        if field != "only_biddable":
            return RedirectResponse("/", status_code=303)
        with closing(connect()) as conn:
            flt = load_filter(conn)
            flt[field] = not flt[field]
            save_filter(conn, flt["find"], flt["hide"], flt["only_biddable"], flt["firms_only"],
                        flt["regions"])
        return RedirectResponse("/", status_code=303)

    @app.post("/remove")
    async def remove(kind: TermList = Form(...), term: str = Form(...)):  # noqa: B008
        """POST for the same reason as /mark, and because the template already posted
        here: as a GET route this returned 405 and the remove button did nothing."""
        with closing(connect()) as conn:
            flt = load_filter(conn)
            key = kind.value
            remaining = [t for t in flt[key] if t != term]
            if key == "find" and not remaining:
                # An empty search list fetches nothing at all. Refusing is kinder than
                # showing a blank page that looks like a quiet market.
                return RedirectResponse(
                    "/?problem=" + quote("That is your last search term. Add another "
                                         "before removing this one."), status_code=303)
            flt[key] = remaining
            save_filter(conn, flt["find"], flt["hide"], flt["only_biddable"], flt["firms_only"],
                        flt["regions"])
        return RedirectResponse("/", status_code=303)

    @app.post("/mark")
    async def mark(notice_id: str = Form(...), state: TriageState = Form(...)):  # noqa: B008
        """POST, not GET. As a GET link this fired on browser prefetch and on any
        cross-site <img>, silently dismissing notices. `state` is a closed enum, so an
        arbitrary value is rejected by FastAPI rather than stored."""
        with closing(connect()) as conn:
            set_state(conn, notice_id, state.value, cached_record(conn, notice_id))
        return RedirectResponse("/", status_code=303)

    return app


def local_addresses() -> list[str]:
    """This machine's own LAN addresses, for printing a URL a phone can reach."""
    import socket

    found: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if not address.startswith("127.") and address not in found:
                found.append(address)
    except OSError:
        pass
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="Bind address. Use 0.0.0.0 to reach it from a phone on the same network.")
    args = parser.parse_args()
    try:
        import uvicorn
    except ImportError:
        raise SystemExit('The dashboard needs FastAPI. Install with:  pip install -e ".[web]"') from None

    # No allowlist poking needed: private addresses are accepted by rule, so the page
    # survives the machine changing network without a restart.
    lan = local_addresses()
    print(f"\n  Opportunity dashboard -> http://127.0.0.1:{args.port}")
    if args.host not in ("127.0.0.1", "localhost"):
        for address in lan:
            print(f"  From another device      http://{address}:{args.port}")
        print("\n  Reachable from your network. There is no login on this page, so anyone\n"
              "  on the same network can read your pipeline and mark tenders.")
    print()
    uvicorn.run(build_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
