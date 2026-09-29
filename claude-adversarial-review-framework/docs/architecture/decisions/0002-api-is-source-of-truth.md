# ADR-0002 — The API is the source of truth; only user-derived state persists

## Context

The project's `AGENTS.md` mandates preserving the raw payload for every external record
and names PostgreSQL. Both were written for the original ingest-and-index connector.
Under the query-on-demand design (ADR-0001) the product re-fetches on every run.

## Decision

Store only what the user creates: their saved filter, which notice ids have been seen,
and which they marked PURSUING or DISMISSED. One SQLite file, `dashboard.db`.

## Evidence

Import probe confirms the product path never loads SQLAlchemy; `wb_connector/db.py` and
`opportunity_engine/persistence/` are reachable only from the POC connector. Measured
SQLite write window is 28 ms against a 5,000 ms busy timeout, so a single file serves two
processes without contention.

## Alternatives considered

PostgreSQL for the product path (rejected: disproportionate at one user and a few hundred
rows; no demonstrated constraint). Raw-payload preservation for every fetched notice
(rejected: the API is re-queryable, so the payload is recoverable).

## Consequences

This diverges from `AGENTS.md` and the divergence is currently undocumented there.
Raw preservation should apply at the *engagement* boundary instead — and today it does
not: `notice_state.raw_payload` exists and is never written, which is the direct cause of
finding B1 (a pursued bid's reminder depends on it still matching the live filter).
Neither store has a migration path.

## Confidence

CONFIRMED for the separation; the `AGENTS.md` divergence is an unrecorded decision.

## Date discovered

2026-09-28.
