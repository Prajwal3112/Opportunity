# Project Architecture

> Evidence-based model, produced by the adversarial review system on 2026-09-28.
> Every statement below is traced to code or to a reproduced behaviour. Claims that
> could not be established are listed under **Known Unknowns** rather than guessed.

## System Purpose

A single-user tool for a business-development person at a vendor selling a SIEM platform
(modules: SOAR, DLP, Anti-Phishing, URL/Content Filtering, WAF, UEBA). It surfaces World
Bank procurement notices the vendor can still bid on. The deliverable is an actionable
tender — buyer, deadline, contact — not a relevance score.

Two surfaces: an on-demand web page, and a scheduled email digest.

## Architecture Overview

A modular monolith with **three** runtime entry points and **two unrelated persistence
layers, only one of which is in the product path**.

```
                 ┌──────────────────────────────────────────────┐
  browser ──────▶│ dashboard.py   FastAPI, bound 127.0.0.1      │
                 │  GET /  → search() → render                  │
                 │  /add /remove /mark → 303 → GET /            │
                 └───────────────┬──────────────────────────────┘
                                 │  shares search()
                 ┌───────────────▼──────────────────────────────┐
  scheduler ────▶│ digest.py      one-shot, prints + emails     │
                 └───────────────┬──────────────────────────────┘
                                 │
        ┌────────────────────────▼─────────────────────────────┐
        │ search()  dashboard.py:139                            │
        │   1 HTTP request per filter term, 4 concurrent        │
        │   → union/dedup by id                                 │
        │   → mark_seen()            ◀── COMMITS HERE (:185)    │
        │   → normalize_record()                                │
        │   → strip_html()                                      │
        │   → is_applyable / firms_only / hide / engine gates   │
        │   → sort by days-to-deadline                          │
        └───────────┬───────────────────────────┬───────────────┘
                    │                           │
     ┌──────────────▼────────────┐   ┌──────────▼───────────────┐
     │ wb_connector.client       │   │ opportunity_engine        │
     │  httpx, retry, allowlist  │   │  spans → scoring → tier   │
     └──────────────┬────────────┘   │  driven by profile JSON   │
                    │                └───────────────────────────┘
     ┌──────────────▼────────────┐
     │ search.worldbank.org      │        ┌────────────────────┐
     │  /api/procnotices         │        │ dashboard.db       │
     └───────────────────────────┘        │ SQLite: filters,   │
                                          │ seen, notice_state │
                                          └────────────────────┘

  NOT IN THE PRODUCT PATH (POC only, verified by import probe):
     wb_connector/db.py  +  opportunity_engine/persistence/  →  Postgres
```

## Component Inventory

| Component | Path | Role | In product path |
| --- | --- | --- | --- |
| Dashboard | `src/wb_connector/dashboard.py` | FastAPI app, query-on-demand, SQLite state | Yes |
| Page markup | `src/wb_connector/dashboard_template.py` | Hand-built HTML, manual escaping | Yes |
| Digest | `src/wb_connector/digest.py` | Scheduled diff + email | Yes |
| HTTP client | `src/wb_connector/client.py` | Retry, backoff, envelope parsing, download allowlist | Yes |
| Connectors | `src/wb_connector/connectors.py` | Field normalization per endpoint | Yes |
| Domain models | `src/wb_connector/models.py` | Pydantic records, `is_applyable` | Yes |
| Matching | `src/opportunity_engine/matching/` | Span matching, scoring, tiering, HTML strip | Yes |
| Taxonomy schema | `src/opportunity_engine/domain/taxonomy.py` | Term/tier/context schema + linter | Yes |
| Domain data | `profiles/cybersecurity.json` | 89 terms, 110 blocked phrases, 6 context groups | Yes |
| Recon / sweep | `src/wb_connector/recon.py`, `sweep.py` | One-off API investigation tools | Operator-run |
| Capture | `src/wb_connector/capture.py` | Single-response capture for fixtures | Operator-run |
| Postgres layer | `src/wb_connector/db.py`, `src/opportunity_engine/persistence/` | POC persistence for the original connector | **No** |

## Dependency Graph

`dashboard` → `digest` (reverse: digest imports dashboard) → `client`, `connectors`,
`models`, `opportunity_engine.{domain,matching}` → `profiles/cybersecurity.json`.

One layering violation, CONFIRMED: `opportunity_engine/matching/engine.py:295` imports
`APPLYABLE_NOTICE_TYPES` from `wb_connector.models`. The generic engine reaches into the
World-Bank-specific connector for procurement vocabulary.

`sqlalchemy` is **not** imported by the product path (verified by import probe).

## Critical Data Flows

1. **Discovery.** Filter term → `GET /api/procnotices?qterm=<term>&rows=1000` → union.
   One request per term, 4 concurrent. `qterm` searches the full notice body and is
   AND-across-tokens, not phrase matching (both live-tested 2026-09-08).
2. **Normalization.** Raw record → `ProcurementNotice`; `notice_text` (99.9% HTML) →
   `strip_html` → matched and displayed.
3. **Relevance.** Span matching with character offsets → blocked-span containment →
   context windows → noisy-OR confidence → tier from evidence kind + acquisition intent.
   Used by the dashboard **only** as a boolean noise filter (`tier != NO_MATCH`).
4. **Novelty.** `seen` ⊕ fetched ids. There is no date parameter on the API, so novelty
   has no other possible definition.
5. **Delivery.** Digest prints, then optionally emails via SMTP from environment vars.

## Critical Control Flows

- `GET /` performs network I/O and **mutates** persistent state (`mark_seen`).
- `/add`, `/remove`, `/mark` all 303-redirect to `/`, re-running the full search.
- `digest.run` snapshots `seen`, calls `search()` (which writes `seen`), diffs, delivers.

## External Integrations

| Dependency | Trust | Notes |
| --- | --- | --- |
| `search.worldbank.org/api/procnotices` | Untrusted data source | Undocumented `qterm`; no date filter; numeric envelope fields arrive as strings |
| SMTP (optional) | Operator-configured | Credentials from environment only; `starttls` unconditional |
| `documents.worldbank.org` | Untrusted | Download path exists, hardened, and has **no production caller** |

## Storage / Source of Truth

**The API is the source of truth for notices.** Nothing about a notice is stored
durably; every run re-fetches. `dashboard.db` stores only user-derived state:

| Table | Contents | Load-bearing for |
| --- | --- | --- |
| `filters` | one row, `name='default'` | the user's search definition |
| `seen` | notice id + one batch timestamp | **the entire definition of "new"** |
| `notice_state` | id → PURSUING/DISMISSED | triage; `raw_payload` column exists and is never written |

## Authentication / Authorization

None. Bound to `127.0.0.1`. No cookies, no sessions, no middleware of any kind
(CONFIRMED: no CORS, TrustedHost, Origin or CSRF check anywhere in `src/`).

## Configuration

`pydantic-settings` (`config.py`) for the API client, from `.env`. The dashboard
deliberately bypasses it: `DB_PATH` and `TAXONOMY_PATH` are module constants, and
`DEFAULT_FIND` is a hard-coded 14-term seed list.

## Deployment Model

Run locally by the user. `pip install -e ".[web]"`, `python -m wb_connector.dashboard`,
and the digest via Windows Task Scheduler or cron. No container for the app, no service
definition, no health endpoint, no graceful shutdown. `docker-compose.yml` starts
Postgres only — for the POC layer the product does not use.

## Runtime Assumptions

1. The operator's network can reach `search.worldbank.org`. **Frequently false**: the
   development machine sits behind a TLS-intercepting FortiGate that blocks exactly that
   host. `docs/world-bank-api-validation.md` records this.
2. Every filter term's whole population fits in one `rows=1000` page. **False above 1000**.
3. The process runs with the repository as its working directory.
4. UTC dates are close enough to the user's local dates. The user is UTC+05:30.

## Failure Handling

Retries are bounded (3 attempts, exponential backoff, `Retry-After` honoured, capped at
60 s). Pagination is bounded by a local counter plus a repeated-page guard. A non-JSON
body raises a named `WorldBankPayloadError` that explicitly identifies a filter
interstitial. **Delivery failure is not handled**: `send_email` returns a string and
`run()` exits 0 regardless.

## Scaling Model

None required and none present. ~15 relevant notices per month, one user, ~40 requests
per full refresh, 3.5 ms/notice scoring. SQLite write window measured at 28 ms against a
5 s busy timeout. No scaling work is justified.

## Observability

Effectively none. No `logging` call anywhere in `src/`. The only signals are stdout
(discarded under Task Scheduler) and the process exit code, which is `0` on the most
important failure. `sync_runs`-style run history does not exist for the product path.

## Security Boundaries

- **Untrusted API data → browser.** Crossed by `strip_html` then manual `_esc`. No XSS:
  all 18 interpolation sites escaped (CONFIRMED by enumeration and probing).
- **Untrusted URL → outbound request.** `assert_downloadable` + per-hop revalidation.
  Falsification attempted and failed. Currently dormant — no production caller.
- **Local HTTP surface.** No auth, GET-based mutation, no Host validation.

## Architectural Risks

1. `search()` couples discovery, novelty accounting and persistence in one function with
   a side effect, and is shared by an interactive and a scheduled caller with different
   needs. This single design choice is the root of the three highest findings.
2. Applyability is expressed independently in five places (model, engine, dashboard,
   `sweep.py`, README prose) and has already diverged.
3. The taxonomy filters but does not retrieve: recall is capped by a 14-term seed while
   the taxonomy holds 74 EXCLUSIVE/STRONG terms, so the domain is not swappable by
   replacing the JSON.
4. Two persistence layers with no shared migration story; neither `CREATE TABLE IF NOT
   EXISTS` nor `create_all` can alter an existing table.

## Known Unknowns

| Unknown | Why it matters | Validation |
| --- | --- | --- |
| Does the World Bank mutate notice records in place? | Governs one trigger of the novelty loss | Capture the same `qterm` daily for two weeks; diff per id |
| Are notice ids stable over weeks? | If reissued, `seen` suppresses real opportunities | Two captures a week apart, diff id sets against `bid_reference_no` |
| Is the `rows=1000` window stable between runs? | Unstable membership compounds the novelty loss | Two identical requests a day apart, diff id sets |
| ~~Is the digest actually scheduled, and with what working directory?~~ **No longer gates anything.** | The wrong-database finding was fixed at the root: `DB_PATH` resolves from `__file__`, not the process working directory | Still worth knowing for operations, but no defect now depends on the answer |
| Is SMTP configured at all? | If unset, the delivery-loss defect has been firing on every run | Check env; inspect `seen` population against mail received |
| ~~Should a notice with no published deadline be shown?~~ **Resolved 2026-09-28: yes.** | Governed 24.4% of the bid-eligible population; measured at 96% of the open set | Product decision taken. Shown in their own *Closing date not published* band with a gloss telling the reader the date is in the bid document, never mixed into a dated band |

## Important Design Decisions

Recorded as ADRs under `docs/architecture/decisions/`:

- **ADR-0001** Query-on-demand instead of mirroring the corpus.
- **ADR-0002** The API is the source of truth; only user-derived state persists.
- **ADR-0003** Relevance scores are never shown to the user.
- **ADR-0004** The domain lives in versioned JSON, not in code (**now holds for retrieval
  too, as of 2026-09-28**: `default_find()` derives the seed terms from the taxonomy's
  EXCLUSIVE-then-STRONG tiers instead of a hard-coded list, so swapping the JSON swaps the
  domain for recall and not only for filtering).
