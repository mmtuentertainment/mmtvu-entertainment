# MMTVU Operator CRM (Revenue OS)

Internal prospecting tool for the MMTVU outreach assignment: track prospects, follow-up actions, and evidence, and publish a redacted dashboard of real SMB outreach without leaking prospect identity.

## Language

### Privacy seam

**Private dataset**:
The full CRM export from SQLite (`db.export_dataset`): prospects with real contact identity, actions, artifacts, loops, experiments, metrics. Never published.

**Public export**:
The redacted dataset written to `ops-crm/data/public/`. Reaches disk only through the redaction seam.
_Avoid_: sanitized output, public data

**Redaction seam**:
`redaction.derive_public(private) -> public` — the only path from private to public. Fail-closed: the output starts empty and a collection without a publish rule raises.
_Avoid_: scrubbing step, filter, sanitizer

**Publish rule**:
The per-collection decision inside the redaction seam: rebuilt with identity anonymized (prospects, next_actions, assets, offers, evidence), scrubbed as free text (loops, experiments, metrics, summary), or metadata (mode, generated_at).

**Backstop**:
`redaction.assert_public_safe` — the whole-output net (phones, emails, credentials, company names and slugs, private paths). Runs as an unconditional postcondition inside `derive_public`; it cannot be forgotten by a caller.
_Avoid_: validator, safety check

### Outreach funnel

**Funnel status**:
A prospect's position in the design doc's outreach funnel: `not_contacted → contacted → replied → discovery_booked → pilot_proposed → won/lost`, with `not_fit` and `follow_up_later` as off-ramps. Defined once as `db.FUNNEL_STATUSES`, enforced by `prospect.schema.json` and the status write path.
_Avoid_: lead stage, pipeline state, the legacy `new/needs_follow_up/booked/customer` vocabulary

**Status write path**:
`db.set_action_status` / `db.set_prospect_status`, reached through `serve.py`'s POST endpoints — the only way operator status changes enter the system. SQLite is the one status store; the dashboard's localStorage holds notes only.
_Avoid_: local state, browser state (for anything but notes)

**Metric definition site**:
`db.compute_metrics` — every Revenue OS metric is defined there and computed at export time, so post-import status changes are always reflected. Funnel totals are cumulative over each prospect's high-water-mark stage (`db.STAGE_RANK` / `db._effective_stage_rank`, persisted as `prospects.max_stage_rank`), not just current status: a prospect that genuinely reached `discovery_booked` or `pilot_proposed` still counts there even after later off-ramping to `lost` or `follow_up_later` — that real activity happened and the 14-day numbers shouldn't reverse. `not_fit` is the exception: it vetoes unconditionally, since a disqualification must never inflate the outreach-progress numbers even if the prospect had prior recorded progress. Spend sums the structured `cost_usd` field on evidence records. The doc's case-study metrics (first response time, follow-ups sent, stale leads revived) slot in here when interaction logging lands (ADR 0001).
_Avoid_: metric math in builders or the dashboard; deriving funnel totals from current `status` alone

**14-day targets**:
The doc's success criteria — 50 identified / 30 contacted / 5 discovery / 1 pilot — carried as the `target` column on metric rows so the brief and dashboard render progress (`3/30 contacted`).

**Dashboard funnel coverage**:
The prospect funnel `<select>` only renders where the dashboard has a next-action row to attach it to — today that's the 16/64 prospects who are owner-operated or Vapi-called (`generate.py::build_next_actions`). The other 48 move through the funnel by editing the outreach CSV's status column and re-running `generate.py`, not through the dashboard. This is a deliberate scope limit, not a bug: the dashboard is for prospects the operator is actively working, and the CSV path is fully supported for funnel-status writes (see finding #4 in `ops-crm-candidates-3-4-review-HANDOFF.md`).
_Avoid_: assuming every prospect has a dashboard funnel control
