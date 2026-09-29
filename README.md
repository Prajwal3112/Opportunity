# World Bank connector POC

Finds World Bank procurement notices a vendor can still bid on, and delivers them
two ways: a local dashboard you drive with your own filter terms, and a scheduled
email digest that reports only what changed.

```powershell
pip install -e ".[web]"
python -m wb_connector.dashboard      # then open http://127.0.0.1:8000
python -m wb_connector.digest         # preview; --send to email
```

## Reaching it from a phone

```powershell
python -m wb_connector.dashboard --host 0.0.0.0
```

The banner then prints the LAN URL to open on the phone. Two things to know:

- **Windows blocks the inbound port by default.** Once, as Administrator:
  `New-NetFirewallRule -DisplayName "Tender dashboard 8000" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Private,Public`
- **`search.worldbank.org` must be reachable.** On a network running SSL deep inspection
  the page renders a "cannot reach" state with `CERTIFICATE_VERIFY_FAILED`, which is not
  the same as a quiet week. A phone hotspot is the usual way round it.

The Host check accepts loopback and any private, link-local or IPv6 private address, so
the page keeps working when the machine changes network and gets a new address. It
refuses hostnames unless named in `WB_ALLOWED_HOSTS`, because DNS rebinding -- a page
pointing a name it controls at your private address to become same-origin with this app
-- always arrives as a name.

## Putting it on a server

**There is no authentication, and this is a single-user tool.** The page shows which
tenders the organisation is pursuing and lets anyone who loads it rewrite the product
catalogue. Public addresses are refused by the Host check, so exposing it directly by IP
will not work by accident. To serve it properly:

1. Bind it to loopback (`--host 127.0.0.1`) and put a reverse proxy in front.
2. Terminate TLS and require authentication at the proxy, not in this app.
3. Set `WB_ALLOWED_HOSTS` to the hostname the proxy uses.
4. Point `WB_DASHBOARD_DB`, `WB_TAXONOMY` and `WB_DOCUMENT_DIR` at writable paths
   outside the checkout, since the catalogue is rewritten at runtime.

Environment variables, all optional:

| Variable | Default | Purpose |
| --- | --- | --- |
| `WB_DASHBOARD_DB` | `dashboard.db` beside the package | Filter, triage state, dossier cache |
| `WB_TAXONOMY` | `profiles/cybersecurity.json` | The product catalogue, rewritten at runtime |
| `WB_DOCUMENT_DIR` | `documents/` beside the database | Where PDF-only documents are saved |
| `WB_ALLOWED_HOSTS` | unset | Extra hostnames to answer to |
| `WB_MAX_SEED_TERMS` | `24` | How many taxonomy terms seed the search |

It does not include AI scoring, authentication, CRM integration, or multi-tenancy.
Relevance is deterministic and driven entirely by `profiles/cybersecurity.json`.

## Why it is fast

A notice body is **96.7% of the notice's weight** — measured on a live search for
`cybersecurity`: 486 records, 7.66 MB, of which `notice_text` was 7.19 MB. Of those 486,
**six** survived the biddability filter. So the fetch runs in two phases:

1. Every field **except** the body (`fl` is honoured by `/api/procnotices`, undocumented
   like `qterm`): 0.27 MB instead of 7.66 MB, 1.0s instead of 2.6s.
2. Bodies for the survivors only, one request each by `id` — also undocumented, verified
   to return exactly one record. Comma-separated ids return nothing, so there is no batching.

Three caches sit behind that, with lifetimes matched to how fast each thing actually
changes: term results for 15 minutes, notice bodies for 3 days, and engine verdicts keyed
by `taxonomy_version` so editing the catalogue invalidates every decision at once. Bodies
are stored already stripped of markup, because re-stripping 8 MB of HTML per page load
cost more than the download.

Measured on live data, 24 search terms:

| | time |
| --- | --- |
| One full page per term (the original) | 41.6s |
| Two-phase, nothing cached | 5.4s |
| Removing a term, excluding a word, flipping a switch | **1.4s** |

Both paths return the **identical** 18 tenders — verified by diffing the result sets, since
a cache that answers differently from a fresh fetch is worse than no cache. **Refresh**
drops the term cache when you want certainty, and the footer always says which you are
looking at.

## What the dashboard shows

Tenders group under **closing horizons**, so the page answers "what must I act on this
week" without the reader doing arithmetic. Three things sit on each row: the buyer's own
subject line, a live mailto to the buyer, and the project's published documents.

- **Project documents** (`dossier.py`) come from the Documents & Reports endpoint, keyed
  by the `project_id` every notice carries. They are the project's *own* reports, not the
  bid package — no World Bank API publishes the tender document, which comes from the
  buyer. Where the service publishes a `txturl` the text is already extracted, so **OCR
  is unnecessary**; the page only says "needs OCR" for a PDF with no text rendering.
- **The product catalogue** (`catalogue.py`) is editable from the page. The taxonomy JSON
  *is* the catalogue, and products evolve, so a term can be added or removed at runtime:
  validated and linted before the file changes, written atomically, and version-bumped.
  Adding a product also starts searching for it, because a catalogue entry nobody
  retrieves would never produce a tender. Hand-written terms are shown but not removable
  — they carry context guards and blocklists a text box cannot express.

## Setup

Needs **Python 3.11 or newer** and git. Nothing else — no database server, no `.env`,
no Docker. Every setting has a working default, and the dashboard keeps its own state in
a SQLite file it creates on first run.

**Windows (PowerShell)**

```powershell
git clone https://github.com/Prajwal3112/Opportunity.git
cd Opportunity
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[web,dev]"
pytest -m "not live" -q
python -m wb_connector.dashboard
```

**Linux / macOS**

```bash
git clone https://github.com/Prajwal3112/Opportunity.git
cd Opportunity
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[web,dev]"
pytest -m "not live" -q
python -m wb_connector.dashboard
```

Then open <http://127.0.0.1:8000>. The first load takes a few seconds while it fetches;
after that the caches make it near-instant. Expect `147 passed` from the test run.

If PowerShell refuses to run the activate script, allow it for this session only:
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`.

### Check the setup before blaming the tool

```bash
python -m wb_connector.check
```

It verifies the Python version, the web packages, that the product catalogue loads and
passes its own lint, that the state file can be written, and that `search.worldbank.org`
actually answers:

```
  [ok] Python             Python 3.13
  [ok] Web packages       fastapi, uvicorn and python-multipart are installed
  [ok] Product catalogue  89 terms, version 2026.09.9-pilot
  [ok] Writable state     state file .../dashboard.db
  [ok] World Bank API     reachable -- 'SIEM' matches 213 notices
```

Exit codes are meant to be told apart: **0** everything works, **1** something is
genuinely broken, **2** the install is fine but the network blocks the World Bank. That
last one is common on corporate wifi running SSL deep inspection, and it is reported as
`TLS is being intercepted on this network` rather than as a failure — the fix is a
different network, not a reinstall.

### Postgres is optional and unrelated

`docker-compose.yml` and the `psycopg` dependency belong to `db.py`, an earlier
persistence path the dashboard does not use. Ignore both unless you are working on that
module; the dashboard, the digest and every test run without them.

## Architecture

- `client.py` owns timeout, retry/backoff, JSON handling, and response envelopes.
- `connectors.py` owns World Bank endpoint-specific normalization.
- `models.py` contains Pydantic normalized records, always retaining raw payloads.
- `db.py` persists normalized entities plus an immutable raw snapshot per retrieval.
- `tests/test_client.py` is mocked unit coverage and intentionally has no invented
  World Bank record fixture. `tests/test_live_world_bank.py` is opt-in live coverage.

See [API validation notes](docs/world-bank-api-validation.md) before enabling a
live sync. The document distinguishes documented, inferred, unverified, and
live-tested behavior.

## Finding opportunities you can actually apply to

A relevance score is not the deliverable; a tender you can still bid on is.
`opportunity_engine.biddability.assess()` is the single owner of that question, and it
returns a three-state verdict rather than a boolean: **BIDDABLE** (bid-eligible type, live
status, deadline still ahead), **DEADLINE_UNKNOWN** (all of the above but no published
deadline — the date is in the bid document) or **CLOSED**. `ProcurementNotice.is_applyable()`
delegates to it and treats the first two as open.

Treating a missing deadline as closed was the single most expensive defect in the project:
**33 notices looked open under that rule; 933 are open under this one — 96% of the market
was hidden.** The dashboard keeps DEADLINE_UNKNOWN tenders in their own closing band so a
guess never masquerades as a date.

Two field semantics are easy to get wrong, both established by capture on 2026-09-04:

- **`submission_date` is the publication date, not a deadline.** It equals `noticedate`
  in every record that carries both. The deadline is `submission_deadline_date`.
- **72.5% of notices are `Contract Award`** — already decided, and they never carry a
  deadline. `discover_applyable()` excludes them at the service rather than after
  downloading them.

`qterm` and `notice_type` are both honoured by `/api/procnotices` even though neither
is documented for it, and they compose: `cybersecurity` alone narrows 417,771 notices
to 475, and adding `notice_type=Invitation for Bids` narrows that to 20. A domain sweep
is therefore a few hundred targeted requests, not a full crawl.

```python
async for notice in ProcurementConnector(client).discover_applyable(qterm="cybersecurity"):
    if notice.is_applyable():
        print(notice.submission_deadline_date, notice.bid_description, notice.contact_email)
```

## Reconnaissance

`python -m wb_connector.recon --pages 20` captures raw responses from all three
endpoints and writes `RECON.md` reporting real field names and fill rates, pagination
behaviour, which query parameters the service honours, and how many notices are
actually applyable. Run it only from a network permitted to reach the service; a
filter/interstitial page is reported as an environment failure, not an API failure.

## Real-response captures and live tests

Capture a real response only from a network allowed to reach the official service:

```powershell
python -m wb_connector.capture --endpoint documents --projectid P165557 --rows 10 --output wds-p165557.json
```

The capture metadata and response are preserved under `captures/`; never create
an API fixture manually. To run real HTTP integration tests, explicitly opt in:

```powershell
$env:RUN_LIVE_WORLD_BANK_TESTS = "1"
pytest -m live -q
```

## Phase 2: deterministic opportunity candidates

The `opportunity_engine` package evaluates a normalized procurement notice and
project metadata against a versioned JSON capability profile. It produces evidence
snippets, matched terms, component scores, and a configurable relevance tier. It
does not make network calls and does not implement AI or semantic ranking. See
[candidate engine documentation](docs/opportunity-candidate-engine.md) and the
[example profile](examples/capability-profile.example.json).
