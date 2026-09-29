"""Project dossier: the documents behind a tender.

A procurement notice tells you almost nothing about the project it belongs to. The
notice payload carries no document link of any kind -- verified across 20,000 captured
records, where the only URL-shaped field is ``contact_web_url`` at 9.9% fill. What it
does carry, in 100% of records, is ``project_id``, and the Documents & Reports endpoint
is queryable by exactly that.

So the dossier is the project's own paper trail: assessments, briefs, working papers,
implementation reports. For a business developer judging fit, that is often more
decisive than the notice itself -- a cybersecurity assessment published against the
project two years earlier is what tells you whether this buyer knows what it wants.

**What this is not.** It is not the bid document. The tender package (RFP/RFB) is not
published through any World Bank API; it is obtained from the buyer named in the notice.
Every captured document carried ``majdocty`` of "Publications & Research", never a
procurement package. The UI must say so rather than imply a download exists.

**On OCR.** Every captured document exposed a ``txturl`` alongside its ``pdfurl`` -- the
service publishes a plain-text rendering itself. Where txt_url exists, OCR is wasted
work; read the text. OCR is only needed for a PDF the buyer sends you directly.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .client import WorldBankApiClient
from .connectors import DocumentsConnector
from .errors import WorldBankConnectorError

logger = logging.getLogger(__name__)

# A project's document set changes on the order of months, and a stale dossier is a
# cosmetic problem where a stale deadline would be a commercial one. Seven days keeps
# the page fast without ever being the reason a user misses something.
CACHE_TTL = timedelta(days=7)

# Per project. A dossier is orientation, not an archive; the newest few are what get read.
MAX_PER_PROJECT = 6

# Concurrent WDS requests. Matches the notice fetch: polite, still finishes in seconds.
CONCURRENCY = 4


@dataclass(frozen=True)
class DossierDocument:
    """One document, reduced to what a decision needs."""

    title: str
    kind: str | None
    published: str | None
    pdf_url: str | None
    txt_url: str | None

    @property
    def readable_url(self) -> str | None:
        """Prefer the text rendering: it is already extracted, so it needs no OCR."""
        return self.txt_url or self.pdf_url

    @property
    def needs_ocr(self) -> bool:
        return self.txt_url is None and self.pdf_url is not None


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS project_dossier (
               project_id TEXT PRIMARY KEY, documents TEXT, fetched_at TEXT)"""
    )
    conn.commit()


def cached(conn: sqlite3.Connection, project_ids: set[str]) -> dict[str, list[DossierDocument]]:
    """Fresh cache entries only. A stale row is treated as absent, not as data."""
    if not project_ids:
        return {}
    ensure_schema(conn)
    cutoff = (datetime.now(UTC) - CACHE_TTL).isoformat()
    marks = ",".join("?" * len(project_ids))
    rows = conn.execute(
        # `marks` is a run of literal "?" placeholders, never user text.
        f"SELECT project_id, documents FROM project_dossier "
        f"WHERE project_id IN ({marks}) AND fetched_at >= ?",
        (*sorted(project_ids), cutoff),
    ).fetchall()
    out: dict[str, list[DossierDocument]] = {}
    for row in rows:
        try:
            payload = json.loads(row["documents"])
        except (ValueError, TypeError):
            continue
        out[row["project_id"]] = [DossierDocument(**d) for d in payload]
    return out


def store(conn: sqlite3.Connection, fetched: dict[str, list[DossierDocument]]) -> None:
    """Cache a fetch, including an empty result.

    Caching the empty case is the point: most projects have no published documents, and
    without it every page load would re-ask WDS about all of them.
    """
    if not fetched:
        return
    ensure_schema(conn)
    now = datetime.now(UTC).isoformat()
    conn.executemany(
        """INSERT INTO project_dossier (project_id, documents, fetched_at) VALUES (?,?,?)
           ON CONFLICT(project_id) DO UPDATE SET documents=excluded.documents,
                                                fetched_at=excluded.fetched_at""",
        [(pid, json.dumps([d.__dict__ for d in docs]), now) for pid, docs in fetched.items()],
    )
    conn.commit()


async def fetch(client: WorldBankApiClient, project_ids: set[str]) -> dict[str, list[DossierDocument]]:
    """Fetch dossiers for projects, one request per distinct project.

    A failure is recorded as "no documents" for that project and never raised: a WDS
    outage must not cost the user a tender they could otherwise have bid on.
    """
    results: dict[str, list[DossierDocument]] = {}
    connector = DocumentsConnector(client)
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def one(project_id: str) -> None:
        documents: list[DossierDocument] = []
        async with semaphore:
            try:
                for document in await connector.first_page_for_project(project_id, rows=MAX_PER_PROJECT):
                    documents.append(
                        DossierDocument(
                            title=(document.title or "Untitled document").strip(),
                            kind=document.document_type,
                            published=document.published_at.isoformat() if document.published_at else None,
                            pdf_url=str(document.pdf_url) if document.pdf_url else None,
                            txt_url=str(document.txt_url) if document.txt_url else None,
                        )
                    )
            except (WorldBankConnectorError, OSError) as exc:
                logger.warning("dossier fetch failed", extra={"project_id": project_id, "error": str(exc)})
                return  # Not cached: a transient failure must not be remembered as "none".
        results[project_id] = documents

    await asyncio.gather(*(one(pid) for pid in sorted(project_ids)))
    return results


async def attach(client: WorldBankApiClient, project_ids: set[str], *, connect) -> dict[str, list[DossierDocument]]:
    """Cache-first dossier lookup for a set of projects."""
    project_ids = {p for p in project_ids if p}
    if not project_ids:
        return {}
    with closing(connect()) as conn:
        known = cached(conn, project_ids)
    missing = project_ids - set(known)
    if missing:
        fresh = await fetch(client, missing)
        with closing(connect()) as conn:
            store(conn, fresh)
        known.update(fresh)
    return known
