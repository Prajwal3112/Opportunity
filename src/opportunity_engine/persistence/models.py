"""Candidate-layer SQLAlchemy models; raw World Bank tables remain unchanged."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, Date, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from wb_connector.db import Base


class OrganizationProfileRow(Base):
    __tablename__ = "organization_capability_profiles"
    __table_args__ = (UniqueConstraint("profile_id", "version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[str] = mapped_column(String(128), index=True)
    version: Mapped[str] = mapped_column(String(128))
    organization_name: Mapped[str] = mapped_column(String(512))
    profile_payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CandidateEvaluationRow(Base):
    """One scored candidate.

    The unique key includes ``taxonomy_version`` deliberately. Without it, editing
    ``profiles/cybersecurity.json`` reused the existing row and silently destroyed the
    previous evaluation — so a score could not be reproduced and a before/after
    comparison across a taxonomy change was impossible. Including it means a taxonomy
    edit writes a NEW row and history survives.
    """

    __tablename__ = "candidate_evaluations"
    __table_args__ = (
        UniqueConstraint(
            "profile_id", "profile_version", "taxonomy_version",
            "scoring_engine_version", "source_external_id",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_key: Mapped[str] = mapped_column(String(512), index=True)
    source_external_id: Mapped[str] = mapped_column(String(128), index=True)
    source_raw_record_id: Mapped[int | None] = mapped_column(ForeignKey("raw_source_records.id"))
    project_id: Mapped[str | None] = mapped_column(String(128), index=True)
    profile_id: Mapped[str] = mapped_column(String(128), index=True)
    profile_version: Mapped[str] = mapped_column(String(128))
    taxonomy_version: Mapped[str] = mapped_column(String(128), default="")
    scoring_engine_version: Mapped[str] = mapped_column(String(128))
    final_score: Mapped[float]
    tier: Mapped[str] = mapped_column(String(16))
    # Carried so a queue can rank by applyability without re-deriving it from the notice.
    opportunity_class: Mapped[str] = mapped_column(String(32), default="")
    is_applyable: Mapped[bool] = mapped_column(default=False)
    submission_deadline_date: Mapped[date | None] = mapped_column(Date, index=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ScoreComponentRow(Base):
    __tablename__ = "candidate_score_components"
    __table_args__ = (UniqueConstraint("candidate_evaluation_id", "component_name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_evaluation_id: Mapped[int] = mapped_column(ForeignKey("candidate_evaluations.id"), index=True)
    component_name: Mapped[str] = mapped_column(String(128))
    value: Mapped[float | None]


class CandidateEvidenceRow(Base):
    __tablename__ = "candidate_evidence"
    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_evaluation_id: Mapped[int] = mapped_column(ForeignKey("candidate_evaluations.id"), index=True)
    term: Mapped[str] = mapped_column(String(1024))
    term_id: Mapped[str] = mapped_column(String(256), default="")
    source_field: Mapped[str] = mapped_column(String(256))
    snippet: Mapped[str] = mapped_column(Text)
    match_type: Mapped[str] = mapped_column(String(128))
    tier: Mapped[str] = mapped_column(String(32), default="")
    category: Mapped[str] = mapped_column(String(128), default="")
    start_offset: Mapped[int] = mapped_column(default=0)
    end_offset: Mapped[int] = mapped_column(default=0)
    # Retained deliberately: what the rules discarded is the only signal for tuning them.
    suppressed_by: Mapped[str | None] = mapped_column(String(256))
    context_satisfied_by: Mapped[str | None] = mapped_column(String(256))
    is_negative: Mapped[bool]


class CandidateLabelRow(Base):
    __tablename__ = "candidate_labels"
    __table_args__ = (UniqueConstraint("candidate_evaluation_id", "source"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_evaluation_id: Mapped[int] = mapped_column(ForeignKey("candidate_evaluations.id"), index=True)
    label: Mapped[str] = mapped_column(String(32))
    notes: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(128), default="manual")
    labelled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
