"""Deterministic relevance scoring. Scores are not win probabilities.

Replaces a coverage-ratio model that divided every component by the size of the
profile. That model asked "what fraction of my whole catalogue does this notice
mention?", so a textbook SIEM tender scored 0.675 against a 2-term profile and 0.004
against a 400-term one -- adding a term to improve recall lowered every score,
including the perfect ones, and HIGH was unreachable at any size.

Two ideas replace it:

* **Confidence has no denominator.** A noisy-OR combination of per-term strengths, so
  the number of terms in the taxonomy cannot move the score.
* **Confidence does not choose the tier.** Noisy-OR saturates -- past a handful of
  matches a strategy paper and a live tender both read ~0.99. The tier comes from what
  *kind* of evidence matched and whether something is being *bought*; confidence gates
  surfacing and orders results inside a tier.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from opportunity_engine.domain.models import RelevanceTier, ScoreComponents
from opportunity_engine.domain.taxonomy import OpportunityClass, TermTier

# Evidence strength by kind. EXCLUSIVE means "no plausible non-domain reading".
TIER_WEIGHT: dict[TermTier, float] = {
    TermTier.EXCLUSIVE: 1.00,
    TermTier.STRONG: 0.85,
    TermTier.VENDOR: 0.85,
    TermTier.CORROBORATING: 0.45,
    TermTier.PROGRAM: 0.45,
    TermTier.WEAK: 0.15,
}

# Where a match carries weight. The three boilerplate fields are zero on purpose:
# `notice_type` and `procurement_method_name` carry near-constant procurement strings,
# so any term colliding with them scored on every notice in the corpus.
FIELD_WEIGHT: dict[str, float] = {
    "bid_description": 1.00,
    "notice_text": 0.85,
    "document_text": 0.95,
    "project_name": 0.60,
    "notice_type": 0.00,
    "procurement_method_name": 0.00,
    "project_country_name": 0.00,
}
METADATA_FIELD_WEIGHT = 0.50

# Fields specific enough that an EXCLUSIVE hit there can carry a HIGH on its own.
SCOPE_FIELDS = frozenset({"bid_description", "notice_text", "document_text"})

EXCLUSIVE_TIERS = frozenset({TermTier.EXCLUSIVE, TermTier.STRONG, TermTier.VENDOR})


def field_weight(field: str) -> float:
    if field.startswith("project_metadata."):
        return METADATA_FIELD_WEIGHT
    return FIELD_WEIGHT.get(field, 0.30)


class CandidateScoringConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engine_version: str = "tiered-v2"
    # Confidence contributed by one perfect EXCLUSIVE hit in bid_description. The single
    # interpretable knob: at 0.55, one such hit reads 0.55, two read 0.80, three 0.91.
    lambda_: float = Field(default=0.55, gt=0, lt=1, alias="lambda")
    surface_threshold: float = Field(default=0.05, ge=0, le=1)
    direct_high_threshold: float = Field(default=0.70, ge=0, le=1)
    embedded_high_threshold: float = Field(default=0.95, ge=0, le=1)
    embedded_high_min_exclusive: int = Field(default=4, ge=1)
    # Share of evidence that must be EXCLUSIVE-family for the notice to read as a
    # direct purchase rather than a passing mention inside a larger scope.
    direct_share: float = Field(default=0.45, ge=0, le=1)
    # The product exists to surface tenders that can still be bid on, so an
    # unapplyable record is never HIGH however strong its evidence.
    require_applyable_for_high: bool = True

    @model_validator(mode="after")
    def thresholds_are_ordered(self) -> CandidateScoringConfig:
        if not self.embedded_high_threshold >= self.direct_high_threshold >= self.surface_threshold:
            raise ValueError("thresholds must satisfy embedded_high >= direct_high >= surface")
        return self


def term_strength(tier: TermTier, fields: list[str]) -> float:
    """One contribution per term, taken at its strongest surviving field.

    Best-match rather than sum-over-fields: the same term echoed into several fields is
    one piece of evidence, not several. Summing let a single word repeated across fields
    look like broad corroboration.
    """
    if not fields:
        return 0.0
    return TIER_WEIGHT[tier] * max(field_weight(field) for field in fields)


def confidence(strengths: list[float], *, lambda_: float) -> float:
    """Noisy-OR combination. Independent of how many terms the taxonomy holds."""
    product = 1.0
    for strength in strengths:
        product *= 1.0 - lambda_ * strength
    return max(0.0, min(1.0, 1.0 - product))


def classify_opportunity(
    *,
    tiers_present: set[TermTier],
    exclusive_share: float,
    has_acquisition_intent: bool,
    config: CandidateScoringConfig,
) -> OpportunityClass:
    """What kind of opportunity this is, independently of how relevant it is.

    A national cyber strategy and a live SIEM tender are both 'relevant' and are not
    comparable on one ladder; forcing them onto one is what made a single tier field
    unable to express the product.
    """
    if not tiers_present:
        return OpportunityClass.NOT_RELEVANT
    has_exclusive = bool(tiers_present & EXCLUSIVE_TIERS)
    if TermTier.PROGRAM in tiers_present and not has_acquisition_intent and not has_exclusive:
        return OpportunityClass.UPSTREAM
    if has_exclusive and exclusive_share >= config.direct_share:
        return OpportunityClass.DIRECT
    if has_exclusive or TermTier.CORROBORATING in tiers_present:
        return OpportunityClass.EMBEDDED
    return OpportunityClass.UPSTREAM if TermTier.PROGRAM in tiers_present else OpportunityClass.NOT_RELEVANT


def classify_tier(
    *,
    score: float,
    components: ScoreComponents,
    opportunity_class: OpportunityClass,
    exclusive_in_scope_fields: int,
    distinct_corroborating: int,
    has_program_evidence: bool,
    vetoed: bool,
    is_applyable: bool,
    config: CandidateScoringConfig,
) -> RelevanceTier:
    """First matching rule wins. Deliberately a ladder, not arithmetic."""
    if vetoed:
        return RelevanceTier.EXCLUDED
    if score < config.surface_threshold:
        return RelevanceTier.NO_MATCH
    has_exclusive = components.exclusive_count + components.strong_count > 0
    if not has_exclusive and distinct_corroborating < 2 and not has_program_evidence:
        return RelevanceTier.LOW
    # A strategy or assessment is not a purchase, however strong the language.
    if opportunity_class is OpportunityClass.UPSTREAM:
        return RelevanceTier.MEDIUM
    high_allowed = is_applyable or not config.require_applyable_for_high
    direct_high = (
        opportunity_class is OpportunityClass.DIRECT
        and exclusive_in_scope_fields >= 1
        and score >= config.direct_high_threshold
    )
    embedded_high = (
        opportunity_class is OpportunityClass.EMBEDDED
        and exclusive_in_scope_fields >= config.embedded_high_min_exclusive
        and score >= config.embedded_high_threshold
    )
    if high_allowed and (direct_high or embedded_high):
        return RelevanceTier.HIGH
    if has_exclusive or distinct_corroborating >= 2:
        return RelevanceTier.MEDIUM
    return RelevanceTier.LOW
