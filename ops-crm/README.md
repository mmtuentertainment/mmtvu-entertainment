# MMTVU Operator CRM

Local-first static CRM for running the MMTVUEntertainment business with Hermes.

## Generate data

```bash
uv run python ops-crm/generate.py
```

Outputs:

- `ops-crm/data/private/*.json` — local CRM data with prospect/campaign details.
- `ops-crm/data/public/*.json` — redacted public-safe data derived from private data.

## Use dashboard

Browser `file://` mode may block JSON fetches. Serve the repo instead:

```bash
python3 -m http.server 8000
```

Then open:

```text
http://127.0.0.1:8000/ops-crm/
```

The dashboard is an operator workspace, not just a report:

- Search/filter the action queue.
- Select an action and inspect evidence/source detail.
- Mark actions open/done/blocked.
- Save operator notes in localStorage.
- Copy the next-step prompt for Hermes or Matthew.
- Export local CRM state as JSON.
- Toggle private/public redacted data.

Keyboard shortcuts:

- `/` search
- `j` / `k` next/previous action
- `d` mark done
- `b` mark blocked
- `c` copy selected action

## Test

```bash
uv run pytest ops-crm/tests
```
