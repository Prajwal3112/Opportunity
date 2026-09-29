# Real-response captures

This directory is for JSON captures made by `python -m wb_connector.capture`
from a network that can reach the official World Bank service. A capture includes
the endpoint, exact request parameters, final request URL, UTC capture time, and
the unmodified response payload.

Do not hand-author API responses. Do not alter source field names. If a redaction
is genuinely necessary, document exactly what was removed and why in an adjacent
`<capture-name>.redaction.md` file; do not otherwise transform the response.

Example:

```powershell
python -m wb_connector.capture --endpoint documents --projectid P165557 --rows 10 --output wds-p165557.json
```

Captures are intentionally outside `src/`. Keep large captures outside Git or use
Git LFS according to the repository policy. Regression tests may read any checked-in
capture and validate its provenance metadata and unchanged response envelope.
