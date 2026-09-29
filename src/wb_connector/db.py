"""PostgreSQL persistence with immutable raw-response snapshots."""
from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from pydantic import AnyUrl
from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from .models import Document, ProcurementNotice, Project


class Base(DeclarativeBase):
    pass


class RawSourceRecord(Base):
    __tablename__ = "raw_source_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    entity_type: Mapped[str] = mapped_column(String(64), index=True)
    external_id: Mapped[str] = mapped_column(String(128), index=True)
    source_url: Mapped[str] = mapped_column(String(2048))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class ExternalEntity(Base):
    __abstract__ = True
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(64), default="world_bank")
    external_id: Mapped[str] = mapped_column(String(128))
    source_url: Mapped[str] = mapped_column(String(2048))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    latest_raw_id: Mapped[int | None] = mapped_column(ForeignKey("raw_source_records.id"))


class ProjectRow(ExternalEntity):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("source", "external_id"),)
    project_id: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str | None] = mapped_column(String(2048))
    status: Mapped[str | None] = mapped_column(String(128))
    country: Mapped[str | None] = mapped_column(String(512))
    board_approval_date: Mapped[date | None] = mapped_column(Date)


class DocumentRow(ExternalEntity):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("source", "external_id"),)
    project_id: Mapped[str | None] = mapped_column(String(128), index=True)
    title: Mapped[str | None] = mapped_column(String(2048))
    document_type: Mapped[str | None] = mapped_column(String(256))
    pdf_url: Mapped[str | None] = mapped_column(String(2048))
    txt_url: Mapped[str | None] = mapped_column(String(2048))
    published_at: Mapped[date | None] = mapped_column(Date)


class ProcurementNoticeRow(ExternalEntity):
    __tablename__ = "procurement_notices"
    __table_args__ = (UniqueConstraint("source", "external_id"),)
    project_id: Mapped[str | None] = mapped_column(String(128), index=True)
    notice_type: Mapped[str | None] = mapped_column(String(256))
    notice_date: Mapped[date | None] = mapped_column(Date)
    notice_language: Mapped[str | None] = mapped_column(String(256))
    notice_status: Mapped[str | None] = mapped_column(String(128))
    project_country_name: Mapped[str | None] = mapped_column(String(512))
    project_name: Mapped[str | None] = mapped_column(String(2048))
    bid_reference_no: Mapped[str | None] = mapped_column(String(1024))
    bid_description: Mapped[str | None] = mapped_column(Text)
    procurement_method_code: Mapped[str | None] = mapped_column(String(128))
    procurement_method_name: Mapped[str | None] = mapped_column(String(512))
    procurement_group: Mapped[str | None] = mapped_column(String(128))
    submission_date: Mapped[date | None] = mapped_column(Date)
    submission_deadline_date: Mapped[date | None] = mapped_column(Date, index=True)
    submission_deadline_time: Mapped[str | None] = mapped_column(String(64))
    notice_text: Mapped[str | None] = mapped_column(Text)
    contact_organization: Mapped[str | None] = mapped_column(String(1024))
    contact_name: Mapped[str | None] = mapped_column(String(512))
    contact_email: Mapped[str | None] = mapped_column(String(512))
    contact_phone_no: Mapped[str | None] = mapped_column(String(256))
    contact_address: Mapped[str | None] = mapped_column(Text)
    contact_web_url: Mapped[str | None] = mapped_column(String(2048))
    contact_country_name: Mapped[str | None] = mapped_column(String(512))


def create_database(database_url: str) -> None:
    # Register candidate-layer tables without making the connector import its business logic at module load.
    import opportunity_engine.persistence.models  # noqa: F401

    Base.metadata.create_all(create_engine(database_url))


_ENTITY_MAPPING: dict[type, tuple[str, type]] = {
    Project: ("project", ProjectRow),
    Document: ("document", DocumentRow),
    ProcurementNotice: ("procurement_notice", ProcurementNoticeRow),
}
_UNMAPPED = frozenset({"source", "source_url", "external_id", "retrieved_at", "raw_payload"})


def _column_values(record: Project | Document | ProcurementNotice, row_type: type) -> dict[str, Any]:
    """Model fields as column values, failing loudly if the model and table have drifted.

    ``mode="json"`` would render every date as a string and hand it to a ``Date``
    column, which raises on SQLite and is dialect-dependent elsewhere. Python mode keeps
    real ``date`` objects; only the URL fields need explicit coercion.
    """
    values = record.model_dump(exclude=set(_UNMAPPED), mode="python")
    columns = {column.name for column in row_type.__table__.columns}
    unknown = sorted(set(values) - columns)
    if unknown:
        raise TypeError(
            f"{type(record).__name__} declares field(s) {unknown} that {row_type.__name__} "
            "has no column for. Add the column or exclude the field explicitly."
        )
    return {key: (str(value) if isinstance(value, AnyUrl) else value) for key, value in values.items()}


def persist(session: Session, record: Project | Document | ProcurementNotice) -> None:
    try:
        entity_type, row_type = _ENTITY_MAPPING[type(record)]
    except KeyError:
        raise TypeError(
            f"persist() has no table mapping for {type(record).__name__}; "
            f"known types are {sorted(t.__name__ for t in _ENTITY_MAPPING)}"
        ) from None
    snapshot = RawSourceRecord(source=record.source, entity_type=entity_type, external_id=record.external_id,
                               source_url=record.source_url, retrieved_at=record.retrieved_at, payload=record.raw_payload)
    session.add(snapshot)
    session.flush()
    row = session.query(row_type).filter_by(source=record.source, external_id=record.external_id).one_or_none()
    now = datetime.now(UTC)
    values = _column_values(record, row_type)
    if row is None:
        row = row_type(source=record.source, external_id=record.external_id, source_url=record.source_url,
                       first_seen_at=now, last_seen_at=now, latest_raw_id=snapshot.id, **values)
        session.add(row)
    else:
        row.source_url, row.last_seen_at, row.latest_raw_id = record.source_url, now, snapshot.id
        for key, value in values.items():
            setattr(row, key, value)
