from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from opportunity_engine.biddability import Biddability, assess


class SourceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = "world_bank"
    source_url: str
    external_id: str
    retrieved_at: datetime
    raw_payload: dict[str, Any]


class Project(SourceRecord):
    project_id: str
    name: str | None = None
    status: str | None = None
    country: str | None = None
    board_approval_date: date | None = None

    @field_validator("project_id", "external_id")
    @classmethod
    def nonempty_identifier(cls, value: str) -> str:
        value = value.strip().upper()
        if not value:
            raise ValueError("identifier cannot be blank")
        return value


class Document(SourceRecord):
    project_id: str | None = None
    title: str | None = None
    document_type: str | None = None
    pdf_url: HttpUrl | None = None
    txt_url: HttpUrl | None = None
    published_at: date | None = None


# Notice types a bidder can still respond to. Live-tested 2026-09-04: "Contract Award"
# and "General Procurement Notice" never carry a submission deadline, because the first
# is already decided and the second is only an advance advertisement.
APPLYABLE_NOTICE_TYPES = frozenset(
    {"Invitation for Bids", "Request for Expression of Interest", "Invitation for Prequalification"}
)

# Procurement methods that select a named individual, not a firm. An organisation
# cannot bid these, and in the corpus every "cyber" notice using one was a job advert.
INDIVIDUAL_ONLY_METHODS = frozenset({"individual consultant selection"})


class ProcurementNotice(SourceRecord):
    project_id: str | None = None
    notice_type: str | None = None
    notice_date: date | None = None
    notice_language: str | None = None
    notice_status: str | None = None
    project_country_name: str | None = None
    project_name: str | None = None
    bid_reference_no: str | None = None
    bid_description: str | None = None
    procurement_method_code: str | None = None
    procurement_method_name: str | None = None
    procurement_group: str | None = None
    # Live-tested: `submission_date` equals `noticedate` in 16,319/16,319 records that
    # carry both. It is the publication date, NOT a deadline. The deadline is
    # `submission_deadline_date`, present on ~74-79% of bid-eligible notice types.
    submission_date: date | None = None
    submission_deadline_date: date | None = None
    submission_deadline_time: str | None = None
    notice_text: str | None = None
    # How a bidder actually responds. Populated on roughly a third of notices.
    contact_organization: str | None = None
    contact_name: str | None = None
    contact_email: str | None = None
    contact_phone_no: str | None = None
    contact_address: str | None = None
    contact_web_url: str | None = None
    contact_country_name: str | None = None

    def biddability(self, *, as_of: date | None = None) -> Biddability:
        """Why this notice can or cannot be bid. Delegates to the single shared owner."""
        return assess(
            notice_type=self.notice_type,
            notice_status=self.notice_status,
            procurement_method_name=self.procurement_method_name,
            submission_deadline_date=self.submission_deadline_date,
            as_of=as_of or datetime.now(UTC).date(),
        )

    def is_applyable(self, *, as_of: date | None = None) -> bool:
        """True when a firm could still respond.

        Includes notices with no published deadline: the deadline is in the bid document,
        and treating its absence as expiry hid 24.4% of the bid-eligible population.
        """
        return self.biddability(as_of=as_of).is_open


class Page(BaseModel):
    records: list[dict[str, Any]] = Field(default_factory=list)
    page: int | None = None
    pages: int | None = None
    offset: int | None = None
    rows: int | None = None
    total: int | None = None
    raw_payload: dict[str, Any]
