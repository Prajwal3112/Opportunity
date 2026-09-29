# ADR-0001 — Query the API on demand instead of mirroring the corpus

## Context

The World Bank procurement endpoint holds ~417,900 notices. An earlier design proposed
ingesting and indexing the corpus locally. The API exposes no date-filter parameter
(probed: `noticedate`, `fromdate`, `startdate`, `submission_deadline_date_from` — all
ignored), so a mirror has no delta and must be fully re-crawled to stay fresh.

## Decision

Query on demand. One HTTP request per filter term at `rows=1000`; union and de-duplicate
in memory; persist nothing about a notice.

## Evidence

Each product term's entire population fits in one request (measured live: `SIEM` 213,
`DLP` 276, `cybersecurity` 476, `WAF` 44). A 14-term filter is 14 requests. A mirror is
~500 requests with no delta, and up to 24 hours stale. Query-on-demand is therefore both
cheaper and fresher — there is no trade-off to weigh.

## Alternatives considered

Full mirror with nightly re-crawl (rejected: more requests, staler data, and a migration
burden). Server-side `qterm` as the sole recall mechanism (rejected: `qterm` is
AND-across-tokens, verified by scrambling word order and by a nonsense-word probe, so
precision must be local).

## Consequences

Novelty cannot be derived from the API and must be remembered locally — this is the
origin of the `seen` ledger and, transitively, of findings F-A1/F-A2/F-A3. Phrase search
and arbitrary local history are lost; neither is needed at this volume. **A term whose
total exceeds the page size is silently truncated (ARCHITECTURE_AUDIT → F-A5).**

## Confidence

CONFIRMED. Verified live 2026-09-08.

## Date discovered

2026-09-28.
