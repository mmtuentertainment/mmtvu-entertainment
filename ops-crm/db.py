#!/usr/bin/env python3
"""SQLite source-of-truth layer for the MMTVU Revenue OS.

The generator still knows how to derive records from repo artifacts, but V1 writes
those records through this module and exports dashboard JSON from SQLite. That
lets operator status, loops, experiments, and metrics
survive regeneration instead of being overwritten by generated JSON files.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DB_REL_PATH = Path("ops-crm") / "crm.sqlite"

# P0.7: Table whitelist for safe dynamic SQL in _existing_created_at
ALLOWED_TABLES = frozenset({
    "prospects",
    "actions",
    "artifacts",
    "loops",
    "experiments",
    "metrics",
})

# P0.6: Tables that get stale-record archival (is_current/archived_at columns)
ARCHIVAL_TABLES = frozenset({"prospects", "actions", "artifacts"})

# Outreach funnel vocabulary from docs/mmtvu-ai-automation-holding-company-design-2026-07-08.md
# (48-hour outreach logging sheet). Must match prospect.schema.json's status enum.
FUNNEL_STATUSES = (
    "not_contacted",
    "contacted",
    "replied",
    "discovery_booked",
    "pilot_proposed",
    "won",
    "lost",
    "not_fit",
    "follow_up_later",
)

# Pre-funnel statuses left behind by earlier schema versions. needs_follow_up maps to
# contacted: those prospects were called (touch attempted); the retry intent lives in
# their open actions, and "follow_up_later" in the doc means a post-conversation deferral.
LEGACY_PROSPECT_STATUS_MAP = {
    "new": "not_contacted",
    "needs_follow_up": "contacted",
    "booked": "discovery_booked",
    "customer": "won",
}

# Operator vocabulary for action rows. Must match next_action.schema.json's status enum.
ACTION_STATUSES = frozenset({"open", "doing", "done", "blocked", "cancelled"})

# Rank of the funnel's main progression line (not_contacted -> won). Off-ramps
# (lost, not_fit, follow_up_later) have no rank of their own: set_prospect_status
# and import never bump the persisted high-water mark for them, so a prospect's
# real progress survives an off-ramp instead of silently reversing (finding #5).
STAGE_RANK = {
    "not_contacted": 0,
    "contacted": 1,
    "replied": 2,
    "discovery_booked": 3,
    "pilot_proposed": 4,
    "won": 5,
}


def _effective_stage_rank(status: str, max_stage_rank: int) -> int:
    """Highest funnel stage a prospect has ever demonstrably reached.

    not_fit vetoes unconditionally — disqualifications must never inflate the
    outreach-progress numbers, even if the prospect had prior recorded progress.
    lost/follow_up_later fall back to "at least contacted" but otherwise defer to
    the persisted high-water mark, so a real discovery call or pilot pitch stays
    counted after an off-ramp.
    """
    if status == "not_fit":
        return -1
    floor = STAGE_RANK.get(status, STAGE_RANK["contacted"])
    return max(floor, max_stage_rank)

# 14-day success criteria from the design doc: identify 50, contact 30,
# book 5 discovery conversations, get 1 pilot.
FOURTEEN_DAY_TARGETS = {
    "prospects_total": 50.0,
    "contacted_total": 30.0,
    "discovery_booked_total": 5.0,
    "pilot_proposed_total": 1.0,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def db_path(root: Path) -> Path:
    return root / DB_REL_PATH


def connect(root: Path, path: Path | None = None) -> sqlite3.Connection:
    target = path or db_path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create all Revenue OS tables idempotently."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS prospects (
            id TEXT PRIMARY KEY,
            company_name TEXT NOT NULL,
            city TEXT,
            niche TEXT,
            website TEXT,
            priority TEXT NOT NULL,
            status TEXT NOT NULL,
            owner_operator INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            is_current INTEGER NOT NULL DEFAULT 1,
            archived_at TEXT,
            max_stage_rank INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS actions (
            id TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            action TEXT NOT NULL,
            status TEXT NOT NULL,
            priority TEXT NOT NULL,
            score REAL NOT NULL,
            due_at TEXT,
            reason TEXT,
            expected_revenue_path TEXT,
            source_entity_type TEXT,
            source_entity_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            is_current INTEGER NOT NULL DEFAULT 1,
            archived_at TEXT
        );

        CREATE TABLE IF NOT EXISTS loops (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            goal TEXT NOT NULL,
            stage TEXT NOT NULL,
            budget_cap REAL,
            success_signal TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS artifacts (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            name TEXT NOT NULL,
            path_or_url TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            is_current INTEGER NOT NULL DEFAULT 1,
            archived_at TEXT
        );

        CREATE TABLE IF NOT EXISTS experiments (
            id TEXT PRIMARY KEY,
            hypothesis TEXT NOT NULL,
            metric TEXT NOT NULL,
            budget_cap REAL,
            status TEXT NOT NULL,
            result TEXT,
            learning TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS metrics (
            id TEXT PRIMARY KEY,
            metric_name TEXT NOT NULL,
            metric_value REAL NOT NULL,
            unit TEXT NOT NULL,
            measured_at TEXT NOT NULL,
            source TEXT NOT NULL,
            target REAL
        );
        """
    )

    # P0.6: Add archival columns to existing tables if missing (migration)
    for table in ARCHIVAL_TABLES:
        cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "is_current" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN is_current INTEGER NOT NULL DEFAULT 1")
        if "archived_at" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN archived_at TEXT")

    # Funnel vocabulary migration: map legacy prospect statuses to the design doc's
    # outreach funnel and drop the retired money_signal_actions metric.
    for old, new in LEGACY_PROSPECT_STATUS_MAP.items():
        conn.execute("UPDATE prospects SET status = ? WHERE status = ?", (new, old))
    conn.execute("DELETE FROM metrics WHERE metric_name = 'money_signal_actions'")
    metric_cols = {row["name"] for row in conn.execute("PRAGMA table_info(metrics)")}
    if "target" not in metric_cols:
        conn.execute("ALTER TABLE metrics ADD COLUMN target REAL")

    prospect_cols = {row["name"] for row in conn.execute("PRAGMA table_info(prospects)")}
    if "max_stage_rank" not in prospect_cols:
        conn.execute("ALTER TABLE prospects ADD COLUMN max_stage_rank INTEGER NOT NULL DEFAULT 0")

    conn.commit()


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _load(value: str | None, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


def _existing_created_at(conn: sqlite3.Connection, table: str, record_id: str, fallback: str) -> str:
    # P0.7: Validate table name against whitelist to prevent SQL injection
    if table not in ALLOWED_TABLES:
        raise ValueError(f"unsupported table for created_at lookup: {table}")
    row = conn.execute(f"SELECT created_at FROM {table} WHERE id = ?", (record_id,)).fetchone()
    return row["created_at"] if row else fallback


def _existing_prospect_status(conn: sqlite3.Connection, prospect_id: str, fallback: str) -> str:
    """P0.1: Preserve operator-set prospect status on re-import."""
    row = conn.execute("SELECT status FROM prospects WHERE id = ?", (prospect_id,)).fetchone()
    return row["status"] if row and row["status"] else fallback


def _existing_action_status(conn: sqlite3.Connection, action_id: str, fallback: str) -> str:
    row = conn.execute("SELECT status FROM actions WHERE id = ?", (action_id,)).fetchone()
    return row["status"] if row and row["status"] else fallback


def import_dataset(conn: sqlite3.Connection, dataset: dict[str, Any]) -> None:
    """Upsert generated private records into SQLite without clobbering operator state."""
    init_db(conn)
    now = dataset.get("generated_at") or utc_now()

    # P0.1: Preserve prospect status
    prospect_ids = []
    for p in dataset.get("prospects", []):
        prospect_ids.append(p["id"])
        created = _existing_created_at(conn, "prospects", p["id"], now)
        # P0.1: Preserve operator-set prospect status
        preserved_status = _existing_prospect_status(conn, p["id"], p.get("status", "not_contacted"))
        # Seed the high-water mark for a brand-new prospect from its initial status;
        # an existing prospect's mark is preserved as-is (ON CONFLICT never touches
        # it), since it already reflects real set_prospect_status history.
        seed_rank = STAGE_RANK.get(preserved_status, 0)
        stored = dict(p)
        stored["status"] = preserved_status
        conn.execute(
            """
            INSERT INTO prospects(id, company_name, city, niche, website, priority, status, owner_operator, created_at, updated_at, payload_json, is_current, archived_at, max_stage_rank)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, ?)
            ON CONFLICT(id) DO UPDATE SET
              company_name=excluded.company_name,
              city=excluded.city,
              niche=excluded.niche,
              website=excluded.website,
              priority=excluded.priority,
              owner_operator=excluded.owner_operator,
              updated_at=excluded.updated_at,
              payload_json=excluded.payload_json,
              is_current=1,
              archived_at=NULL
            """,
            (
                p["id"],
                p.get("company_name", ""),
                p.get("city"),
                p.get("niche"),
                p.get("website"),
                p.get("priority", "low"),
                preserved_status,
                1 if p.get("owner_operator") else 0,
                created,
                now,
                _dump(stored),
                seed_rank,
            ),
        )

    # Preserve action status (existing pattern)
    action_ids = []
    for a in dataset.get("next_actions", []):
        action_ids.append(a["id"])
        preserved_status = _existing_action_status(conn, a["id"], a.get("status", "open"))
        stored = dict(a)
        stored["status"] = preserved_status
        created = _existing_created_at(conn, "actions", a["id"], now)
        conn.execute(
            """
            INSERT INTO actions(id, owner, action, status, priority, score, due_at, reason, expected_revenue_path, source_entity_type, source_entity_id, created_at, updated_at, payload_json, is_current, archived_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL)
            ON CONFLICT(id) DO UPDATE SET
              owner=excluded.owner,
              action=excluded.action,
              priority=excluded.priority,
              score=excluded.score,
              due_at=excluded.due_at,
              reason=excluded.reason,
              expected_revenue_path=excluded.expected_revenue_path,
              source_entity_type=excluded.source_entity_type,
              source_entity_id=excluded.source_entity_id,
              updated_at=excluded.updated_at,
              payload_json=excluded.payload_json,
              is_current=1,
              archived_at=NULL
            """,
            (
                a["id"],
                a.get("owner", "Hermes"),
                a.get("action", ""),
                preserved_status,
                a.get("priority", "low"),
                float(a.get("score", 0)),
                a.get("due_at"),
                a.get("reason"),
                a.get("expected_revenue_path"),
                a.get("source_entity_type"),
                a.get("source_entity_id"),
                created,
                now,
                _dump(stored),
            ),
        )

    # Artifacts (assets, offers, evidence) - preserve status and archival columns
    artifact_ids = []
    for asset in dataset.get("assets", []):
        artifact_ids.append(asset["id"])
        _upsert_artifact(conn, asset, "asset", now)
    for offer in dataset.get("offers", []):
        artifact_ids.append(offer["id"])
        _upsert_artifact(conn, offer, "offer", now)
    for evidence in dataset.get("evidence", []):
        artifact_ids.append(evidence["id"])
        _upsert_artifact(conn, evidence, "evidence", now)

    # P0.6: Archive stale records not present in current dataset
    _archive_missing(conn, "prospects", prospect_ids, now)
    _archive_missing(conn, "actions", action_ids, now)
    _archive_missing(conn, "artifacts", artifact_ids, now)

    _seed_revenue_os(conn, now)
    conn.commit()


def _upsert_artifact(conn: sqlite3.Connection, item: dict[str, Any], typ: str, now: str) -> None:
    created = _existing_created_at(conn, "artifacts", item["id"], now)
    conn.execute(
        """
        INSERT INTO artifacts(id, type, name, path_or_url, status, created_at, updated_at, payload_json, is_current, archived_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, 1, NULL)
        ON CONFLICT(id) DO UPDATE SET
          type=excluded.type,
          name=excluded.name,
          path_or_url=excluded.path_or_url,
          status=excluded.status,
          updated_at=excluded.updated_at,
          payload_json=excluded.payload_json,
          is_current=1,
          archived_at=NULL
        """,
        (
            item["id"],
            typ,
            item.get("name") or item.get("summary") or item["id"],
            item.get("path_or_url") or item.get("evidence_link"),
            item.get("status", "active"),
            created,
            now,
            _dump(item),
        ),
    )


def _archive_missing(conn: sqlite3.Connection, table: str, current_ids: Iterable[str], now: str) -> None:
    """P0.6: Mark records not in current dataset as archived."""
    if table not in ARCHIVAL_TABLES:
        raise ValueError(f"unsupported table for archival: {table}")

    ids = [i for i in current_ids if i]
    if ids:
        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            f"""
            UPDATE {table}
            SET is_current = 0,
                archived_at = COALESCE(archived_at, ?),
                updated_at = ?
            WHERE is_current = 1
              AND id NOT IN ({placeholders})
            """,
            (now, now, *ids),
        )
    else:
        conn.execute(
            f"""
            UPDATE {table}
            SET is_current = 0,
                archived_at = COALESCE(archived_at, ?),
                updated_at = ?
            WHERE is_current = 1
            """,
            (now, now),
        )


def _seed_revenue_os(conn: sqlite3.Connection, now: str) -> None:
    # Milestone 1 canonical loop and experiment. INSERT OR IGNORE preserves future operator edits.
    conn.execute(
        """
        INSERT OR IGNORE INTO loops(id, name, goal, stage, budget_cap, success_signal, status, created_at, updated_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "loop-owner-operated-cold-call-v1",
            "Owner-operated cold-call v1",
            "Reach local home-service owners directly and turn one money signal into a booked follow-up.",
            "business-hours-retry",
            10.0,
            "Human reached, reply, booked call, or qualified opportunity",
            "active",
            now,
            now,
        ),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO experiments(id, hypothesis, metric, budget_cap, status, result, learning, created_at, updated_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "experiment-owner-operated-direct-contact",
            "Owner-operated home-service companies are easier to reach than large-company IVR targets.",
            "humans_reached_per_dollar",
            10.0,
            "running",
            "After-hours calls reached closed-office/voicemail paths; next best move is business-hours retry.",
            "Avoid large-company IVR batches until DTMF/direct-owner targets are solved.",
            now,
            now,
        ),
    )


def compute_metrics(
    prospects: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
) -> dict[str, tuple[float, str, float | None]]:
    """The single definition site for every Revenue OS metric.

    Funnel counts are cumulative over each prospect's high-water-mark stage (a
    prospect that reached discovery_booked still counts there even after an
    off-ramp to lost/follow_up_later — see finding #5 and _effective_stage_rank);
    not_fit vetoes unconditionally. Spend sums the structured cost_usd field on
    evidence records. Returns {metric_name: (value, unit, target-or-None)}. The
    doc's case-study metrics (first response time, follow-ups sent, stale leads
    revived) slot in here when interaction logging lands (see ADR 0001).
    """
    ranks = [_effective_stage_rank(p.get("status", ""), p.get("max_stage_rank", 0)) for p in prospects]
    values: dict[str, tuple[float, str]] = {
        "prospects_total": (float(len(prospects)), "count"),
        "contacted_total": (float(sum(1 for r in ranks if r >= STAGE_RANK["contacted"])), "count"),
        "discovery_booked_total": (float(sum(1 for r in ranks if r >= STAGE_RANK["discovery_booked"])), "count"),
        "pilot_proposed_total": (float(sum(1 for r in ranks if r >= STAGE_RANK["pilot_proposed"])), "count"),
        "owner_operator_prospects": (float(sum(1 for p in prospects if p.get("owner_operator"))), "count"),
        "open_actions": (float(sum(1 for a in actions if a.get("status") == "open")), "count"),
        "documented_call_spend": (round(sum(float(e.get("cost_usd") or 0) for e in evidence), 4), "usd"),
    }
    return {name: (value, unit, FOURTEEN_DAY_TARGETS.get(name)) for name, (value, unit) in values.items()}


def set_action_status(conn: sqlite3.Connection, action_id: str, status: str) -> None:
    """Operator write path for action status — the dashboard posts through this.

    Validated here so a typo can never plant an out-of-enum status that breaks
    schema validation at the next export.
    """
    if status not in ACTION_STATUSES:
        raise ValueError(f"unknown action status {status!r}; expected one of {sorted(ACTION_STATUSES)}")
    cur = conn.execute(
        "UPDATE actions SET status = ?, updated_at = ? WHERE id = ? AND is_current = 1",
        (status, utc_now(), action_id),
    )
    if cur.rowcount == 0:
        raise KeyError(f"no current action {action_id!r}")
    conn.commit()


def set_prospect_status(conn: sqlite3.Connection, prospect_id: str, status: str) -> None:
    """Operator write path for moving a prospect through the outreach funnel."""
    if status not in FUNNEL_STATUSES:
        raise ValueError(f"unknown prospect status {status!r}; expected one of {', '.join(FUNNEL_STATUSES)}")
    rank = STAGE_RANK.get(status)
    if rank is None:
        # Off-ramp (lost/not_fit/follow_up_later): leave the high-water mark as-is.
        cur = conn.execute(
            "UPDATE prospects SET status = ?, updated_at = ? WHERE id = ? AND is_current = 1",
            (status, utc_now(), prospect_id),
        )
    else:
        cur = conn.execute(
            "UPDATE prospects SET status = ?, updated_at = ?, max_stage_rank = MAX(max_stage_rank, ?) WHERE id = ? AND is_current = 1",
            (status, utc_now(), rank, prospect_id),
        )
    if cur.rowcount == 0:
        raise KeyError(f"no current prospect {prospect_id!r}")
    conn.commit()


def export_dataset(conn: sqlite3.Connection, generated_at: str | None = None) -> dict[str, Any]:
    """Export dashboard-compatible private JSON from SQLite."""
    init_db(conn)
    generated = generated_at or utc_now()
    prospect_rows = list(
        conn.execute(
            """
            SELECT * FROM prospects
            WHERE is_current = 1
            ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                     owner_operator DESC, company_name
            """
        )
    )
    prospects = [_record_with_overrides(row, {"status": row["status"]}) for row in prospect_rows]
    actions = [
        _record_with_overrides(row, {"status": row["status"]})
        for row in conn.execute(
            "SELECT * FROM actions WHERE is_current = 1 ORDER BY score DESC, due_at, action"
        )
    ]
    artifacts = list(conn.execute("SELECT * FROM artifacts WHERE is_current = 1 ORDER BY type, name"))
    offers = [_load(row["payload_json"], {}) for row in artifacts if row["type"] == "offer"]
    assets = [_load(row["payload_json"], {}) for row in artifacts if row["type"] == "asset"]
    evidence = [_load(row["payload_json"], {}) for row in artifacts if row["type"] == "evidence"]
    loops = [dict(row) for row in conn.execute("SELECT * FROM loops ORDER BY status, name")]
    experiments = [dict(row) for row in conn.execute("SELECT * FROM experiments ORDER BY status, id")]

    # Metrics are computed here, at export time, so operator status changes made
    # after import are reflected; the table row is refreshed so SQLite always
    # matches the last published export. max_stage_rank is attached only for this
    # computation, not merged into the `prospects` collection above — it's DB
    # bookkeeping, not part of the exported/public prospect shape.
    metrics_prospects = [
        dict(p, max_stage_rank=row["max_stage_rank"]) for p, row in zip(prospects, prospect_rows)
    ]
    for name, (value, unit, target) in compute_metrics(metrics_prospects, actions, evidence).items():
        conn.execute(
            """
            INSERT INTO metrics(id, metric_name, metric_value, unit, measured_at, source, target)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET metric_value=excluded.metric_value, unit=excluded.unit,
              measured_at=excluded.measured_at, source=excluded.source, target=excluded.target
            """,
            (f"metric-{name}", name, value, unit, generated, "ops-crm/db.py::compute_metrics", target),
        )
    conn.commit()
    metrics = [dict(row) for row in conn.execute("SELECT * FROM metrics ORDER BY metric_name")]
    active_actions = [a for a in actions if a.get("status") not in {"done", "cancelled"}]
    next_best_action = active_actions[0] if active_actions else None

    dataset = {
        "mode": "private",
        "generated_at": generated,
        "offers": offers,
        "prospects": prospects,
        "assets": assets,
        "next_actions": actions,
        "evidence": evidence,
        "loops": loops,
        "experiments": experiments,
        "metrics": metrics,
        "summary": {
            "prospects": len(prospects),
            "assets": len(assets),
            "next_actions": len(actions),
            "evidence": len(evidence),
            "loops": len(loops),
            "experiments": len(experiments),
            "metrics": len(metrics),
            "top_action_id": next_best_action["id"] if next_best_action else None,
            "next_best_move": next_best_action["action"] if next_best_action else "No open action available",
        },
    }
    return dataset


def _record_with_overrides(row: sqlite3.Row, overrides: dict[str, Any]) -> dict[str, Any]:
    record = _load(row["payload_json"], {})
    record.update(overrides)
    return record


def daily_brief(dataset: dict[str, Any]) -> str:
    summary = dataset.get("summary", {})
    metrics = {m["metric_name"]: m for m in dataset.get("metrics", [])}
    loops = dataset.get("loops", [])
    experiments = dataset.get("experiments", [])
    actions = dataset.get("next_actions", [])
    top = next((a for a in actions if a.get("id") == summary.get("top_action_id")), None)
    if top is None:
        top = next((a for a in actions if a.get("status") not in {"done", "cancelled"}), actions[0] if actions else {})
    spend = metrics.get("documented_call_spend", {}).get("metric_value", 0)
    owner_targets = metrics.get("owner_operator_prospects", {}).get("metric_value", 0)

    def progress(name: str) -> str:
        m = metrics.get(name, {})
        target = m.get("target") or FOURTEEN_DAY_TARGETS.get(name, 0)
        return f"{int(m.get('metric_value', 0))}/{int(target)}"

    return "\n".join(
        [
            "# MMTVU Daily Operator Brief",
            "",
            f"Generated: {dataset.get('generated_at', 'unknown')}",
            "",
            "## Are we closer to money than yesterday? (14-day targets)",
            f"- Identified prospects: {progress('prospects_total')}",
            f"- Contacted: {progress('contacted_total')}",
            f"- Discovery booked: {progress('discovery_booked_total')}",
            f"- Pilots proposed: {progress('pilot_proposed_total')}",
            f"- Documented call spend: ${float(spend):.4f}",
            f"- Owner-operated prospects in SQLite: {int(owner_targets)}",
            f"- Active loops: {sum(1 for l in loops if l.get('status') == 'active')}",
            "",
            "## Next best move",
            f"- {top.get('action', summary.get('next_best_move', 'No next action'))}",
            f"- Owner: {top.get('owner', 'n/a')}",
            f"- Why: {top.get('reason', 'n/a')}",
            "",
            "## Current experiment",
            f"- {experiments[0].get('hypothesis') if experiments else 'No experiment recorded'}",
            f"- Learning: {experiments[0].get('learning') if experiments else 'n/a'}",
            "",
            "## Needs Matthew",
            "- Approve any outbound call/email/spend before Hermes executes it.",
        ]
    ) + "\n"
