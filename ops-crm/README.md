# MMTVU Operator CRM

Local-first static CRM for running the MMTVUEntertainment business with Hermes.

## Generate data

```bash
uv run python ops-crm/generate.py
```

Outputs:

- `ops-crm/data/private/*.json` — local CRM data with prospect/campaign details.
- `ops-crm/data/public/*.json` — redacted public-safe data derived from private data.

## View dashboard

Browser `file://` mode may block JSON fetches. Serve the repo instead:

```bash
python3 -m http.server 8000
```

Then open:

```text
http://127.0.0.1:8000/ops-crm/
```

## Test

```bash
uv run pytest ops-crm/tests
```
