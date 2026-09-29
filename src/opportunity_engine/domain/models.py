"""Domain models independent of HTTP clients and SQLAlchemy."""
from __future__ import annotations

import json
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RelevanceTier(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    # Distinct from LOW: nothing matched at all. Under a domain cap ~99% of the corpus
    # lands here, and collapsing it into LOW made the tier column carry no information.
    NO_MATCH = "NO_MATCH"
    EXCLUDED = "EXCLUDED"


class EvaluationLabel(StrEnum):
    RELEVANT = "RELEVANT"
    NOT_RELEVANT = "NOT_RELEVANT"
    UNCERTAIN = "UNCERTAIN"


class CapabilityProfile(BaseModel):
    """Replaceable, versioned organization input for deterministic matching."""

    model_config = ConfigDict(extra="forbid")

    profile_id: str
    version: str
    organization_name: str
    products: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    services: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)
    positive_keywords: list[str] = Field(default_factory=list)
    negative_keywords: list[str] = Field(default_factory=list)
    geographic_preferences: list[str] = Field(default_factory=list)
    excluded_categories: list[str] = Field(default_factory=list)

    @field_validator(
        "products", "capabilities", "services", "industries", "technologies", "positive_keywords",
        "negative_keywords", "geographic_preferences", "excluded_categories",
    )
    @classmethod
    def strip_and_deduplicate(cls, values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            cleaned = value.strip()
            key = cleaned.casefold()
            if cleaned and key not in seen:
                seen.add(key)
                result.append(cleaned)
        return result

    @classmethod
    def from_json(cls, path: Path) -> CapabilityProfile:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def as_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=True)

    def positive_terms(self) -> dict[str, list[str]]:
        return {
            "positive_keyword": self.positive_keywords,
            "product": self.products,
            "capability": self.capabilities,
            "service": self.services,
            "technology": self.technologies,
        }


class CandidateInput(BaseModel):
    """Ordinary source data used by matching; original source text is never mutated."""

    model_config = ConfigDict(extra="forbid")

    source_external_id: str
    project_id: str | None = None
    bid_description: str | None = None
    notice_text: str | None = None
    project_name: str | None = None
    notice_type: str | None = None
    procurement_method_name: str | None = None
    project_country_name: str | None = None
    project_metadata: dict[str, str] = Field(default_factory=dict)
    # Applyability. Carried so ranking can put a live tender above a better-matching
    # but already-awarded one, which is what the product is actually for.
    notice_status: str | None = None
    submission_deadline_date: date | None = None
    bid_reference_no: str | None = None
    contact_email: str | None = None
    source_url: str | None = None

    def text_fields(self) -> dict[str, str]:
        fields = {
            "bid_description": self.bid_description,
            "notice_text": self.notice_text,
            "project_name": self.project_name,
            "notice_type": self.notice_type,
            "procurement_method_name": self.procurement_method_name,
            "project_country_name": self.project_country_name,
        }
        return {name: value for name, value in fields.items() if value and value.strip()}

    def metadata_fields(self) -> dict[str, str]:
        return {f"project_metadata.{key}": value for key, value in self.project_metadata.items() if value.strip()}


class MatchEvidence(BaseModel):
    """One surviving or suppressed occurrence of a taxonomy term."""

    term: str
    term_id: str = ""
    source_field: str
    snippet: str
    match_type: str
    tier: str = ""
    category: str = ""
    # Offsets into the source field, so a reviewer can locate the exact occurrence and
    # so containment suppression is decidable.
    start_offset: int = 0
    end_offset: int = 0
    # Populated when a rule discarded this occurrence. Suppressed evidence is retained
    # deliberately: what the rules killed is the only signal for tuning them.
    suppressed_by: str | None = None
    context_satisfied_by: str | None = None
    is_negative: bool = False


class ScoreComponents(BaseModel):
    """Explainable components. `confidence` orders results; it never picks the tier."""

    confidence: float
    exclusive_count: int = 0
    strong_count: int = 0
    corroborating_count: int = 0
    category_count: int = 0
    suppressed_count: int = 0
    acquisition_intent: bool = False
    semantic: float | None = None


class CandidateEvaluation(BaseModel):
    candidate_key: str
    source_external_id: str
    project_id: str | None = None
    final_score: float
    tier: RelevanceTier
    opportunity_class: str = "NOT_RELEVANT"
    components: ScoreComponents
    positive_matches: list[MatchEvidence]
    negative_matches: list[MatchEvidence]
    # What the rules discarded. Kept so over-blocking is discoverable.
    suppressed_matches: list[MatchEvidence] = Field(default_factory=list)
    # Carried through from the source so a queue can rank by applyability first.
    is_applyable: bool = False
    submission_deadline_date: date | None = None
    generated_at: datetime
    scoring_engine_version: str
    profile_id: str
    profile_version: str
    taxonomy_version: str = ""
    source_raw_record_id: int | None = None

    def sort_key(self) -> tuple[int, int, float]:
        """Rank applyable opportunities above everything else, then by tier, then score.

        A live tender you can bid on outranks a better-matching contract that was
        awarded last year. Relevance alone would invert that.
        """
        tier_rank = {
            RelevanceTier.HIGH: 0, RelevanceTier.MEDIUM: 1, RelevanceTier.LOW: 2,
            RelevanceTier.NO_MATCH: 3, RelevanceTier.EXCLUDED: 4,
        }
        return (0 if self.is_applyable else 1, tier_rank[self.tier], -self.final_score)


class CandidateLabel(BaseModel):
    candidate_key: str
    label: EvaluationLabel
    labelled_at: datetime
    notes: str | None = None
    source: str = "manual"
