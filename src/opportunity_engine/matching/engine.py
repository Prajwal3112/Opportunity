"""Pure deterministic candidate engine; it makes no network or AI calls.

Pipeline, per field, per term:

    occurrences -> blocked-span containment -> required context -> blocked context
                -> field scoping -> surviving evidence

then one contribution per term at its strongest field, combined by noisy-OR, with the
tier decided by evidence kind and acquisition intent rather than by the score.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Protocol

from opportunity_engine.biddability import assess
from opportunity_engine.domain.models import (
    CandidateEvaluation,
    CandidateInput,
    CapabilityProfile,
    MatchEvidence,
    RelevanceTier,
    ScoreComponents,
)
from opportunity_engine.domain.taxonomy import (
    ContextRule,
    DomainTaxonomy,
    OpportunityClass,
    Term,
    TermTier,
)

from .scoring import (
    EXCLUSIVE_TIERS,
    SCOPE_FIELDS,
    CandidateScoringConfig,
    classify_opportunity,
    classify_tier,
    confidence,
    term_strength,
)
from .spans import (
    Span,
    contains_any,
    find_any_spans,
    find_any_with_text,
    find_spans,
    is_suppressed,
    prepare,
    snippet,
    token_offsets,
    window_span,
    window_text,
)


class SemanticRanker(Protocol):
    """Future embeddings/vector implementation contract; intentionally unused."""

    def score(self, candidate: CandidateInput, profile: CapabilityProfile) -> float | None: ...


class CandidateEngine:
    def __init__(
        self,
        config: CandidateScoringConfig,
        taxonomy: DomainTaxonomy,
        semantic_ranker: SemanticRanker | None = None,
    ) -> None:
        self.config = config
        self.taxonomy = taxonomy
        self.semantic_ranker = semantic_ranker
        self._term_texts: tuple[str, ...] | None = None

    def _group_spans(self, group_name: str, text: str, cache: dict[str, list[tuple[Span, str]]]) -> list[tuple[Span, str]]:
        """Locate a context group's phrases once per field, not once per match.

        Scanning ~30 qualifier phrases for every candidate occurrence dominated runtime
        (35 ms/notice); locating them once and testing containment is the same answer.
        """
        if group_name not in cache:
            group = self.taxonomy.context_groups.get(group_name)
            cache[group_name] = find_any_with_text(text, group.terms) if group is not None else []
        return cache[group_name]

    def _context_satisfied(
        self,
        text: str,
        span: Span,
        rule: ContextRule,
        tokens: list[Span],
        cache: dict[str, list[tuple[Span, str]]],
    ) -> str | None:
        """Return the context phrase that satisfied the rule, or None."""
        candidates = self._group_spans(rule.group, text, cache)
        if not candidates:
            return None
        # In a short field a clause window is meaningless, so the whole field is used.
        if len(tokens) <= rule.whole_field_below_tokens:
            external = [p for f, p in candidates if not (f.start < span.end and f.end > span.start)]
            return external[0] if external else None
        window = window_span(text, span, rule.window_tokens, tokens)
        for found, phrase in candidates:
            # A match may not satisfy its own context requirement. When a term's text is
            # also a member of its required group, self-satisfaction silently turns the
            # guard into a no-op -- which is how "standard penetration testing" survived
            # a CYBER_QUALIFIER gate written specifically to stop it.
            if found.start < span.end and found.end > span.start:
                continue
            if found.start < window.end and found.end > window.start:
                return phrase
        return None

    def _all_term_texts(self) -> tuple[str, ...]:
        if self._term_texts is None:
            self._term_texts = tuple(term.text for term in self.taxonomy.terms)
        return self._term_texts

    def _evaluate_field(
        self, field: str, raw_text: str, global_blocked: list[str]
    ) -> tuple[list[MatchEvidence], list[MatchEvidence]]:
        text = prepare(raw_text)
        # Under a domain cap the overwhelming majority of notices contain no taxonomy
        # phrase at all, so one alternation pass decides whether any work is needed.
        if not contains_any(text, self._all_term_texts()):
            return [], []
        tokens = token_offsets(text)
        blocked_spans = find_any_spans(text, global_blocked)
        context_cache: dict[str, list[tuple[Span, str]]] = {}
        kept: list[MatchEvidence] = []
        suppressed: list[MatchEvidence] = []

        for term in self.taxonomy.terms:
            if term.allowed_fields is not None and field not in term.allowed_fields:
                continue
            term_blocked = blocked_spans + find_any_spans(text, term.blocked_ngrams)
            for span in find_spans(text, term.text, case_sensitive=term.case_sensitive):
                evidence = MatchEvidence(
                    term=term.text,
                    term_id=term.id,
                    source_field=field,
                    snippet=snippet(text, span),
                    match_type=term.category,
                    tier=term.tier.value,
                    category=term.category,
                    start_offset=span.start,
                    end_offset=span.end,
                )
                reason = self._suppression_reason(term, text, span, tokens, term_blocked, context_cache)
                if reason is not None:
                    suppressed.append(evidence.model_copy(update={"suppressed_by": reason}))
                    continue
                if term.requires_context is not None:
                    satisfied = self._context_satisfied(text, span, term.requires_context, tokens, context_cache)
                    evidence = evidence.model_copy(update={"context_satisfied_by": satisfied})
                kept.append(evidence)
        return kept, suppressed

    def _suppression_reason(
        self,
        term: Term,
        text: str,
        span: Span,
        tokens: list[Span],
        blocked: list[Span],
        cache: dict[str, list[tuple[Span, str]]],
    ) -> str | None:
        if is_suppressed(span, blocked):
            return "blocked_ngram"
        if (
            term.requires_context is not None
            and self._context_satisfied(text, span, term.requires_context, tokens, cache) is None
        ):
            return f"missing_context:{term.requires_context.group}"
        if term.blocked_context is not None:
            hit = self._context_satisfied(text, span, term.blocked_context, tokens, cache)
            if hit is not None:
                return f"blocked_context:{term.blocked_context.group}:{hit}"
        return None

    def _has_acquisition_intent(self, fields: dict[str, str], evidence: list[MatchEvidence]) -> bool:
        """Is something being bought here, or merely written about?

        Checked in a window around EXCLUSIVE-family evidence, not document-wide: almost
        any long document contains a procurement verb somewhere.
        """
        if not self.taxonomy.acquire_lexicon:
            return False
        for item in evidence:
            if TermTier(item.tier) not in EXCLUSIVE_TIERS:
                continue
            text = prepare(fields.get(item.source_field, ""))
            if not text:
                continue
            tokens = token_offsets(text)
            scope = window_text(text, Span(item.start_offset, item.end_offset),
                                self.taxonomy.acquire_window_tokens, tokens)
            if contains_any(scope, tuple(self.taxonomy.acquire_lexicon)):
                return True
        return False

    def evaluate(
        self,
        candidate: CandidateInput,
        profile: CapabilityProfile,
        *,
        source_raw_record_id: int | None = None,
        extra_fields: dict[str, str] | None = None,
    ) -> CandidateEvaluation:
        fields = {**candidate.text_fields(), **candidate.metadata_fields(), **(extra_fields or {})}
        kept: list[MatchEvidence] = []
        suppressed: list[MatchEvidence] = []
        for field, text in fields.items():
            field_kept, field_suppressed = self._evaluate_field(field, text, self.taxonomy.blocked_ngrams)
            kept.extend(field_kept)
            suppressed.extend(field_suppressed)

        # One contribution per term, at its strongest surviving field.
        fields_by_term: dict[str, list[str]] = {}
        tier_by_term: dict[str, TermTier] = {}
        for item in kept:
            fields_by_term.setdefault(item.term_id, []).append(item.source_field)
            tier_by_term[item.term_id] = TermTier(item.tier)
        strengths = [
            term_strength(tier_by_term[term_id], term_fields)
            for term_id, term_fields in fields_by_term.items()
        ]
        score = confidence(strengths, lambda_=self.config.lambda_)

        tiers_present = set(tier_by_term.values())
        exclusive_terms = {t for t, tier in tier_by_term.items() if tier in EXCLUSIVE_TIERS}
        exclusive_share = len(exclusive_terms) / len(tier_by_term) if tier_by_term else 0.0
        intent = self._has_acquisition_intent(fields, kept)
        opportunity_class = classify_opportunity(
            tiers_present=tiers_present,
            exclusive_share=exclusive_share,
            has_acquisition_intent=intent,
            config=self.config,
        )
        exclusive_in_scope = len(
            {i.term_id for i in kept if TermTier(i.tier) in EXCLUSIVE_TIERS and i.source_field in SCOPE_FIELDS}
        )
        distinct_corroborating = len(
            {t for t, tier in tier_by_term.items() if tier is TermTier.CORROBORATING}
        )
        components = ScoreComponents(
            confidence=score,
            exclusive_count=sum(1 for tier in tier_by_term.values() if tier is TermTier.EXCLUSIVE),
            strong_count=sum(1 for tier in tier_by_term.values() if tier in {TermTier.STRONG, TermTier.VENDOR}),
            corroborating_count=distinct_corroborating,
            category_count=len({i.category for i in kept}),
            suppressed_count=len(suppressed),
            acquisition_intent=intent,
            semantic=None,
        )
        is_applyable = candidate.notice_type is not None and self._applyable(candidate)
        tier = classify_tier(
            score=score,
            components=components,
            opportunity_class=opportunity_class,
            exclusive_in_scope_fields=exclusive_in_scope,
            distinct_corroborating=distinct_corroborating,
            has_program_evidence=TermTier.PROGRAM in tiers_present,
            vetoed=False,
            is_applyable=is_applyable,
            config=self.config,
        )
        if tier in {RelevanceTier.NO_MATCH, RelevanceTier.EXCLUDED}:
            opportunity_class = OpportunityClass.NOT_RELEVANT
        return CandidateEvaluation(
            # taxonomy_version is part of the key because the row's uniqueness includes
            # it. Omitting it here made two evaluations share a key, and the one consumer
            # that assumes uniqueness (persist_label) raised MultipleResultsFound.
            candidate_key=(
                f"{profile.profile_id}:{profile.version}:{self.taxonomy.taxonomy_version}:"
                f"{self.config.engine_version}:{candidate.source_external_id}"
            ),
            source_external_id=candidate.source_external_id,
            project_id=candidate.project_id,
            final_score=score,
            tier=tier,
            opportunity_class=opportunity_class.value,
            components=components,
            positive_matches=kept,
            negative_matches=[],
            suppressed_matches=suppressed,
            is_applyable=is_applyable,
            submission_deadline_date=candidate.submission_deadline_date,
            generated_at=datetime.now(UTC),
            scoring_engine_version=self.config.engine_version,
            profile_id=profile.profile_id,
            profile_version=profile.version,
            taxonomy_version=self.taxonomy.taxonomy_version,
            source_raw_record_id=source_raw_record_id,
        )

    @staticmethod
    def _applyable(candidate: CandidateInput, *, as_of: date | None = None) -> bool:
        """Delegates to the shared predicate.

        This used to be a private copy that omitted the individual-consultant clause, so
        job adverts were HIGH-eligible and sorted above genuine firm tenders. It also read
        the clock itself, which made an evaluation irreproducible.
        """
        return assess(
            notice_type=candidate.notice_type,
            notice_status=candidate.notice_status,
            procurement_method_name=candidate.procurement_method_name,
            submission_deadline_date=candidate.submission_deadline_date,
            as_of=as_of or datetime.now(UTC).date(),
        ).is_open
