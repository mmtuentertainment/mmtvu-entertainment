# Preserve funnel high-water-mark metrics

Status: Accepted
Date: 2026-07-09
Project: ops-crm / MMTVU Revenue OS

## Context

The Revenue OS v0 plan measures outreach progress against the first 14-day funnel targets:

- 50 prospects identified
- 30 contacted
- 5 discovery conversations
- 1 pilot proposed or won

The review raised a Tier 2 decision about what cumulative funnel metrics mean when a prospect moves to an off-ramp status such as `lost` or `follow_up_later`.

A single current `status` value cannot by itself express both the prospect's current disposition and the highest funnel stage the prospect actually reached. If metrics use current status only, a prospect that reached `pilot_proposed` and later moved to `lost` would stop counting toward discovery/pilot progress, even though the campaign did achieve those stages.

## Decision

Use high-water-mark funnel semantics.

A prospect continues to count toward every real funnel stage it demonstrably reached, even if its current status later moves to `lost` or `follow_up_later`.

`not_fit` is the exception: it is a hard disqualification veto and contributes no funnel credit, regardless of prior status history.

This preserves the current code direction:

- `prospects.max_stage_rank` stores the highest real stage reached.
- Status writes update the high-water mark with `MAX(max_stage_rank, ?)`.
- `_effective_stage_rank(status, max_stage_rank)` computes metric credit.
- `lost` and `follow_up_later` defer to the preserved high-water mark.
- `not_fit` returns no credit.

Implementation note: this behavior is already implemented in code (Tier 1 commit `d294492` added the `max_stage_rank` column, `_effective_stage_rank()`, and the `MAX(max_stage_rank, ?)` write path). This ADR ratifies that implementation choice rather than authorizing new work.

## Rationale

The 14-day funnel metrics are meant to measure campaign progress, not only the current disposition of each prospect.

If an operator books discovery or proposes a pilot, that progress should remain visible in the funnel totals even if the prospect later declines, stalls, or is moved to a follow-up-later bucket.

This keeps the metrics aligned with the business question: did the outreach campaign create enough real conversations and pilot opportunities?

## Consequences

Cumulative funnel totals are intentionally not the same as counts of current statuses.

A prospect currently marked `lost` may still contribute to `contacted_total`, `discovery_booked_total`, or `pilot_proposed_total` if it previously reached those stages.

A prospect marked `not_fit` contributes nothing to funnel progress because it was not a valid target for the campaign.

Documentation, tests, and future UI should describe these as high-water-mark metrics rather than current-status counts.