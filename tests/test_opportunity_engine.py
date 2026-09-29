"""Synthetic strings test generic algorithms; these are not World Bank fixtures.

The tender language below is written to represent real drafting conventions. Where a
string mirrors something observed in a real capture it is noted as such.
"""
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from opportunity_engine.domain.models import CandidateInput, CapabilityProfile, RelevanceTier
from opportunity_engine.domain.taxonomy import (
    ContextGroup,
    ContextRule,
    DomainTaxonomy,
    OpportunityClass,
    Term,
    TermTier,
    load_taxonomy,
)
from opportunity_engine.matching.engine import CandidateEngine
from opportunity_engine.matching.normalization import normalize_for_matching, unique_phrases
from opportunity_engine.matching.scoring import CandidateScoringConfig, confidence
from opportunity_engine.matching.spans import Span, find_spans, is_suppressed
from opportunity_engine.persistence.models import CandidateEvaluationRow
from opportunity_engine.persistence.repository import persist_evaluation, persist_profile
from wb_connector.db import Base

PROFILE = CapabilityProfile(profile_id="cyber", version="1", organization_name="Domain sweep")
FUTURE = date(2099, 1, 1)


def taxonomy(**overrides) -> DomainTaxonomy:
    base = {
        "taxonomy_id": "test",
        "taxonomy_version": "1",
        "terms": [
            Term(id="siem.expanded", text="security information and event management",
                 tier=TermTier.EXCLUSIVE, category="siem"),
            Term(id="siem.acronym", text="SIEM", tier=TermTier.STRONG, category="siem",
                 case_sensitive=True, blocked_ngrams=["siem reap"]),
            Term(id="dlp", text="data loss prevention", tier=TermTier.EXCLUSIVE, category="dlp"),
            Term(id="ir", text="incident response", tier=TermTier.CORROBORATING, category="soc",
                 requires_context=ContextRule(group="CYBER", window_tokens=12)),
        ],
        "context_groups": {"CYBER": ContextGroup(name="CYBER", terms=["cyber", "cybersecurity", "malware"])},
        "blocked_ngrams": ["food security", "siem reap"],
        "acquire_lexicon": ["supply of", "procurement of"],
    }
    return DomainTaxonomy(**{**base, **overrides})


def engine(taxonomy_override: DomainTaxonomy | None = None, **config) -> CandidateEngine:
    return CandidateEngine(CandidateScoringConfig(**config), taxonomy_override or taxonomy())


def candidate(description: str, **overrides) -> CandidateInput:
    base = {
        "source_external_id": "OP-SYNTHETIC-1",
        "project_id": "P-SYNTHETIC-1",
        "bid_description": description,
        "notice_type": "Invitation for Bids",
        "notice_status": "Published",
        "submission_deadline_date": FUTURE,
    }
    return CandidateInput(**{**base, **overrides})


def test_text_normalization_and_duplicate_phrase_handling() -> None:
    assert normalize_for_matching("  ＳＩＥＭ—Security! ") == "siem security"
    assert unique_phrases(["SIEM", "siem", " SIEM "]) == ["SIEM"]


# --------------------------------------------------------------------------------------
# The headline fix: the score must not depend on how many terms the taxonomy holds.
# --------------------------------------------------------------------------------------

def test_score_is_invariant_to_taxonomy_size() -> None:
    """The previous model divided by profile size, so a perfect SIEM tender fell from
    0.675 to 0.004 as the taxonomy grew. Adding unrelated terms must change nothing."""
    text = "Supply of a security information and event management solution"
    small = engine().evaluate(candidate(text), PROFILE)
    padded = taxonomy(terms=[
        *taxonomy().terms,
        *[Term(id=f"filler.{i}", text=f"unrelated capability phrase number {i}",
               tier=TermTier.CORROBORATING, category="filler") for i in range(400)],
    ])
    large = engine(padded).evaluate(candidate(text), PROFILE)
    assert small.final_score == pytest.approx(large.final_score)
    assert small.tier == large.tier


def test_confidence_saturates_without_a_denominator() -> None:
    assert confidence([], lambda_=0.55) == 0.0
    one = confidence([1.0], lambda_=0.55)
    two = confidence([1.0, 1.0], lambda_=0.55)
    assert one == pytest.approx(0.55)
    assert two > one and two < 1.0


# --------------------------------------------------------------------------------------
# Span-level disambiguation
# --------------------------------------------------------------------------------------

def test_blocked_phrase_suppresses_only_the_contained_occurrence() -> None:
    """A notice may legitimately mention 'food security' in background and 'SIEM' in
    scope. Document-level negation would discard the genuine opportunity."""
    result = engine().evaluate(
        candidate("Supply of a security information and event management platform "
                  "for the national food security programme"),
        PROFILE,
    )
    assert {m.term for m in result.positive_matches} == {"security information and event management"}
    assert result.tier in {RelevanceTier.HIGH, RelevanceTier.MEDIUM}


def test_siem_reap_title_case_is_stopped_by_case_sensitivity() -> None:
    result = engine().evaluate(candidate("Siem Reap Water Supply and Sanitation Project"), PROFILE)
    assert result.positive_matches == []
    assert result.tier is RelevanceTier.NO_MATCH


def test_siem_reap_in_all_caps_is_stopped_by_the_blocklist() -> None:
    """Real bid descriptions are frequently ALL CAPS, and so are PDF headings. Case
    gating carries no information there, so span containment must do the work."""
    result = engine().evaluate(candidate("SIEM REAP PROVINCIAL ROAD REHABILITATION PROJECT"), PROFILE)
    assert result.positive_matches == []
    assert result.tier is RelevanceTier.NO_MATCH
    assert any(m.suppressed_by == "blocked_ngram" for m in result.suppressed_matches)


def test_case_sensitive_acronym_ignores_the_lowercase_word() -> None:
    assert find_spans("Food prices soar during the lean season", "SOAR", case_sensitive=True) == []
    assert find_spans("Supply of SOAR tooling", "SOAR", case_sensitive=True)


def test_ampersand_matches_the_word_and() -> None:
    """Every EXCLUSIVE phrase contains 'and', and '&' is the dominant rendering in
    tender headings. The previous matcher silently missed all of them."""
    result = engine().evaluate(candidate("Procurement of a Security Information & Event Management platform"), PROFILE)
    assert result.positive_matches
    assert result.positive_matches[0].term == "security information and event management"


def test_separator_does_not_cross_a_sentence_boundary() -> None:
    assert find_spans("road traffic incident. Response times shall be reported", "incident response") == []
    assert find_spans("the cyber incident response plan", "incident response")


def test_required_context_gates_an_ambiguous_term() -> None:
    without = engine().evaluate(candidate("Emergency incident response plan for flood events"), PROFILE)
    assert not without.positive_matches
    assert any(m.suppressed_by == "missing_context:CYBER" for m in without.suppressed_matches)

    with_context = engine().evaluate(candidate("Cybersecurity incident response retainer"), PROFILE)
    assert any(m.term == "incident response" for m in with_context.positive_matches)


def test_span_containment_helper() -> None:
    assert is_suppressed(Span(5, 9), [Span(0, 13)])
    assert not is_suppressed(Span(5, 9), [Span(6, 13)])


def test_every_occurrence_is_found_not_only_the_first() -> None:
    text = "SIEM procurement; a second SIEM reference; a third SIEM mention"
    assert len(find_spans(text, "SIEM", case_sensitive=True)) == 3


# --------------------------------------------------------------------------------------
# Tier and class
# --------------------------------------------------------------------------------------

def test_unmatched_notice_is_no_match_not_low() -> None:
    result = engine().evaluate(candidate("Supply of 400 desks and 800 chairs to primary schools"), PROFILE)
    assert result.tier is RelevanceTier.NO_MATCH
    assert result.opportunity_class == OpportunityClass.NOT_RELEVANT.value


def test_two_exclusive_terms_with_intent_reach_high() -> None:
    result = engine().evaluate(
        candidate("Procurement of a security information and event management platform "
                  "and a data loss prevention solution"),
        PROFILE,
    )
    assert result.tier is RelevanceTier.HIGH
    assert result.opportunity_class == OpportunityClass.DIRECT.value
    assert result.components.acquisition_intent


def test_high_requires_an_applyable_notice() -> None:
    """A perfectly matching Contract Award is not an opportunity; it is already decided."""
    text = ("Procurement of a security information and event management platform "
            "and a data loss prevention solution")
    awarded = engine().evaluate(candidate(text, notice_type="Contract Award"), PROFILE)
    assert awarded.is_applyable is False
    assert awarded.tier is not RelevanceTier.HIGH


def test_applyable_results_sort_above_better_matching_closed_ones() -> None:
    strong_closed = engine().evaluate(
        candidate("Procurement of security information and event management and data loss prevention",
                  notice_type="Contract Award"),
        PROFILE,
    )
    weaker_open = engine().evaluate(candidate("Supply of a data loss prevention solution"), PROFILE)
    assert min([strong_closed, weaker_open], key=lambda e: e.sort_key()) is weaker_open


# --------------------------------------------------------------------------------------
# The shipped taxonomy
# --------------------------------------------------------------------------------------

def test_shipped_cybersecurity_taxonomy_loads_and_lints_clean() -> None:
    shipped = load_taxonomy("profiles/cybersecurity.json")
    assert shipped.lint() == []
    assert shipped.terms


def test_taxonomy_rejects_a_rule_naming_an_unknown_context_group() -> None:
    with pytest.raises(ValueError, match="unknown context group"):
        DomainTaxonomy(
            taxonomy_id="t", taxonomy_version="1",
            terms=[Term(id="a", text="alpha", tier=TermTier.WEAK, category="c",
                        requires_context=ContextRule(group="MISSING"))],
        )


def test_linter_flags_silent_authoring_failures() -> None:
    problems = DomainTaxonomy(
        taxonomy_id="t", taxonomy_version="1",
        terms=[
            Term(id="paren", text="security guard(s)", tier=TermTier.WEAK, category="c"),
            Term(id="slash", text="data in motion/at rest", tier=TermTier.WEAK, category="c"),
            Term(id="acronym", text="DLP", tier=TermTier.STRONG, category="c"),
        ],
    ).lint()
    assert any("alternation" in p and "paren" in p for p in problems)
    assert any("slash" in p for p in problems)
    assert any("case_sensitive" in p for p in problems)


# --------------------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------------------

def test_profile_versions_and_external_id_deduplication() -> None:
    db = create_engine("sqlite://")
    Base.metadata.create_all(db)
    evaluation = engine().evaluate(
        candidate("Supply of a security information and event management solution"),
        CapabilityProfile(profile_id="cyber", version="2", organization_name="Domain sweep"),
    )
    with Session(db) as session:
        first = persist_profile(session, PROFILE)
        second = persist_profile(
            session, CapabilityProfile(profile_id="cyber", version="2", organization_name="Domain sweep")
        )
        persist_evaluation(session, evaluation)
        persist_evaluation(session, evaluation)
        session.commit()
        assert first.version != second.version
        assert len(session.scalars(select(CandidateEvaluationRow)).all()) == 1


def test_candidate_key_distinguishes_engine_versions() -> None:
    """Sharing a key across engine versions made label lookups ambiguous."""
    text = "Supply of a security information and event management solution"
    v1 = engine(engine_version="tiered-v2").evaluate(candidate(text), PROFILE)
    v2 = engine(engine_version="tiered-v3").evaluate(candidate(text), PROFILE)
    assert v1.candidate_key != v2.candidate_key


def test_evaluation_records_taxonomy_version_for_reproducibility() -> None:
    result = engine().evaluate(candidate("Supply of data loss prevention"), PROFILE)
    assert result.taxonomy_version == "1"
    assert result.generated_at <= datetime.now(UTC)


def test_a_match_cannot_satisfy_its_own_context_requirement() -> None:
    """A guard whose required group contains the term's own text is a silent no-op.

    This shipped: `penetration testing` required CYBER_QUALIFIER and was itself a member
    of that group, so geotechnical soil-testing notices passed a guard written to stop
    exactly them, and the linter reported clean.
    """
    tax = DomainTaxonomy(
        taxonomy_id="t", taxonomy_version="1",
        terms=[Term(id="pentest", text="penetration testing", tier=TermTier.CORROBORATING,
                    category="vuln", requires_context=ContextRule(group="CYBER", window_tokens=12))],
        context_groups={"CYBER": ContextGroup(name="CYBER", terms=["cyber", "penetration testing"])},
    )
    assert any("satisfied by the match itself" in p for p in tax.lint())

    result = CandidateEngine(CandidateScoringConfig(), tax).evaluate(
        candidate("Soil investigation including standard penetration testing at 20 boreholes"), PROFILE
    )
    assert result.positive_matches == []
    assert any(m.suppressed_by == "missing_context:CYBER" for m in result.suppressed_matches)


def test_shipped_taxonomy_has_no_self_satisfying_guards() -> None:
    shipped = load_taxonomy("profiles/cybersecurity.json")
    for term in shipped.terms:
        if term.requires_context:
            group = shipped.context_groups[term.requires_context.group].terms
            assert not any(g.casefold() == term.text.casefold() for g in group), term.id


# --------------------------------------------------------------------------------------
# Reproducibility: a taxonomy edit must not destroy the previous evaluation
# --------------------------------------------------------------------------------------

def test_taxonomy_version_change_creates_a_new_row_instead_of_overwriting() -> None:
    """The unique key omitted taxonomy_version, so editing profiles/cybersecurity.json
    silently reused the row and a score became unreproducible."""
    db = create_engine("sqlite://")
    Base.metadata.create_all(db)
    text = "Supply of a security information and event management solution"

    v1 = engine(taxonomy(taxonomy_version="1")).evaluate(candidate(text), PROFILE)
    v2 = engine(taxonomy(taxonomy_version="2")).evaluate(candidate(text), PROFILE)
    assert v1.taxonomy_version != v2.taxonomy_version

    with Session(db) as session:
        persist_evaluation(session, v1)
        persist_evaluation(session, v2)
        session.commit()
        rows = session.scalars(select(CandidateEvaluationRow)).all()
        assert len(rows) == 2, "a taxonomy edit must not overwrite the earlier evaluation"
        assert {r.taxonomy_version for r in rows} == {"1", "2"}


def test_suppressed_evidence_is_persisted_for_tuning() -> None:
    """What the rules discarded is the only signal for tuning them. The column existed
    and nothing ever wrote to it."""
    from opportunity_engine.persistence.models import CandidateEvidenceRow

    db = create_engine("sqlite://")
    Base.metadata.create_all(db)
    # "Emergency incident response" is gated by the CYBER context rule, so it is
    # suppressed rather than matched.
    result = engine().evaluate(candidate("Emergency incident response plan for floods"), PROFILE)
    assert result.suppressed_matches, "expected the guard to suppress something"

    with Session(db) as session:
        persist_evaluation(session, result)
        session.commit()
        stored = session.scalars(select(CandidateEvidenceRow)).all()
        assert any(e.suppressed_by for e in stored), "suppressed evidence must reach the database"


def test_evaluation_row_carries_applyability_for_ranking() -> None:
    db = create_engine("sqlite://")
    Base.metadata.create_all(db)
    result = engine().evaluate(candidate("Supply of a data loss prevention solution"), PROFILE)
    with Session(db) as session:
        row = persist_evaluation(session, result)
        session.commit()
        assert row.is_applyable is True
        assert row.submission_deadline_date == FUTURE
        assert row.opportunity_class
