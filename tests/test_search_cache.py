"""Two-phase fetching and its caches.

The property under test is not speed but losslessness: a cache that returns a different
answer from a fresh fetch is worse than no cache, because the thing being dropped is a
tender somebody could have bid on.
"""
from __future__ import annotations

import importlib
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from wb_connector.config import Settings


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("WB_DASHBOARD_DB", str(tmp_path / "t.db"))
    from wb_connector import dashboard

    importlib.reload(dashboard)
    return dashboard


def conn_for(dashboard) -> sqlite3.Connection:
    return dashboard.connect()


def client_with(handler):
    from wb_connector.client import WorldBankApiClient

    return WorldBankApiClient(Settings(), transport=httpx.MockTransport(handler))


def test_a_stale_term_is_treated_as_absent_not_as_data(db) -> None:
    with closing(conn_for(db)) as conn:
        db.store_term_results(conn, {"siem": (7, ["OP1", "OP2"])})
        assert db.cached_term_results(conn, ["siem"]) == {"siem": (7, ["OP1", "OP2"])}
        stale = (datetime.now(UTC) - db.TERM_CACHE_TTL - timedelta(minutes=1)).isoformat()
        conn.execute("UPDATE term_cache SET fetched_at=?", (stale,))
        conn.commit()
        assert db.cached_term_results(conn, ["siem"]) == {}


def test_bodies_survive_longer_than_terms(db) -> None:
    """A body is immutable in practice and expensive to fetch; which notices a term
    returns is neither, so the two cannot share a lifetime."""
    assert db.BODY_CACHE_TTL > db.TERM_CACHE_TTL


def test_a_verdict_is_scoped_to_the_catalogue_version(db) -> None:
    """Editing the catalogue must invalidate every remembered decision at once, or the
    page shows verdicts reached under rules that no longer exist."""
    with closing(conn_for(db)) as conn:
        db.store_verdicts(conn, {"OP1": (True, "SIEM")}, "2026.09.3")
        assert db.cached_verdicts(conn, ["OP1"], "2026.09.3") == {"OP1": (True, "SIEM")}
        assert db.cached_verdicts(conn, ["OP1"], "2026.09.4") == {}


def test_a_negative_verdict_is_remembered_too(db) -> None:
    """Forgetting rejections would mean re-deciding thousands of notices per load, and
    the rejections are the overwhelming majority."""
    with closing(conn_for(db)) as conn:
        db.store_verdicts(conn, {"OP1": (False, "")}, "v1")
        assert db.cached_verdicts(conn, ["OP1"], "v1") == {"OP1": (False, "")}


def test_light_records_never_carry_the_body(db) -> None:
    """The body is 96.7% of a record and lives in its own table; duplicating it here
    would defeat the split that makes phase one cheap."""
    with closing(conn_for(db)) as conn:
        db.store_light_records(
            conn, {"OP1": {"id": "OP1", "notice_text": "x" * 5000, "bid_description": "SIEM"}}
        )
        got = db.light_records(conn, {"OP1"})
    assert "notice_text" not in got["OP1"]
    assert got["OP1"]["bid_description"] == "SIEM"


OPEN_NOTICE = {
    "notice_type": "Invitation for Bids",
    "notice_status": "Active",
    "procurement_method_name": "Request for Bids",
    "submission_deadline_date": "2027-01-01T00:00:00Z",
}
AS_OF = date(2026, 9, 28)


def test_cheap_filters_decide_without_building_a_model(db) -> None:
    """Validating a Pydantic model per record cost most of a warm page. The verdict still
    comes from the shared biddability owner, so it cannot drift from the rest."""
    assert db._survives_cheap_filters(OPEN_NOTICE, only_biddable=True, firms_only=True, as_of=AS_OF)

    award = {**OPEN_NOTICE, "notice_type": "Contract Award"}
    assert not db._survives_cheap_filters(award, only_biddable=True, firms_only=True, as_of=AS_OF)

    individual = {**OPEN_NOTICE, "procurement_method_name": "Individual Consultant Selection"}
    assert not db._survives_cheap_filters(individual, only_biddable=True, firms_only=True, as_of=AS_OF)
    # `biddability.assess` already returns INDIVIDUAL_ONLY for this method, and that is not
    # open to a firm -- so the firms filter is subsumed whenever the biddability filter is
    # on. Measured live: 262 survivors either way. The dashboard therefore stopped offering
    # it as a switch, because a control that cannot change the list is worse than none.
    assert not db._survives_cheap_filters(individual, only_biddable=True, firms_only=False, as_of=AS_OF)
    # It is the only thing acting once the biddability filter is off.
    assert db._survives_cheap_filters(individual, only_biddable=False, firms_only=False, as_of=AS_OF)
    assert not db._survives_cheap_filters(individual, only_biddable=False, firms_only=True, as_of=AS_OF)


def test_a_missing_deadline_is_still_open(db) -> None:
    """The three-state biddability rule, reached through the raw-record path. Treating a
    missing deadline as expiry hid 96% of the open set."""
    no_date = {
        "notice_type": "Request for Expression of Interest",
        "notice_status": "Active",
        "procurement_method_name": "Consultant Qualification Selection",
        "submission_deadline_date": None,
    }
    assert db._survives_cheap_filters(no_date, only_biddable=True, firms_only=True, as_of=AS_OF)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-10-23T00:00:00Z", date(2026, 10, 23)),
        ("2026-10-23", date(2026, 10, 23)),
        ("", None),
        (None, None),
        ("not a date", None),
    ],
)
def test_deadline_parsing_off_the_raw_record(db, raw, expected) -> None:
    assert db._cheap_date(raw) == expected


@pytest.mark.asyncio
async def test_bodies_are_stripped_once_at_fetch(db) -> None:
    """Stripping 8 MB of markup on every page load cost more than the download did."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"procnotices": [{"id": "OP1", "notice_text": "<p>Scope: <b>SIEM</b>.</p>"}],
                  "total": 1},
        )

    warnings: list[str] = []
    async with client_with(handler) as client:
        got = await db.fetch_bodies(client, ["OP1"], warnings)
    assert "<b>" not in got["OP1"]
    assert "SIEM" in got["OP1"]
    assert warnings == []


@pytest.mark.asyncio
async def test_a_body_that_cannot_be_fetched_is_reported_not_silently_dropped(db) -> None:
    def dead(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("blocked")

    warnings: list[str] = []
    async with client_with(dead) as client:
        got = await db.fetch_bodies(client, ["OP1"], warnings)
    assert got == {}
    assert warnings and "full text" in warnings[0]


@pytest.mark.asyncio
async def test_an_empty_body_is_cached_so_it_is_not_re_asked(db) -> None:
    """3.6% of notices carry no body at all. Not recording that would re-request them on
    every single load, forever."""

    def empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"procnotices": [], "total": 0})

    warnings: list[str] = []
    async with client_with(empty) as client:
        got = await db.fetch_bodies(client, ["OP1"], warnings)
    assert got == {"OP1": ""}


@pytest.mark.asyncio
async def test_too_many_bodies_says_so_rather_than_fetching_forever(db, monkeypatch) -> None:
    monkeypatch.setattr(db, "MAX_BODY_FETCH", 3)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"procnotices": [{"id": "X", "notice_text": "t"}], "total": 1})

    warnings: list[str] = []
    async with client_with(handler) as client:
        await db.fetch_bodies(client, [f"OP{i}" for i in range(10)], warnings)
    assert len(calls) == 3
    assert warnings and "too many to read in full" in warnings[0]


def test_light_fields_cover_every_field_the_model_reads(db) -> None:
    """A field left out of the `fl` list arrives as None -- a silent data loss, not an
    error. This is the guard that keeps the projection and the model in step."""
    from wb_connector.models import ProcurementNotice

    requested = set(db.LIGHT_FIELDS.split(","))
    # The API spells these two differently from the model, and the body is fetched apart.
    aliases = {"project_country_name": "project_ctry_name", "notice_language": "notice_lang_name",
               "notice_date": "noticedate", "contact_country_name": "contact_ctry_name"}
    missing = set()
    for name in ProcurementNotice.model_fields:
        # Set by the connector rather than read from the payload, plus the body, which is
        # fetched separately and on purpose.
        if name in {"notice_text", "source", "source_url", "external_id", "retrieved_at",
                    "raw_payload"}:
            continue
        if aliases.get(name, name) not in requested:
            missing.add(name)
    assert not missing, f"fields the model reads but the projection omits: {sorted(missing)}"


def test_exclusions_match_whole_words_not_substrings() -> None:
    """A plain `in` test made every short exclusion catastrophic: excluding "x" removed
    all 18 tenders, because "x" sits inside some word of practically every notice, and
    the page then reported "nothing open" as though the market were quiet."""
    from opportunity_engine.matching.spans import contains_any

    body = "Supply of firewall appliances and an expansion of the existing network."
    assert not contains_any(body, ("x",))
    assert not contains_any(body, ("wall",))
    assert contains_any(body, ("firewall",))

    # The exclusion this ships to solve: the province, without losing the product.
    siem_reap = "Road works in Siem Reap province."
    real_siem = "Deployment of a SIEM platform."
    assert contains_any(siem_reap, ("Siem Reap",))
    assert not contains_any(real_siem, ("Siem Reap",))
