"""Persistence operations with external-ID-based candidate deduplication."""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from opportunity_engine.domain.models import CandidateEvaluation, CandidateLabel, CapabilityProfile

from .models import (
    CandidateEvaluationRow,
    CandidateEvidenceRow,
    CandidateLabelRow,
    OrganizationProfileRow,
    ScoreComponentRow,
)


def persist_profile(session: Session, profile: CapabilityProfile) -> OrganizationProfileRow:
    row = session.query(OrganizationProfileRow).filter_by(profile_id=profile.profile_id, version=profile.version).one_or_none()
    if row is None:
        row = OrganizationProfileRow(
            profile_id=profile.profile_id,
            version=profile.version,
            organization_name=profile.organization_name,
            profile_payload=profile.model_dump(mode="json"),
            created_at=datetime.now(UTC),
        )
        session.add(row)
    return row


def persist_evaluation(session: Session, evaluation: CandidateEvaluation) -> CandidateEvaluationRow:
    """Replace one deterministic evaluation, avoiding duplicates across ingestion runs."""
    row = session.query(CandidateEvaluationRow).filter_by(
        profile_id=evaluation.profile_id,
        profile_version=evaluation.profile_version,
        taxonomy_version=evaluation.taxonomy_version,
        scoring_engine_version=evaluation.scoring_engine_version,
        source_external_id=evaluation.source_external_id,
    ).one_or_none()
    if row is None:
        row = CandidateEvaluationRow()
        session.add(row)
    row.candidate_key = evaluation.candidate_key
    row.source_external_id = evaluation.source_external_id
    row.source_raw_record_id = evaluation.source_raw_record_id
    row.project_id = evaluation.project_id
    row.profile_id = evaluation.profile_id
    row.profile_version = evaluation.profile_version
    row.taxonomy_version = evaluation.taxonomy_version
    row.scoring_engine_version = evaluation.scoring_engine_version
    row.final_score = evaluation.final_score
    row.tier = evaluation.tier.value
    row.opportunity_class = evaluation.opportunity_class
    row.is_applyable = evaluation.is_applyable
    row.submission_deadline_date = evaluation.submission_deadline_date
    row.generated_at = evaluation.generated_at
    session.flush()
    session.query(ScoreComponentRow).filter_by(candidate_evaluation_id=row.id).delete()
    session.query(CandidateEvidenceRow).filter_by(candidate_evaluation_id=row.id).delete()
    for name, value in evaluation.components.model_dump().items():
        # Components are a mix of floats, ints, a bool and a string; the column is a
        # nullable float, so only numeric components are stored here.
        if isinstance(value, bool):
            value = float(value)
        session.add(ScoreComponentRow(
            candidate_evaluation_id=row.id, component_name=name,
            value=float(value) if isinstance(value, (int, float)) else None,
        ))
    # Suppressed matches are persisted too. What the rules discarded is the only signal
    # for tuning them, and the column existed while nothing ever wrote to it.
    for evidence in [*evaluation.positive_matches, *evaluation.negative_matches,
                     *evaluation.suppressed_matches]:
        session.add(CandidateEvidenceRow(candidate_evaluation_id=row.id, **evidence.model_dump()))
    return row


def persist_label(session: Session, label: CandidateLabel) -> CandidateLabelRow:
    evaluation = session.query(CandidateEvaluationRow).filter_by(candidate_key=label.candidate_key).one()
    row = session.query(CandidateLabelRow).filter_by(candidate_evaluation_id=evaluation.id, source=label.source).one_or_none()
    if row is None:
        row = CandidateLabelRow(candidate_evaluation_id=evaluation.id, source=label.source)
        session.add(row)
    row.label = label.label.value
    row.notes = label.notes
    row.labelled_at = label.labelled_at
    return row
