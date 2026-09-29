"""Domain taxonomy schema. Contains no domain knowledge itself.

Every cybersecurity fact lives in a versioned JSON profile; this module only defines
the shape such a profile must take. Swapping the JSON swaps the domain.

The tiers deliberately carry no arithmetic weight of their own. A tier states what
*kind* of evidence a term is, and the tier ladder in ``matching.scoring`` turns kinds
into a decision. Scores order results within a tier; they never choose the tier.
"""
from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TermTier(StrEnum):
    """What kind of evidence a matched term constitutes."""

    EXCLUSIVE = "EXCLUSIVE"          # No plausible non-domain sense. Can stand alone.
    STRONG = "STRONG"                # Strongly domain-specific, but acronym-shaped.
    CORROBORATING = "CORROBORATING"  # Real domain language, ambiguous alone.
    PROGRAM = "PROGRAM"              # Policy/strategy signal, not a purchase.
    WEAK = "WEAK"                    # Contributes corroboration only. Never fires alone.
    VENDOR = "VENDOR"                # Named commercial product. Corroboration only.


class OpportunityClass(StrEnum):
    """What kind of opportunity this is, independent of how relevant it is."""

    DIRECT = "DIRECT"                # The notice itself buys the thing.
    EMBEDDED = "EMBEDDED"            # A component inside a larger procurement.
    UPSTREAM = "UPSTREAM"            # Strategy/assessment implying future procurement.
    NOT_RELEVANT = "NOT_RELEVANT"


class ContextRule(BaseModel):
    """Require or forbid a context group within a token window of a match."""

    model_config = ConfigDict(extra="forbid")

    group: str
    window_tokens: int = Field(default=12, ge=1, le=200)
    # Short fields (a bid description is often title-length) make a clause window
    # meaningless, so the whole field is used instead below this token count.
    whole_field_below_tokens: int = Field(default=40, ge=0)


class Term(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    text: str
    tier: TermTier
    category: str
    # Uppercase-only matching. Needed wherever an acronym collides with an ordinary
    # word or another sector's abbreviation.
    case_sensitive: bool = False
    requires_context: ContextRule | None = None
    blocked_context: ContextRule | None = None
    # Phrases that, when they contain a match, suppress it. Span containment, not a score.
    blocked_ngrams: list[str] = Field(default_factory=list)
    allowed_fields: list[str] | None = None
    # Opt out of the linter's "short acronym needs a guard" rule. Requires `notes`
    # justifying it, so the exception is reviewable rather than invisible.
    guard_exempt: bool = False
    notes: str | None = None


class ContextGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    terms: list[str]


class DomainTaxonomy(BaseModel):
    """A complete, versioned domain definition."""

    model_config = ConfigDict(extra="forbid")

    taxonomy_id: str
    taxonomy_version: str
    description: str | None = None
    terms: list[Term]
    context_groups: dict[str, ContextGroup] = Field(default_factory=dict)
    # Suppress any match contained inside one of these phrases, everywhere.
    blocked_ngrams: list[str] = Field(default_factory=list)
    # Phrases indicating something is being bought rather than described.
    acquire_lexicon: list[str] = Field(default_factory=list)
    acquire_window_tokens: int = Field(default=25, ge=1, le=200)

    @model_validator(mode="after")
    def references_resolve(self) -> DomainTaxonomy:
        """Fail loudly on a rule naming a context group that does not exist.

        An orphaned rule silently stops gating, which is how a taxonomy quietly loses
        its precision months after anyone last looked at it.
        """
        seen: set[str] = set()
        for term in self.terms:
            if term.id in seen:
                raise ValueError(f"duplicate term id {term.id!r}")
            seen.add(term.id)
            for rule in (term.requires_context, term.blocked_context):
                if rule is not None and rule.group not in self.context_groups:
                    raise ValueError(f"term {term.id!r} references unknown context group {rule.group!r}")
        return self

    @classmethod
    def from_json(cls, path: Path) -> DomainTaxonomy:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def terms_by_tier(self, *tiers: TermTier) -> list[Term]:
        wanted = set(tiers)
        return [term for term in self.terms if term.tier in wanted]

    def lint(self) -> list[str]:
        """Report authoring mistakes that otherwise fail silently.

        Every problem below produces a term that matches nothing, or matches far more
        than intended, with no error at load time. Across a few hundred hand-written
        terms these are the dominant source of taxonomy rot.
        """
        problems: list[str] = []
        by_text: dict[str, str] = {}
        for term in self.terms:
            text = term.text
            if any(character in text for character in "()[]"):
                problems.append(f"{term.id}: '(' or '[' is literal, not alternation -- {text!r}")
            if "/" in text:
                problems.append(f"{term.id}: '/' is literal, not alternation; split into separate terms -- {text!r}")
            if len(text) <= 4 and text.isupper() and not term.case_sensitive:
                problems.append(f"{term.id}: short acronym without case_sensitive -- {text!r}")
            if (
                len(text) <= 4
                and text.isupper()
                and term.requires_context is None
                and term.tier in {TermTier.EXCLUSIVE, TermTier.STRONG}
                and not term.blocked_ngrams
                and not term.guard_exempt
            ):
                problems.append(f"{term.id}: short acronym at tier {term.tier} with no context or blocklist guard")
            if term.guard_exempt and not term.notes:
                problems.append(f"{term.id}: guard_exempt requires notes justifying the exemption")
            rule = term.requires_context
            if rule is not None:
                group = self.context_groups.get(rule.group)
                if group and any(g.casefold() == text.casefold() for g in group.terms):
                    problems.append(
                        f"{term.id}: its own text is a member of required group {rule.group!r}; "
                        "the guard would be satisfied by the match itself and do nothing"
                    )
            # Key on the NORMALIZED form: "anti phishing" and "anti-phishing" compile to
            # the same pattern, so casefold alone let a term be counted twice in the
            # noisy-OR -- the exact double-counting best-match scoring exists to prevent.
            key = _normalized_key(text)
            if key in by_text:
                problems.append(f"{term.id}: duplicates {by_text[key]!r} after normalization")
            by_text[key] = term.id

            rule = term.blocked_context
            if rule is not None:
                group = self.context_groups.get(rule.group)
                if group and any(g.casefold() == text.casefold() for g in group.terms):
                    problems.append(
                        f"{term.id}: its own text is in blocked group {rule.group!r}; the "
                        "term would suppress itself whenever it appears twice in a field"
                    )

        # The universal rule. A phrase that cannot match its own literal text is a silent
        # zero-recall rule -- and it subsumes the punctuation checks above, because any
        # character the normalizer deletes without the matcher accepting it as a separator
        # opens a token gap nothing can fill. Applied to EVERY phrase list, not just
        # `terms`: the other three hold 200+ phrases and were never linted at all.
        problems.extend(self._unmatchable_phrases())
        return problems

    def _unmatchable_phrases(self) -> list[str]:
        from opportunity_engine.matching.spans import find_spans, prepare

        problems: list[str] = []

        def check(label: str, phrase: str, case_sensitive: bool = False) -> None:
            if phrase and not find_spans(prepare(phrase), phrase, case_sensitive=case_sensitive):
                problems.append(f"{label}: {phrase!r} cannot match its own text")

        for term in self.terms:
            check(f"term {term.id}", term.text, term.case_sensitive)
            for ngram in term.blocked_ngrams:
                check(f"term {term.id} blocked_ngram", ngram)
        for name, group in self.context_groups.items():
            for phrase in group.terms:
                check(f"context_group {name}", phrase)
        for phrase in self.blocked_ngrams:
            check("blocked_ngram", phrase)
        for phrase in self.acquire_lexicon:
            check("acquire_lexicon", phrase)
        return problems


def _normalized_key(text: str) -> str:
    from opportunity_engine.matching.normalization import normalize_for_matching

    return normalize_for_matching(text)


def load_taxonomy(path: str | Path, *, strict: bool = False) -> DomainTaxonomy:
    """Load a taxonomy. With ``strict``, refuse one that fails its own linter.

    ``lint()`` previously ran only in tests, which is why four phrases that could not
    match their own text shipped unnoticed.
    """
    taxonomy = DomainTaxonomy.from_json(Path(path))
    if strict:
        problems = taxonomy.lint()
        if problems:
            joined = "; ".join(problems)
            raise ValueError(f"taxonomy failed lint: {joined}")
    return taxonomy


def dump_taxonomy(taxonomy: DomainTaxonomy) -> str:
    return json.dumps(taxonomy.model_dump(mode="json", exclude_none=True), indent=2, sort_keys=True)


TierLiteral = Literal["EXCLUSIVE", "STRONG", "CORROBORATING", "PROGRAM", "WEAK", "VENDOR"]
