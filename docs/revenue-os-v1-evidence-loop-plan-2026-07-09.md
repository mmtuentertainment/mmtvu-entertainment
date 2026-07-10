# Evidence Loop V1: Paid Validation, Not Revenue OS Infrastructure

Created: 2026-07-09
Last revised: 2026-07-10
Repo: `/mnt/d/mmtvu-entertainment`
Status: **scope approved; implementation completed and final verification passed**
Review mode: **scope reduction**

## Executive decision

Do not build a larger Revenue OS V1.

Build the smallest private evidence instrument needed to sell one concrete founding pilot, then use it for a gated ten-business calibration batch. Expand from ten to thirty businesses only when reach, pain/economics, and buyer-movement gates all pass.

Evidence Loop V1 succeeds only when MMTVU receives a **$500 payment for the defined founding pilot**. Conversations, pain scores, workflow reviews, and proposals are diagnostic evidence—not revenue validation.

## Objective and falsifiable hypothesis

### Business objective

Win one $500 paid founding pilot from the initial Columbus roofing segment without building SaaS, dashboard UI, automated outbound, or speculative CRM infrastructure.

### Hypothesis

> Among office-staffed residential roof-replacement companies in Columbus, Ohio, at least some have economically meaningful unsold-estimate leakage and will take an artifact-backed buying step after a concrete paid ask.

### What the experiment must distinguish

1. Bad list or targeting.
2. Failed delivery, timing, IVR, or gatekeeper access.
3. Humans reached but no relevant pain.
4. Pain exists but lacks economic value.
5. Qualified pain exists but trust/message is weak.
6. Buyer movement occurs but the paid offer is rejected.
7. A prospect pays for the pilot.

## Locked ICP

Initial segment:

- Columbus, Ohio.
- Residential roof-replacement companies.
- Approximately 5–30 employees **or** clear office/admin capacity.
- Visible replacement-estimate workflow.
- Active lead-generation signal such as an estimate form, financing page, advertising, or substantial recent review activity.
- Public business contact information.

Start with the six existing Columbus roofing records and source only four additional verified businesses.

Do not exclude a business merely because it has a CRM or automation. Record that as metadata; exclude only when conversation evidence shows estimate follow-up is actually solved.

Exclude or mark `not_fit` when the business is a solo operator without an office process, emergency/repair-only, a franchise/corporate call center, outside the geography/buying motion, not a real business, or lacks a meaningful replacement-estimate workflow.

## Locked founding offer

### Price and scope

**$500 upfront for a 14-day founding pilot:**

- one client-owned lead source;
- up to 25 unsold estimates;
- the client represents that it has lawful contact rights for the supplied leads;
- one agreed follow-up workflow;
- daily handoff of qualified replies or hot leads to the client’s staff;
- an end-of-pilot report.

No revenue guarantee. Before launch, MMTVU and the client choose the operational success measures: replies, re-engaged opportunities, booked inspections, or another concrete downstream result.

### Manual fulfillment path

If a prospect buys before automation exists:

1. Obtain the client’s lead file through an agreed private transfer method.
2. Verify the source, age, required fields, suppression flags, and represented contact rights.
3. Select one lawful channel and script with the client.
4. Run the narrow workflow manually or with simple client-approved automation.
5. Hand qualified replies to the named client owner daily.
6. Maintain suppression and disposition records.
7. Deliver the agreed end report.

SMS requires documented applicable consent. No AI voice, prerecorded voice, autodialing, or cold SMS is part of this pilot.

### Trust artifact required before paid asks

Prepare a one-page pilot brief and a sanitized sample daily hot-lead handoff. Do not fabricate a case study or imply prior results.

### Free-pilot rule

The default validation offer is paid. Any free pilot requires Matthew’s explicit approval and must purchase concrete proof such as usable outcome data, a named case study, or a testimonial. Waiving a setup fee is not the default gate.

## Conversation and buying flow

### Diagnostic sequence

1. “Do you have unsold replacement estimates from the last 90 days that nobody is actively chasing?”
2. “Roughly how many, and what is a typical roof-replacement value?”
3. “Who owns follow-up now, and what happens after the first attempt?”

If economically qualified pain appears, make a concrete next-step ask during the same conversation:

- book a 15-minute workflow review; or
- ask permission to send the $500 founding-pilot terms.

Do not end a qualified conversation with only “interesting,” “worth testing,” or an unowned “follow up later.”

### Artifact-backed buyer movement

Count buyer movement only when at least one artifact exists:

- accepted calendar event for a workflow review;
- inbound email/message explicitly requesting pilot terms;
- sent proposal or invoice tied to a named prospect;
- payment receipt.

A proposal initiated without prospect engagement is activity, not buyer movement.

## Outreach rules

Allowed:

- manual business phone calls;
- manual email to a relevant public business address;
- appropriate voicemail;
- manual LinkedIn outreach when clearly relevant.

Not allowed in V1 without separate approval:

- AI voice outbound;
- automated or prerecorded calling;
- autodialing;
- cold SMS;
- mass email;
- automated sequences;
- Vapi tuning as the validation path.

Before outreach:

- perform applicable DNC/suppression checks;
- use lawful calling windows based on prospect local time;
- identify the operator responsible for the outreach record;
- include appropriate identity and opt-out language in email;
- avoid using the main sending domain for scaled cold email testing without a separate deliverability decision;
- record opt-outs immediately and enforce them durably.

## System architecture

```text
Matthew
   |
   v
record_outreach.py (guided local CLI)
   |
   +--> validate the complete command in memory
   |
   v
BEGIN SQLite transaction
   +--> outreach_attempts         one row per real call/email/LinkedIn attempt
   +--> commercial_events         one row per later buyer/commercial milestone
   +--> prospects                 suppression + evidence-derived funnel stage
   |
   v
COMMIT authoritative evidence
   |
   v
existing refresh_exports()
   +--> private aggregate metrics/brief
   +--> export failure: SQLite facts remain committed; projection is stale
   +--> explicit public metric allowlist
   +--> no raw evidence or outreach metrics cross the public seam
```

SQLite is authoritative. Generated JSON is a projection. Each command writes its related attempt/event, suppression, and justified funnel transition in one transaction. Export runs only after that transaction commits. If export fails, the CLI exits nonzero, prints the committed record ID, says the projection is stale, and supplies the exact regeneration command.

The implementation should extract a shared non-committing prospect-stage helper from the existing setter. The dashboard setter calls it and commits as before; the evidence logger calls it inside its larger transaction. Evidence-driven progression is forward-only. Off-ramps are explicit fit/conversation outcomes, not inferred from inactivity.

No endpoint, background job, external CRM, generic event framework, dashboard form, JSON-schema expansion, generator change, or network dependency is added.

## Data model

### Existing `prospects` responsibility

Keep identity, fit, current funnel high-water state, and durable contact suppression on the existing prospect row.

Add only:

- `contact_suppressed_at` nullable timestamp;
- `contact_suppression_reason` nullable text.

An opt-out sets suppression durably. Future attempt logging is blocked unless an explicit unsuppress action records a reason.

### New `outreach_attempts` responsibility

Store one row per real attempt. Do not collapse history into `attempt_count`, `last_attempt_at`, `attempt_channel`, or one mutable `outcome` field.

Group input so live logging remains usable:

**Pre-call**

- `attempt_id`;
- `prospect_id`;
- offset-aware `attempted_at`;
- `channel`;
- DNC/check acknowledgement where applicable.

**Attempt result**

- `contact_role`;
- `disposition`;
- `human_reached`.

**Post-conversation, only when applicable**

- `conversation_outcome`;
- `substantive_conversation`;
- `pain_score_0_3`;
- `pain_type`;
- exact quote or evidence paraphrase;
- estimated eligible unsold-estimate volume;
- ticket-value band;
- current follow-up owner/process;
- objection category;
- buyer-movement artifact/reference;
- next action.

Sensitive text is entered through the guided CLI or an ignored private JSON input file, not shell flags.

### New `commercial_events` responsibility

Store later buyer/commercial milestones separately from the attempt that originated the opportunity. This prevents mutating an old call row when a buyer books, receives a proposal, accepts, or pays days later.

Minimum fields:

- `event_id`;
- `prospect_id`;
- nullable originating `attempt_id`;
- `event_type`;
- offset-aware `occurred_at`;
- `offer_version`;
- explicit price fields when applicable;
- evidence grade;
- required artifact reference for gated milestones;
- operator and `created_at`.

Allowed event types are narrow business facts, not a generic event store:

- `discovery_scheduled`;
- `discovery_completed`;
- `discovery_no_show`;
- `paid_proposal_sent`;
- `paid_pilot_accepted`;
- `paid_pilot_declined`;
- `payment_received`.

`payment_received` is the only event that advances the prospect to `won`. Written acceptance is useful commercial evidence but is not payment.

### Separate state concepts

Attempt dispositions describe what happened on one attempt, for example:

- `no_answer`;
- `voicemail_left`;
- `ivr_blocked`;
- `wrong_number`;
- `gatekeeper_no_transfer`;
- `human_no_time`;
- `substantive_conversation`;
- `email_sent`;
- `email_replied`;
- `opt_out`.

Conversation outcomes describe the substantive result, for example:

- `no_relevant_pain`;
- `pain_unqualified`;
- `pain_qualified_no_interest`;
- `follow_up_requested`;
- `paid_terms_requested`.

Calendar acceptance, proposal delivery, acceptance, and payment are recorded as `commercial_events`, not overloaded conversation outcomes.

Prospect funnel state remains the existing high-water state. The implementation must reuse the existing monotonic transition path rather than invent a second funnel.

### Evidence-derived funnel mapping

- Any valid outbound attempt advances at most to `contacted`.
- A real human reply or substantive exchange may advance to `replied`.
- An accepted calendar artifact advances to `discovery_booked`.
- An engaged proposal or invoice artifact advances to `pilot_proposed`.
- A payment receipt advances to `won`.
- An explicit later request may set `follow_up_later`.
- A qualified explicit rejection may set `lost`.
- A confirmed ICP disqualification may set `not_fit`.
- An opt-out preserves the high-water stage and sets durable suppression.
- No-answer, voicemail, IVR, gatekeeper, and inactivity outcomes never imply `lost`.

The CLI collects typed facts and artifacts; it does not expose an arbitrary prospect-status selector.

### Evidence rules

- Pain score `0`: no relevant pain.
- Pain score `1`: mild inconvenience or unquantified manual work.
- Pain score `2`: inconsistent ownership or credible leakage with evidence.
- Pain score `3`: acute, economically meaningful leakage with concrete evidence.
- Grade A evidence is a direct artifact: written reply, lawful transcript/recording, calendar event, sent proposal, written acceptance, or payment receipt.
- Grade B evidence is a same-day verbatim note containing timestamp, contact role, exact quote, operator, and business ID.
- Grade C evidence is a later paraphrase or unsupported memory. It remains qualitative context but cannot satisfy a gate.
- Only grades A and B can support pain score `2–3` or a positive decision gate.
- Contradictory or incomplete records are rejected before write; no draft queue.
- Unknown, archived, or suppressed prospects are rejected.
- Missing required evidence means `not_countable`, not negative.
- Zero denominators render as `not_enough_evidence`, never a misleading `0%`.

## Private metrics and denominators

| Metric | Definition |
|---|---|
| `verified_contactable_businesses` | ICP-fit prospects with usable contact data and required compliance checks |
| `attempted_businesses` | Distinct verified prospects with at least one logged attempt |
| `completed_sequences` | Distinct prospects with a terminal result or all planned attempts completed |
| `human_reached` | Distinct prospects with a live human or substantive email reply |
| `substantive_conversations` | Distinct prospects that answered the workflow/economic questions meaningfully |
| `qualified_pain` | Distinct prospects with pain score 2–3, evidence, and qualifying estimate volume/economics |
| `human_reach_rate` | `human_reached / attempted_businesses` |
| `substantive_rate` | `substantive_conversations / human_reached` |
| `qualified_pain_rate` | `qualified_pain / substantive_conversations` |
| `workflow_reviews_booked` | Distinct prospects with accepted calendar artifacts |
| `paid_terms_requested` | Distinct prospects that explicitly request terms |
| `artifact_backed_offers` | Distinct prospects with engaged proposal/invoice artifacts |
| `paid_pilots` | Distinct prospects with received payment evidence |
| `invalid_endpoint_rate` | Invalid phone/email endpoints divided by endpoints attempted |
| `ivr_block_rate` | IVR-blocked calls divided by call attempts |
| `gatekeeper_block_rate` | Gatekeeper blocks divided by live call connects |
| `suppression_rate` | Opt-outs/compliance suppressions divided by attempted businesses |

Operator-only diagnostics include attempts by channel/time bucket, role reached, objection category, evidence completeness, DNC blocks, price exposure, payment state, and projection freshness.

Every rate is backed by separately stored numerator and denominator count metrics. Count rows always exist. A rate row is emitted only when its denominator is greater than zero; otherwise the private brief renders `not enough evidence (0 / 0)` or the actual numerator/zero pair. Every displayed rate includes both counts, not only a percentage.

All new metrics use the `outreach_` prefix. They appear in the private export and brief only. Public redaction uses an explicit metric allowlist; unknown or future metrics do not become public automatically. Raw attempts, commercial events, evidence text, objections, prices, and artifact references remain SQLite-only and are never added to generated JSON.

## Gated operating sequence

### Day 0–1: instrument and prepare ten

- Implement and test the small evidence logger only after implementation approval.
- Source only four additional verified Columbus roofing businesses.
- Complete applicable DNC/suppression checks.
- Prepare the truthful one-page pilot brief and sanitized handoff sample.
- Stop list-building at ten.

### Days 1–5: calibration batch

- Contact ten businesses manually.
- Make up to three attempts per business across appropriate local windows.
- Record every attempt immediately.
- Make the concrete paid next-step ask as soon as qualified pain appears.
- Do not wait for a later discovery phase.

### End of day 5: sequential gates

**Reach gate**

- At least five live humans; and
- at least three substantive conversations with relevant roles.

If this fails, repair the list/channel/timing and run another ten-business calibration. Do not declare the offer invalid.

**Pain/economics gate**

- At least two substantive conversations with pain score 2–3 and evidence; and
- approximately ten eligible unsold estimates in 90 days or equivalent recurring volume/economics.

If this fails after the reach gate passes, pivot the ICP/problem framing. Do not build product.

**Buyer-movement gate**

- At least one accepted workflow-review calendar event or explicit request for paid pilot terms after a concrete paid ask.

If this fails after the first two gates pass, revise the trust artifact, message, or offer before scaling.

Expand from ten to thirty businesses only when all three gates pass.

### Days 6–10: conditional expansion

- Add/contact twenty more businesses only after the gates pass.
- Keep the same ICP, offer, and price.
- Change one outreach variable at a time and label the batch.

### Days 11–14: paid close and review

- Complete workflow reviews.
- Send concrete terms/invoices.
- Ask for payment.
- Review evidence and choose continue, repair, pivot, or stop.

### Final paid gate

Success: at least one $500 payment.

If thirty completed sequences include at least three artifact-backed paid offers and zero payments, pivot the offer, price, or trust mechanism. Do not build SaaS or claim validation.

## Error and rescue registry

| Codepath | Failure | Rescue/action | Operator sees |
|---|---|---|---|
| CLI parsing | Missing/wrong type | Reject before write | Field-specific usage error |
| Attempt validation | Invalid enum, contradiction, missing evidence | Reject before write | Violated rule and field |
| Prospect lookup | Unknown/archived/suppressed | Reject before write | Prospect ID and corrective command |
| SQLite insert | Constraint violation | Roll back the command | Specific constraint failure |
| SQLite write | Database locked | Five-second busy timeout, then fail | No-write statement and retry guidance |
| Atomic evidence write | Attempt/event, suppression, or stage constraint fails | Roll back the entire command | No partial-write statement and violated rule |
| Funnel transition | Evidence cannot justify requested progression | Reject before commit | Required artifact/evidence and unchanged funnel state |
| Private export | Filesystem/write failure | Preserve committed SQLite facts; mark projection stale | Record ID and regeneration command |
| Projection validation | Invalid JSON/schema | Do not replace last valid projection | Validation details and stale warning |

No catch-all exception handling. Rescued failures must either reject before write, retry within a bound, preserve primary evidence with a loud degraded state, or re-raise with context.

## Failure-modes registry

| Codepath | Failure mode | Rescued? | Test? | Silent? | Logged/output? |
|---|---|---:|---:|---:|---:|
| CLI | Empty/invalid input | Yes | Yes | No | Yes |
| CLI | Duplicate attempt/event ID | Yes | Yes | No | Yes |
| Validation | Pain >1 without grade A/B evidence | Yes | Yes | No | Yes |
| Validation | Commercial milestone without required artifact | Yes | Yes | No | Yes |
| Validation | Oversized/Unicode text | Yes | Yes | No | Yes |
| Atomic write | Attempt/event commits but suppression/stage does not | Fail transaction | Yes | No | Yes |
| Suppression | Attempt after opt-out | Yes | Yes | No | Yes |
| SQLite | Lock timeout | Yes | Yes | No | Yes |
| Export | Failure after committed SQLite facts | Yes | Yes | No | Yes |
| Redaction | Raw evidence or `outreach_*` metric reaches public output | Fail closed | Yes | No | Yes |
| Metrics | Zero denominator | Omit rate row; render inconclusive | Yes | No | Yes |

No critical silent gaps are accepted for implementation.

## Verification requirements

Before outreach uses the new logger:

1. Unit tests for enums, evidence grades, artifact requirements, suppression, deterministic stage mapping, and metric denominators.
2. CLI integration tests for both `attempt` and `commercial-event` subcommands against a temporary SQLite database.
3. Atomicity tests proving attempt/event, suppression, and funnel state commit together or not at all.
4. Unknown, archived, suppressed, duplicate, contradictory, oversized, Unicode, and zero-denominator cases.
5. SQLite five-second lock-timeout and post-commit export-failure tests.
6. Public-redaction tests proving no company name, phone, email, quote, objection, price, artifact path, raw attempt/event row, or `outreach_*` metric leaks.
7. Existing full test suite.
8. Generator run without modifying `generate.py`.
9. CLI smoke using test data, not a real prospect.
10. Verify only the intended implementation files changed.

## Deployment and rollback

```text
tests pass
   |
backup ignored local SQLite file
   |
run additive init_db
   |
CLI smoke with test data
   |
verify private metrics + public safety
   |
use for outreach

Failure
   +--> revert code
   +--> restore DB backup only if migration damaged data
   +--> never delete recorded buyer evidence just to roll back code
```

The two narrow tables and suppression columns are additive. Old code can ignore them. No destructive down-migration is required.

## What already exists and will be reused

- Existing SQLite prospect source of truth and direct schema initialization in `ops-crm/db.py`.
- Existing monotonic funnel transition path.
- Existing metrics definition site in `ops-crm/db.py`.
- Existing private/public generator and fail-closed redaction seam.
- Existing JSON schemas and test structure.
- Existing funnel/runtime controls and private operator surface.
- Existing Columbus roofing records in the local database.

The implementation must extend these seams rather than create a second CRM, spreadsheet, service layer, or status system.

## Intended implementation surface

The engineering implementation should remain within seven code/documentation files:

1. `ops-crm/db.py`;
2. new `ops-crm/record_outreach.py`;
3. `ops-crm/redaction.py`;
4. `ops-crm/README.md`;
5. `ops-crm/tests/test_db.py`;
6. new `ops-crm/tests/test_record_outreach.py`;
7. `ops-crm/tests/test_redaction.py`.

No change is intended for `generate.py`, `serve.py`, dashboard HTML, JSON schemas, or generated data files. Offer collateral and private prospect preparation remain separate business-operation tasks, not part of the evidence-logger code change.

## Explicitly not in scope

- SaaS or public dashboard UI.
- Dashboard logging form.
- Generic interactions/event framework or event sourcing. The narrow `commercial_events` table is limited to this paid-validation lifecycle.
- External CRM integration.
- Background jobs or queues.
- AI voice, prerecorded voice, autodialing, or Vapi tuning.
- Cold SMS, mass email, or automated sequences.
- Multi-city or multi-trade cohort before the gate.
- Pricing experiments.
- Free pilot by default.
- Revenue guarantee.
- Statistical-validity claims.
- Product build after only positive conversations.
- Client fulfillment platform beyond the manual founding-pilot path.

## Dream-state delta

The 12-month ideal is a private operator system whose workflows, metrics, and automation are all earned by real sales and fulfillment evidence. V1 does not build that platform. It adds two narrow factual insert paths—attempts and commercial milestones—plus durable suppression and honest denominators so the next architecture decision is based on buyer behavior and payment.

Reversibility: **5/5**. The narrow `outreach_attempts` and `commercial_events` tables are useful if the experiment works and harmless if it does not. Future client-lead interaction history remains a separate domain learned from fulfillment.

## Stale diagram audit

This plan replaces the prior single-row “evidence table” concept with an attempt-level architecture. No repository diagram outside this plan is authorized for modification in the plan-only phase. During implementation, inspect diagrams in any touched file and update only those made inaccurate by the additive logger.

## Implementation tasks

Synthesized from this review. Do not execute until Matthew separately approves implementation.

**Parallelization:** Sequential implementation, no worktree parallelization opportunity. T1–T5 share the `ops-crm` database contracts and tests; splitting them would create avoidable interface and merge conflicts. T6–T7 remain gated business-operation tasks after the logger passes verification.

- [ ] **T1 (P1, human: ~2h / agent: ~20m)** — Database — Add `outreach_attempts`, `commercial_events`, durable prospect suppression, and shared non-committing stage transitions.
  - Surfaced by: Architecture/Data review — mutable prospect snapshots lose attempt history, later milestones do not belong on call rows, and notes do not enforce opt-outs.
  - Files: `ops-crm/db.py`, focused DB tests.
  - Verify: additive initialization, atomic writes, suppression block, evidence-derived funnel mapping.
- [ ] **T2 (P1, human: ~3h / agent: ~30m)** — Operator CLI — Build guided `attempt` and `commercial-event` logging with strict validation.
  - Surfaced by: Security/Error review — shell flags leak quotes and permissive input corrupts a tiny sample.
  - Files: `ops-crm/record_outreach.py`, CLI integration tests.
  - Verify: valid attempt/event writes plus every named rejection/error path.
- [ ] **T3 (P1, human: ~2h / agent: ~20m)** — Metrics — Derive attempt, reach, pain, buyer-movement, and paid metrics with explicit denominators.
  - Surfaced by: CEO review — original OR gates and mixed outcomes cannot support decisions.
  - Files: `ops-crm/db.py`, DB/CLI tests.
  - Verify: numerator/denominator counts are explicit, zero denominators omit rate rows, and business counts are distinct.
- [ ] **T4 (P1, human: ~2h / agent: ~20m)** — Privacy — Prove attempt evidence cannot cross the public seam.
  - Surfaced by: Security review — private quotes and prospect identity are high-impact data.
  - Files: `ops-crm/redaction.py`, redaction tests.
  - Verify: explicit public metric allowlist; no raw attempt/event rows, `outreach_*` metrics, or sensitive fields cross.
- [ ] **T5 (P1, human: ~1h / agent: ~10m)** — Reliability — Handle lock, duplicate, and post-commit export failures explicitly.
  - Surfaced by: Error/Data review — primary evidence must survive projection failure without silent staleness.
  - Files: CLI/DB error handling and tests.
  - Verify: committed record ID and regeneration command appear after forced export failure.
- [ ] **T6 (P1, human: ~1h / agent: ~10m)** — Offer — Create the truthful one-page paid-pilot brief and sanitized daily handoff sample.
  - Surfaced by: Outside voice — cold revenue-recovery outreach needs a concrete trust artifact.
  - Files: offer collateral under the existing offer/docs structure.
  - Verify: exact price, scope, consent condition, no fabricated proof, concrete CTA.
- [ ] **T7 (P1, human: ~1h / agent: ~10m)** — Operations — Verify ten Columbus roofing prospects and prepare the first compliant attempt batch.
  - Surfaced by: Scope reduction — only four new prospects are needed before selling.
  - Files: private CRM data only; no public identity leakage.
  - Verify: ten ICP-fit records, suppression checks, fit evidence, local calling windows.

## Approval boundary

This revision approves the **scope and plan document only**.

Separate approval is still required before:

- implementing T1–T7;
- changing code, schemas, CRM data, public exports, or offer collateral;
- starting outbound contact;
- using AI voice or SMS;
- accepting a free pilot;
- changing the ICP, price, or sequential gates;
- building any dashboard/product UI.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|---|---|---|---:|---|---|
| CEO Review | `/plan-ceo-review` | Scope and strategy | 1 | CLEAR | Scope reduced to a gated ten-business Columbus-roofing paid-validation loop; 0 critical gaps |
| Outside Voice | automatic independent challenge | Plan challenge | 2 | ISSUES ADDRESSED | CEO Codex pass plus engineering Claude fallback; paid-outcome, falsifiability, evidence, and infrastructure findings resolved or explicitly rejected |
| Eng Review | `/plan-eng-review` | Architecture and tests; required shipping gate | 1 | CLEAR | 6 issues found and folded; 0 critical gaps; 0 unresolved decisions |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | NOT REQUIRED | No UI scope |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | NOT REQUIRED | One local guided CLI using existing project conventions |

- **CODEX:** The CEO outside voice supported the ten-business paid gate while challenging measurement infrastructure; the engineering pass used the required Claude fallback because Codex was unavailable.
- **CROSS-MODEL:** All reviewers converged on payment evidence, one narrow ICP, attempt-level history, explicit denominators, and no SaaS theater. The spreadsheet-versus-SQLite tension was resolved by retaining the repository’s existing private SQLite source of truth while limiting implementation to seven files, two narrow fact tables, private aggregates, and a fail-closed public metric allowlist.
- **VERDICT:** CEO + ENG CLEARED. Matthew approved and the T1–T7 implementation passed final verification. Separate authorization remains required before changing live outreach collateral or starting outbound activity.

NO UNRESOLVED DECISIONS
