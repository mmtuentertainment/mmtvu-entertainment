# Add full prospect-list funnel controls

Status: Accepted
Date: 2026-07-09
Project: ops-crm / MMTVU Revenue OS

## Context

The Revenue OS v0 plan measures execution against the first 14-day outreach targets:

- 50 prospects identified
- 30 contacted
- 5 discovery conversations
- 1 pilot proposed or won

The dashboard currently exposes funnel controls only where a prospect has a next-action row. In the current dataset, that covers the current action-backed subset, not every prospect in the 64-prospect CRM. That means the operator dashboard cannot directly move all campaign prospects through the funnel, even though the v0 plan depends on tracking outreach progress across the whole prospect set.

The CSV/import/regeneration path remains valid, but it should not be the only supported update path for prospects outside the dashboard's next-action subset.

## Decision

Add a full prospect-list view with inline funnel controls for every prospect.

- Every prospect gets an inline dashboard funnel selector
- New UI surface: prospect list table + funnel dropdown per row
- Requires: new export collection (redaction publish rule + test), UI, tests

Each prospect should have a dashboard-supported way to update its funnel status through the canonical CRM status write path.

The CSV/import/regeneration path remains supported for bulk or offline updates, but the dashboard should support the v0 operating loop directly.

## Rationale

This is necessary for the Revenue OS v0 workflow.

The dashboard is the operator surface for tracking the outreach campaign. If the 14-day target is 30 contacted prospects, the operator should be able to update all campaign prospects from the dashboard instead of relying on an incomplete next-action subset plus a fallback CSV path.

This keeps funnel tracking aligned with the business goal: measure real outreach progress across the full prospect list.

Benefit: single unified path for all prospects; 30-contacted target reachable in dashboard.

## Consequences

Implementation will require:

- A full prospect-list dashboard section or equivalent UI surface.
- Inline funnel selectors for every prospect.
- POSTs through the existing canonical prospect-status endpoint.
- A new export collection with redaction publish rule + test.
- UI tests covering that every public/dashboard prospect has an update control.
- Careful handling of the privacy/redaction seam; the public dashboard must not leak private prospect identity.

The next-action list remains useful for prioritization, but it is no longer the only dashboard location where funnel status can be updated.