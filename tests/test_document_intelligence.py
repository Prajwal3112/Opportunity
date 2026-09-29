"""Synthetic generic text only; no fabricated World Bank response.

These exercise the evidence-grounding guardrail, which is the control standing between
a model's claim and a fabricated capability match.
"""
from datetime import UTC, datetime

import pytest

from opportunity_engine.document_intelligence import (
    CapabilityFinding,
    EvidenceAnalysis,
    EvidenceReference,
    checksum,
    chunk_text,
    lexical_retrieve,
    validate_analysis,
)

SOURCE_URL = "https://documents.worldbank.org/a"
PASSAGE = (
    "The Bidder shall supply and commission a security information and event "
    "management platform with centralized log correlation and 24x7 monitoring."
)


def analysis(*, quote: str, chunk_id: str = "D1:0", retrieved: list[str] | None = None) -> EvidenceAnalysis:
    return EvidenceAnalysis(
        candidate_id="x",
        relevance_tier="HIGH",
        summary="",
        capability_findings=[
            CapabilityFinding(
                capability="SIEM",
                assessment="MATCH",
                evidence=[
                    EvidenceReference(
                        document_id="D1", chunk_id=chunk_id, source_url=SOURCE_URL, quote=quote
                    )
                ],
            )
        ],
        evidence_sufficient=True,
        analysis_version="1",
        model_name="test",
        prompt_version="1",
        analysed_at=datetime.now(UTC),
        retrieved_chunk_ids=["D1:0"] if retrieved is None else retrieved,
    )


def test_chunks_retrieval_and_checksum() -> None:
    chunks = chunk_text("D1", "P1", SOURCE_URL, PASSAGE, 900)
    assert checksum(b"x")
    assert lexical_retrieve("SIEM security", chunks)[0].lexical_score > 0


def test_grounded_quote_is_accepted() -> None:
    chunks = chunk_text("D1", None, SOURCE_URL, PASSAGE, 900)
    validate_analysis(analysis(quote="security information and event management platform"), chunks)


def test_empty_quote_is_rejected() -> None:
    """An empty string is a substring of every string, so it must not validate."""
    chunks = chunk_text("D1", None, SOURCE_URL, PASSAGE, 900)
    with pytest.raises(ValueError, match="characters"):
        validate_analysis(analysis(quote=""), chunks)


def test_trivially_short_quote_is_rejected() -> None:
    chunks = chunk_text("D1", None, SOURCE_URL, PASSAGE, 900)
    with pytest.raises(ValueError, match="characters"):
        validate_analysis(analysis(quote="SIEM"), chunks)


def test_quote_absent_from_the_chunk_is_rejected() -> None:
    chunks = chunk_text("D1", None, SOURCE_URL, PASSAGE, 900)
    with pytest.raises(ValueError, match="does not appear"):
        validate_analysis(analysis(quote="the Borrower shall procure a fleet of ambulances"), chunks)


def test_unknown_chunk_id_is_rejected() -> None:
    chunks = chunk_text("D1", None, SOURCE_URL, PASSAGE, 900)
    with pytest.raises(ValueError, match="unknown chunk"):
        validate_analysis(analysis(quote=PASSAGE[:60], chunk_id="D1:999"), chunks)


def test_unknown_retrieved_chunk_id_is_rejected() -> None:
    """`retrieved_chunk_ids` was previously never validated at all."""
    chunks = chunk_text("D1", None, SOURCE_URL, PASSAGE, 900)
    with pytest.raises(ValueError, match="retrieved_chunk_ids"):
        validate_analysis(
            analysis(quote=PASSAGE[:60], retrieved=["NO-SUCH-CHUNK"]), chunks
        )


def test_quote_matching_tolerates_rewrapped_whitespace() -> None:
    """PDF-extracted text rewraps, so comparison collapses whitespace on both sides."""
    chunks = chunk_text("D1", None, SOURCE_URL, PASSAGE, 900)
    validate_analysis(analysis(quote="security information   and event\n  management platform"), chunks)


def test_missing_evidence_rejected() -> None:
    with pytest.raises(ValueError):
        EvidenceAnalysis(
            candidate_id="x",
            relevance_tier="",
            summary="",
            capability_findings=[CapabilityFinding(capability="x", assessment="MATCH")],
            evidence_sufficient=False,
            analysis_version="1",
            model_name="x",
            prompt_version="1",
            analysed_at=datetime.now(UTC),
            retrieved_chunk_ids=[],
        )
