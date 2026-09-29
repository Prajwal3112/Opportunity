# Architecture Audit

## Audit Status

Performed 2026-09-28. Five specialist investigations (failure, security, data/state,
domain integrity, dependency) plus one hostile review of the audit itself. Baseline at
time of audit: `69 passed, 2 skipped, 1 deselected`, ruff clean.

**No production code was modified during the assessment.** Findings below are evidence
and proposals; implementation awaits explicit approval.

Severities were revised by the hostile review: one finding was **rejected**, one
**upgraded**, two **downgraded**, and two previously-missed findings were **added**.

### The severity axis used

This product's asset is a 15-day median bidding window on a ~15-notice/month channel.
The question that orders severity is therefore *"can a biddable opportunity be silently
lost forever?"* — not confidentiality, uptime or throughput. Severities are calibrated
on that axis and deliberately **not** inflated for a single-user localhost tool.

### Standing qualifier

Project memory records the Gate 2 verdict: *"no enterprise cyber product opportunities in
the World Bank channel — the volume isn't there."* The audit found two defects
(**D2**, **D6**) that mean that verdict was measured through a 14-term retriever with a
filter discarding 24.4% of bid-eligible notices. **The volume verdict should be treated
as unsettled until those two are fixed and the measurement repeated.**

---

## Executive Findings

1. The digest can permanently and silently lose a biddable opportunity, on the ordinary
   happy path. Three independent mechanisms; one shared root cause.
2. The single most valuable alert the product could send — *the tender you are bidding
   has been cancelled* — is currently indistinguishable from a quiet market.
3. Marking a notice PURSUING does not guarantee a deadline reminder.
4. A quarter of the bid-eligible population is unconditionally invisible because a
   missing deadline is treated as a past deadline.
5. Recall is capped by a hard-coded 14-term seed list, not by the 89-term taxonomy, so
   replacing the profile JSON does not replace the domain.
6. Security posture is better than expected: **no XSS**, and the SSRF allowlist survived
   falsification. The two security findings that stand are correctness issues wearing
   security clothing.

## Production Blockers

**P1.** F-A2 — delivery failure burns the notice and reports success.
**P2.** F-A1 + F-A3 — novelty is consumed before, and independently of, delivery.
**P3.** B2 — no notice-state-change detection, so cancellation reads as silence.
**P4.** B1 — a pursued bid's reminder depends on it still matching the live filter.

None is a blocker for *running* the tool. All four are blockers for *trusting* it.

---

## Critical Findings

### F-A2 — Delivery failure burns the notice and exits 0

- **Finding.** The `seen` ledger is committed ~30 lines before the only delivery attempt.
  `send_email` returns a string rather than raising, and `run()` returns 0 regardless.
- **Evidence.** Reproduced independently twice. SMTP on a closed port: digest renders the
  body, prints `not sent: ConnectionRefusedError`, commits `seen`, exits **0**. Next run:
  `nothing new`. Hostile review found the likelier variant — with `DIGEST_*` unset,
  `send_email` returns `"not sent: set DIGEST_SMTP_HOST and DIGEST_TO to enable email"`
  and the same loss occurs. A user who schedules `--send` before configuring SMTP loses
  every notice from then on.
- **Location.** `src/wb_connector/digest.py:129` (search commits `seen`) → `:155-161`
  (render, print, send) → `:161` (`return 0`); `send_email` at `:89-119`.
- **Failure mechanism.** The only durable artifact of a run is written before the only
  delivery attempt, and failure is demoted to a printed string that Task Scheduler
  discards.
- **Impact.** Silent, permanent, unbounded loss of the product's single output. Task
  Scheduler shows green. The failure signature is identical to correct operation in a
  design where silence is the intended steady state.
- **Confidence.** CONFIRMED (reproduced).
- **Severity.** CRITICAL — upheld by hostile review as the one finding where CRITICAL is
  unambiguously earned.
- **Preconditions.** SMTP unreachable, unconfigured, or credentials expired; or the
  process dying between search and send. None exotic.
- **Recommended mitigation.** Commit delivery only after a confirmed send. `run()` must
  return non-zero when `--send` was requested and mail did not go out. Note the design
  *intent* of `send_email` (print even when mail fails) is sound and should be preserved —
  the fix is the commit ordering, not raising.
- **Validation.** Two consecutive runs with `DIGEST_SMTP_PORT=1`; assert run 1 exits
  non-zero and run 2 re-reports the same notices.
- **Residual risk.** A crash between a successful SMTP handoff and the commit re-sends one
  digest. Duplicate mail is strictly better than silence.

---

## High Severity Findings

### F-A1 — `mark_seen` records what was *fetched*, not what was *delivered*

- **Finding.** Every fetched id is committed to `seen` **before** the four rejection
  stages run. Anything fetched-but-filtered is permanently "seen" and can never be
  reported later.
- **Evidence.** Reproduced: a notice published before its `submission_deadline_date` is
  populated is burned on day 1; on day 2, live and open and matching, the digest prints
  `nothing new. 1 still open`. Hostile review confirmed the more important property —
  **this needs no API-side mutation at all**: adding then removing a `hide` term, or
  editing the taxonomy, does it. There is no `DELETE FROM seen` anywhere in the codebase.
- **Location.** `src/wb_connector/dashboard.py:185` (commit) versus `:195` (only_biddable),
  `:197` (firms_only), `:203` (hide), `:212` (engine NO_MATCH).
- **Failure mechanism.** `seen` answers "what did a process fetch", while the digest asks
  "what has the user been told". The difference is discarded permanently.
- **Impact.** Every filter loosening is retroactively dead — the user changes a setting to
  see more and sees nothing, with no indication why. That makes the taxonomy tuning loop
  one-way, which is the mechanism by which the domain config is supposed to be corrected.
- **Confidence.** CONFIRMED (reproduced independently).
- **Severity.** HIGH — **downgraded from CRITICAL** by hostile review: no notice data is
  destroyed (every run re-fetches), and the dashboard still lists the row. The loss is
  confined to the NEW badge and to digest delivery.
- **Correction applied.** The `only_biddable`/`firms_only` triggers were **removed** from
  this finding: `dashboard_template.py:102,104` render both checkboxes `disabled`, and no
  endpoint can change them. They are reachable only by hand-editing SQLite.
- **Recommended mitigation.** Separate a *delivered* ledger from the *fetched* ledger;
  novelty becomes `id in rows AND id not in delivered`. Bound the candidate set by age
  rather than by burning on fetch.
- **Validation.** Fixture where a notice gains a deadline between two runs; assert it is
  reported on the second.
- **Residual risk.** A permanently-unreportable notice stays a candidate forever unless
  age-capped.

### F-A3 — Any page load consumes the digest's novelty

- **Finding.** `GET /` calls `search()`, which marks seen. `/add`, `/remove` and `/mark`
  all 303-redirect to `/`, re-running it. `is_new` is per-request and never persisted.
- **Evidence.** Reproduced: load #1 shows 40 rows / 40 NEW; a refresh or any click shows
  0 NEW; the evening digest says `nothing new`.
- **Location.** `src/wb_connector/dashboard.py:286-295`, `:305`, `:314`, `:320`, `:227`.
- **Failure mechanism.** `search()`'s side effect is shared by an interactive caller that
  fires on every render (including redirect targets) and a scheduled caller that depends
  on it. Browser prefetch on a known localhost port does it with no human present.
- **Impact.** Hostile review named the sharpest consequence: **clicking "Not interested"
  on one notice destroys the NEW badge on every other notice.** The act of triaging the
  list destroys the list's own triage state. The cheaper surface silently disarms the
  durable one.
- **Confidence.** CONFIRMED. **Severity.** HIGH.
- **Recommended mitigation.** Falls out of F-A1's fix, plus render "new" from
  `first_seen_at` age rather than from the current request's `fresh` set.
- **Validation.** Load `/`, POST `/mark`, follow the redirect, assert untouched rows still
  render NEW.

### F-A5 — Silent truncation at 1000 records, and the window is not date-ordered

- **Finding.** `TOO_BROAD = 5000` but each term is fetched with a single `rows=1000`
  request. A term whose total falls between 1001 and 5000 passes the guard and silently
  contributes only the first 1000 records, while the totals strip displays the full count
  and implies completeness.
- **Evidence.** **Upgraded by hostile review, and I verified the basis.** One real
  `os=0&rows=1000` page spans notice dates **2006–2025 in no date order**, and the
  dashboard sends no `sort` parameter. Real measured total: `security monitoring` = 3,633
  → 27.5% coverage, no warning. Default term `security information and event management`
  is already at 811 against the 1000 cap, in a corpus growing ~45 notices/day.
- **Location.** `src/wb_connector/dashboard.py:59`, `:154`, `:164-169`.
- **Failure mechanism.** Truncating an unordered window discards an arbitrary slice.
  Currently-open notices are a small minority of any term's multi-year history, so
  truncation preferentially discards exactly what the product exists to find.
- **Impact.** Unbounded, invisible recall loss that grows as the corpus grows.
- **Confidence.** CONFIRMED for truncation and for the unordered window.
  HIGH-CONFIDENCE that the ordering generalises to `qterm`-filtered queries — the
  evidence is a page from the same endpoint without `qterm`.
- **Severity.** HIGH — **upgraded from MEDIUM.**
- **Recommended mitigation.** Set `TOO_BROAD` to the page size actually requested, or
  paginate with the existing `paginate_offset` when `total > rows`.
- **Validation.** One `rows=1000` request per >1000-total term; compare its date histogram
  to the term's full histogram via `paginate_offset`.

### B1 — Marking a notice PURSUING does not guarantee a deadline reminder

- **Finding.** `closing_soon` is computed only from freshly-fetched rows, and `/mark`
  always passes `raw=None`, so the `raw_payload` column that exists for exactly this
  purpose is never written.
- **Evidence.** Reproduced on a PURSUING notice closing in 4 days: reminders stop
  silently when the user edits `find`, adds an innocuous `hide` term, the notice drops out
  of the truncated window (F-A5), or a taxonomy edit makes it NO_MATCH. Each case prints
  `nothing new. 0 still open`. `notice_state.raw_payload` verified NULL.
- **Location.** `src/wb_connector/dashboard.py:319`, `:121-127`; `src/wb_connector/digest.py:139-141`.
- **Impact.** The bid the user is actively working is the highest-value object in the
  product and the least durable thing in the store.
- **Confidence.** CONFIRMED. **Severity.** HIGH. **Found only by hostile review.**
- **Recommended mitigation.** Persist the raw record on `PURSUING`, and derive reminders
  from stored state rather than from the current filter result. Guard the upsert with
  `COALESCE` so a later NULL cannot erase a stored payload.
- **Validation.** Mark PURSUING, remove every matching `find` term, run the digest, assert
  the reminder still fires.

### B2 — A cancelled tender is delivered as "nothing new"

- **Finding.** There is no notice-state-change detection anywhere. `notice_state` records
  only what the *user* did, never what the *notice* did.
- **Evidence.** Reproduced: flip a pursued notice's `notice_status` to `Cancelled` →
  `is_applyable` drops it → it leaves `rows` → digest prints `nothing new. 0 still open`.
- **Location.** absence; `src/wb_connector/digest.py:137-141`.
- **Impact.** The single most important alert this product could send is
  indistinguishable from a quiet market. A user continues preparing a bid for a tender
  that no longer exists.
- **Confidence.** CONFIRMED. **Severity.** HIGH. **Found only by hostile review.**
- **Recommended mitigation.** For PURSUING notices, compare the fetched record against
  the stored payload and report status, deadline and description changes — including
  disappearance — as their own digest section.
- **Validation.** Fixture flipping status on a pursued notice; assert the digest reports it.

### D1 — `CandidateEngine._applyable` omits the individual-consultant clause

- **Finding.** The engine's applyability copy implements 4 of the model's 5 clauses.
- **Evidence.** Exhaustive truth table over 1,296 combinations: model vs dashboard
  **0/1296 disagreements**; model vs engine **60/1296**, every one on the missing
  `INDIVIDUAL_ONLY_METHODS` axis. On real records the engine calls 40 applyable where the
  model calls 33, and **7 of the engine's 40 are individual-consultant job adverts** —
  precisely the class the rule exists to exclude. Example: `OP00464170`, *"Recruitment of
  Monitoring and Evaluation Officer"*.
- **Location.** `src/opportunity_engine/matching/engine.py:293-303` (missing clause);
  canonical at `src/wb_connector/models.py:91-99`.
- **Failure mechanism.** `is_applyable=True` feeds `classify_tier`'s `high_allowed` gate
  and `CandidateEvaluation.is_applyable`, which is the primary sort key. Job adverts
  become HIGH-eligible and sort above genuine firm tenders.
- **Impact.** `Individual Consultant Selection` is 21.5% of the corpus. The dashboard is
  shielded only because it calls the *model*; any consumer of the *evaluation* is wrong.
- **Confidence.** CONFIRMED. **Severity.** HIGH.
- **Recommended mitigation.** One `as_of`-parameterised predicate in
  `opportunity_engine/domain/`, with the policy sets loaded from the profile JSON. All
  five call sites delegate to it. This also removes the reverse import at `engine.py:295`.
- **Validation.** Property test over the captures asserting model and engine agree for
  several `as_of` dates. No such cross-check exists today.

### D2 — A missing deadline is treated as a past deadline

- **Finding.** All four implementations hard-drop `submission_deadline_date is None`.
- **Evidence.** Verified independently on the captures: **1,063 of 4,361 bid-eligible
  notices (24.4%) have no published deadline** — Invitation for Bids 21.2%, REOI 26.3%,
  Prequalification 65.5%. Two of the five strongest DIRECT-class matches in the corpus sit
  in this bucket.
- **Location.** `src/wb_connector/models.py:97`; `engine.py:301`; `dashboard.py:195`;
  `sweep.py:151`.
- **Failure mechanism.** Absent conflated with expired. The model's own docstring records
  that the deadline is present on only ~74–79% of bid-eligible types — the code documents
  the quarter it then discards. A notice with no published deadline is not closed; its
  deadline is in the attached bid document.
- **Impact.** A quarter of the addressable population is unconditionally invisible, and
  the empty state misattributes it: *"none is currently accepting bids"* is a claim the
  code cannot support for a deadline-less notice.
- **Confidence.** CONFIRMED. **Severity.** HIGH.
- **Recommended mitigation.** Make "no deadline published" a third state: keep it, label
  it *deadline unknown — check the bid document*, sort it after dated notices, and never
  let it satisfy or fail a reminder window.
- **Residual risk.** Some deadline-less notices are stale; this trades a known 24.4%
  false-negative rate for a measurable false-positive rate. **Product decision required.**

---

## Medium Severity Findings

| # | Finding | Evidence | Confidence |
| --- | --- | --- | --- |
| D3 | `blocked_ngrams` are never applied to context groups, and groups match case-insensitively — so a phrase the author blocklisted can still satisfy a guard. `'monitoreo'` is 45% of all `CYBER_QUALIFIER` firings; `Soc` (already blocklisted as `soc trang`) satisfied it 19 times. A synthetic case reaches HIGH. | `engine.py:74-83` | CONFIRMED mechanism / POTENTIAL live rate |
| D4 | Four profile phrases cannot match their own text (`ISO 27001:2022`, `defect's liability period`, `supply, installation`, one qualifier) because `:` `,` `'` are deleted but are not separators. `lint()` covers only `terms` — the 236 phrases in the other three lists are never linted — and `lint()` never runs at load. | `taxonomy.py:121-161`, `spans.py:28` | CONFIRMED |
| D5 | Dead rules: `RelevanceTier.EXCLUDED` is unreachable (`vetoed=False` hard-coded); `scoring.py:169` never executes; the UPSTREAM half of `scoring.py:128` is unreachable; `require_applyable_for_high` is applied twice and the second application is a no-op. Established by tracing 50,535 combinations. | `scoring.py`, `engine.py:263` | CONFIRMED |
| D6 | The taxonomy filters but does not retrieve. 55 of 74 EXCLUSIVE/STRONG terms are never searched for (verified independently); three divergent seed lists exist. Replacing the profile JSON replaces the filter, not the domain. | `dashboard.py:48-53`, `sweep.py:42-54` | CONFIRMED |
| D7 | Timezone: `is_applyable` and the countdown both use `datetime.now(UTC).date()` for a UTC+05:30 user. The countdown is one day optimistic for 5.5 h/day, so a pursued tender's final reminder can fire the morning after it closed. `submission_deadline_time` is normalized and never used. | `models.py:99`, `dashboard.py:183,226` | CONFIRMED |
| F-DB | `DB_PATH = Path("dashboard.db")` is CWD-relative; a digest launched from elsewhere opens a different store, ignoring the user's saved terms and losing all `notice_state`. **Hostile review corrected the reproduction**: System32 is non-writable, so that case is a loud crash, not silent. The silent variant needs a *writable* wrong CWD. The dashboard has no `--db` flag at all, and `README.md:4` actively denies that the dashboard and digest exist. | `dashboard.py:42`, `digest.py:169` | CONFIRMED (severity downgraded HIGH→MEDIUM) |
| F-A4 | A *partial* outage reads as "nothing new" and exits 0; the guard only fires on a total blackout. **Hostile review's correction reduces this**: failed terms never enter `raw_by_id`, so nothing is marked seen — the signal is a one-run false negative and **self-healing**, not permanent loss. | `digest.py:148-152` | CONFIRMED |
| B3 | `sweep.py` crashes on its own failure-reporting path: `_say` takes one argument, and `:119`/`:236` call it with two. The entry point a user runs on a restricted network dies with `TypeError` while reporting why it failed, and `:119` discards the findings. **Introduced by this session's encoding fix.** | `sweep.py:98,119,236` | CONFIRMED |
| B4 | "Only what I can bid on" and "Firms only" are rendered as `disabled` checkboxes with no endpoint and no CLI flag. They are presented to the user as settings and cannot be changed except by hand-editing SQLite. | `dashboard_template.py:102,104` | CONFIRMED |
| S-CSRF | `/mark` and `/remove` mutate state over GET with no token, no Origin/`Sec-Fetch-Site` check and no `TrustedHostMiddleware`. Hostile review **downgraded the attacker limb to LOW** (worst case is locally recoverable) and identified the stronger un-investigated half: **browser speculative prefetch needs no attacker at all** to fire these links. | `dashboard.py:284,307-320` | CONFIRMED mechanism / POTENTIAL for prefetch |
| F-LABEL | This session widened `CandidateEvaluationRow`'s unique key to include `taxonomy_version` but did not widen `candidate_key`, so `persist_label` raises `MultipleResultsFound` after a taxonomy edit. No callers, no test. The new test added this session institutionalises the exact precondition that breaks it. | `engine.py:270`, `repository.py:80` | CONFIRMED |
| D-TZ2 | A user west of UTC loses up to 11 h of the final bidding day; east of UTC a notice shows up to ~13 h after its local deadline day ended. | `models.py:99` | CONFIRMED |

## Low Severity Findings

| # | Finding | Confidence |
| --- | --- | --- |
| S-MAILTO | `_esc` (HTML escaping) used in a `mailto:` URL, so `&` in `bid_reference_no` becomes a mailto header separator. **Re-classified by hostile review from security to correctness**: 161 of 19,681 real references (0.82%) contain URL-unsafe characters, and the evidenced impact is corrupted email subject lines. The malicious `&bcc=` variant requires a borrower agency to craft the field, and the victim would see their own prefilled BCC. Fix (`urllib.parse.quote`) still worth making — there is no URL-encoding helper anywhere in `src/`. | CONFIRMED |
| B6 | `/mark` validates nothing: any `state` string and any `notice_id` are stored. Inert today because `state` is never rendered; a loaded gun the day it is. | CONFIRMED |
| S-HOST | No `Host` validation, so DNS rebinding could give an attacker origin read access to the page. Low-value payload (public procurement data plus the user's commercial intent). | HIGH-CONFIDENCE |
| S-GIT | `dashboard.db` (28 KB, repo root) and `captures/sweep-*/` are not gitignored. Records which tenders the user is pursuing. No `.git` yet, so nothing has leaked. | CONFIRMED |
| DEP | `psycopg[binary]` is a required runtime dependency for a path the product never executes; the dashboard and digest are SQLite-only. | CONFIRMED |
| D10 | With `only_biddable` off, `days` goes negative and the header reads *"next closes in -373 days"*. `empty_state` attributes an empty result to "none accepting bids" even when the cause was the `hide` list or a `TOO_BROAD` term. | HIGH-CONFIDENCE |
| D11 | `.strip().casefold()` against a frozenset does not collapse *internal* whitespace. The API demonstrably emits `'Consultant Qualification  Selection'` with a double space. Shared gap, not a divergence. | CONFIRMED |
| D9 | `CandidateEvaluation.sort_key` ranks by tier then score; the dashboard sorts by deadline. A write-only contract contradicting shipped behaviour. Unused in production. | CONFIRMED |
| S-PORT | `assert_downloadable` raises bare `ValueError` on a malformed port, outside the connector's error taxonomy. Not an SSRF bypass — the request is never made. | CONFIRMED |

## Rejected Findings

| # | Claim | Verdict |
| --- | --- | --- |
| S-URL(a) | A crafted `external_id` makes the "Pursue" button submit DISMISS and target another notice. | **REJECTED as exploitable.** Verified independently: **16,380 of 16,380** real ids match `^OP\d+$`; **zero** contain `& # ? = " < > %`. The proof-of-concept used an input the source cannot emit. A real code smell with no reachable exploit. The URL-encoding fix is still worth making. |
| — | XSS via notice HTML. | **REJECTED.** All 18 interpolation sites escaped; mXSS payloads reduced to inert text. `_esc` is sufficient for the double-quoted attribute and text contexts used. |
| — | SSRF bypass in the download path. | **REJECTED.** Falsification attempted against 11 payload classes; all denied. The control is `urlunparse` rebuilding from the parsed hostname. Path is also dormant — no production caller. |
| — | First-run flood of hundreds of notices. | **REJECTED.** The rejection stages cut 340 fetched → 40 shown. The first-run risk is *silence* (F-A1 × F-A3), not flood. |
| — | `render_rows` raises on an all-None notice, burning novelty. | **REJECTED.** Verified: renders "(untitled notice)" and "no deadline published". |
| — | Concurrent SQLite writers lose data. | **REJECTED.** Measured: 28 ms write window against a 5,000 ms busy timeout, no connection held across network I/O, and contention fails *before* the commit — it fails safe. |

## Failure Chains

1. **F-A1 × F-A3 → first-run silence.** Reproduced: 40 open + 300 filtered records; the
   page load burns all 340, so the first digest ever sent says *"nothing new. 40 still
   open."* The natural install order — install, open the page, then schedule — guarantees
   the entire standing backlog is never emailed, at the moment the product must prove
   itself.
2. **F-A1 × operator mistake.** `hide` is an unbounded substring test with no word
   boundary and no minimum length. `hide=["a"]` → 0 rows shown, 25 ids permanently
   burned. One keystroke, irreversible, silent.
3. **F-A1 × F-A5 × p10=0.** 10% of notices publish with the deadline already that day.
   For those, a page load on the same morning loses the notice permanently.
4. **F-A2 × F-DB — these partially cancel, which constrains fix order.** While F-DB
   stands, the digest runs on a visibly-wrong database. Fixing F-DB *alone* converts a
   visibly-wrong digest into a confidently-silent one. **F-A1 and F-A2 must be fixed
   before or together with F-DB.**
5. **F-A4 does *not* compound with F-A1** — the one pair safer than it looks.

## Testing Gaps

- **The entire product path is untested.** `tests/test_contract.py:353` heads a section
  *"Digest: silence is the default, and an unreachable service is not 'nothing new'"* and
  the tests below it exercise only `render_text`/`subject_line` against fake objects.
  Nothing tests `run()`, `send_email`, `mark_seen`, `search()`, or the seen ledger.
- No cross-check test asserts the applyability implementations agree (D1).
- No test calls `persist_label` (F-LABEL).
- `lint()` is asserted to return `[]`, which is why the four self-non-matching phrases
  went unnoticed (D4) — the green suite is the reason nobody looked.
- The shipped `dashboard.db` has `filters` populated but `seen` and `notice_state`
  **both empty**, which means the novelty machinery has never run against real data.

## Architectural Debt

1. `search()` couples discovery, novelty accounting and persistence, and is shared by two
   callers with incompatible needs. Root cause of P1–P2.
2. Applyability duplicated in five places; already diverged in two directions
   (engine omits a clause; README documents the weaker version).
3. Retrieval is hard-coded while filtering is configuration, so the "swappable domain"
   property does not hold.
4. No migration path for either store. `seen` uses a positional INSERT, so adding a
   column fails loudly exactly where novelty is recorded.

## Unknowns / Missing Evidence

Carried in `PROJECT_ARCHITECTURE.md` → **Known Unknowns**. The two that gate real
decisions: whether the World Bank mutates records in place, and whether a notice with no
published deadline should be shown (a product decision governing 24.4% of the population).

## Remediation Roadmap

Ordered by consequence, respecting the F-A1/F-A2-before-F-DB constraint.

**Status: all five stages applied and verified on 2026-09-28**, on the instruction "fix all
of these, start with F1 F2 F3". Each stage below records how it was verified; the framework's
no-code-during-assessment rule governed the assessment, which had already concluded. Suite
at time of writing: 75 passed, 2 skipped, ruff clean.

**Stage 1 — stop losing opportunities.** F-A2 + F-A1 + F-A3 together. Introduce a
*delivered* ledger distinct from the *fetched* ledger; commit it only after a confirmed
send; return non-zero when `--send` was requested and mail did not go out; derive the NEW
badge from `first_seen_at` age. Hostile review named three holes the naive version leaves:
delivery ordering, badge derivation, and what `send=False` means — all three must be
decided explicitly, not inherited.

*Applied.* `digest.py` writes a `delivered` ledger distinct from `seen`, commits it only
after `send_email()` returns success, and exits non-zero when `--send` was asked and mail
did not leave. `search(record_seen=False)` keeps the digest out of the fetch ledger.
*Verified by reproduction:* day-2 re-report exits 3; a forced send failure leaves
`delivered` empty, exits 1, and re-reports the same notice next run; the NEW badge derived
from `first_seen_at` survived three consecutive loads (`[True, True, True]`) where the old
derivation cleared it on the second.

**Stage 2 — make the working set durable.** B1 + B2. Persist the raw record on PURSUING;
derive reminders from stored state; detect and report status/deadline/disappearance
changes on pursued notices.

*Applied.* `notice_state.raw_payload` persists the record on PURSUING, written with
`COALESCE(excluded.raw_payload, notice_state.raw_payload)` so a later state change cannot
null it. `changes_on_pursued()` reports status, deadline and disappearance.

**Stage 3 — stop hiding a quarter of the market.** D2 (needs a product decision) and
F-A5 (`TOO_BROAD` → the page size actually requested, or paginate).

*Applied.* D2 is now owned by `opportunity_engine/biddability.py`, a three-state verdict
(BIDDABLE / DEADLINE_UNKNOWN / CLOSED) replacing the boolean. **Measured effect: 33 notices
open under the old rule, 933 under the new one — 96% of the open set had been hidden.**
`TOO_BROAD = PAGE_ROWS`, so the warning now fires at the size actually requested instead of
a stale constant.

**Stage 4 — one owner for the rules.** D1's shared `as_of` predicate, with policy from
the profile; then D6 (derive the seed from the taxonomy, move `TAXONOMY_PATH` into
settings, delete the private seed copies) which removes the reverse import.

*Applied.* `biddability.assess()` is the single owner of "can a firm still bid on this?",
replacing five divergent copies; it sits at the top of `opportunity_engine` rather than in
`domain/` to avoid the circular import through `domain/__init__` → `adapters` →
`wb_connector.models`. `default_find()` derives the seed from the taxonomy (24 EXCLUSIVE-
then-STRONG terms), and `TAXONOMY_PATH` comes from the environment.

**Stage 5 — small and self-contained.** B3 (`_say` arity — a defect introduced this
session), B4 (wire or remove the dead checkboxes), F-LABEL (widen `candidate_key`),
D4 (one universal *phrase must match itself* lint rule, run at load), URL-encoding helper,
`.gitignore`, `/mark` input validation, `TrustedHostMiddleware`.

*Applied.* B3 arity fixed; the two switches are real POST forms (zero `disabled`
attributes in the rendered page); `candidate_key` widened with `taxonomy_version` to match
the DB unique key; D4's universal *phrase must match itself* rule covers terms,
context_groups, blocked_ngrams and acquire_lexicon, and **runs at load** — both
`load_taxonomy` call sites pass `strict=True`, so the search path refuses a taxonomy that
fails its own lint (verified: an injected `data(loss)prevention` term stops `search()` with
a ValueError and degrades the seed to the safe fallback). `quote_url` URL-encodes where the
old code HTML-escaped, which mattered for the 0.82% of real references containing `&` or
`#`. TrustedHostMiddleware bound to loopback; `/mark` and `/toggle` are POST with an enum.

**Explicitly not recommended.** No queue, broker, Redis, Postgres migration for the
product path, WAL, horizontal scaling, container orchestration or rewrite. No demonstrated
constraint justifies any of them at one user and ~15 notices per month.
