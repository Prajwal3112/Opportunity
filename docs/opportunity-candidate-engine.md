# Opportunity Candidate Engine

Phase 2 is a deterministic candidate-detection layer. It evaluates ordinary
normalized notice/project objects against a replaceable organization capability
profile. It makes no HTTP, LLM, embedding, vector-database, or document calls.
`candidate_input_from_procurement` is the explicit adapter from a normalized
World Bank procurement record to the engine input; optional project metadata is
provided as ordinary strings by the caller.

## Profile

Profiles are versioned JSON documents. See
[`examples/capability-profile.example.json`](../examples/capability-profile.example.json).
The profile supplies products, capabilities, services, industries, technologies,
positive and negative keywords, geographic preferences, and excluded categories.
Only the lexical term lists are currently scored; geographic preferences and
excluded categories are preserved for a reviewed future policy rule.

## Evidence and normalization

Source text is retained unchanged. Matching applies NFKC Unicode normalization,
case folding, punctuation-to-space normalization, whitespace collapse, and
duplicate-phrase suppression in memory only. It uses phrase boundaries rather
than substring matching. Each match stores its term, match type, source field,
and original-context snippet. Negative matches are stored separately.

## Score formula

All components are in `[0, 1]` and stored separately:

```text
final = clamp(
  keyword × keyword_weight
  + bid_description × bid_description_weight
  + notice_text × notice_text_weight
  + project_metadata × project_metadata_weight
  - negative_penalty × negative_penalty_weight,
  0, 1
)
```

Each relevance component is the number of matched unique positive terms in its
source group divided by the number of unique positive profile terms. The negative
penalty uses the corresponding negative-term ratio. Default weights and thresholds
are starting configuration values, not optimized values or probabilities.

`HIGH`, `MEDIUM`, and `LOW` are relevance tiers. `EXCLUDED` is issued when the
configurable negative-penalty threshold is reached. Tiers are never a probability
of winning a contract. `semantic` remains `null`; `SemanticRanker` is an interface
only and is not called.

## Persistence and deduplication

`candidate_evaluations` is unique by organization profile ID/version, scoring
engine version, and World Bank source external ID. Repeated ingestion replaces the
same deterministic evaluation rather than creating another opportunity. The row
can reference a `raw_source_records` snapshot and always retains the source
external ID and project ID. Score components and evidence are stored in separate
tables. `candidate_labels` supports manual `RELEVANT`, `NOT_RELEVANT`, and
`UNCERTAIN` labels; quality metrics are undefined until real labels exist.

## Limitations / explicitly not implemented

- No LLM, embeddings, semantic retrieval, or vector database.
- No PDF/TXT intelligence; WDS documents are deliberately not used at this stage.
- No dashboard, authentication, alerts, CRM, or multi-tenancy.
- No claimed precision, recall, or model quality without real labelled records.
- No automatic geographic-preference/excluded-category scoring rule yet.
