# ADR-0003 — Relevance scores, tiers and evidence chips are never shown to the user

## Context

An earlier prototype led with match confidence, evidence chips and a
scanned/matched/flagged funnel. The user — a business-development person — rejected it:
"this is not at all interesting for me."

## Decision

The engine runs as an invisible noise filter. The page shows only
`tier != NO_MATCH`. No score, no tier badge, no confidence bar, no evidence chips. A row
shows what is being bought, when it closes, one quoted reason, and how to respond.

## Evidence

A score exists to triage volume, and there is no volume: ~13 open notices for the whole
cyber term set, which is short enough to read end to end. Ranking matters; scoring has no
work to do. The engine's real value is unglamorous and invisible — it keeps *Siem Reap*,
*Defect Liability Period* and "prices soar" out of the list.

## Alternatives considered

Show the tier as a badge (rejected: the user does not act on classifier accuracy).
Threshold sliders (rejected: nothing to tune at this volume).

## Consequences

`direct_high_threshold`, `embedded_high_threshold`, `embedded_high_min_exclusive` and
`require_applyable_for_high` become unobservable in the shipped product — see audit D5,
where `require_applyable_for_high` is applied twice and the second application is a no-op.
Whether the tier is a display signal, a queue order, or purely diagnostic is **unresolved**
and should be decided before that configuration is trusted.

## Confidence

CONFIRMED as the shipped behaviour. The rationale is the user's own stated reaction.

## Date discovered

2026-09-28.
