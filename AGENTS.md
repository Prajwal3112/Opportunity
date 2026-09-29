# World Bank Opportunity Intelligence Platform

## Project Objective

Build a production-grade platform that continuously discovers, analyzes,
and ranks World Bank-funded projects and procurement opportunities that may
match an organization's products and capabilities.

The initial data source is the World Bank.

The long-term architecture must allow additional development-finance
institutions and procurement sources to be added later without rewriting
the core opportunity engine.

---

## Critical Engineering Principle

API-first.

Do NOT use browser scraping when an official World Bank API or structured
dataset provides the required information.

Web scraping/browser automation is a fallback only when the required data
cannot be obtained through an official structured interface.

Never assume an undocumented endpoint, field, parameter, response format,
rate limit, or relationship.

If an API behavior is not verified, mark it as unverified and create a test
or investigation task rather than building production logic around an
assumption.

---

## Current Official Data Sources

The initial system should investigate and integrate:

1. World Bank Projects API
2. World Bank Documents & Reports API
3. World Bank Procurement Notices API/dataset
4. World Bank Business Opportunities pages where useful for validation/fallback

Official documentation must be treated as the source of truth.

Do not substitute third-party World Bank API wrappers unless explicitly
approved.

---

## Initial POC Objective

The first milestone is NOT the dashboard.

Build a World Bank connector POC that proves:

1. Project discovery
2. Project metadata retrieval
3. Project ID normalization
4. Document discovery by project ID
5. Document metadata retrieval
6. TXT document retrieval when available
7. PDF retrieval when required
8. Procurement notice retrieval
9. Project ↔ document relationship
10. Project ↔ procurement relationship
11. Pagination
12. Retry/error handling
13. Raw response preservation
14. Structured normalized output

Do not implement the AI relevance engine until this data pipeline is proven.

---

## Technology Direction

Initial backend:

- Python
- FastAPI
- PostgreSQL
- SQLAlchemy
- Pydantic
- pytest
- httpx

Use async I/O where it materially improves ingestion performance.

Use Docker for local infrastructure.

Do not introduce unnecessary microservices in the POC.

Prefer a modular monolith initially.

---

## Data Model

Core entities:

- projects
- documents
- procurement_notices

Later entities:

- organizations
- products
- capabilities
- opportunities
- evidence
- document_chunks
- opportunity_scores

Project ID should be the primary relationship between World Bank
projects, documents and procurement records wherever the source data
supports that relationship.

---

## Raw Data Preservation

For every external World Bank record, preserve the raw source payload.

Do not discard fields simply because they are not currently used.

Use a JSON/JSONB raw payload where appropriate.

The system must allow future schema evolution without losing source data.

---

## Data Integrity

Every external record should retain:

- source
- source URL/API
- external ID
- retrieval timestamp
- first-seen timestamp
- last-seen timestamp
- raw payload

Do not silently overwrite source data.

Track changes where practical.

---

## Document Processing

Preferred order:

1. World Bank TXT representation if officially available
2. World Bank PDF representation
3. Local PDF text extraction
4. OCR only when normal extraction is insufficient

Do not OCR every PDF by default.

Store document processing status and extraction errors.

---

## AI Architecture

Do NOT make an LLM call for every World Bank document.

The future pipeline should be:

metadata filtering
→ lexical retrieval
→ semantic retrieval
→ candidate selection
→ LLM analysis
→ evidence extraction
→ opportunity scoring

The LLM must produce structured output.

Never rely on free-form LLM text as the only source of truth.

Every AI relevance decision must be traceable to source evidence.

---

## Opportunity Scoring

Do not confuse relevance score with probability of winning a contract.

Potential future dimensions:

- domain relevance
- capability match
- explicit requirement
- procurement signal
- opportunity maturity
- timing
- geography
- project value

Scores must be explainable.

---

## Production Requirements

Design for:

- idempotent ingestion
- retries
- pagination
- rate limiting
- structured logging
- metrics
- error handling
- incremental synchronization
- deduplication
- testability
- configuration through environment variables
- secrets never committed to git

---

## Security

Never hardcode:

- API keys
- database passwords
- credentials
- tokens

Use environment variables/secrets.

Validate external URLs before downloading.

Do not introduce SSRF vulnerabilities.

Do not blindly execute content retrieved from documents.

Treat World Bank documents as untrusted input.

---

## Development Rules

Before making major architectural changes:

1. Inspect the existing repository.
2. Explain what you intend to change.
3. Prefer small, reviewable changes.
4. Run relevant tests after modifications.
5. Do not fabricate successful API responses.
6. Do not claim an endpoint works unless it has actually been tested.
7. Document externally verified API behavior.
8. Keep source-specific code isolated from domain/business logic.

---

## Current Goal

Build only the World Bank connector POC.

Do not build:

- dashboard
- authentication system
- AI scoring
- notification system
- CRM integration
- multi-tenant architecture

until the connector has been validated against real World Bank data.

The first deliverable should be a working local POC plus tests and a
technical validation report.