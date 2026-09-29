# ADR-0004 — The domain lives in versioned JSON, not in code (ASPIRATIONAL)

## Context

The owner requires the domain to stay swappable configuration so another domain can
replace cybersecurity without a rewrite. `opportunity_engine/domain/taxonomy.py:1-4`
states: "Swapping the JSON swaps the domain."

## Decision

Domain knowledge lives in `profiles/cybersecurity.json` — 89 terms with tiers, context
groups, blocked n-grams and an acquisition lexicon. The engine is domain-agnostic.

## Evidence — and why this decision does not currently hold

It holds for **filtering** and fails for **retrieval**. Verified independently: **55 of
the 74 EXCLUSIVE/STRONG terms are never searched for**, because recall is bounded by a
hard-coded 14-term `DEFAULT_FIND` in `dashboard.py:48-53`. Three divergent seed lists
exist (`dashboard`, `sweep`, `recon`). `TAXONOMY_PATH` is a module constant with no
settings override. `APPLYABLE_NOTICE_TYPES` and `INDIVIDUAL_ONLY_METHODS` are frozensets
in `wb_connector.models`, so the *biddability* half of the domain is not swappable at all
— and `engine.py:295` imports them across the layer boundary.

So replacing the JSON replaces the noise filter while leaving a cybersecurity search in
place: a new domain would get a coherent-looking but wrong system.

## Alternatives considered

Hard-coding cybersecurity throughout (rejected by the owner). A code-level plugin per
domain (not evaluated; the JSON schema is expressive enough for the filtering half).

## Consequences

The stated property is not delivered. It also means the Gate 2 volume verdict recorded in
project memory measured the recall of 14 phrases, not of the 89-term taxonomy — so that
verdict should be treated as unsettled. Remediation: derive the seed from
`taxonomy.terms_by_tier(EXCLUSIVE, STRONG)`, move `TAXONOMY_PATH` into settings, and move
the biddability policy into the profile.

## Confidence

CONFIRMED that the decision is stated; CONFIRMED that it is not met for retrieval.

## Date discovered

2026-09-28.
