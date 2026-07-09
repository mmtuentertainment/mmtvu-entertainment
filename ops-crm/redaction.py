#!/usr/bin/env python3
"""The privacy seam between private CRM data and the public dashboard export.

Interface: derive_public(private) -> public. Every rule about what may leave
the private dataset lives in this module; assert_public_safe is the
whole-output backstop net.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

SENSITIVE_KEYS = {"phone", "email", "all_phones", "transcript", "call_id", "id_raw"}
PHONE_RE = re.compile(r"(?:\+\d{3}\*{2,}\d{4}|\(?\b\d{3}\)?[\s.-]+\d{3}[\s.-]+\d{4}\b)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
CREDENTIAL_RE = re.compile(r"(?i)(api[_ -]?key|token|secret|authorization|bearer)[:=]\s*\S+")


def slugify(*parts: str) -> str:
    raw = "-".join(p for p in parts if p)
    raw = raw.lower().replace("&", " and ")
    raw = re.sub(r"[^a-z0-9]+", "-", raw)
    return raw.strip("-") or "unknown"


def anonymize_id(original_id: str, prefix: str) -> str:
    """Deterministically anonymize an ID to a short hash."""
    if not original_id:
        return f"{prefix}-unknown"
    return f"{prefix}-{hashlib.sha256(original_id.encode('utf-8')).hexdigest()[:12]}"


def redact_text(value: str) -> str:
    value = PHONE_RE.sub("[redacted-phone]", value)
    value = EMAIL_RE.sub("[redacted-email]", value)
    value = CREDENTIAL_RE.sub("[redacted-secret]", value)
    return value


def redact_record(value: Any, *, anonymize_company: bool = False) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in SENSITIVE_KEYS:
                continue
            if k == "company_name" and anonymize_company:
                out["company_name"] = "Local prospect"
                continue
            if k == "website" and anonymize_company:
                continue
            if k == "source_hashes":
                # Keep the key for schema compliance but drop private path keys
                out[k] = {}
                continue
            if k == "source_precedence":
                # Required by prospect schema, but original value can expose private source paths.
                out[k] = "private-source-redacted"
                continue
            out[k] = redact_record(v, anonymize_company=anonymize_company)
        return out
    if isinstance(value, list):
        return [redact_record(v, anonymize_company=anonymize_company) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def anonymize_public_action(action: dict[str, Any], company_names: list[str], prospect_id_map: dict[str, str]) -> dict[str, Any]:
    public_action = redact_record(action)
    original_action = str(action.get("action", ""))
    for name in company_names:
        public_action = replace_sensitive_name(public_action, name)
    # Preserve useful public operational status without leaking prospect identity.
    if original_action.startswith("Retry ") and " during business hours" in original_action:
        public_action["action"] = "Retry a local prospect during business hours"
    elif "after voicemail" in original_action:
        public_action["action"] = "Send a short email fallback after voicemail"
    elif original_action.startswith("Prepare direct follow-up angle"):
        public_action["action"] = "Prepare a direct follow-up angle for a reached prospect"
    elif "owner-operated business-hours batch" in original_action:
        public_action["action"] = "Call a local owner-operated prospect in the business-hours batch"
    public_action["source_entity_id"] = prospect_id_map.get(action.get("source_entity_id", ""), "redacted-prospect") if action.get("source_entity_type") == "prospect" else public_action.get("source_entity_id")
    # Always redact source fields for public output regardless of entity type
    public_action["evidence_link"] = PRIVATE_SOURCE
    public_action["source_paths"] = [PRIVATE_SOURCE]
    public_action["source_hashes"] = {}
    return public_action


def replace_sensitive_name(value: Any, name: str) -> Any:
    if not name:
        return value
    if isinstance(value, dict):
        return {k: replace_sensitive_name(v, name) for k, v in value.items()}
    if isinstance(value, list):
        return [replace_sensitive_name(v, name) for v in value]
    if isinstance(value, str):
        return value.replace(name, "a local prospect")
    return value


PRIVATE_SOURCE = "private-source-redacted"

# Every top-level collection derive_public knows how to publish. A private
# dataset containing anything else is a hard error: new collections must get
# an explicit publish rule here before they can reach the public export.
REBUILT_COLLECTIONS = ("prospects", "next_actions", "assets", "offers", "evidence")
SCRUBBED_COLLECTIONS = ("loops", "experiments", "metrics", "summary")
METADATA_KEYS = ("mode", "generated_at")


def _redact_collection(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Redact a collection whose records carry source paths and evidence links."""
    out = []
    for record in records:
        redacted = redact_record(record)
        redacted["source_paths"] = [PRIVATE_SOURCE]
        redacted["evidence_link"] = PRIVATE_SOURCE
        out.append(redacted)
    return out


def derive_public(private: dict[str, Any]) -> dict[str, Any]:
    unknown = set(private) - set(REBUILT_COLLECTIONS) - set(SCRUBBED_COLLECTIONS) - set(METADATA_KEYS)
    if unknown:
        raise ValueError("derive_public has no publish rule for: " + ", ".join(sorted(unknown)))

    # public starts empty: nothing crosses the seam without an explicit rule below
    public: dict[str, Any] = {"generated_at": private.get("generated_at")}
    company_names = [p.get("company_name", "") for p in private.get("prospects", [])]

    # Build deterministic ID mappings for prospects and actions
    prospect_ids = [p.get("id", "") for p in private.get("prospects", [])]
    action_ids = [a.get("id", "") for a in private.get("next_actions", [])]
    prospect_id_map = {pid: anonymize_id(pid, "prospect") for pid in prospect_ids}
    action_id_map = {aid: anonymize_id(aid, "action") for aid in action_ids}

    # Redact prospects and apply ID mapping
    public["prospects"] = []
    for p in private.get("prospects", []):
        redacted = redact_record(p, anonymize_company=True)
        old_id = redacted.get("id", "")
        new_id = prospect_id_map.get(old_id, old_id)
        redacted["id"] = new_id
        # Remap next_action_id if present
        old_next = redacted.get("next_action_id")
        if old_next:
            redacted["next_action_id"] = action_id_map.get(old_next, "redacted-action")
        # Redact source paths and evidence link
        redacted["source_paths"] = [PRIVATE_SOURCE]
        redacted["evidence_link"] = PRIVATE_SOURCE
        public["prospects"].append(redacted)

    # Redact actions and apply ID mapping
    public["next_actions"] = []
    for a in private.get("next_actions", []):
        redacted = anonymize_public_action(a, company_names, prospect_id_map)
        old_id = redacted.get("id", "")
        new_id = action_id_map.get(old_id, old_id)
        redacted["id"] = new_id
        public["next_actions"].append(redacted)

    # Scrub free-text collections with the same rules as everything else
    for key in SCRUBBED_COLLECTIONS:
        public[key] = redact_record(private.get(key, {} if key == "summary" else []))

    # Update summary cross-references
    if isinstance(public.get("summary"), dict):
        if public["next_actions"]:
            public["summary"]["next_best_move"] = public["next_actions"][0].get("action")
        old_top = public["summary"].get("top_action_id")
        if old_top:
            public["summary"]["top_action_id"] = action_id_map.get(old_top, old_top)

    # Redact assets, offers, evidence (drop source paths for public)
    for key in ("assets", "offers", "evidence"):
        public[key] = _redact_collection(private.get(key, []))

    public["mode"] = "public"
    public["privacy"] = {"status": "public_redacted", "note": "Derived from private data after schema validation; sensitive contact, prospect identity, and call-detail fields are omitted."}

    # Postcondition: derive_public never returns output that fails the backstop.
    assert_public_safe(public, private_company_names=company_names)
    return public


def assert_public_safe(public: dict[str, Any], *, private_company_names: list[str] | None = None) -> None:
    text = json.dumps(public, ensure_ascii=False)
    problems = []
    for name, pattern in [("phone", PHONE_RE), ("email", EMAIL_RE), ("credential", CREDENTIAL_RE)]:
        if pattern.search(text):
            problems.append(name)
    forbidden_keys = [f'"{k}"' for k in SENSITIVE_KEYS]
    for key in forbidden_keys:
        if key in text:
            problems.append(key)
    # Check for private company names and slug variants
    if private_company_names:
        for name in private_company_names:
            if not name:
                continue
            if name.lower() in text.lower():
                problems.append(f"company-name:{name}")
            # Check slugified variants
            slug = slugify(name)
            if slug and slug in text:
                problems.append(f"company-slug:{slug}")
    # Check for internal paths and sqlite references
    internal_patterns = [
        (".sqlite", "sqlite-reference"),
        ("outreach/owner-operated-call-summaries.json", "call-summaries-path"),
        ("outreach/high-priority-contacts", "high-priority-contacts-path"),
        ("outreach/outreach-list", "outreach-list-path"),
        ("ops-crm/data/private", "private-data-path"),
    ]
    for pattern, label in internal_patterns:
        if pattern in text:
            problems.append(label)
    if problems:
        raise ValueError("public output failed redaction checks: " + ", ".join(sorted(set(problems))))
