# MMTVU Operator CRM

Local-first static CRM for running the MMTVUEntertainment business with Hermes.

## Generate data

```bash
uv run python ops-crm/generate.py
```

Outputs:

- `ops-crm/crm.sqlite` — local SQLite source of truth for the Revenue OS (ignored by git because it can contain private prospect state).
- `ops-crm/data/private/*.json` — local dashboard exports with prospect/campaign details.
- `ops-crm/data/public/*.json` — redacted public-safe exports derived from the private SQLite-backed dataset.
- `ops-crm/data/private/daily-brief.md` and `ops-crm/data/public/daily-brief.md` — operator briefs derived from SQLite metrics/actions.

## Use dashboard

Serve the repo with the operator server (it also carries the status write path;
plain `file://` or a dumb static server would leave status buttons unable to save):

```bash
uv run python ops-crm/serve.py
```

Then open:

```text
http://127.0.0.1:8000/ops-crm/
```

The dashboard is an operator workspace, not just a report:

- Search/filter the action queue.
- Select an action and inspect evidence/source detail.
- Mark actions open/done/blocked — writes to SQLite via `POST /api/action-status`.
- Move the selected action's prospect through the outreach funnel (`POST /api/prospect-status`).
- Save operator notes in localStorage (notes are the only browser-local state).
- Copy the next-step prompt for Hermes or Matthew.
- Export local notes as JSON.
- Toggle private/public redacted data.
- Inspect the SQLite-backed Revenue OS loop, 14-day funnel progress, next best move, and daily brief link.

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
