"""Contract tests for behaviour confirmed against the live World Bank API on 2026-09-04.

Two groups:

* Tests marked ``contract`` read real captures under ``captures/`` and skip when none
  are present, so a fresh clone stays green.
* The rest use ``httpx.MockTransport`` to pin the four defects the live session exposed,
  so none of them can silently return.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from wb_connector.client import WorldBankApiClient
from wb_connector.config import Settings
from wb_connector.connectors import DocumentsConnector, ProcurementConnector, _country_name
from wb_connector.db import Base, DocumentRow, _column_values, persist
from wb_connector.errors import WorldBankPayloadError, WorldBankResponseError
from wb_connector.models import Document, ProcurementNotice, Project

CAPTURES = Path(__file__).resolve().parent.parent / "captures"
NOTICE_CAPTURES = sorted(CAPTURES.glob("**/10-procurement-*.json"))


def settings(**overrides: object) -> Settings:
    base = {"world_bank_base_url": "https://search.worldbank.org", "world_bank_max_retries": 1}
    return Settings(**{**base, **overrides})


def mock_client(handler) -> WorldBankApiClient:
    return WorldBankApiClient(settings(), transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------------------
# Contract tests against real captured responses
# --------------------------------------------------------------------------------------

@pytest.mark.contract
@pytest.mark.skipif(not NOTICE_CAPTURES, reason="no real procurement captures are present")
def test_every_real_notice_normalizes() -> None:
    capture = json.loads(NOTICE_CAPTURES[0].read_text(encoding="utf-8"))
    records = capture["response"]["procnotices"]
    assert records, "capture contains no records"
    for record in records:
        notice = ProcurementConnector.normalize_record(
            record, source_url=capture["request_url"], retrieved_at=datetime.now(UTC)
        )
        if notice is None:
            continue
        assert notice.raw_payload == record, "raw payload must be preserved byte-for-byte"


@pytest.mark.contract
@pytest.mark.skipif(not NOTICE_CAPTURES, reason="no real procurement captures are present")
def test_submission_date_is_the_publication_date_not_a_deadline() -> None:
    """Live-tested: submission_date equals noticedate in 16,319/16,319 records.

    If this ever stops holding, the field's meaning changed and the product's notion of
    a deadline must be revisited.
    """
    capture = json.loads(NOTICE_CAPTURES[0].read_text(encoding="utf-8"))
    compared = matching = 0
    for record in capture["response"]["procnotices"]:
        notice = ProcurementConnector.normalize_record(
            record, source_url="x", retrieved_at=datetime.now(UTC)
        )
        if notice and notice.notice_date and notice.submission_date:
            compared += 1
            matching += notice.notice_date == notice.submission_date
    assert compared, "no record carried both dates"
    assert matching == compared


@pytest.mark.contract
@pytest.mark.skipif(not NOTICE_CAPTURES, reason="no real procurement captures are present")
def test_contract_awards_are_never_applyable_and_carry_no_deadline() -> None:
    capture = json.loads(NOTICE_CAPTURES[0].read_text(encoding="utf-8"))
    awards = 0
    for record in capture["response"]["procnotices"]:
        notice = ProcurementConnector.normalize_record(
            record, source_url="x", retrieved_at=datetime.now(UTC)
        )
        if notice and notice.notice_type == "Contract Award":
            awards += 1
            assert notice.submission_deadline_date is None
            assert not notice.is_applyable()
    assert awards, "capture contained no Contract Award notices"


# --------------------------------------------------------------------------------------
# Applyability
# --------------------------------------------------------------------------------------

def notice(**overrides: object) -> ProcurementNotice:
    base: dict[str, object] = {
        "source_url": "https://search.worldbank.org/x",
        "external_id": "OP1",
        "retrieved_at": datetime.now(UTC),
        "raw_payload": {},
        "notice_type": "Invitation for Bids",
        "notice_status": "Published",
        "submission_deadline_date": date(2099, 1, 1),
    }
    return ProcurementNotice(**{**base, **overrides})


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, True),
        ({"notice_type": "Contract Award"}, False),
        ({"notice_type": "General Procurement Notice"}, False),
        ({"notice_status": "Cancelled"}, False),
        ({"notice_status": "Draft"}, False),
        # A notice with NO published deadline is open, not closed: the deadline is in the
        # bid document. 24.4% of bid-eligible notices publish none, and treating that as
        # expiry hid a quarter of the addressable population.
        ({"submission_deadline_date": None}, True),
        ({"submission_deadline_date": date(2000, 1, 1)}, False),
        ({"procurement_method_name": "Individual Consultant Selection"}, False),
        # The API emits irregular internal spacing; normalization must collapse it.
        ({"procurement_method_name": "Individual  Consultant   Selection"}, False),
    ],
)
def test_is_applyable(overrides: dict[str, object], expected: bool) -> None:
    assert notice(**overrides).is_applyable(as_of=date(2026, 9, 4)) is expected


def test_biddability_distinguishes_unknown_from_closed() -> None:
    """Three states, not two -- the whole point of the change."""
    from opportunity_engine.biddability import Biddability

    as_of = date(2026, 9, 4)
    assert notice().biddability(as_of=as_of) is Biddability.BIDDABLE
    assert notice(submission_deadline_date=None).biddability(as_of=as_of) is Biddability.DEADLINE_UNKNOWN
    assert notice(submission_deadline_date=date(2000, 1, 1)).biddability(as_of=as_of) is Biddability.CLOSED
    assert notice(notice_type="Contract Award").biddability(as_of=as_of) is Biddability.WRONG_TYPE
    assert notice(notice_status="Cancelled").biddability(as_of=as_of) is Biddability.WITHDRAWN
    assert notice(procurement_method_name="Individual Consultant Selection").biddability(
        as_of=as_of) is Biddability.INDIVIDUAL_ONLY


def test_all_call_sites_agree_on_biddability() -> None:
    """The rule lived in five places and had diverged: the engine omitted the
    individual-consultant clause, so 7 of 40 real records disagreed -- all job adverts."""
    from opportunity_engine.domain.adapters import candidate_input_from_procurement
    from opportunity_engine.matching.engine import CandidateEngine

    as_of = date(2026, 9, 4)
    for notice_type in ("Invitation for Bids", "Request for Expression of Interest", "Contract Award"):
        for method in ("Request for Bids", "Individual Consultant Selection"):
            for status in ("Published", "Cancelled"):
                for deadline in (date(2099, 1, 1), date(2000, 1, 1), None):
                    n = notice(notice_type=notice_type, procurement_method_name=method,
                               notice_status=status, submission_deadline_date=deadline)
                    model = n.is_applyable(as_of=as_of)
                    engine = CandidateEngine._applyable(
                        candidate_input_from_procurement(n), as_of=as_of)
                    assert model == engine, (notice_type, method, status, deadline)


def test_country_name_unwraps_the_json_array_the_api_actually_returns() -> None:
    assert _country_name({"countryname": '["People\'s Republic of Bangladesh"]'}) == (
        "People's Republic of Bangladesh"
    )
    assert _country_name({"countryname": '["Kenya", "Uganda"]'}) == "Kenya; Uganda"
    assert _country_name({"project_ctry_name": "Kenya"}) == "Kenya"
    assert _country_name({}) is None


# --------------------------------------------------------------------------------------
# Defect 1: page pagination does not advance on /api/v2/projects
# --------------------------------------------------------------------------------------

async def test_paginate_stops_instead_of_looping_when_page_is_ignored() -> None:
    """Live-tested: /api/v2/projects returns page 1 forever. This must not loop."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = {"page": 1, "pages": 50, "total": 1000,
                "projects": {"P1": {"id": "P1"}, "P2": {"id": "P2"}}}
        return httpx.Response(200, json=body, request=request)

    async with mock_client(handler) as client:
        with pytest.raises(WorldBankPayloadError, match="does not support"):
            async for _ in client.paginate("/api/v2/projects", {"rows": 2}, record_key="projects"):
                pass
    assert calls == 2, "must detect the repeat on the second page, not keep requesting"


# --------------------------------------------------------------------------------------
# Defect 2: offset pages overlap (18.1% duplicate ids observed)
# --------------------------------------------------------------------------------------

async def test_paginate_offset_suppresses_duplicate_records_across_pages() -> None:
    pages = [
        {"total": 4, "rows": 2, "os": 0, "procnotices": [{"id": "A"}, {"id": "B"}]},
        {"total": 4, "rows": 2, "os": 2, "procnotices": [{"id": "B"}, {"id": "C"}]},
    ]
    served = iter(pages)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(served, {"total": 4, "procnotices": []}), request=request)

    seen: list[str] = []
    async with mock_client(handler) as client:
        async for page, _ in client.paginate_offset(
            "/api/procnotices", {"rows": 2, "os": 0}, record_key="procnotices"
        ):
            seen.extend(record["id"] for record in page.records)
    assert seen == ["A", "B", "C"], "duplicate B must be suppressed"


# --------------------------------------------------------------------------------------
# Defect 3: document URLs are http:// and redirect between hosts
# --------------------------------------------------------------------------------------

def test_http_document_urls_are_upgraded_not_rejected() -> None:
    upgraded = WorldBankApiClient.assert_downloadable(
        "http://documents.worldbank.org/curated/en/x.pdf",
        frozenset({"documents.worldbank.org"}),
    )
    assert upgraded.startswith("https://documents.worldbank.org/")


@pytest.mark.parametrize(
    "url",
    [
        "http://evil.example/x.pdf",
        "https://x.worldbank.org.attacker.com/x.pdf",
        "https://documents.worldbank.org:8443/x.pdf",
        "file:///etc/passwd",
    ],
)
def test_disallowed_document_urls_are_refused(url: str) -> None:
    with pytest.raises(WorldBankResponseError):
        WorldBankApiClient.assert_downloadable(url, frozenset({"documents.worldbank.org"}))


async def test_redirect_off_the_allowlist_is_blocked() -> None:
    """The pre-request check alone let a 302 walk to cloud instance metadata."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "documents.worldbank.org":
            return httpx.Response(
                302, headers={"Location": "http://169.254.169.254/latest/meta-data/"}, request=request
            )
        return httpx.Response(200, content=b"CREDENTIALS", request=request)

    async with mock_client(handler) as client:
        with pytest.raises(WorldBankResponseError, match="allowlist"):
            await DocumentsConnector(client).retrieve_representation(
                "http://documents.worldbank.org/curated/en/x.pdf"
            )


async def test_redirect_within_the_allowlist_is_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "documents.worldbank.org":
            return httpx.Response(
                302,
                headers={"Location": "https://documents1.worldbank.org/x.pdf"},
                request=request,
            )
        return httpx.Response(
            200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"}, request=request
        )

    async with mock_client(handler) as client:
        assert await DocumentsConnector(client).retrieve_representation(
            "http://documents.worldbank.org/curated/en/x.pdf"
        ) == b"%PDF-1.4"


async def test_oversized_document_is_refused_mid_stream() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"x" * 5000, headers={"content-type": "application/pdf"}, request=request
        )

    async with mock_client(handler) as client:
        with pytest.raises(WorldBankResponseError, match="limit"):
            await DocumentsConnector(client).retrieve_representation(
                "https://documents.worldbank.org/x.pdf", max_bytes=1000
            )


async def test_html_block_page_is_not_accepted_as_a_document() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"<html>Web Page Blocked</html>",
            headers={"content-type": "text/html"}, request=request,
        )

    async with mock_client(handler) as client:
        with pytest.raises(WorldBankResponseError, match="content-type"):
            await DocumentsConnector(client).retrieve_representation(
                "https://documents.worldbank.org/x.pdf"
            )


# --------------------------------------------------------------------------------------
# Defect 4: a non-JSON body must raise a domain error, not AttributeError
# --------------------------------------------------------------------------------------

async def test_interstitial_page_raises_a_domain_error_naming_the_cause() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"<html>FortiGuard - Web Page Blocked</html>",
            headers={"content-type": "text/html"}, request=request,
        )

    async with mock_client(handler) as client:
        with pytest.raises(WorldBankPayloadError, match="filter/interstitial"):
            await client.get_json("/api/procnotices", {"rows": 1})


# --------------------------------------------------------------------------------------
# Persistence: previously crashed on every Document and on any record carrying a date
# --------------------------------------------------------------------------------------

def persistable() -> list[Project | Document | ProcurementNotice]:
    common = {
        "source_url": "https://search.worldbank.org/x",
        "retrieved_at": datetime.now(UTC),
        "raw_payload": {"a": 1},
    }
    return [
        Project(external_id="P1", project_id="P1", name="n",
                board_approval_date=date(2025, 6, 1), **common),
        ProcurementNotice(external_id="N1", bid_description="d", notice_date=date(2026, 1, 2),
                          submission_deadline_date=date(2026, 3, 4),
                          contact_email="a@example.org", **common),
        Document(external_id="D1", project_id="P1", title="t", document_type="Project Appraisal Document",
                 pdf_url="https://documents1.worldbank.org/x.pdf",
                 published_at=date(2024, 5, 6), **common),
    ]


@pytest.mark.parametrize("record", persistable(), ids=lambda r: type(r).__name__)
def test_persist_round_trips_every_record_type_including_dates(record) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        persist(session, record)
        session.commit()


def test_persist_is_idempotent_for_the_same_external_id() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    record = persistable()[2]
    with Session(engine) as session:
        persist(session, record)
        persist(session, record)
        session.commit()
        assert session.query(DocumentRow).count() == 1


def test_column_values_reports_model_and_column_drift_instead_of_crashing_opaquely() -> None:
    """A field added to a model without its column must fail loudly and by name.

    This is how `document_type` silently broke every Document insert.
    """
    class Drifted(Document):
        invented_field: str | None = None

    record = Drifted(
        external_id="D2", source_url="https://x/y", retrieved_at=datetime.now(UTC),
        raw_payload={}, invented_field="boom",
    )
    with pytest.raises(TypeError, match="invented_field"):
        _column_values(record, DocumentRow)


def test_persist_rejects_an_unmapped_record_type_with_a_useful_message() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session, pytest.raises(TypeError, match="no table mapping"):
        persist(session, object())


# --------------------------------------------------------------------------------------
# Digest: silence is the default, and an unreachable service is not "nothing new"
# --------------------------------------------------------------------------------------

def test_digest_renders_only_what_changed() -> None:
    from wb_connector.digest import render_text, subject_line

    class FakeNotice:
        bid_description = "Supply of a data loss prevention solution"
        project_country_name = "Kenya"
        notice_type = "Invitation for Bids"
        contact_email = "proc@example.org"
        bid_reference_no = "REF/1"
        external_id = "OP1"

    class FakeRow:
        notice = FakeNotice()
        why = "data loss prevention"
        days = 5

    body = render_text([FakeRow()], [], [])
    assert "1 new opportunity" in body
    assert "data loss prevention" in body
    assert "proc@example.org" in body
    assert "2 new" not in body
    assert subject_line([FakeRow()], [], []) == "Opportunities: 1 new"
    # The body is printed to a Windows console as well as emailed.
    body.encode("cp1252")


def test_digest_subject_names_the_soonest_deadline() -> None:
    from wb_connector.digest import subject_line

    class Row:
        def __init__(self, days):
            self.days = days
    assert "next in 2d" in subject_line([], [Row(9), Row(2)], [])


def test_digest_reports_a_change_to_something_being_pursued() -> None:
    """A cancelled tender previously read as 'nothing new' -- the most important message
    this tool can send was indistinguishable from a quiet market."""
    from wb_connector.digest import changes_on_pursued, render_text, subject_line

    stored = {"OP1": {"bid_description": "SIEM platform", "notice_status": "Published",
                      "submission_deadline_date": "2026-10-20T00:00:00Z"}}
    events = changes_on_pursued(stored, [])          # it vanished from the results
    assert events and "no longer in your results" in events[0][2]
    assert "changed" in subject_line([], [], events)
    assert "SIEM platform" in render_text([], [], events)


def test_digest_reports_a_status_flip_on_something_being_pursued() -> None:
    from types import SimpleNamespace

    from wb_connector.digest import changes_on_pursued

    stored = {"OP1": {"bid_description": "SIEM platform", "notice_status": "Published"}}
    live = [SimpleNamespace(notice=SimpleNamespace(
        external_id="OP1", notice_status="Cancelled", submission_deadline_date=None))]
    events = changes_on_pursued(stored, live)
    assert events and "Published -> Cancelled" in events[0][2]
