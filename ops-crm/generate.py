#!/usr/bin/env python3
"""Generate the MMTVU Operator CRM static data.

Public interface:
  uv run python ops-crm/generate.py
  python3 ops-crm/generate.py --root /path/to/repo

The generator is intentionally local-first. It reads existing repo artifacts,
normalizes them into private JSON, derives public redacted JSON from the private
records, validates both with JSON Schema, and writes a summary for the static UI.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db as crm_db
from redaction import derive_public, slugify

from jsonschema import Draft202012Validator

SOURCE_PRECEDENCE = {
    "outreach/owner-operated-vapi-prospects.json": 40,
    "outreach/high-priority-contacts.json": 30,
    "outreach/outreach-list-50.json": 20,
    "outreach/outreach-list-50.csv": 10,
}

@dataclass(frozen=True)
class SourceMeta:
    path: str
    sha256: str
    mtime: str


def source_meta(root: Path, rel_path: str) -> SourceMeta:
    path = root / rel_path
    data = path.read_bytes()
    return SourceMeta(
        path=rel_path,
        sha256=hashlib.sha256(data).hexdigest(),
        mtime=datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    )


def existing_sources(root: Path, paths: list[str]) -> dict[str, SourceMeta]:
    out: dict[str, SourceMeta] = {}
    for p in paths:
        if (root / p).exists():
            out[p] = source_meta(root, p)
    return out


def read_json(root: Path, rel_path: str, default: Any) -> Any:
    p = root / rel_path
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def read_csv(root: Path, rel_path: str) -> list[dict[str, str]]:
    p = root / rel_path
    if not p.exists():
        return []
    with p.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def norm_priority(value: Any) -> str:
    v = str(value or "").strip().lower()
    return "high" if v == "high" else "medium" if v == "medium" else "low"


def funnel_status(raw: Any) -> str:
    """Map a source-sheet status ("Not contacted", "Discovery booked", …) to the funnel vocabulary.

    Unknown values raise: a typo silently landing in the wrong funnel bucket would
    corrupt the outreach-progress counts.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", str(raw or "").strip().lower()).strip("_")
    if not slug:
        return "not_contacted"
    slug = crm_db.LEGACY_PROSPECT_STATUS_MAP.get(slug, slug)
    if slug not in crm_db.FUNNEL_STATUSES:
        raise ValueError(f"unknown prospect status {raw!r}; expected one of {', '.join(crm_db.FUNNEL_STATUSES)}")
    return slug


def prospect_id(record: dict[str, Any]) -> str:
    return slugify(str(record.get("name") or record.get("company_name") or ""), str(record.get("city") or ""))


def merge_prospect(existing: dict[str, Any] | None, incoming: dict[str, Any], source: str) -> dict[str, Any]:
    inc_rank = SOURCE_PRECEDENCE[source]
    if existing is None:
        out = deepcopy(incoming)
        out["_precedence_rank"] = inc_rank
        out["source_precedence"] = f"{source} won as initial source"
        return out
    out = deepcopy(existing)
    out.setdefault("source_paths", [])
    for p in incoming.get("source_paths", []):
        if p not in out["source_paths"]:
            out["source_paths"].append(p)
    if inc_rank >= out.get("_precedence_rank", 0):
        for key, value in incoming.items():
            if key == "source_paths":
                continue
            if value not in (None, "", []):
                out[key] = value
        out["_precedence_rank"] = inc_rank
        out["source_precedence"] = f"{source} won by source precedence"
    else:
        out["source_precedence"] = out.get("source_precedence") or "existing record won by source precedence"
    return out


def build_prospects(root: Path, generated_at: str) -> list[dict[str, Any]]:
    sources = [
        "outreach/outreach-list-50.csv",
        "outreach/outreach-list-50.json",
        "outreach/high-priority-contacts.json",
        "outreach/owner-operated-vapi-prospects.json",
    ]
    metas = existing_sources(root, sources)
    merged: dict[str, dict[str, Any]] = {}

    for row in read_csv(root, "outreach/outreach-list-50.csv"):
        rec = {
            "id": prospect_id(row),
            "company_name": row.get("name") or "",
            "city": row.get("city") or "",
            "niche": row.get("niche") or "",
            "phone": row.get("phone") or None,
            "all_phones": [],
            "email": row.get("email") or None,
            "website": row.get("website") or None,
            "priority": norm_priority(row.get("priority")),
            "status": funnel_status(row.get("status")),
            "last_touch_at": row.get("outreach_date") or None,
            "next_action_id": None,
            "owner_operator": False,
            "source_paths": ["outreach/outreach-list-50.csv"],
            "evidence_link": "outreach/outreach-list-50.csv",
            "generated_at": generated_at,
            "source_hashes": {p: m.sha256 for p, m in metas.items() if p == "outreach/outreach-list-50.csv"},
        }
        merged[rec["id"]] = merge_prospect(merged.get(rec["id"]), rec, "outreach/outreach-list-50.csv")

    list_json = read_json(root, "outreach/outreach-list-50.json", [])
    if isinstance(list_json, list):
        for row in list_json:
            if not isinstance(row, dict):
                continue
            rec = {
                "id": prospect_id(row),
                "company_name": row.get("name") or row.get("company_name") or "",
                "city": row.get("city") or "",
                "niche": row.get("niche") or "",
                "phone": row.get("phone") or None,
                "all_phones": row.get("all_phones") or [],
                "email": row.get("email") or None,
                "website": row.get("website") or None,
                "priority": norm_priority(row.get("priority")),
                "status": funnel_status(row.get("status")),
                "last_touch_at": None,
                "next_action_id": None,
                "owner_operator": False,
                "source_paths": ["outreach/outreach-list-50.json"],
                "evidence_link": "outreach/outreach-list-50.json",
                "generated_at": generated_at,
                "source_hashes": {p: m.sha256 for p, m in metas.items() if p == "outreach/outreach-list-50.json"},
            }
            merged[rec["id"]] = merge_prospect(merged.get(rec["id"]), rec, "outreach/outreach-list-50.json")

    high = read_json(root, "outreach/high-priority-contacts.json", {})
    for row in high.get("contacts", []) if isinstance(high, dict) else []:
        rec = {
            "id": prospect_id(row),
            "company_name": row.get("name") or "",
            "city": row.get("city") or "",
            "niche": row.get("niche") or "",
            "phone": row.get("phone") or None,
            "all_phones": [row.get("phone")] if row.get("phone") else [],
            "email": row.get("email") or None,
            "website": row.get("website") or None,
            "priority": "high",
            "status": "not_contacted",
            "last_touch_at": None,
            "next_action_id": None,
            "owner_operator": False,
            "source_paths": ["outreach/high-priority-contacts.json"],
            "evidence_link": "outreach/high-priority-contacts.json",
            "generated_at": generated_at,
            "source_hashes": {p: m.sha256 for p, m in metas.items() if p == "outreach/high-priority-contacts.json"},
        }
        merged[rec["id"]] = merge_prospect(merged.get(rec["id"]), rec, "outreach/high-priority-contacts.json")

    owner = read_json(root, "outreach/owner-operated-vapi-prospects.json", [])
    if isinstance(owner, list):
        for row in owner:
            if not isinstance(row, dict):
                continue
            rec = {
                "id": prospect_id(row),
                "company_name": row.get("name") or "",
                "city": row.get("city") or "",
                "niche": row.get("niche") or "",
                "phone": row.get("phone") or None,
                "all_phones": row.get("all_phones") or [],
                "email": row.get("email") or None,
                "website": row.get("website") or None,
                "priority": norm_priority(row.get("priority")),
                "status": "contacted" if row.get("called_2026_07_08_after_hours") else "not_contacted",
                "last_touch_at": "2026-07-08" if row.get("called_2026_07_08_after_hours") else None,
                "next_action_id": None,
                "owner_operator": True,
                "owner_operator_reason": row.get("owner_operator_reason") or "Owner-operated target",
                "source_paths": ["outreach/owner-operated-vapi-prospects.json"],
                "evidence_link": "outreach/owner-operated-vapi-prospects.json",
                "generated_at": generated_at,
                "source_hashes": {p: m.sha256 for p, m in metas.items() if p == "outreach/owner-operated-vapi-prospects.json"},
            }
            merged[rec["id"]] = merge_prospect(merged.get(rec["id"]), rec, "outreach/owner-operated-vapi-prospects.json")

    prospects = []
    for rec in merged.values():
        rec.pop("_precedence_rank", None)
        rec["source_hashes"] = {p: metas[p].sha256 for p in rec.get("source_paths", []) if p in metas}
        prospects.append(rec)
    return sorted(prospects, key=lambda r: ({"high": 0, "medium": 1, "low": 2}[r["priority"]], not r.get("owner_operator"), r["company_name"]))


def build_assets(root: Path, generated_at: str) -> list[dict[str, Any]]:
    specs = [
        ("offer-doc-home-service-ai-follow-up", "offer_doc", "Home Service AI Follow-Up Starter", "offers/home-service-ai-follow-up-starter.md", "Defines the first monetizable offer and target customer."),
        ("landing-page-home-service-offer", "landing_page", "Home-service follow-up landing page", "landing/index.html", "Public conversion asset for the offer."),
        ("report-first-vapi-campaign", "report", "First Vapi cold-call campaign report", "outreach/vapi-campaign-report.md", "Shows IVR/DTMF failure and big-company calling lessons."),
        ("report-owner-operated-vapi", "report", "Owner-operated Vapi after-hours report", "outreach/owner-operated-vapi-report.md", "Evidence that owner-operated prospects are a better next call target."),
        ("script-business-hours-calls", "script", "Owner-operated business-hours call runner", "outreach/run-owner-operated-business-hours-calls.sh", "Prepared next campaign script for the strongest follow-up action."),
        ("policy-vapi-cost-control", "report", "Vapi cost-control fix", "outreach/vapi-cost-control-fix.md", "Documents call-cost guardrails after early campaign learning."),
        ("assistant-owner-operator-config", "assistant", "Owner-operator Vapi assistant config", "outreach/vapi-owner-operator-cost-controlled-config.json", "Current assistant behavior for smaller local prospects."),
    ]
    assets = []
    for asset_id, typ, name, path, why in specs:
        if not (root / path).exists():
            continue
        meta = source_meta(root, path)
        assets.append({
            "id": asset_id,
            "type": typ,
            "name": name,
            "path_or_url": path,
            "status": "active",
            "last_touched_at": meta.mtime,
            "why_it_matters": why,
            "source_paths": [path],
            "evidence_link": path,
            "generated_at": generated_at,
            "source_hashes": {path: meta.sha256},
        })
    return assets


def load_call_summaries(root: Path) -> dict[str, dict[str, Any]]:
    out = {}
    for rel_path in ["outreach/owner-operated-call-summaries.json", "outreach/vapi-call-summaries.json"]:
        rows = read_json(root, rel_path, [])
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and row.get("business"):
                    row = deepcopy(row)
                    row["source_path"] = rel_path
                    out[row["business"].lower()] = row
    return out


def action_score(prospect: dict[str, Any], call: dict[str, Any] | None) -> float:
    score = 10.0
    if prospect.get("owner_operator"):
        score += 25
    if prospect.get("status") == "contacted":
        score += 30
    if prospect.get("priority") == "high":
        score += 12
    elif prospect.get("priority") == "medium":
        score += 6
    if call:
        text = f"{call.get('summary') or ''} {call.get('endedReason') or ''}".lower()
        if "closed" in text or "business hours" in text:
            score += 35
        if "voicemail" in text:
            score += 25
        if "live agent" in text or "real person" in text:
            score += 15
        if call.get("cost", 0):
            score += min(float(call.get("cost") or 0) * 10, 5)
    return round(score, 2)


def build_next_actions(root: Path, prospects: list[dict[str, Any]], assets: list[dict[str, Any]], generated_at: str) -> list[dict[str, Any]]:
    calls = load_call_summaries(root)
    metas = existing_sources(root, ["outreach/owner-operated-vapi-report.md", "outreach/owner-operated-call-summaries.json", "outreach/vapi-campaign-report.md", "outreach/vapi-call-summaries.json", "outreach/run-owner-operated-business-hours-calls.sh"])
    actions: list[dict[str, Any]] = []
    for p in prospects:
        call = calls.get(p["company_name"].lower())
        if p.get("owner_operator") or call:
            score = action_score(p, call)
            summary_text = str(call.get("summary", "")).lower() if call else ""
            if call and ("closed" in summary_text or "business hours" in summary_text):
                action = f"Retry {p['company_name']} during business hours"
                owner = "Hermes"
                reason = "After-hours call hit a closed-office flow; the assistant said it would try again during business hours."
                due = "10:00 ET next business day"
            elif call and "voicemail" in summary_text:
                action = f"Send a short email fallback to {p['company_name']} after voicemail"
                owner = "Matthew"
                reason = "Voicemail was reached; the report says the AI offered to have Matthew send one short email."
                due = "next business day"
            elif call and "live agent" in str(call.get("summary", "")).lower():
                action = f"Prepare direct follow-up angle for {p['company_name']}"
                owner = "Both"
                reason = "The first batch reached a live agent but failed to qualify before the call ended."
                due = "this week"
            else:
                action = f"Call {p['company_name']} in the owner-operated business-hours batch"
                owner = "Hermes"
                reason = "Owner-operated local prospects are the best next segment after large-company IVR failures."
                due = "10:00 ET next business day"
            source_paths = ["outreach/owner-operated-vapi-prospects.json"]
            if call:
                source_paths.append(call["source_path"])
            source_paths.append("outreach/owner-operated-vapi-report.md")
            source_hashes = {sp: source_meta(root, sp).sha256 for sp in sorted(set(source_paths)) if (root / sp).exists()}
            priority = "high" if score >= 70 else "medium" if score >= 45 else "low"
            actions.append({
                "id": "action-" + p["id"],
                "owner": owner,
                "action": action,
                "priority": priority,
                "score": score,
                "due_at": due,
                "reason": reason,
                "expected_revenue_path": "Better contact → reply or booked call → home-service follow-up starter sale",
                "evidence_link": "outreach/owner-operated-vapi-report.md",
                "status": "open",
                "source_entity_type": "prospect",
                "source_entity_id": p["id"],
                "source_paths": sorted(set(source_paths)),
                "generated_at": generated_at,
                "source_hashes": source_hashes,
            })
    # Add privacy/control action from the design review.
    redaction_sources = ["outreach/vapi-cost-control-fix.md"]
    if (root / redaction_sources[0]).exists():
        meta = source_meta(root, redaction_sources[0])
        actions.append({
            "id": "action-check-public-redaction-before-publish",
            "owner": "Hermes",
            "action": "Verify public CRM redaction before any GitHub Pages publishing",
            "priority": "high",
            "score": 72,
            "due_at": "before public publish",
            "reason": "The CRM contains prospect data and call evidence; public output must prove that sensitive fields are removed.",
            "expected_revenue_path": "Protect trust and keep public status useful without leaking prospect/campaign details",
            "evidence_link": "outreach/vapi-cost-control-fix.md",
            "status": "open",
            "source_entity_type": "asset",
            "source_entity_id": "policy-vapi-cost-control",
            "source_paths": redaction_sources,
            "generated_at": generated_at,
            "source_hashes": {meta.path: meta.sha256},
        })
    actions.sort(key=lambda a: (-a["score"], a["due_at"], a["action"]))
    for p in prospects:
        for a in actions:
            if a["source_entity_type"] == "prospect" and a["source_entity_id"] == p["id"]:
                p["next_action_id"] = a["id"]
                break
    return actions


def build_offer(root: Path, generated_at: str) -> list[dict[str, Any]]:
    paths = ["offers/home-service-ai-follow-up-starter.md", "landing/index.html"]
    metas = existing_sources(root, paths)
    return [{
        "id": "home-service-ai-follow-up-starter",
        "name": "Home Service AI Follow-Up Starter",
        "price_or_pricing_notes": "$300/month positioning in outreach copy; one recovered job should cover it.",
        "target_customer": "Owner-operated or local home-service businesses that miss quote/estimate follow-up.",
        "current_public_url": "landing/index.html",
        "source_paths": [p for p in paths if p in metas],
        "evidence_link": "offers/home-service-ai-follow-up-starter.md",
        "generated_at": generated_at,
        "source_hashes": {p: m.sha256 for p, m in metas.items()},
    }]


def build_evidence(root: Path, generated_at: str) -> list[dict[str, Any]]:
    # cost_usd is the structured spend record; db.compute_metrics sums it for
    # documented_call_spend instead of regex-mining the summary prose.
    specs = [
        ("evidence-owner-operated-report", "outreach/owner-operated-vapi-report.md", "Owner-operated after-hours test: 3 calls, $0.4506, business-hours retry recommended.", 0.4506),
        ("evidence-first-vapi-report", "outreach/vapi-campaign-report.md", "First Vapi campaign: 7 calls, about $1.06, IVR/DTMF is the blocker.", 1.06),
        ("evidence-business-hours-script", "outreach/run-owner-operated-business-hours-calls.sh", "Prepared script for the seven remaining owner-operated prospects.", None),
        ("evidence-offer-doc", "offers/home-service-ai-follow-up-starter.md", "Offer source for target customer and positioning.", None),
        ("evidence-landing-page", "landing/index.html", "Public landing page asset exists.", None),
    ]
    out = []
    for eid, path, summary, cost_usd in specs:
        if (root / path).exists():
            meta = source_meta(root, path)
            record = {"id": eid, "summary": summary, "evidence_link": path, "source_paths": [path], "generated_at": generated_at, "source_hashes": {path: meta.sha256}, "source_mtime": meta.mtime}
            if cost_usd is not None:
                record["cost_usd"] = cost_usd
            out.append(record)
    return out


def validate_records(root: Path, dataset: dict[str, list[dict[str, Any]]]) -> None:
    schema_dir = root / "ops-crm" / "schemas"
    mapping = {
        "offers": "offer.schema.json",
        "prospects": "prospect.schema.json",
        "assets": "asset.schema.json",
        "next_actions": "next_action.schema.json",
    }
    for key, schema_name in mapping.items():
        schema = json.loads((schema_dir / schema_name).read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        for idx, rec in enumerate(dataset.get(key, [])):
            errors = sorted(validator.iter_errors(rec), key=lambda e: e.path)
            if errors:
                joined = "; ".join(f"{key}[{idx}].{'.'.join(map(str, e.path))}: {e.message}" for e in errors)
                raise ValueError(joined)
    prospect_ids = {p["id"] for p in dataset.get("prospects", [])}
    asset_ids = {a["id"] for a in dataset.get("assets", [])}
    offer_ids = {o["id"] for o in dataset.get("offers", [])}
    for action in dataset.get("next_actions", []):
        typ = action["source_entity_type"]
        sid = action["source_entity_id"]
        allowed = prospect_ids if typ == "prospect" else asset_ids if typ == "asset" else offer_ids
        if dataset.get("mode") == "public" and typ == "prospect" and sid == "redacted-prospect":
            continue
        if sid not in allowed:
            raise ValueError(f"next_actions.{action['id']} references missing {typ} {sid}")
    validate_revenue_os_records(dataset)


def require_keys(kind: str, records: list[dict[str, Any]], keys: list[str]) -> None:
    for idx, rec in enumerate(records):
        missing = [key for key in keys if key not in rec or rec[key] in (None, "")]
        if missing:
            raise ValueError(f"{kind}[{idx}] missing required keys: {', '.join(missing)}")


def validate_revenue_os_records(dataset: dict[str, Any]) -> None:
    require_keys("loops", dataset.get("loops", []), ["id", "name", "goal", "stage", "status", "created_at", "updated_at"])
    require_keys("experiments", dataset.get("experiments", []), ["id", "hypothesis", "metric", "status", "created_at", "updated_at"])
    require_keys("metrics", dataset.get("metrics", []), ["id", "metric_name", "metric_value", "unit", "measured_at", "source"])
    if "summary" in dataset:
        summary = dataset["summary"]
        for key in ["prospects", "next_actions", "top_action_id"]:
            if key not in summary:
                raise ValueError(f"summary missing required key: {key}")
        if (dataset.get("loops") or dataset.get("metrics")) and "next_best_move" not in summary:
            raise ValueError("summary missing required key: next_best_move")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def build_source_dataset(root: Path, generated_at: str | None = None) -> dict[str, Any]:
    """Derive a private dataset from repo artifacts before persisting it to SQLite."""
    generated_at = generated_at or crm_db.utc_now()
    offers = build_offer(root, generated_at)
    prospects = build_prospects(root, generated_at)
    assets = build_assets(root, generated_at)
    actions = build_next_actions(root, prospects, assets, generated_at)
    evidence = build_evidence(root, generated_at)
    return {
        "mode": "private",
        "generated_at": generated_at,
        "offers": offers,
        "prospects": prospects,
        "assets": assets,
        "next_actions": actions,
        "evidence": evidence,
        "summary": {
            "prospects": len(prospects),
            "assets": len(assets),
            "next_actions": len(actions),
            "evidence": len(evidence),
            "top_action_id": actions[0]["id"] if actions else None,
        },
    }


def generate(root: Path, db_file: Path | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Generate dashboard data through SQLite as the source of truth."""
    generated_at = crm_db.utc_now()
    source_private = build_source_dataset(root, generated_at)
    validate_records(root, source_private)

    with crm_db.connect(root, db_file) as conn:
        crm_db.init_db(conn)
        crm_db.import_dataset(conn, source_private)
        private = crm_db.export_dataset(conn, generated_at)

    validate_records(root, private)
    public = derive_public(private)
    validate_records(root, public)
    return private, public


def refresh_exports(root: Path, db_file: Path | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Re-export dashboard JSON from SQLite without re-importing source artifacts.

    The status write path (serve.py) calls this so an operator status change
    lands in data/ — and in the recomputed funnel metrics — immediately.
    """
    with crm_db.connect(root, db_file) as conn:
        private = crm_db.export_dataset(conn)
    validate_records(root, private)
    public = derive_public(private)
    validate_records(root, public)
    write_outputs(root, private, public)
    return private, public


def write_outputs(root: Path, private: dict[str, Any], public: dict[str, Any]) -> None:
    base = root / "ops-crm" / "data"
    for mode, dataset in [("private", private), ("public", public)]:
        mode_dir = base / mode
        write_json(mode_dir / "summary.json", dataset)
        for key in ["offers", "prospects", "assets", "next_actions", "evidence", "loops", "experiments", "metrics"]:
            write_json(mode_dir / f"{key}.json", dataset.get(key, []))
        (mode_dir / "daily-brief.md").write_text(crm_db.daily_brief(dataset), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate MMTVU Operator CRM data")
    parser.add_argument("--root", default=Path(__file__).resolve().parents[1], type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    private, public = generate(root)
    write_outputs(root, private, public)
    print(f"Generated CRM data at {root / 'ops-crm' / 'data'}")
    print(f"Top action: {private['next_actions'][0]['action'] if private['next_actions'] else 'none'}")
    print(f"Private records: prospects={len(private['prospects'])} assets={len(private['assets'])} actions={len(private['next_actions'])} evidence={len(private['evidence'])}")
    print("Public redaction: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
