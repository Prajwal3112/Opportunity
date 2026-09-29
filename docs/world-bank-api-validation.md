# World Bank API validation

Last reviewed: 2026-09-04.

This report separates published interface documentation, connector live tests,
implementation inferences, and unknowns. A documented interface is not recorded
as live-tested until a real response has been captured from the connector's own
HTTP stack.

## Environment

The primary development machine sits behind a FortiGate appliance that
TLS-intercepts and policy-blocks **`search.worldbank.org` only** (block page:
"Web Filter Violation", category "General Organizations"; leaf certificate issued
by `O=Fortinet, CN=FGT60FTK2209CRST`). Every sibling host —
`documents.worldbank.org`, `documents1.worldbank.org`, `projects.worldbank.org`,
`api.worldbank.org`, `datacatalogapi.worldbank.org` — resolves with a clean
public certificate chain and is reachable.

Because all three connector endpoints live on the blocked host, the session on
**2026-09-04** was run from an unfiltered network. Certificate issuer on that
network was `O=Google Trust Services, CN=WE1`, confirming the interception was
absent. Captures are under `captures/recon-20260904-1103/`.

## Validation matrix

| Capability | Endpoint | Documented | Live-tested | Evidence |
| --- | --- | --- | --- | --- |
| Procurement discovery | `GET /api/procnotices` | Yes | **Yes, 2026-09-04** | 20 pages captured; `total` = 417,771 notices. |
| Procurement envelope | top-level `rows`, `os`, `page`, `total`, `procnotices` | Observed | **Yes** | All five keys present. **Numeric values are JSON strings** (`"417771"`, `"0"`, `"1"`), not integers. |
| Procurement page size | request `rows` | Partly | **Yes** | `rows=1000` honoured and returns 1000 records. `100`, `500`, `1000` all returned exactly as requested; the ceiling is at or above 1000. |
| Procurement offset pagination | request `os` | Yes | **Yes, with a caveat** | `os` advances. **Records overlap across pages**: 20,000 fetched yielded 16,380 unique `id`s (18.1% duplicates). Deduplication by `id` is mandatory. |
| Procurement text filter | `qterm` | Not documented for this endpoint | **Yes — honoured** | `qterm=cybersecurity` narrows `total` 417,771 → 475. `q` is **ignored** (total unchanged). |
| Procurement type filter | `notice_type` | Not documented | **Yes — honoured** | `notice_type=Invitation for Bids` combined with `qterm=cybersecurity` narrows 475 → 20. Server-side filtering is therefore composable. |
| Procurement record identity | `procnotices[*].id`, `project_id` | Yes | **Yes** | `project_id` populated on **100%** of 16,380 unique notices. |
| Procurement normalization | `ProcurementConnector.normalize_record` | — | **Yes** | 1000/1000 real records normalized with no validation error. |
| Projects discovery | `GET /api/v2/projects` | Yes | **Yes** | Returns records; `projects` is a **mapping keyed by project id**, not a list. |
| Projects `page` pagination | request `page` | Inferred only | **Yes — DOES NOT WORK** | `page=2` returns the **same records** as `page=1`. `client.paginate` derives the next page from the echoed `page` value and will therefore **loop forever**. Must not be used against this endpoint. |
| Documents & Reports | `GET /api/v3/wds` | Yes | **Yes** | `qterm=cybersecurity` returns `total` = 1,549 documents. `documents` is a mapping. |
| WDS document type | `documents[*].docty`, `majdocty` | — | **Yes, partial** | `docty` (e.g. "Brief") and `majdocty` (e.g. "Publications & Research") are the real type fields. **Sparsely populated** — absent on some records. |
| WDS representations | `documents[*].pdfurl`, `txturl` | Yes | **Yes** | Both present. **URLs are `http://`, not `https://`** — see the download-policy note below. |
| WDS taxonomy fields | `theme`, `majtheme`, `sectr`, `subsc`, `teratopic`, `subtopic`, `keywd`, `trustfund` | Not previously known | **Yes** | A real sector/theme taxonomy exists on documents. Several are nested JSON-in-string. |

## Field semantics established by capture

**`submission_date` is the publication date, not a bid deadline.** It equals
`noticedate` in **16,319 of 16,319** records that carry both (100.0%). The actual
deadline field is **`submission_deadline_date`**, paired with
`submission_deadline_time`. Neither is currently on `ProcurementNotice`.

Deadline coverage is concentrated in exactly the notice types that can be bid on:

| notice_type | records | with a deadline |
| --- | ---: | ---: |
| Contract Award | 11,869 | 0 (0.0%) |
| Request for Expression of Interest | 2,480 | 1,828 (73.7%) |
| Invitation for Bids | 1,852 | 1,460 (78.8%) |
| General Procurement Notice | 146 | 0 (0.0%) |
| Invitation for Prequalification | 29 | 10 (34.5%) |

**72.5% of all notices are Contract Awards** — already-decided contracts that
carry no deadline. They must be excluded at ingest, and `notice_type` supports
doing so server-side.

Contact fields (`contact_email`, `contact_name`, `contact_organization`,
`contact_phone_no`, `contact_address`, `contact_web_url`) are present on ~34% of
notices and are the route by which a bidder actually responds. None is currently
normalized.

`projects[*].countryname` is a **JSON array serialized as a string**
(`'["People\'s Republic of Bangladesh"]'`), not a plain country name.

## Download policy consequence

`DocumentsConnector.retrieve_representation` requires `https://` and a host
ending in `.worldbank.org`. Every real `pdfurl`/`txturl` observed uses
`http://documents.worldbank.org/...`, so the current check **rejects 100% of real
documents**. The fix is to upgrade the scheme to HTTPS before the request rather
than to relax the check, and to validate every redirect hop — the host is
reachable over HTTPS and redirects between `documents.` and `documents1.`.

## Reproducing

```powershell
python -m wb_connector.recon --pages 20
```

Writes raw captures plus `RECON.md` and `findings.json` under
`captures/recon-<timestamp>/`. Run only from a network permitted to reach the
service. Record a filter/interstitial response as an environment failure, not an
API failure.
