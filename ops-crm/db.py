#!/usr/bin/env python3
"""SQLite source-of-truth layer for the MMTVU Revenue OS.

The generator still knows how to derive records from repo artifacts, but V1 writes
those records through this module and exports dashboard JSON from SQLite. That
lets operator status, loops, experiments, and metrics
survive regeneration instead of being overwritten by generated JSON files.
"""
from __future__ import annotations

import json
import math
import sqlite3
import uuid
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
OUTREACH_CHANNELS = frozenset({"phone", "email", "linkedin"})
RELEVANT_BUYER_ROLES = frozenset({
    "owner", "founder", "general_manager", "office_manager",
    "sales_manager", "estimator", "marketing_manager",
})
ATTEMPT_DISPOSITIONS = frozenset({
    "no_answer", "voicemail_left", "ivr_blocked", "wrong_number",
    "gatekeeper_no_transfer", "human_no_time", "substantive_conversation",
    "email_sent", "email_replied", "email_bounced",
    "linkedin_sent", "linkedin_replied", "opt_out",
})
# One authoritative disposition/channel/fact contract. Tuple values are:
# (allowed channels, required human_reached, required substantive_conversation).
ATTEMPT_CONSISTENCY_MATRIX = {
    "no_answer": ({"phone"}, False, False),
    "voicemail_left": ({"phone"}, False, False),
    "ivr_blocked": ({"phone"}, False, False),
    "wrong_number": ({"phone"}, False, False),
    "gatekeeper_no_transfer": ({"phone"}, False, False),
    "human_no_time": ({"phone"}, True, False),
    "substantive_conversation": ({"phone", "email", "linkedin"}, True, True),
    "email_sent": ({"email"}, False, False),
    "email_replied": ({"email"}, True, False),
    "email_bounced": ({"email"}, False, False),
    "linkedin_sent": ({"linkedin"}, False, False),
    "linkedin_replied": ({"linkedin"}, True, False),
    "opt_out": ({"phone", "email", "linkedin"}, True, False),
}
COMMERCIAL_EVENT_CONTRACT = {
    # The canonical model declares originating attempt_id nullable, so each event
    # explicitly preserves that policy rather than relying on accidental omission.
    "discovery_scheduled": {
        "evidence_grades": frozenset({"A"}), "offer_version_required": False,
        "price_required": False, "stage": "discovery_booked", "attempt_required": False,
    },
    "discovery_completed": {
        "evidence_grades": frozenset({"A", "B"}), "offer_version_required": False,
        "price_required": False, "stage": None, "attempt_required": False,
    },
    "discovery_no_show": {
        "evidence_grades": frozenset({"A", "B"}), "offer_version_required": False,
        "price_required": False, "stage": None, "attempt_required": False,
    },
    "paid_proposal_sent": {
        "evidence_grades": frozenset({"A"}), "offer_version_required": True,
        "price_required": True, "stage": "pilot_proposed", "attempt_required": False,
    },
    "paid_pilot_accepted": {
        "evidence_grades": frozenset({"A"}), "offer_version_required": True,
        "price_required": True, "stage": "pilot_proposed", "attempt_required": False,
    },
    "paid_pilot_declined": {
        "evidence_grades": frozenset({"A", "B"}), "offer_version_required": True,
        "price_required": True, "stage": "lost", "attempt_required": False,
    },
    "payment_received": {
        "evidence_grades": frozenset({"A"}), "offer_version_required": True,
        "price_required": True, "stage": "won", "attempt_required": False,
    },
}
COMMERCIAL_EVENT_TYPES = frozenset(COMMERCIAL_EVENT_CONTRACT)
CONVERSATION_OUTCOMES = frozenset({
    "no_relevant_pain", "pain_unqualified", "pain_qualified_no_interest",
    "follow_up_requested", "paid_terms_requested", "confirmed_not_fit",
})
# (minimum pain score, maximum pain score, requires qualified economics).
# None score bounds mean the outcome explicitly carries no pain score.
CONVERSATION_OUTCOME_MATRIX = {
    "no_relevant_pain": (0, 0, False),
    "pain_unqualified": (0, 1, False),
    "pain_qualified_no_interest": (2, 3, True),
    "follow_up_requested": (2, 3, True),
    "paid_terms_requested": (2, 3, True),
    "confirmed_not_fit": (None, None, False),
}
EVIDENCE_GRADES = frozenset({"A", "B", "C"})
MAX_EVIDENCE_TEXT_LENGTH = 4000
MAX_PRIVATE_ARTIFACT_REF_LENGTH = 512
FOUNDING_OFFER_VERSION = "founding-pilot-v1"
FOUNDING_OFFER_PRICE_AMOUNT = 500.0
FOUNDING_OFFER_PRICE_CURRENCY = "USD"

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
    conn = sqlite3.connect(target, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
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
            max_stage_rank INTEGER NOT NULL DEFAULT 0,
            contact_suppressed_at TEXT,
            contact_suppression_reason TEXT,
            icp_fit_verified_at TEXT,
            contact_endpoint_verified_at TEXT,
            contact_endpoint_type TEXT,
            sequence_planned_attempts INTEGER,
            sequence_completed_at TEXT,
            sequence_completion_reason TEXT,
            current_sequence_id TEXT,
            lifecycle_high_water_at TEXT
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

        CREATE TABLE IF NOT EXISTS outreach_sequences (
            id TEXT PRIMARY KEY,
            prospect_id TEXT NOT NULL REFERENCES prospects(id),
            endpoint_type TEXT NOT NULL CHECK(endpoint_type IN ('phone', 'email', 'linkedin')),
            verified_at TEXT NOT NULL,
            planned_attempts INTEGER NOT NULL CHECK(planned_attempts BETWEEN 1 AND 3),
            completed_at TEXT,
            completion_reason TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS outreach_attempts (
            id TEXT PRIMARY KEY,
            prospect_id TEXT NOT NULL REFERENCES prospects(id),
            sequence_id TEXT REFERENCES outreach_sequences(id),
            attempted_at TEXT NOT NULL,
            channel TEXT NOT NULL,
            dnc_checked INTEGER NOT NULL,
            contact_role TEXT,
            disposition TEXT NOT NULL,
            human_reached INTEGER NOT NULL DEFAULT 0,
            conversation_outcome TEXT,
            substantive_conversation INTEGER NOT NULL DEFAULT 0,
            pain_score INTEGER,
            pain_type TEXT,
            evidence_grade TEXT,
            evidence_text TEXT,
            eligible_unsold_estimates INTEGER,
            ticket_value_band TEXT,
            follow_up_owner_process TEXT,
            objection_category TEXT,
            artifact_ref TEXT,
            next_action TEXT,
            operator TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS commercial_events (
            id TEXT PRIMARY KEY,
            prospect_id TEXT NOT NULL REFERENCES prospects(id),
            sequence_id TEXT REFERENCES outreach_sequences(id),
            attempt_id TEXT REFERENCES outreach_attempts(id),
            event_type TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            offer_version TEXT,
            price_amount REAL,
            price_currency TEXT,
            evidence_grade TEXT NOT NULL,
            artifact_ref TEXT,
            evidence_text TEXT,
            operator TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS prospect_reopenings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prospect_id TEXT NOT NULL REFERENCES prospects(id),
            from_status TEXT NOT NULL,
            to_status TEXT NOT NULL,
            reason TEXT NOT NULL,
            evidence_grade TEXT NOT NULL,
            artifact_ref TEXT NOT NULL,
            operator TEXT NOT NULL,
            reopened_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TRIGGER IF NOT EXISTS prevent_prospect_reopenings_update
        BEFORE UPDATE ON prospect_reopenings
        BEGIN
            SELECT RAISE(ABORT, 'prospect_reopenings are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS prevent_prospect_reopenings_delete
        BEFORE DELETE ON prospect_reopenings
        BEGIN
            SELECT RAISE(ABORT, 'prospect_reopenings are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS prevent_outreach_sequences_identity_update
        BEFORE UPDATE OF prospect_id, endpoint_type, verified_at, planned_attempts
        ON outreach_sequences
        BEGIN
            SELECT RAISE(ABORT, 'outreach sequence provenance is immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS prevent_outreach_sequences_delete
        BEFORE DELETE ON outreach_sequences
        BEGIN
            SELECT RAISE(ABORT, 'outreach sequence provenance is immutable');
        END;

        CREATE INDEX IF NOT EXISTS idx_outreach_attempts_prospect_at
            ON outreach_attempts(prospect_id, attempted_at);
        CREATE INDEX IF NOT EXISTS idx_commercial_events_prospect_at
            ON commercial_events(prospect_id, occurred_at);
        CREATE INDEX IF NOT EXISTS idx_outreach_sequences_prospect_at
            ON outreach_sequences(prospect_id, verified_at);
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
    if "contact_suppressed_at" not in prospect_cols:
        conn.execute("ALTER TABLE prospects ADD COLUMN contact_suppressed_at TEXT")
    if "contact_suppression_reason" not in prospect_cols:
        conn.execute("ALTER TABLE prospects ADD COLUMN contact_suppression_reason TEXT")
    for column, definition in {
        "icp_fit_verified_at": "TEXT",
        "contact_endpoint_verified_at": "TEXT",
        "contact_endpoint_type": "TEXT",
        "sequence_planned_attempts": "INTEGER",
        "sequence_completed_at": "TEXT",
        "sequence_completion_reason": "TEXT",
        "current_sequence_id": "TEXT",
        "lifecycle_high_water_at": "TEXT",
    }.items():
        if column not in prospect_cols:
            conn.execute(f"ALTER TABLE prospects ADD COLUMN {column} {definition}")

    attempt_cols = {row["name"] for row in conn.execute("PRAGMA table_info(outreach_attempts)")}
    if "dnc_checked" not in attempt_cols:
        conn.execute("ALTER TABLE outreach_attempts ADD COLUMN dnc_checked INTEGER NOT NULL DEFAULT 0")
    if "sequence_id" not in attempt_cols:
        conn.execute("ALTER TABLE outreach_attempts ADD COLUMN sequence_id TEXT REFERENCES outreach_sequences(id)")

    event_cols = {row["name"] for row in conn.execute("PRAGMA table_info(commercial_events)")}
    if "sequence_id" not in event_cols:
        conn.execute("ALTER TABLE commercial_events ADD COLUMN sequence_id TEXT REFERENCES outreach_sequences(id)")

    # Migration/backfill must run before immutable fact triggers are installed.
    # Drop narrower triggers from any interrupted/earlier initialization so the
    # additive provenance repair can complete before full immutability resumes.
    conn.execute("DROP TRIGGER IF EXISTS prevent_outreach_attempts_provenance_update")
    conn.execute("DROP TRIGGER IF EXISTS prevent_commercial_events_provenance_update")

    # Preserve only the currently reconstructable legacy snapshot. Superseded
    # sequence provenance cannot be recovered safely from mutable prospect fields.
    legacy_rows = conn.execute(
        """SELECT id, contact_endpoint_type, contact_endpoint_verified_at,
                  sequence_planned_attempts, sequence_completed_at,
                  sequence_completion_reason
           FROM prospects
           WHERE current_sequence_id IS NULL
             AND contact_endpoint_type IN ('phone', 'email', 'linkedin')
             AND contact_endpoint_verified_at IS NOT NULL
             AND sequence_planned_attempts BETWEEN 1 AND 3"""
    ).fetchall()
    for row in legacy_rows:
        try:
            verified_instant = _validate_offset_timestamp(
                row["contact_endpoint_verified_at"], "contact_endpoint_verified_at"
            )
            completed_instant = (
                _validate_offset_timestamp(row["sequence_completed_at"], "sequence_completed_at")
                if row["sequence_completed_at"]
                else None
            )
        except ValueError:
            # Ambiguous legacy chronology is not safe to attribute immutably.
            continue
        has_completion_time = bool(row["sequence_completed_at"])
        has_completion_reason = bool(row["sequence_completion_reason"])
        if has_completion_time != has_completion_reason:
            continue
        if completed_instant is not None and completed_instant < verified_instant:
            continue

        seed = row["id"] + "|" + row["contact_endpoint_verified_at"]
        sequence_id = f"legacy-{uuid.uuid5(uuid.NAMESPACE_URL, seed)}"
        conn.execute(
            """INSERT OR IGNORE INTO outreach_sequences(
               id, prospect_id, endpoint_type, verified_at, planned_attempts,
               completed_at, completion_reason, created_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                sequence_id, row["id"], row["contact_endpoint_type"],
                row["contact_endpoint_verified_at"], row["sequence_planned_attempts"],
                row["sequence_completed_at"], row["sequence_completion_reason"], utc_now(),
            ),
        )
        conn.execute(
            "UPDATE prospects SET current_sequence_id = ? WHERE id = ?",
            (sequence_id, row["id"]),
        )
        legacy_attempts = conn.execute(
            """SELECT id, attempted_at FROM outreach_attempts
               WHERE prospect_id = ? AND sequence_id IS NULL AND channel = ?""",
            (row["id"], row["contact_endpoint_type"]),
        ).fetchall()
        for legacy_attempt in legacy_attempts:
            try:
                attempted_instant = _validate_offset_timestamp(
                    legacy_attempt["attempted_at"], "attempted_at"
                )
            except ValueError:
                continue
            if attempted_instant < verified_instant:
                continue
            if completed_instant is not None and attempted_instant > completed_instant:
                continue
            conn.execute(
                "UPDATE outreach_attempts SET sequence_id = ? WHERE id = ? AND sequence_id IS NULL",
                (sequence_id, legacy_attempt["id"]),
            )
        legacy_events = conn.execute(
            """SELECT id, attempt_id, occurred_at FROM commercial_events
               WHERE prospect_id = ? AND sequence_id IS NULL""",
            (row["id"],),
        ).fetchall()
        for legacy_event in legacy_events:
            try:
                occurred_instant = _validate_offset_timestamp(
                    legacy_event["occurred_at"], "occurred_at"
                )
            except ValueError:
                continue
            if occurred_instant < verified_instant:
                continue
            target_sequence_id = sequence_id
            if legacy_event["attempt_id"] is not None:
                origin_attempt = conn.execute(
                    """SELECT sequence_id, attempted_at FROM outreach_attempts
                       WHERE id = ? AND prospect_id = ? AND sequence_id = ?""",
                    (legacy_event["attempt_id"], row["id"], sequence_id),
                ).fetchone()
                if origin_attempt is None:
                    continue
                try:
                    origin_attempted_instant = _validate_offset_timestamp(
                        origin_attempt["attempted_at"], "attempted_at"
                    )
                except ValueError:
                    continue
                if occurred_instant < origin_attempted_instant:
                    continue
                target_sequence_id = origin_attempt["sequence_id"]
            conn.execute(
                "UPDATE commercial_events SET sequence_id = ? WHERE id = ? AND sequence_id IS NULL",
                (target_sequence_id, legacy_event["id"]),
            )

    # Reconstruct chronology only from explicit lifecycle evidence. Generic
    # prospect.updated_at is intentionally excluded because imports refresh it.
    for prospect in conn.execute(
        "SELECT id FROM prospects WHERE lifecycle_high_water_at IS NULL"
    ).fetchall():
        prospect_id = prospect["id"]
        candidates = [
            row["at"]
            for row in conn.execute(
                """SELECT icp_fit_verified_at AS at FROM prospects WHERE id = ?
                   UNION ALL SELECT sequence_completed_at FROM prospects WHERE id = ?
                   UNION ALL SELECT contact_suppressed_at FROM prospects WHERE id = ?
                   UNION ALL SELECT attempted_at FROM outreach_attempts WHERE prospect_id = ?
                   UNION ALL SELECT occurred_at FROM commercial_events WHERE prospect_id = ?
                   UNION ALL SELECT reopened_at FROM prospect_reopenings WHERE prospect_id = ?""",
                (prospect_id,) * 6,
            ).fetchall()
            if row["at"]
        ]
        if candidates:
            latest = max(candidates, key=lambda value: _validate_offset_timestamp(value, "lifecycle timestamp"))
            conn.execute(
                "UPDATE prospects SET lifecycle_high_water_at = ? WHERE id = ?",
                (latest, prospect_id),
            )

    # Provenance is writable only during the migration above and initial inserts.
    # Sequence completion is a one-way transition; it cannot be cleared or rewritten.
    for trigger_sql in (
        """CREATE TRIGGER IF NOT EXISTS prevent_outreach_attempts_provenance_update
           BEFORE UPDATE ON outreach_attempts
           BEGIN
               SELECT RAISE(ABORT, 'outreach attempt provenance is immutable');
           END""",
        """CREATE TRIGGER IF NOT EXISTS prevent_outreach_attempts_delete
           BEFORE DELETE ON outreach_attempts
           BEGIN
               SELECT RAISE(ABORT, 'outreach attempts are append-only');
           END""",
        """CREATE TRIGGER IF NOT EXISTS prevent_commercial_events_provenance_update
           BEFORE UPDATE ON commercial_events
           BEGIN
               SELECT RAISE(ABORT, 'commercial event provenance is immutable');
           END""",
        """CREATE TRIGGER IF NOT EXISTS prevent_commercial_events_delete
           BEFORE DELETE ON commercial_events
           BEGIN
               SELECT RAISE(ABORT, 'commercial events are append-only');
           END""",
        """CREATE TRIGGER IF NOT EXISTS prevent_outreach_sequence_completion_rewrite
           BEFORE UPDATE OF completed_at, completion_reason ON outreach_sequences
           WHEN OLD.completed_at IS NOT NULL
             OR NEW.completed_at IS NULL
             OR NEW.completion_reason NOT IN ('planned_attempts_completed', 'terminal_result')
           BEGIN
               SELECT RAISE(ABORT, 'outreach sequence completion is append-only');
           END""",
    ):
        conn.execute(trigger_sql)

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
    now_instant = _validate_offset_timestamp(now, "generated_at")
    conn.execute("BEGIN IMMEDIATE")

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
        existing = conn.execute(
            """SELECT updated_at, is_current, archived_at, lifecycle_high_water_at
               FROM prospects WHERE id = ?""",
            (p["id"],),
        ).fetchone()
        write_updated_at = now
        write_is_current = 1
        write_archived_at = None
        write_high_water = now
        if existing is not None:
            write_updated_at = _latest_timestamp(existing["updated_at"], now)
            write_is_current = existing["is_current"]
            write_archived_at = existing["archived_at"]
            write_high_water = existing["lifecycle_high_water_at"]
            if not existing["is_current"]:
                high_water = existing["lifecycle_high_water_at"]
                if high_water is None or now_instant >= _validate_offset_timestamp(
                    high_water, "prospect.lifecycle_high_water_at"
                ):
                    write_is_current = 1
                    write_archived_at = None
                    write_high_water = now
        conn.execute(
            """
            INSERT INTO prospects(id, company_name, city, niche, website, priority, status, owner_operator, created_at, updated_at, payload_json, is_current, archived_at, max_stage_rank, lifecycle_high_water_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              company_name=excluded.company_name,
              city=excluded.city,
              niche=excluded.niche,
              website=excluded.website,
              priority=excluded.priority,
              owner_operator=excluded.owner_operator,
              updated_at=excluded.updated_at,
              payload_json=excluded.payload_json,
              is_current=excluded.is_current,
              archived_at=excluded.archived_at,
              lifecycle_high_water_at=excluded.lifecycle_high_water_at
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
                write_updated_at,
                _dump(stored),
                write_is_current,
                write_archived_at,
                seed_rank,
                write_high_water,
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
    if table == "prospects":
        now_instant = _validate_offset_timestamp(now, "generated_at")
        placeholders = ",".join("?" for _ in ids)
        predicate = f"AND id NOT IN ({placeholders})" if ids else ""
        rows = conn.execute(
            f"""SELECT id, updated_at, lifecycle_high_water_at FROM prospects
                WHERE is_current = 1 {predicate}""",
            tuple(ids),
        ).fetchall()
        for row in rows:
            high_water = row["lifecycle_high_water_at"]
            if high_water is not None and now_instant < _validate_offset_timestamp(
                high_water, "prospect.lifecycle_high_water_at"
            ):
                continue
            conn.execute(
                """UPDATE prospects SET is_current = 0,
                       archived_at = COALESCE(archived_at, ?),
                       updated_at = ?, lifecycle_high_water_at = ?
                   WHERE id = ? AND is_current = 1""",
                (now, _latest_timestamp(row["updated_at"], now), now, row["id"]),
            )
        return
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


def compute_outreach_metrics(
    conn: sqlite3.Connection,
) -> dict[str, tuple[float, str, float | None]]:
    """Compute private gate populations from immutable sequence provenance."""
    def scalar(sql: str, params: tuple[Any, ...] = ()) -> float:
        return float(conn.execute(sql, params).fetchone()[0])

    sequence_join = """JOIN outreach_sequences s ON s.id = a.sequence_id
        AND s.prospect_id = a.prospect_id"""
    completed_attempt_join = sequence_join + """
        AND s.completed_at IS NOT NULL
        AND s.completion_reason IS NOT NULL"""
    completed_event_join = """JOIN outreach_sequences s ON s.id = e.sequence_id
        AND s.prospect_id = e.prospect_id
        AND s.completed_at IS NOT NULL
        AND s.completion_reason IS NOT NULL"""
    roles = tuple(sorted(RELEVANT_BUYER_ROLES))
    role_slots = ",".join("?" for _ in roles)
    counts = {
        "outreach_attempts_total": scalar("SELECT COUNT(*) FROM outreach_attempts"),
        "outreach_verified_contactable_businesses_total": scalar(
            """SELECT COUNT(*) FROM prospects WHERE is_current = 1
               AND icp_fit_verified_at IS NOT NULL
               AND contact_endpoint_verified_at IS NOT NULL
               AND contact_endpoint_type IN ('phone', 'email', 'linkedin')
               AND contact_suppressed_at IS NULL"""
        ),
        "outreach_attempted_businesses_total": scalar(
            f"SELECT COUNT(DISTINCT a.prospect_id) FROM outreach_attempts a {sequence_join}"
        ),
        "outreach_completed_sequences_total": scalar(
            """SELECT COUNT(DISTINCT prospect_id) FROM outreach_sequences
               WHERE completed_at IS NOT NULL AND completion_reason IS NOT NULL"""
        ),
        "outreach_relevant_human_reached_total": scalar(
            f"""SELECT COUNT(DISTINCT a.prospect_id) FROM outreach_attempts a {completed_attempt_join}
                 WHERE a.human_reached = 1
                 AND LOWER(TRIM(a.contact_role)) IN ({role_slots})""",
            roles,
        ),
        "outreach_substantive_conversations_total": scalar(
            f"""SELECT COUNT(DISTINCT a.prospect_id) FROM outreach_attempts a {completed_attempt_join}
                 WHERE a.substantive_conversation = 1 AND a.evidence_grade IN ('A', 'B')
                 AND LOWER(TRIM(a.contact_role)) IN ({role_slots})""",
            roles,
        ),
        "outreach_pain_qualified_total": scalar(
            f"""SELECT COUNT(DISTINCT a.prospect_id) FROM outreach_attempts a {completed_attempt_join}
                 WHERE a.substantive_conversation = 1 AND a.pain_score BETWEEN 2 AND 3
                 AND a.evidence_grade IN ('A', 'B')
                 AND LOWER(TRIM(a.contact_role)) IN ({role_slots})
                 AND a.eligible_unsold_estimates >= 10
                 AND NULLIF(TRIM(a.ticket_value_band), '') IS NOT NULL""",
            roles,
        ),
        "outreach_paid_terms_requested_total": scalar(
            f"""SELECT COUNT(DISTINCT a.prospect_id) FROM outreach_attempts a {completed_attempt_join}
                 WHERE a.conversation_outcome = 'paid_terms_requested'
                 AND a.pain_score BETWEEN 2 AND 3 AND a.evidence_grade IN ('A', 'B')
                 AND LOWER(TRIM(a.contact_role)) IN ({role_slots})
                 AND a.eligible_unsold_estimates >= 10
                 AND NULLIF(TRIM(a.ticket_value_band), '') IS NOT NULL""",
            roles,
        ),
        "outreach_discovery_booked_total": scalar(
            f"""SELECT COUNT(DISTINCT e.prospect_id) FROM commercial_events e
                 {completed_event_join} WHERE e.event_type = 'discovery_scheduled'"""
        ),
        "outreach_paid_proposals_total": scalar(
            f"""SELECT COUNT(DISTINCT e.prospect_id) FROM commercial_events e
                 {completed_event_join} WHERE e.event_type = 'paid_proposal_sent'"""
        ),
        "outreach_paid_acceptances_total": scalar(
            f"""SELECT COUNT(DISTINCT e.prospect_id) FROM commercial_events e
                 {completed_event_join} WHERE e.event_type = 'paid_pilot_accepted'"""
        ),
        "outreach_payments_total": scalar(
            f"""SELECT COUNT(DISTINCT e.prospect_id) FROM commercial_events e
               {completed_event_join}
               WHERE e.event_type = 'payment_received' AND e.offer_version = ?
               AND e.price_amount = ? AND e.price_currency = ? AND e.evidence_grade = 'A'
               AND NULLIF(TRIM(e.artifact_ref), '') IS NOT NULL""",
            (FOUNDING_OFFER_VERSION, FOUNDING_OFFER_PRICE_AMOUNT, FOUNDING_OFFER_PRICE_CURRENCY),
        ),
        "outreach_invalid_endpoints_total": scalar(
            "SELECT COUNT(*) FROM outreach_attempts WHERE disposition IN ('wrong_number', 'email_bounced')"
        ),
        "outreach_endpoint_attempts_total": scalar(
            "SELECT COUNT(*) FROM outreach_attempts WHERE channel IN ('phone', 'email', 'linkedin')"
        ),
        "outreach_ivr_blocks_total": scalar(
            "SELECT COUNT(*) FROM outreach_attempts WHERE disposition = 'ivr_blocked'"
        ),
        "outreach_gatekeeper_blocks_total": scalar(
            "SELECT COUNT(*) FROM outreach_attempts WHERE disposition = 'gatekeeper_no_transfer'"
        ),
        "outreach_suppressed_businesses_total": scalar(
            """SELECT COUNT(*) FROM prospects WHERE is_current = 1
               AND contact_suppressed_at IS NOT NULL"""
        ),
        "outreach_phone_attempts_total": scalar(
            "SELECT COUNT(*) FROM outreach_attempts WHERE channel = 'phone'"
        ),
        "outreach_live_call_connects_total": scalar(
            """SELECT COUNT(*) FROM outreach_attempts
               WHERE channel = 'phone'
                 AND (human_reached = 1 OR disposition = 'gatekeeper_no_transfer')"""
        ),
    }
    counts["outreach_human_reached_total"] = counts["outreach_relevant_human_reached_total"]
    counts["outreach_buyer_movement_total"] = max(
        counts["outreach_paid_terms_requested_total"], counts["outreach_discovery_booked_total"]
    )
    metrics: dict[str, tuple[float, str, float | None]] = {
        name: (value, "count", None) for name, value in counts.items()
    }

    def add_rate(name: str, numerator: float, denominator: float) -> None:
        metrics[f"{name}_numerator"] = (numerator, "count", None)
        metrics[f"{name}_denominator"] = (denominator, "count", None)
        if denominator:
            metrics[name] = (round(numerator / denominator, 4), "rate", None)

    add_rate("outreach_human_reach_rate", counts["outreach_relevant_human_reached_total"], counts["outreach_attempted_businesses_total"])
    add_rate("outreach_substantive_conversation_rate", counts["outreach_substantive_conversations_total"], counts["outreach_relevant_human_reached_total"])
    add_rate("outreach_pain_qualification_rate", counts["outreach_pain_qualified_total"], counts["outreach_substantive_conversations_total"])
    add_rate("outreach_discovery_booking_rate", counts["outreach_discovery_booked_total"], counts["outreach_pain_qualified_total"])
    add_rate("outreach_paid_proposal_rate", counts["outreach_paid_proposals_total"], counts["outreach_discovery_booked_total"])
    add_rate("outreach_paid_acceptance_rate", counts["outreach_paid_acceptances_total"], counts["outreach_paid_proposals_total"])
    add_rate("outreach_payment_rate", counts["outreach_payments_total"], counts["outreach_paid_acceptances_total"])
    add_rate("outreach_invalid_endpoint_rate", counts["outreach_invalid_endpoints_total"], counts["outreach_endpoint_attempts_total"])
    add_rate("outreach_ivr_block_rate", counts["outreach_ivr_blocks_total"], counts["outreach_phone_attempts_total"])
    add_rate("outreach_gatekeeper_block_rate", counts["outreach_gatekeeper_blocks_total"], counts["outreach_live_call_connects_total"])
    add_rate("outreach_suppression_rate", counts["outreach_suppressed_businesses_total"], counts["outreach_attempted_businesses_total"])
    return metrics


def set_action_status(conn: sqlite3.Connection, action_id: str, status: str) -> None:
    """Operator write path for action status — the dashboard posts through this.

    Validated here so a typo can never plant an out-of-enum status that breaks
    schema validation at the next export.
    """
    if status not in ACTION_STATUSES:
        raise ValueError(f"unknown action status {status!r}; expected one of {sorted(ACTION_STATUSES)}")
    conn.execute("BEGIN IMMEDIATE")
    with conn:
        cur = conn.execute(
            "UPDATE actions SET status = ?, updated_at = ? WHERE id = ? AND is_current = 1",
            (status, utc_now(), action_id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"no current action {action_id!r}")


def _latest_timestamp(first: str, second: str) -> str:
    """Return the later offset-aware timestamp by instant, preserving its spelling."""
    return max(
        (first, second),
        key=lambda value: _validate_offset_timestamp(value, "timestamp"),
    )


def _assert_monotonic_lifecycle_in_transaction(
    conn: sqlite3.Connection,
    prospect_id: str,
    event_at: str,
    *,
    field: str,
) -> sqlite3.Row:
    """Return the locked current prospect or reject a backdated lifecycle fact."""
    event_instant = _validate_offset_timestamp(event_at, field)
    prospect = conn.execute(
        """SELECT * FROM prospects WHERE id = ? AND is_current = 1""",
        (prospect_id,),
    ).fetchone()
    if prospect is None:
        raise KeyError(f"no current prospect {prospect_id!r}")
    high_water = prospect["lifecycle_high_water_at"]
    if high_water is not None and event_instant < _validate_offset_timestamp(
        high_water, "prospect.lifecycle_high_water_at"
    ):
        raise ValueError(f"{field} is older than current prospect chronology")
    return prospect


def _advance_lifecycle_high_water_in_transaction(
    conn: sqlite3.Connection,
    prospect_id: str,
    event_at: str,
) -> None:
    conn.execute(
        """UPDATE prospects SET lifecycle_high_water_at = ?
           WHERE id = ? AND is_current = 1""",
        (event_at, prospect_id),
    )


def _set_prospect_status_in_transaction(
    conn: sqlite3.Connection,
    prospect_id: str,
    status: str,
    *,
    updated_at: str | None = None,
) -> None:
    """Apply a chronology-approved lifecycle transition without committing."""
    if status not in FUNNEL_STATUSES:
        raise ValueError(f"unknown prospect status {status!r}; expected one of {', '.join(FUNNEL_STATUSES)}")
    changed_at = updated_at or utc_now()
    existing = conn.execute(
        "SELECT updated_at FROM prospects WHERE id = ? AND is_current = 1",
        (prospect_id,),
    ).fetchone()
    if existing is None:
        raise KeyError(f"no current prospect {prospect_id!r}")
    changed_at = _latest_timestamp(existing["updated_at"], changed_at)
    rank = STAGE_RANK.get(status)
    if rank is None:
        cur = conn.execute(
            "UPDATE prospects SET status = ?, updated_at = ? WHERE id = ? AND is_current = 1",
            (status, changed_at, prospect_id),
        )
    else:
        cur = conn.execute(
            """UPDATE prospects
               SET status = CASE
                       WHEN status IN ('lost', 'not_fit', 'follow_up_later') THEN status
                       WHEN max_stage_rank <= ? THEN ? ELSE status END,
                   updated_at = CASE
                       WHEN status IN ('lost', 'not_fit', 'follow_up_later') THEN updated_at
                       WHEN max_stage_rank <= ? THEN ? ELSE updated_at END,
                   max_stage_rank = CASE
                       WHEN status IN ('lost', 'not_fit', 'follow_up_later') THEN max_stage_rank
                       ELSE MAX(max_stage_rank, ?) END
               WHERE id = ? AND is_current = 1""",
            (rank, status, rank, changed_at, rank, prospect_id),
        )
    if cur.rowcount == 0:
        raise KeyError(f"no current prospect {prospect_id!r}")


def set_prospect_status(
    conn: sqlite3.Connection,
    prospect_id: str,
    status: str,
    *,
    changed_at: str | None = None,
) -> None:
    """Operator write path for moving a prospect through the outreach funnel."""
    changed_at = changed_at or utc_now()
    conn.execute("BEGIN IMMEDIATE")
    with conn:
        _assert_monotonic_lifecycle_in_transaction(
            conn, prospect_id, changed_at, field="updated_at"
        )
        _set_prospect_status_in_transaction(conn, prospect_id, status, updated_at=changed_at)
        _advance_lifecycle_high_water_in_transaction(conn, prospect_id, changed_at)


def reopen_prospect(
    conn: sqlite3.Connection,
    prospect_id: str,
    *,
    reason: str,
    evidence_grade: str,
    artifact_ref: str,
    operator: str,
    reopened_at: str | None = None,
) -> None:
    """Deliberately reopen an off-ramp with immutable grade-A audit evidence."""
    init_db(conn)
    for field, value in (("reason", reason), ("artifact_ref", artifact_ref), ("operator", operator)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} is required")
    if evidence_grade != "A":
        raise ValueError("explicit reopening requires grade A evidence")
    _validate_private_artifact_ref(artifact_ref)
    at = reopened_at or utc_now()
    _validate_offset_timestamp(at, "reopened_at")
    rank_to_status = {rank: status for status, rank in STAGE_RANK.items()}
    conn.execute("BEGIN IMMEDIATE")
    with conn:
        prospect = _assert_monotonic_lifecycle_in_transaction(
            conn, prospect_id, at, field="reopened_at"
        )
        if prospect["status"] not in {"lost", "not_fit", "follow_up_later"}:
            raise ValueError("prospect is not on a protected off-ramp")
        rank = prospect["max_stage_rank"]
        if type(rank) is not int or rank not in rank_to_status:
            raise ValueError("prospect has invalid max_stage_rank")
        target = rank_to_status[rank]
        write_updated_at = _latest_timestamp(prospect["updated_at"], at)
        conn.execute(
            "UPDATE prospects SET status = ?, updated_at = ? WHERE id = ? AND is_current = 1",
            (target, write_updated_at, prospect_id),
        )
        conn.execute(
            """INSERT INTO prospect_reopenings(
               prospect_id, from_status, to_status, reason, evidence_grade,
               artifact_ref, operator, reopened_at, created_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (prospect_id, prospect["status"], target, reason.strip(), evidence_grade,
             artifact_ref, operator.strip(), at, utc_now()),
        )
        _advance_lifecycle_high_water_in_transaction(conn, prospect_id, at)


def unsuppress_prospect(conn: sqlite3.Connection, prospect_id: str, reason: str) -> None:
    """Clear active suppression only when an operator records why re-contact is lawful."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("unsuppress reason is required")
    at = utc_now()
    conn.execute("BEGIN IMMEDIATE")
    with conn:
        prospect = _assert_monotonic_lifecycle_in_transaction(
            conn, prospect_id, at, field="unsuppressed_at"
        )
        if not prospect["contact_suppressed_at"]:
            raise KeyError(f"no actively suppressed current prospect {prospect_id!r}")
        conn.execute(
            """UPDATE prospects
               SET contact_suppressed_at = NULL,
                   contact_suppression_reason = ?,
                   updated_at = ?
               WHERE id = ? AND is_current = 1""",
            (f"unsuppressed: {reason.strip()}", at, prospect_id),
        )
        _advance_lifecycle_high_water_in_transaction(conn, prospect_id, at)


def verify_prospect_for_outreach(
    conn: sqlite3.Connection,
    prospect_id: str,
    *,
    endpoint_type: str,
    planned_attempts: int,
    verified_at: str | None = None,
) -> None:
    """Create an immutable outreach sequence and project it onto the prospect."""
    if endpoint_type not in OUTREACH_CHANNELS:
        raise ValueError(f"endpoint_type must be one of {sorted(OUTREACH_CHANNELS)}")
    if isinstance(planned_attempts, bool) or not isinstance(planned_attempts, int) or not 1 <= planned_attempts <= 3:
        raise ValueError("planned_attempts must be an integer from 1 to 3")
    at = verified_at or utc_now()
    _validate_offset_timestamp(at, "verified_at")
    sequence_id = f"sequence-{uuid.uuid4()}"
    conn.execute("BEGIN IMMEDIATE")
    with conn:
        prospect = _assert_monotonic_lifecycle_in_transaction(
            conn, prospect_id, at, field="verified_at"
        )
        if prospect["contact_suppressed_at"]:
            raise KeyError(f"no unsuppressed current prospect {prospect_id!r}")
        conn.execute(
            """INSERT INTO outreach_sequences(
               id, prospect_id, endpoint_type, verified_at, planned_attempts, created_at
               ) VALUES(?,?,?,?,?,?)""",
            (sequence_id, prospect_id, endpoint_type, at, planned_attempts, utc_now()),
        )
        write_updated_at = _latest_timestamp(prospect["updated_at"], at)
        conn.execute(
            """UPDATE prospects
               SET icp_fit_verified_at = ?, contact_endpoint_verified_at = ?,
                   contact_endpoint_type = ?, sequence_planned_attempts = ?,
                   sequence_completed_at = NULL, sequence_completion_reason = NULL,
                   current_sequence_id = ?, updated_at = ?
               WHERE id = ? AND is_current = 1 AND contact_suppressed_at IS NULL""",
            (at, at, endpoint_type, planned_attempts, sequence_id, write_updated_at, prospect_id),
        )
        _advance_lifecycle_high_water_in_transaction(conn, prospect_id, at)


def complete_outreach_sequence(
    conn: sqlite3.Connection,
    prospect_id: str,
    *,
    reason: str,
    completed_at: str | None = None,
) -> None:
    """Complete only the current immutable plan in one protected transaction."""
    allowed_reasons = {"planned_attempts_completed", "terminal_result"}
    if reason not in allowed_reasons:
        raise ValueError(f"sequence completion reason must be one of {sorted(allowed_reasons)}")
    at = completed_at or utc_now()
    at_instant = _validate_offset_timestamp(at, "completed_at")
    conn.execute("BEGIN IMMEDIATE")
    with conn:
        prospect = conn.execute(
            "SELECT * FROM prospects WHERE id = ? AND is_current = 1",
            (prospect_id,),
        ).fetchone()
        if prospect is None:
            raise KeyError(f"no current prospect {prospect_id!r}")
        sequence_id = prospect["current_sequence_id"]
        if not sequence_id:
            raise ValueError("prospect has no explicit planned sequence")
        sequence = conn.execute(
            """SELECT planned_attempts, verified_at, completed_at
               FROM outreach_sequences WHERE id = ? AND prospect_id = ?""",
            (sequence_id, prospect_id),
        ).fetchone()
        if sequence is None:
            raise ValueError("prospect has no explicit planned sequence")
        if sequence["completed_at"] is not None:
            raise ValueError("current outreach sequence is already complete")
        if at_instant < _validate_offset_timestamp(sequence["verified_at"], "sequence.verified_at"):
            raise ValueError("sequence completion cannot occur before verification")
        attempt_rows = conn.execute(
            """SELECT attempted_at, disposition FROM outreach_attempts
               WHERE sequence_id = ? ORDER BY attempted_at""",
            (sequence_id,),
        ).fetchall()
        if any(
            at_instant < _validate_offset_timestamp(row["attempted_at"], "attempted_at")
            for row in attempt_rows
        ):
            raise ValueError("sequence completion cannot occur before a supporting attempt")
        if reason == "planned_attempts_completed" and len(attempt_rows) < sequence["planned_attempts"]:
            raise ValueError("planned attempts are not complete")
        if reason == "terminal_result" and not any(
            row["disposition"] in {"wrong_number", "email_bounced", "substantive_conversation", "opt_out"}
            for row in attempt_rows
        ):
            raise ValueError("terminal_result requires a terminal disposition")
        _assert_monotonic_lifecycle_in_transaction(
            conn, prospect_id, at, field="completed_at"
        )
        conn.execute(
            """UPDATE outreach_sequences SET completed_at = ?, completion_reason = ?
               WHERE id = ? AND completed_at IS NULL""",
            (at, reason, sequence_id),
        )
        write_updated_at = _latest_timestamp(prospect["updated_at"], at)
        conn.execute(
            """UPDATE prospects SET sequence_completed_at = ?, sequence_completion_reason = ?,
               updated_at = ? WHERE id = ? AND is_current = 1 AND current_sequence_id = ?""",
            (at, reason, write_updated_at, prospect_id, sequence_id),
        )
        _advance_lifecycle_high_water_in_transaction(conn, prospect_id, at)


def _validate_offset_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an offset-aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an offset-aware ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be an offset-aware ISO timestamp")
    return parsed


def _validate_evidence(grade: Any, artifact_ref: Any, evidence_text: Any) -> None:
    if grade is not None and grade not in EVIDENCE_GRADES:
        raise ValueError("evidence_grade must be A, B, or C")
    if grade == "A" and not artifact_ref:
        raise ValueError("artifact_ref is required for grade A evidence")
    if grade == "B" and not evidence_text:
        raise ValueError("evidence_text is required for grade B evidence")
    if evidence_text is not None:
        if not isinstance(evidence_text, str):
            raise ValueError("evidence_text must be text")
        if len(evidence_text) > MAX_EVIDENCE_TEXT_LENGTH:
            raise ValueError(f"evidence_text exceeds {MAX_EVIDENCE_TEXT_LENGTH} characters")


def _validate_private_artifact_ref(value: Any) -> None:
    """Validate an opaque local locator without dereferencing private evidence."""
    if not isinstance(value, str):
        raise ValueError("artifact_ref must be an opaque private locator string")
    if not value or value != value.strip():
        raise ValueError("artifact_ref must be nonblank with no surrounding whitespace")
    if len(value) > MAX_PRIVATE_ARTIFACT_REF_LENGTH:
        raise ValueError(f"artifact_ref exceeds {MAX_PRIVATE_ARTIFACT_REF_LENGTH} characters")
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in value
    ):
        raise ValueError("artifact_ref must not contain whitespace or control characters")
    lowered = value.lower()
    credential_markers = ("api_key=", "apikey=", "password=", "passwd=", "token=", "secret=")
    authority = value.split("://", 1)[1].split("/", 1)[0] if "://" in value else ""
    if (
        value[0] in "{["
        or lowered.startswith(("bearer", "sk-"))
        or any(marker in lowered for marker in credential_markers)
        or "@" in authority
    ):
        raise ValueError("artifact_ref must be an opaque locator, not raw evidence or credentials")


def _validate_outreach_attempt(attempt: dict[str, Any]) -> None:
    required = ("id", "prospect_id", "attempted_at", "channel", "disposition", "operator")
    missing = [field for field in required if not attempt.get(field)]
    if missing:
        raise ValueError("missing required fields: " + ", ".join(missing))
    if "status" in attempt:
        raise ValueError("status is derived from evidence and cannot be supplied")
    _validate_offset_timestamp(attempt["attempted_at"], "attempted_at")
    if attempt["channel"] not in OUTREACH_CHANNELS:
        raise ValueError("invalid channel")
    if attempt["disposition"] not in ATTEMPT_DISPOSITIONS:
        raise ValueError("invalid disposition")
    allowed_channels, expected_human, expected_substantive = ATTEMPT_CONSISTENCY_MATRIX[
        attempt["disposition"]
    ]
    if attempt["channel"] not in allowed_channels:
        raise ValueError(
            f"disposition {attempt['disposition']!r} is not valid for channel {attempt['channel']!r}"
        )
    if attempt.get("dnc_checked") is not True:
        raise ValueError("dnc_checked must be true before outreach")
    for field in ("human_reached", "substantive_conversation"):
        if field in attempt and not isinstance(attempt[field], bool):
            raise ValueError(f"{field} must be a boolean")
    substantive = attempt.get("substantive_conversation", False)
    human_reached = attempt.get("human_reached", False)
    outcome = attempt.get("conversation_outcome")
    if human_reached is not expected_human:
        raise ValueError(
            f"disposition {attempt['disposition']!r} requires human_reached={expected_human}"
        )
    if substantive is not expected_substantive:
        raise ValueError(
            f"disposition {attempt['disposition']!r} requires substantive_conversation={expected_substantive}"
        )
    if substantive and not human_reached:
        raise ValueError("substantive_conversation requires human_reached")
    if substantive and not outcome:
        raise ValueError("conversation_outcome is required for a substantive conversation")
    if outcome and not substantive:
        raise ValueError("conversation_outcome requires substantive_conversation")
    if outcome and outcome not in CONVERSATION_OUTCOMES:
        raise ValueError("invalid conversation_outcome")
    score = attempt.get("pain_score")
    if score is not None:
        if isinstance(score, bool) or not isinstance(score, int) or score not in range(4):
            raise ValueError("pain_score must be an integer from 0 to 3")
        if not substantive:
            raise ValueError("pain_score requires substantive_conversation")
    _validate_evidence(
        attempt.get("evidence_grade"), attempt.get("artifact_ref"), attempt.get("evidence_text")
    )
    if attempt.get("evidence_grade") == "B" and not attempt.get("contact_role"):
        raise ValueError("contact_role is required for grade B evidence")
    if score is not None and score >= 2 and attempt.get("evidence_grade") not in {"A", "B"}:
        raise ValueError("pain_score 2-3 requires evidence grade A or B")
    estimate_count = attempt.get("eligible_unsold_estimates")
    if estimate_count is not None and (isinstance(estimate_count, bool) or not isinstance(estimate_count, int) or estimate_count < 0):
        raise ValueError("eligible_unsold_estimates must be a non-negative integer")
    if outcome:
        minimum_score, maximum_score, requires_qualified_economics = CONVERSATION_OUTCOME_MATRIX[outcome]
        if outcome == "confirmed_not_fit" and attempt.get("evidence_grade") not in {"A", "B"}:
            raise ValueError("conversation_outcome 'confirmed_not_fit' requires grade A or B evidence")
        if minimum_score is None:
            if score is not None:
                raise ValueError(f"conversation_outcome {outcome!r} does not accept pain_score")
        elif score is None or not minimum_score <= score <= maximum_score:
            raise ValueError(
                f"conversation_outcome {outcome!r} requires pain_score {minimum_score}-{maximum_score}"
            )
        if requires_qualified_economics:
            if attempt.get("evidence_grade") not in {"A", "B"}:
                raise ValueError(f"conversation_outcome {outcome!r} requires grade A or B evidence")
            if not isinstance(attempt.get("contact_role"), str) or not attempt["contact_role"].strip():
                raise ValueError(f"conversation_outcome {outcome!r} requires contact_role")
            if estimate_count is None or estimate_count < 10:
                raise ValueError(
                    f"conversation_outcome {outcome!r} requires at least 10 eligible_unsold_estimates"
                )
            ticket_band = attempt.get("ticket_value_band")
            if not isinstance(ticket_band, str) or not ticket_band.strip():
                raise ValueError(f"conversation_outcome {outcome!r} requires ticket_value_band")


def record_outreach_attempt(conn: sqlite3.Connection, attempt: dict[str, Any]) -> str:
    """Persist one real outreach attempt and its justified prospect effects atomically."""
    init_db(conn)
    _validate_outreach_attempt(attempt)

    attempted_at = attempt["attempted_at"]
    attempted_instant = _validate_offset_timestamp(attempted_at, "attempted_at")
    stage = "replied" if attempt.get("human_reached") or attempt["disposition"] == "email_replied" else "contacted"
    conn.execute("BEGIN IMMEDIATE")
    with conn:
        prospect = _assert_monotonic_lifecycle_in_transaction(
            conn, attempt["prospect_id"], attempted_at, field="attempted_at"
        )
        if prospect["contact_suppressed_at"]:
            raise ValueError("prospect is contact-suppressed")
        sequence_id = None
        if prospect["current_sequence_id"]:
            sequence = conn.execute(
                """SELECT id, endpoint_type, verified_at, completed_at
                   FROM outreach_sequences WHERE id = ? AND prospect_id = ?""",
                (prospect["current_sequence_id"], attempt["prospect_id"]),
            ).fetchone()
            if (
                sequence is not None
                and sequence["completed_at"] is None
                and sequence["endpoint_type"] == attempt["channel"]
                and attempted_instant >= _validate_offset_timestamp(
                    sequence["verified_at"], "sequence.verified_at"
                )
            ):
                sequence_id = sequence["id"]
        values = (
            attempt["id"], attempt["prospect_id"], sequence_id, attempted_at,
            attempt["channel"], 1, attempt.get("contact_role"), attempt["disposition"],
            int(attempt.get("human_reached", False)), attempt.get("conversation_outcome"),
            int(attempt.get("substantive_conversation", False)), attempt.get("pain_score"),
            attempt.get("pain_type"), attempt.get("evidence_grade"), attempt.get("evidence_text"),
            attempt.get("eligible_unsold_estimates"), attempt.get("ticket_value_band"),
            attempt.get("follow_up_owner_process"), attempt.get("objection_category"),
            attempt.get("artifact_ref"), attempt.get("next_action"), attempt["operator"], utc_now(),
        )
        conn.execute(
            """INSERT INTO outreach_attempts(
                id, prospect_id, sequence_id, attempted_at, channel, dnc_checked,
                contact_role, disposition, human_reached, conversation_outcome,
                substantive_conversation, pain_score, pain_type, evidence_grade,
                evidence_text, eligible_unsold_estimates, ticket_value_band,
                follow_up_owner_process, objection_category, artifact_ref,
                next_action, operator, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            values,
        )
        off_ramp = {
            "pain_qualified_no_interest": "lost",
            "follow_up_requested": "follow_up_later",
            "confirmed_not_fit": "not_fit",
        }.get(str(attempt.get("conversation_outcome")))
        if off_ramp:
            # Preserve the evidence-derived mainline high-water mark before entering
            # the protected off-ramp in the same transaction.
            _set_prospect_status_in_transaction(
                conn, attempt["prospect_id"], stage, updated_at=attempted_at
            )
            _set_prospect_status_in_transaction(
                conn, attempt["prospect_id"], off_ramp, updated_at=attempted_at
            )
        elif attempt["disposition"] != "opt_out":
            _set_prospect_status_in_transaction(
                conn, attempt["prospect_id"], stage, updated_at=attempted_at
            )
        else:
            lifecycle_at = (
                attempted_at
                if attempted_instant >= _validate_offset_timestamp(
                    prospect["updated_at"], "prospect.updated_at"
                )
                else prospect["updated_at"]
            )
            conn.execute(
                """UPDATE prospects
                   SET contact_suppressed_at = ?, contact_suppression_reason = ?, updated_at = ?
                   WHERE id = ? AND is_current = 1""",
                (attempted_at, "explicit_opt_out", lifecycle_at, attempt["prospect_id"]),
            )
        _advance_lifecycle_high_water_in_transaction(
            conn, attempt["prospect_id"], attempted_at
        )
    return attempt["id"]


def record_commercial_event(conn: sqlite3.Connection, event: dict[str, Any]) -> str:
    """Append a commercial milestone and apply only its evidence-derived stage effect."""
    init_db(conn)
    required = ("id", "prospect_id", "event_type", "occurred_at", "evidence_grade", "operator")
    missing = [field for field in required if not event.get(field)]
    if missing:
        raise ValueError("missing required fields: " + ", ".join(missing))
    if event["event_type"] not in COMMERCIAL_EVENT_TYPES:
        raise ValueError("invalid event_type")
    if "status" in event:
        raise ValueError("status is derived from evidence and cannot be supplied")
    occurred_at = _validate_offset_timestamp(event["occurred_at"], "occurred_at")
    contract = COMMERCIAL_EVENT_CONTRACT[event["event_type"]]
    if event["evidence_grade"] not in contract["evidence_grades"]:
        if contract["evidence_grades"] == frozenset({"A"}):
            raise ValueError(f"{event['event_type']} requires grade A evidence")
        raise ValueError(f"{event['event_type']} requires evidence grade A or B")
    _validate_evidence(
        event["evidence_grade"], event.get("artifact_ref"), event.get("evidence_text")
    )
    if event.get("artifact_ref") is not None:
        _validate_private_artifact_ref(event["artifact_ref"])
    if contract["offer_version_required"]:
        if event.get("offer_version") != FOUNDING_OFFER_VERSION:
            raise ValueError(
                f"{event['event_type']} offer_version must be {FOUNDING_OFFER_VERSION!r}"
            )
    if contract["price_required"]:
        amount = event.get("price_amount")
        if (
            isinstance(amount, bool)
            or not isinstance(amount, (int, float))
            or not math.isfinite(float(amount))
            or float(amount) != FOUNDING_OFFER_PRICE_AMOUNT
        ):
            raise ValueError(
                f"{event['event_type']} price_amount must be exactly "
                f"{FOUNDING_OFFER_PRICE_AMOUNT:g}"
            )
        if event.get("price_currency") != FOUNDING_OFFER_PRICE_CURRENCY:
            raise ValueError(
                f"{event['event_type']} price_currency must be {FOUNDING_OFFER_PRICE_CURRENCY}"
            )

    conn.execute("BEGIN IMMEDIATE")
    with conn:
        prospect = conn.execute(
            "SELECT * FROM prospects WHERE id = ? AND is_current = 1",
            (event["prospect_id"],),
        ).fetchone()
        if prospect is None:
            raise KeyError(f"no current prospect {event['prospect_id']!r}")
        if prospect["contact_suppressed_at"]:
            raise ValueError("prospect is contact-suppressed")
        sequence_id = None
        if event.get("attempt_id"):
            attempt_owner = conn.execute(
                "SELECT prospect_id, attempted_at, sequence_id FROM outreach_attempts WHERE id = ?",
                (event["attempt_id"],),
            ).fetchone()
            if attempt_owner is None:
                raise KeyError(f"no outreach attempt {event['attempt_id']!r}")
            if attempt_owner["prospect_id"] != event["prospect_id"]:
                raise ValueError("attempt_id must belong to the same prospect")
            attempted_at = _validate_offset_timestamp(attempt_owner["attempted_at"], "attempted_at")
            if occurred_at < attempted_at:
                raise ValueError("commercial event cannot occur before its originating attempt")
            sequence_id = attempt_owner["sequence_id"]
        elif prospect["current_sequence_id"]:
            sequence = conn.execute(
                """SELECT id, verified_at FROM outreach_sequences
                   WHERE id = ? AND prospect_id = ?""",
                (prospect["current_sequence_id"], event["prospect_id"]),
            ).fetchone()
            if sequence is not None and occurred_at >= _validate_offset_timestamp(
                sequence["verified_at"], "sequence.verified_at"
            ):
                sequence_id = sequence["id"]

        _assert_monotonic_lifecycle_in_transaction(
            conn, event["prospect_id"], event["occurred_at"], field="occurred_at"
        )
        conn.execute(
            """INSERT INTO commercial_events(
                id, prospect_id, sequence_id, attempt_id, event_type, occurred_at, offer_version,
                price_amount, price_currency, evidence_grade, artifact_ref,
                evidence_text, operator, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event["id"], event["prospect_id"], sequence_id, event.get("attempt_id"),
                event["event_type"], event["occurred_at"], event.get("offer_version"),
                event.get("price_amount"), event.get("price_currency"),
                event["evidence_grade"], event.get("artifact_ref"),
                event.get("evidence_text"), event["operator"], utc_now(),
            ),
        )
        stage = contract["stage"]
        if stage:
            _set_prospect_status_in_transaction(
                conn, event["prospect_id"], stage, updated_at=event["occurred_at"]
            )
        _advance_lifecycle_high_water_in_transaction(
            conn, event["prospect_id"], event["occurred_at"]
        )
    return event["id"]


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
    metric_values = compute_metrics(metrics_prospects, actions, evidence)
    metric_values.update(compute_outreach_metrics(conn))
    conn.execute("DELETE FROM metrics WHERE metric_name LIKE 'outreach_%'")
    for name, (value, unit, target) in metric_values.items():
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

    def evidence_rate(label: str, name: str) -> str:
        numerator = int(metrics.get(f"{name}_numerator", {}).get("metric_value", 0))
        denominator = int(metrics.get(f"{name}_denominator", {}).get("metric_value", 0))
        if denominator == 0:
            return f"- {label}: not enough evidence ({numerator}/{denominator})"
        value = float(metrics[name]["metric_value"])
        return f"- {label}: {value * 100:.1f}% ({numerator}/{denominator})"

    outreach_rate_lines = [
        evidence_rate("Human reach rate", "outreach_human_reach_rate"),
        evidence_rate("Substantive conversation rate", "outreach_substantive_conversation_rate"),
        evidence_rate("Pain qualification rate", "outreach_pain_qualification_rate"),
        evidence_rate("Discovery booking rate", "outreach_discovery_booking_rate"),
        evidence_rate("Paid proposal rate", "outreach_paid_proposal_rate"),
        evidence_rate("Paid acceptance rate", "outreach_paid_acceptance_rate"),
        evidence_rate("Payment rate", "outreach_payment_rate"),
    ]

    def count(name: str) -> int:
        return int(metrics.get(name, {}).get("metric_value", 0))

    completed = count("outreach_completed_sequences_total")
    relevant_humans = count("outreach_relevant_human_reached_total")
    substantive = count("outreach_substantive_conversations_total")
    qualified = count("outreach_pain_qualified_total")
    buyer_movement = count("outreach_buyer_movement_total")
    payments = count("outreach_payments_total")
    if completed == 0:
        reach_status = pain_status = buyer_status = payment_status = "NOT ENOUGH EVIDENCE"
    elif completed < 10:
        reach_status = pain_status = buyer_status = payment_status = "IN PROGRESS"
    else:
        reach_status = "PASS" if relevant_humans >= 5 and substantive >= 3 else "FAIL"
        pain_status = "PASS" if qualified >= 2 else "FAIL"
        buyer_status = "PASS" if buyer_movement >= 1 else "FAIL"
        payment_status = "PASS" if payments >= 1 else "FAIL"
    recommendation = (
        "EXPAND"
        if completed >= 10 and reach_status == pain_status == buyer_status == "PASS"
        else "STOP" if completed >= 10 else "CONTINUE"
    )
    gate_lines = [
        "## Evidence Loop V1 decision gates",
        f"- Calibration population: {completed}/10 completed planned sequences",
        f"- Reach gate: {reach_status} — relevant humans {relevant_humans}/5; substantive conversations {substantive}/3",
        f"- Pain/economics gate: {pain_status} — qualified businesses {qualified}/2",
        f"- Buyer-movement gate: {buyer_status} — artifact/evidence-backed buyers {buyer_movement}/1",
        f"- Payment gate: {payment_status} — exact $500 USD founding-pilot payments {payments}/1",
        f"- Recommendation: {recommendation}",
        "",
    ]
    private_outreach_lines = (
        ["## Evidence Loop V1 rates", *outreach_rate_lines, "", *gate_lines]
        if dataset.get("mode") == "private"
        else []
    )

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
            *private_outreach_lines,
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
