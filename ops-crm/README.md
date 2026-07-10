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

## Record Evidence Loop V1 activity

Use the guided command so quotes, artifact references, and buyer details never appear in shell history:

```bash
uv run python ops-crm/record_outreach.py attempt
uv run python ops-crm/record_outreach.py commercial-event
```

For prepared input, save one JSON object under the ignored `ops-crm/data/private/` directory, then pass only its path:

```bash
uv run python ops-crm/record_outreach.py attempt \
  --input-json ops-crm/data/private/attempt.json
uv run python ops-crm/record_outreach.py commercial-event \
  --input-json ops-crm/data/private/commercial-event.json
```

The logger rejects JSON outside `ops-crm/data/private/`. It also rejects arbitrary funnel stages, contact against a suppressed prospect, naive timestamps, contradictory conversation fields, and unsupported evidence grades before persistence.

Evidence rules:

- Grade A: direct private artifact; `artifact_ref` is required.
- Grade B: same-day verbatim note; `evidence_text` is required.
- Grade C: context only; it cannot substantiate pain score 2–3.
- Discovery bookings, paid proposals, written acceptances, and payments require Grade A evidence.
- `won` is derived only from an artifact-backed `payment_received` event.

An explicit opt-out durably suppresses further contact. Re-contact requires a reason:

```bash
uv run python ops-crm/record_outreach.py unsuppress PROSPECT_ID
```

The SQLite write commits before exports refresh. If refresh fails, the command exits 2 and preserves the committed record. Retry without re-entering evidence:

```bash
uv run python ops-crm/record_outreach.py refresh
```

Private exports include `outreach_*` count, numerator, denominator, and defined-rate rows. A zero denominator remains visible as `0/0` and “not enough evidence”; no undefined rate row is emitted. The public redaction seam drops every `outreach_*` metric unless a metric is deliberately reviewed and added to the public allowlist.

If WSL contains a stale `.venv` with no Linux Python executable, use an isolated environment instead of deleting or rewriting it:

```bash
UV_PROJECT_ENVIRONMENT=/tmp/mmtvu-v1-venv uv sync --dev
UV_PROJECT_ENVIRONMENT=/tmp/mmtvu-v1-venv uv run pytest ops-crm/tests
```

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
