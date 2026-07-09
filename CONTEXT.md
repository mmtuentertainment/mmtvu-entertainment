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
