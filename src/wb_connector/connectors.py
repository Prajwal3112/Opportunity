"""Source-specific World Bank endpoint adapters, isolated from persistence."""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from typing import Any

from pydantic import ValidationError

from .client import WorldBankApiClient
from .models import APPLYABLE_NOTICE_TYPES, Document, ProcurementNotice, Project

logger = logging.getLogger(__name__)


def _value(record: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = record.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _country_name(record: dict[str, Any]) -> str | None:
    """Read a country name.

    Live-tested 2026-09-04: ``projects[*].countryname`` is a JSON array serialized as a
    string, e.g. ``'["People\\'s Republic of Bangladesh"]'``. Stored raw it would leak
    brackets and quotes into every downstream match and display.
    """
    raw = _value(record, "countryname", "country", "project_ctry_name")
    if not raw:
        return None
    if raw.startswith("[") and raw.endswith("]"):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return raw
        if isinstance(parsed, list):
            names = [str(item).strip() for item in parsed if str(item).strip()]
            return "; ".join(names) or None
    return raw


def _date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        try:
            return datetime.strptime(value, "%d-%b-%Y").replace(tzinfo=UTC).date()
        except ValueError:
            return None


class ProjectsConnector:
    path = "/api/v2/projects"
    record_key = "projects"

    def __init__(self, client: WorldBankApiClient) -> None:
        self.client = client

    async def discover(self, *, rows: int = 100, **filters: Any) -> AsyncIterator[Project]:
        async for page, url in self.client.paginate(self.path, {"rows": rows, **filters}, record_key=self.record_key):
            retrieved_at = datetime.now(UTC)
            for record in page.records:
                project_id = _value(record, "id", "projectid", "project_id")
                if not project_id:
                    continue
                try:
                    yield Project(
                        source_url=url,
                        external_id=project_id,
                        project_id=project_id,
                        name=_value(record, "project_name", "projectname", "name"),
                        status=_value(record, "status", "projectstatusdisplay"),
                        country=_country_name(record),
                        board_approval_date=_date(_value(record, "boardapprovaldate")),
                        retrieved_at=retrieved_at,
                        raw_payload=record,
                    )
                except ValidationError:
                    # One malformed record must not end the stream; the raw payload is
                    # still recoverable from the capture/snapshot.
                    logger.warning("skipping malformed project record", extra={"external_id": project_id})
                    continue


def _document_from_record(record: dict[str, Any], *, source_url: str, retrieved_at: datetime) -> Document | None:
    """Normalize one WDS record, or None if it is unusable.

    Shared by the paginating search and the single-page dossier fetch, so the two can
    never drift on which field holds a document's type or its text rendering.
    """
    document_id = _value(record, "id")
    if not document_id:
        return None
    try:
        return Document(
            source_url=source_url,
            external_id=document_id,
            project_id=_value(record, "projectid"),
            title=_value(record, "display_title"),
            # Live-tested 2026-09-04: `docty` is the real document-type field (e.g.
            # "Brief"), with `majdocty` as the broader grouping. Both are sparsely
            # populated, so neither is required.
            document_type=_value(record, "docty", "majdocty"),
            pdf_url=_value(record, "pdfurl"),
            txt_url=_value(record, "txturl"),
            published_at=_date(_value(record, "docdt")),
            retrieved_at=retrieved_at,
            raw_payload=record,
        )
    except ValidationError:
        logger.warning("skipping malformed document record", extra={"external_id": document_id})
        return None


class DocumentsConnector:
    """Adapter for the officially documented Documents & Reports v3 endpoint."""

    path = "/api/v3/wds"
    record_key = "documents"

    def __init__(self, client: WorldBankApiClient) -> None:
        self.client = client

    async def search(
        self,
        *,
        qterm: str | None = None,
        projectid: str | None = None,
        proid: str | None = None,
        rows: int = 100,
        os: int = 0,
        fl: str | None = None,
    ) -> AsyncIterator[Document]:
        """Search WDS v3 using only documented query parameters."""
        params = {"qterm": qterm, "projectid": projectid, "proid": proid, "rows": rows, "os": os, "fl": fl}
        async for page, url in self.client.paginate_offset(self.path, params, record_key=self.record_key):
            retrieved_at = datetime.now(UTC)
            for record in page.records:
                document = _document_from_record(record, source_url=url, retrieved_at=retrieved_at)
                if document is not None:
                    yield document

    async def for_project(self, project_id: str, *, rows: int = 100, os: int = 0) -> AsyncIterator[Document]:
        async for document in self.search(projectid=project_id, rows=rows, os=os):
            yield document

    async def first_page_for_project(self, project_id: str, *, rows: int = 6) -> list[Document]:
        """One request, one page of a project's documents.

        Deliberately not ``for_project``. ``paginate_offset`` suppresses duplicate ids,
        so when a page returns nothing new it yields nothing and the caller never regains
        control to stop — measured at 259 requests for a single project. A dossier is
        orientation, so one page is the whole requirement.
        """
        payload, url = await self.client.get_json(
            self.path, {"projectid": project_id, "rows": rows, "os": 0}
        )
        page = self.client.page_from_payload(payload, self.record_key)
        retrieved_at = datetime.now(UTC)
        documents = [
            _document_from_record(record, source_url=url, retrieved_at=retrieved_at)
            for record in page.records[:rows]
        ]
        return [document for document in documents if document is not None]

    async def retrieve_representation(self, url: str, *, max_bytes: int = 25_000_000) -> bytes:
        """Download a document representation through the client's validated path.

        Every guarantee lives in ``WorldBankApiClient.get_bytes``: an explicit host
        allowlist re-checked on each redirect hop, an HTTPS upgrade (the published URLs
        are plain HTTP), a content-type allowlist, and a streamed size cap.
        """
        content, _final_url = await self.client.get_bytes(url, max_bytes=max_bytes)
        return content


class ProcurementConnector:
    """Adapter for the official procurement reference endpoint.

    Only fields observed in an official endpoint response are normalized. All
    other fields remain exclusively in the raw payload until documented or
    validated through the project's live test/capture process.
    """

    path = "/api/procnotices"
    record_key = "procnotices"

    def __init__(self, client: WorldBankApiClient) -> None:
        self.client = client

    @staticmethod
    def normalize_record(
        record: dict[str, Any], *, source_url: str, retrieved_at: datetime
    ) -> ProcurementNotice | None:
        """Normalize only fields observed on the official procurement endpoint."""
        notice_id = _value(record, "id")
        if not notice_id:
            return None
        return ProcurementNotice(
            source_url=source_url,
            external_id=notice_id,
            project_id=_value(record, "project_id"),
            notice_type=_value(record, "notice_type"),
            notice_date=_date(_value(record, "noticedate")),
            notice_language=_value(record, "notice_lang_name"),
            notice_status=_value(record, "notice_status"),
            project_country_name=_country_name(record),
            project_name=_value(record, "project_name"),
            bid_reference_no=_value(record, "bid_reference_no"),
            bid_description=_value(record, "bid_description"),
            procurement_method_code=_value(record, "procurement_method_code"),
            procurement_method_name=_value(record, "procurement_method_name"),
            procurement_group=_value(record, "procurement_group"),
            # `submission_date` duplicates `noticedate`; the real deadline is separate.
            submission_date=_date(_value(record, "submission_date")),
            submission_deadline_date=_date(_value(record, "submission_deadline_date")),
            submission_deadline_time=_value(record, "submission_deadline_time"),
            notice_text=_value(record, "notice_text"),
            contact_organization=_value(record, "contact_organization"),
            contact_name=_value(record, "contact_name"),
            contact_email=_value(record, "contact_email"),
            contact_phone_no=_value(record, "contact_phone_no"),
            contact_address=_value(record, "contact_address"),
            contact_web_url=_value(record, "contact_web_url"),
            contact_country_name=_value(record, "contact_ctry_name"),
            retrieved_at=retrieved_at,
            raw_payload=record,
        )

    async def discover(
        self,
        *,
        rows: int = 100,
        os: int = 0,
        qterm: str | None = None,
        notice_type: str | None = None,
        max_pages: int = 1000,
    ) -> AsyncIterator[ProcurementNotice]:
        """Stream procurement notices, optionally filtered by the service itself.

        Live-tested 2026-09-04: ``qterm`` narrows the population from 417,771 notices to
        475 for ``cybersecurity``, and composes with ``notice_type`` to narrow further
        (to 20 for "Invitation for Bids"). Neither parameter is documented for this
        endpoint, so both are optional and the unfiltered crawl remains the default.
        ``q`` was tested and is ignored by the service.
        """
        params = {"rows": rows, "os": os, "qterm": qterm, "notice_type": notice_type}
        async for page, url in self.client.paginate_offset(
            self.path, params, record_key=self.record_key, max_pages=max_pages
        ):
            retrieved_at = datetime.now(UTC)
            for record in page.records:
                try:
                    notice = self.normalize_record(record, source_url=url, retrieved_at=retrieved_at)
                except ValidationError:
                    logger.warning("skipping malformed notice", extra={"external_id": record.get("id")})
                    continue
                if notice is not None:
                    yield notice

    async def discover_applyable(
        self, *, qterm: str, rows: int = 100, max_pages: int = 100
    ) -> AsyncIterator[ProcurementNotice]:
        """Stream only notices a bidder can still respond to, for a given search term.

        Contract Awards are 72.5% of the corpus and are already decided, so they are
        excluded at the service rather than filtered after download.
        """
        for applyable_type in sorted(APPLYABLE_NOTICE_TYPES):
            async for notice in self.discover(
                rows=rows, qterm=qterm, notice_type=applyable_type, max_pages=max_pages
            ):
                yield notice
