"""The dossier attaches a project's published documents to a tender.

Two properties matter commercially and are each asserted below: it must cost one
request per project (an earlier version cost 259), and a document-service failure must
never reduce the list of opportunities the user is shown.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from wb_connector import dossier
from wb_connector.client import WorldBankApiClient
from wb_connector.config import Settings


def _wds_capture() -> Path | None:
    """Any real WDS capture. Discovered rather than hard-coded to a dated directory:
    the recon folder is gitignored, so a pinned path meant these tests silently skipped
    in every fresh clone."""
    root = Path(__file__).resolve().parents[1] / "captures"
    for candidate in sorted(root.glob("**/*documents*.json")):
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if payload.get("endpoint") == "/api/v3/wds":
            return candidate
    return None


CAPTURE = _wds_capture()
pytestmark = pytest.mark.skipif(CAPTURE is None, reason="real WDS capture not present")


@pytest.fixture
def wds_body() -> dict:
    return json.loads(CAPTURE.read_text(encoding="utf-8"))["response"]


@pytest.fixture
def connect(tmp_path, monkeypatch):
    import sqlite3

    def _connect() -> sqlite3.Connection:
        conn = sqlite3.connect(tmp_path / "d.db")
        conn.row_factory = sqlite3.Row
        return conn

    return _connect


def client_for(handler) -> WorldBankApiClient:
    return WorldBankApiClient(Settings(), transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_one_request_per_project(wds_body: dict, connect) -> None:
    """The WDS result set for a project can run to hundreds of documents. Paginating it
    cost 259 requests for a single project, because `paginate_offset` suppresses
    duplicate ids and so never hands control back for the caller to stop."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=wds_body)

    async with client_for(handler) as client:
        found = await dossier.attach(client, {"P160276", "P161667"}, connect=connect)

    assert len(calls) == 2
    assert set(found) == {"P160276", "P161667"}


@pytest.mark.asyncio
async def test_second_lookup_is_served_from_cache(wds_body: dict, connect) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=wds_body)

    async with client_for(handler) as client:
        first = await dossier.attach(client, {"P160276"}, connect=connect)
        after_first = len(calls)
        second = await dossier.attach(client, {"P160276"}, connect=connect)

    assert len(calls) == after_first
    assert first == second


@pytest.mark.asyncio
async def test_an_outage_yields_no_documents_rather_than_raising(connect) -> None:
    """A document-service failure must cost the user documents, never opportunities."""

    def dead(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("blocked")

    async with client_for(dead) as client:
        assert await dossier.attach(client, {"P999999"}, connect=connect) == {}


@pytest.mark.asyncio
async def test_a_failure_is_not_cached_as_an_empty_dossier(wds_body: dict, connect) -> None:
    """Caching a transient failure as "this project has no documents" would hide them
    for a week, long after the service recovered."""
    state = {"fail": True}

    def flaky(request: httpx.Request) -> httpx.Response:
        if state["fail"]:
            raise httpx.ConnectError("blocked")
        return httpx.Response(200, json=wds_body)

    async with client_for(flaky) as client:
        assert await dossier.attach(client, {"P160276"}, connect=connect) == {}
        state["fail"] = False
        recovered = await dossier.attach(client, {"P160276"}, connect=connect)

    assert recovered["P160276"], "a recovered service must produce documents"


@pytest.mark.asyncio
async def test_text_rendering_is_preferred_over_pdf(wds_body: dict, connect) -> None:
    """The service publishes a plain-text rendering of its documents, so OCR is wasted
    work wherever txt_url exists."""
    async with client_for(lambda r: httpx.Response(200, json=wds_body)) as client:
        found = await dossier.attach(client, {"P160276"}, connect=connect)

    documents = found["P160276"]
    assert documents
    for document in documents:
        if document.txt_url:
            assert document.readable_url == document.txt_url
            assert not document.needs_ocr


@pytest.mark.asyncio
async def test_no_projects_makes_no_requests(connect) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={})

    async with client_for(handler) as client:
        assert await dossier.attach(client, {"", None}, connect=connect) == {}  # type: ignore[arg-type]
    assert calls == []
