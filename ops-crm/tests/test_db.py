import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

GEN_SPEC = importlib.util.spec_from_file_location("crm_generate", ROOT / "ops-crm" / "generate.py")
assert GEN_SPEC is not None and GEN_SPEC.loader is not None
crm_generate = importlib.util.module_from_spec(GEN_SPEC)
sys.modules["crm_generate"] = crm_generate
GEN_SPEC.loader.exec_module(crm_generate)

DB_SPEC = importlib.util.spec_from_file_location("crm_db", ROOT / "ops-crm" / "db.py")
assert DB_SPEC is not None and DB_SPEC.loader is not None
crm_db = importlib.util.module_from_spec(DB_SPEC)
sys.modules["crm_db_tests"] = crm_db
DB_SPEC.loader.exec_module(crm_db)

sys.path.insert(0, str(ROOT / "ops-crm"))
import redaction


def test_sqlite_init_is_idempotent_and_has_revenue_os_tables(tmp_path):
    conn = sqlite3.connect(tmp_path / "crm.sqlite")
    conn.row_factory = sqlite3.Row

    crm_db.init_db(conn)
    crm_db.init_db(conn)

    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "prospects",
        "actions",
        "loops",
        "artifacts",
        "experiments",
        "metrics",
        "outreach_attempts",
        "commercial_events",
    }.issubset(tables)
    prospect_columns = {row["name"] for row in conn.execute("PRAGMA table_info(prospects)")}
    assert {"contact_suppressed_at", "contact_suppression_reason"}.issubset(prospect_columns)


def test_import_current_artifacts_to_db_and_export_dashboard_dataset(tmp_path):
    db_file = tmp_path / "crm.sqlite"
    source = crm_generate.build_source_dataset(ROOT, "2026-07-09T00:00:00Z")

    with crm_db.connect(ROOT, db_file) as conn:
        crm_db.import_dataset(conn, source)
        exported = crm_db.export_dataset(conn, "2026-07-09T00:00:00Z")

    assert len(exported["prospects"]) == len(source["prospects"])
    assert len(exported["next_actions"]) == len(source["next_actions"])
    assert exported["loops"][0]["id"] == "loop-owner-operated-cold-call-v1"
    assert exported["experiments"][0]["id"] == "experiment-owner-operated-direct-contact"
    assert {m["metric_name"] for m in exported["metrics"]} >= {"prospects_total", "owner_operator_prospects", "documented_call_spend"}
    assert exported["summary"]["next_best_move"] == exported["next_actions"][0]["action"]


def test_action_status_survives_regeneration_from_sqlite(tmp_path):
    db_file = tmp_path / "crm.sqlite"
    private, _ = crm_generate.generate(ROOT, db_file=db_file)
    action_id = private["next_actions"][0]["id"]
    expected_next = private["next_actions"][1]

    with crm_db.connect(ROOT, db_file) as conn:
        conn.execute("UPDATE actions SET status = 'done' WHERE id = ?", (action_id,))
        conn.commit()

    regenerated, _ = crm_generate.generate(ROOT, db_file=db_file)
    same_action = next(a for a in regenerated["next_actions"] if a["id"] == action_id)
    metrics = {m["metric_name"]: m["metric_value"] for m in regenerated["metrics"]}

    assert same_action["status"] == "done"
    assert regenerated["summary"]["top_action_id"] == expected_next["id"]
    assert regenerated["summary"]["next_best_move"] == expected_next["action"]
    assert metrics["open_actions"] == len([a for a in regenerated["next_actions"] if a["status"] == "open"])


def test_sqlite_export_preserves_public_redaction_and_daily_brief(tmp_path):
    private, public = crm_generate.generate(ROOT, db_file=tmp_path / "crm.sqlite")

    redaction.assert_public_safe(public)
    public_text = json.dumps(public) + crm_db.daily_brief(public)
    assert "Halpin Plumbing Inc" not in public_text
    assert "Empire Contractors LLC" not in public_text
    assert "outreach/owner-operated-call-summaries.json" not in public_text
    assert '"phone"' not in public_text.lower()
    assert '"email"' not in public_text.lower()
    assert "Local prospect" in public_text
    assert "# MMTVU Daily Operator Brief" in crm_db.daily_brief(private)


def test_revenue_os_validation_rejects_missing_loop_fields(tmp_path):
    private, _ = crm_generate.generate(ROOT, db_file=tmp_path / "crm.sqlite")
    broken = json.loads(json.dumps(private))
    del broken["loops"][0]["goal"]

    try:
        crm_generate.validate_records(ROOT, broken)
    except ValueError as exc:
        assert "loops[0] missing required keys: goal" in str(exc)
    else:
        raise AssertionError("missing loop goal should fail validation")


def test_status_vocabularies_match_schema_enums():
    prospect_schema = json.loads((ROOT / "ops-crm" / "schemas" / "prospect.schema.json").read_text())
    action_schema = json.loads((ROOT / "ops-crm" / "schemas" / "next_action.schema.json").read_text())

    assert set(prospect_schema["properties"]["status"]["enum"]) == set(crm_db.FUNNEL_STATUSES)
    assert set(action_schema["properties"]["status"]["enum"]) == crm_db.ACTION_STATUSES


def test_init_db_migrates_legacy_statuses_to_funnel_vocabulary(tmp_path):
    conn = sqlite3.connect(tmp_path / "crm.sqlite")
    conn.row_factory = sqlite3.Row
    crm_db.init_db(conn)
    now = "2026-07-09T00:00:00Z"
    legacy = [
        ("p-new", "new"),
        ("p-contacted", "contacted"),
        ("p-nfu", "needs_follow_up"),
        ("p-notfit", "not_fit"),
        ("p-booked", "booked"),
        ("p-customer", "customer"),
    ]
    for pid, status in legacy:
        conn.execute(
            "INSERT INTO prospects(id, company_name, priority, status, created_at, updated_at, payload_json) VALUES(?,?,?,?,?,?,?)",
            (pid, "Co", "low", status, now, now, "{}"),
        )
    conn.execute(
        "INSERT INTO metrics(id, metric_name, metric_value, unit, measured_at, source) VALUES('metric-money_signal_actions','money_signal_actions',3,'count',?,'x')",
        (now,),
    )
    conn.commit()

    crm_db.init_db(conn)

    got = {row["id"]: row["status"] for row in conn.execute("SELECT id, status FROM prospects")}
    assert got == {
        "p-new": "not_contacted",
        "p-contacted": "contacted",
        "p-nfu": "contacted",
        "p-notfit": "not_fit",
        "p-booked": "discovery_booked",
        "p-customer": "won",
    }
    assert conn.execute("SELECT COUNT(*) FROM metrics WHERE metric_name='money_signal_actions'").fetchone()[0] == 0


def test_init_db_migrates_pre_target_metrics_table(tmp_path):
    # Finding #11: every other test's metrics table already has `target` from the
    # CREATE TABLE statement, so the ALTER TABLE ... ADD COLUMN target guard is dead
    # code from the suite's perspective — it only proves itself against a real
    # pre-change crm.sqlite, which isn't regenerable. Build that old shape by hand.
    conn = sqlite3.connect(tmp_path / "crm.sqlite")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE metrics (
            id TEXT PRIMARY KEY,
            metric_name TEXT NOT NULL,
            metric_value REAL NOT NULL,
            unit TEXT NOT NULL,
            measured_at TEXT NOT NULL,
            source TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO metrics(id, metric_name, metric_value, unit, measured_at, source) VALUES(?,?,?,?,?,?)",
        ("metric-prospects_total", "prospects_total", 5, "count", "2026-07-09T00:00:00Z", "test"),
    )
    conn.commit()

    crm_db.init_db(conn)

    cols = {row["name"] for row in conn.execute("PRAGMA table_info(metrics)")}
    assert "target" in cols
    row = conn.execute("SELECT * FROM metrics WHERE id='metric-prospects_total'").fetchone()
    assert row["metric_value"] == 5
    assert row["target"] is None


def test_init_db_adds_and_backfills_sequence_provenance_on_previous_schema(tmp_path):
    conn = sqlite3.connect(tmp_path / "crm.sqlite")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE prospects (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            contact_endpoint_type TEXT,
            contact_endpoint_verified_at TEXT,
            sequence_planned_attempts INTEGER,
            sequence_completed_at TEXT,
            sequence_completion_reason TEXT
        );
        CREATE TABLE outreach_attempts (
            id TEXT PRIMARY KEY,
            prospect_id TEXT NOT NULL,
            attempted_at TEXT NOT NULL,
            channel TEXT NOT NULL
        );
        CREATE TABLE commercial_events (
            id TEXT PRIMARY KEY,
            prospect_id TEXT NOT NULL,
            attempt_id TEXT,
            occurred_at TEXT NOT NULL
        );
        INSERT INTO prospects VALUES(
            'p1', 'contacted', 'phone', '2026-07-10T08:00:00-04:00', 1,
            '2026-07-10T10:00:00-04:00', 'planned_attempts_completed'
        );
        INSERT INTO outreach_attempts VALUES(
            'a1', 'p1', '2026-07-10T09:00:00-04:00', 'phone'
        );
        INSERT INTO outreach_attempts VALUES(
            'a-before-actual-verification', 'p1', '2026-07-10T11:30:00Z', 'phone'
        );
        INSERT INTO outreach_attempts VALUES(
            'a-after-completion', 'p1', '2026-07-10T15:00:00Z', 'phone'
        );
        INSERT INTO commercial_events VALUES(
            'e1', 'p1', 'a1', '2026-07-10T09:30:00-04:00'
        );
        INSERT INTO commercial_events VALUES(
            'e-before', 'p1', 'a-before-actual-verification', '2026-07-10T11:45:00Z'
        );
        INSERT INTO commercial_events VALUES(
            'e-after', 'p1', 'a-after-completion', '2026-07-10T15:15:00Z'
        );
        """
    )
    conn.commit()

    crm_db.init_db(conn)
    crm_db.init_db(conn)

    prospect = conn.execute(
        "SELECT current_sequence_id, lifecycle_high_water_at FROM prospects WHERE id='p1'"
    ).fetchone()
    attempt = conn.execute(
        "SELECT sequence_id FROM outreach_attempts WHERE id='a1'"
    ).fetchone()
    event = conn.execute(
        "SELECT sequence_id FROM commercial_events WHERE id='e1'"
    ).fetchone()
    sequence = conn.execute(
        "SELECT prospect_id, completed_at FROM outreach_sequences WHERE id = ?",
        (prospect["current_sequence_id"],),
    ).fetchone()
    invalid_attempts = conn.execute(
        """SELECT id, sequence_id FROM outreach_attempts
           WHERE id IN ('a-before-actual-verification', 'a-after-completion') ORDER BY id"""
    ).fetchall()
    invalid_events = conn.execute(
        """SELECT id, sequence_id FROM commercial_events
           WHERE id IN ('e-before', 'e-after') ORDER BY id"""
    ).fetchall()

    assert prospect["current_sequence_id"] == attempt["sequence_id"] == event["sequence_id"]
    assert prospect["lifecycle_high_water_at"] == "2026-07-10T15:15:00Z"
    assert tuple(sequence) == ("p1", "2026-07-10T10:00:00-04:00")
    assert [(row["id"], row["sequence_id"]) for row in invalid_attempts] == [
        ("a-after-completion", None),
        ("a-before-actual-verification", None),
    ]
    assert [(row["id"], row["sequence_id"]) for row in invalid_events] == [
        ("e-after", None),
        ("e-before", None),
    ]


def test_init_db_does_not_bind_legacy_event_when_originating_attempt_is_ineligible(tmp_path):
    conn = sqlite3.connect(tmp_path / "crm.sqlite")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE prospects (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            contact_endpoint_type TEXT,
            contact_endpoint_verified_at TEXT,
            sequence_planned_attempts INTEGER,
            sequence_completed_at TEXT,
            sequence_completion_reason TEXT
        );
        CREATE TABLE outreach_attempts (
            id TEXT PRIMARY KEY,
            prospect_id TEXT NOT NULL,
            attempted_at TEXT NOT NULL,
            channel TEXT NOT NULL
        );
        CREATE TABLE commercial_events (
            id TEXT PRIMARY KEY,
            prospect_id TEXT NOT NULL,
            attempt_id TEXT,
            occurred_at TEXT NOT NULL
        );
        INSERT INTO prospects VALUES(
            'p1', 'contacted', 'phone', '2026-07-10T08:00:00-04:00', 1,
            '2026-07-10T10:00:00-04:00', 'planned_attempts_completed'
        );
        INSERT INTO outreach_attempts VALUES(
            'a-email', 'p1', '2026-07-10T09:00:00-04:00', 'email'
        );
        INSERT INTO commercial_events VALUES(
            'e-email', 'p1', 'a-email', '2026-07-10T09:30:00-04:00'
        );
        """
    )
    conn.commit()

    crm_db.init_db(conn)

    attempt = conn.execute(
        "SELECT sequence_id FROM outreach_attempts WHERE id='a-email'"
    ).fetchone()
    event = conn.execute(
        "SELECT sequence_id FROM commercial_events WHERE id='e-email'"
    ).fetchone()

    assert attempt["sequence_id"] is None
    assert event["sequence_id"] is None


def _funnel_dataset(now="2026-07-09T12:00:00Z"):
    def prospect(pid, status, owner=False):
        return {"id": pid, "company_name": pid, "priority": "low", "status": status, "owner_operator": owner}

    return {
        "generated_at": now,
        "prospects": [
            prospect("p1", "not_contacted"),
            prospect("p2", "contacted", owner=True),
            prospect("p3", "replied"),
            prospect("p4", "discovery_booked"),
            prospect("p5", "pilot_proposed"),
            prospect("p6", "won"),
            prospect("p7", "lost"),
            prospect("p8", "not_fit"),
            prospect("p9", "follow_up_later"),
        ],
        "next_actions": [
            {"id": "a1", "owner": "Hermes", "action": "call p2", "status": "open", "priority": "low", "score": 2.0},
            {"id": "a2", "owner": "Hermes", "action": "call p3", "status": "done", "priority": "low", "score": 1.0},
        ],
        "assets": [],
        "offers": [],
        "evidence": [
            {"id": "e1", "summary": "3 calls made", "cost_usd": 0.4506},
            {"id": "e2", "summary": "7 calls made", "cost_usd": 1.06},
            {"id": "e3", "summary": "no cost attached"},
        ],
    }


def test_import_refresh_does_not_advance_or_regress_lifecycle_chronology(tmp_path):
    with crm_db.connect(Path("."), tmp_path / "crm.sqlite") as conn:
        crm_db.import_dataset(conn, _funnel_dataset("2026-07-09T12:00:00Z"))
        crm_db.record_commercial_event(
            conn,
            {
                "id": "event-13",
                "prospect_id": "p1",
                "event_type": "discovery_scheduled",
                "occurred_at": "2026-07-09T13:00:00Z",
                "evidence_grade": "A",
                "artifact_ref": "private/calendar/13",
                "operator": "Matthew",
            },
        )
        crm_db.import_dataset(conn, _funnel_dataset("2026-07-09T15:00:00Z"))
        after_import = conn.execute(
            "SELECT updated_at, lifecycle_high_water_at FROM prospects WHERE id='p1'"
        ).fetchone()
        crm_db.record_commercial_event(
            conn,
            {
                "id": "event-14",
                "prospect_id": "p1",
                "event_type": "discovery_scheduled",
                "occurred_at": "2026-07-09T14:00:00Z",
                "evidence_grade": "A",
                "artifact_ref": "private/calendar/14",
                "operator": "Matthew",
            },
        )
        after_event = conn.execute(
            "SELECT updated_at, lifecycle_high_water_at FROM prospects WHERE id='p1'"
        ).fetchone()

    assert tuple(after_import) == ("2026-07-09T15:00:00Z", "2026-07-09T13:00:00Z")
    assert tuple(after_event) == ("2026-07-09T15:00:00Z", "2026-07-09T14:00:00Z")


def test_stale_import_cannot_archive_a_prospect_after_newer_lifecycle_evidence(tmp_path):
    with crm_db.connect(Path("."), tmp_path / "crm.sqlite") as conn:
        crm_db.import_dataset(conn, _funnel_dataset("2026-07-09T08:00:00Z"))
        crm_db.record_commercial_event(
            conn,
            {
                "id": "event-newer-than-import",
                "prospect_id": "p1",
                "event_type": "discovery_scheduled",
                "occurred_at": "2026-07-09T12:00:00Z",
                "evidence_grade": "A",
                "artifact_ref": "private/calendar/newer-than-import",
                "operator": "Matthew",
            },
        )
        stale = _funnel_dataset("2026-07-09T10:00:00Z")
        stale["prospects"] = [p for p in stale["prospects"] if p["id"] != "p1"]

        crm_db.import_dataset(conn, stale)

        row = conn.execute(
            """SELECT is_current, archived_at, updated_at, lifecycle_high_water_at
               FROM prospects WHERE id='p1'"""
        ).fetchone()

    assert tuple(row) == (
        1,
        None,
        "2026-07-09T12:00:00Z",
        "2026-07-09T12:00:00Z",
    )


def test_stale_import_cannot_reactivate_a_prospect_after_newer_archival(tmp_path):
    with crm_db.connect(Path("."), tmp_path / "crm.sqlite") as conn:
        crm_db.import_dataset(conn, _funnel_dataset("2026-07-09T08:00:00Z"))
        current_without_p1 = _funnel_dataset("2026-07-09T12:00:00Z")
        current_without_p1["prospects"] = [
            p for p in current_without_p1["prospects"] if p["id"] != "p1"
        ]
        crm_db.import_dataset(conn, current_without_p1)

        crm_db.import_dataset(conn, _funnel_dataset("2026-07-09T10:00:00Z"))

        row = conn.execute(
            """SELECT is_current, archived_at, updated_at, lifecycle_high_water_at
               FROM prospects WHERE id='p1'"""
        ).fetchone()

    assert tuple(row) == (
        0,
        "2026-07-09T12:00:00Z",
        "2026-07-09T12:00:00Z",
        "2026-07-09T12:00:00Z",
    )


def test_export_metrics_are_funnel_counts_from_one_definition_site(tmp_path):
    now = "2026-07-09T12:00:00Z"
    with crm_db.connect(Path("."), tmp_path / "crm.sqlite") as conn:
        crm_db.import_dataset(conn, _funnel_dataset(now))
        # Metrics must reflect operator changes made AFTER import: they are
        # computed at export time, not frozen at import time.
        conn.execute("UPDATE prospects SET status = 'replied' WHERE id = 'p2'")
        conn.commit()
        exported = crm_db.export_dataset(conn, now)

    metrics = {m["metric_name"]: m for m in exported["metrics"]}
    assert "money_signal_actions" not in metrics
    assert metrics["prospects_total"]["metric_value"] == 9
    assert metrics["prospects_total"]["target"] == 50
    # Cumulative: everything at-or-past a first touch counts as contacted;
    # not_fit stays out so disqualifications never inflate the 30-contacted target.
    assert metrics["contacted_total"]["metric_value"] == 7
    assert metrics["contacted_total"]["target"] == 30
    assert metrics["discovery_booked_total"]["metric_value"] == 3
    assert metrics["discovery_booked_total"]["target"] == 5
    assert metrics["pilot_proposed_total"]["metric_value"] == 2
    assert metrics["pilot_proposed_total"]["target"] == 1
    assert metrics["open_actions"]["metric_value"] == 1
    assert metrics["owner_operator_prospects"]["metric_value"] == 1
    assert metrics["documented_call_spend"]["metric_value"] == 1.5106
    assert metrics["documented_call_spend"]["target"] is None


def test_record_outreach_attempt_commits_history_suppression_and_stage_atomically(tmp_path):
    now = "2026-07-09T12:00:00Z"
    with crm_db.connect(Path("."), tmp_path / "crm.sqlite") as conn:
        crm_db.import_dataset(conn, _funnel_dataset(now))

        record_id = crm_db.record_outreach_attempt(
            conn,
            {
                "id": "attempt-1",
                "prospect_id": "p1",
                "attempted_at": "2026-07-09T12:30:00-04:00",
                "channel": "phone",
                "dnc_checked": True,
                "contact_role": "owner",
                "disposition": "opt_out",
                "human_reached": True,
                "substantive_conversation": False,
                "operator": "Matthew",
            },
        )

        attempt = conn.execute("SELECT * FROM outreach_attempts WHERE id = ?", (record_id,)).fetchone()
        prospect = conn.execute("SELECT * FROM prospects WHERE id = 'p1'").fetchone()

    assert attempt["prospect_id"] == "p1"
    assert attempt["disposition"] == "opt_out"
    assert prospect["status"] == "not_contacted"
    assert prospect["max_stage_rank"] == crm_db.STAGE_RANK["not_contacted"]
    assert prospect["contact_suppressed_at"] == "2026-07-09T12:30:00-04:00"
    assert prospect["contact_suppression_reason"] == "explicit_opt_out"



def test_set_status_write_path_validates_and_persists(tmp_path):
    now = "2026-07-09T12:00:00Z"
    with crm_db.connect(Path("."), tmp_path / "crm.sqlite") as conn:
        crm_db.import_dataset(conn, _funnel_dataset(now))

        crm_db.set_action_status(conn, "a1", "done")
        crm_db.set_prospect_status(conn, "p1", "replied")

        assert conn.execute("SELECT status FROM actions WHERE id='a1'").fetchone()["status"] == "done"
        assert conn.execute("SELECT status FROM prospects WHERE id='p1'").fetchone()["status"] == "replied"

        with pytest.raises(ValueError, match="finished"):
            crm_db.set_action_status(conn, "a1", "finished")
        with pytest.raises(ValueError, match="maybe"):
            crm_db.set_prospect_status(conn, "p1", "maybe")
        with pytest.raises(KeyError, match="missing-action"):
            crm_db.set_action_status(conn, "missing-action", "done")
        with pytest.raises(KeyError, match="missing-prospect"):
            crm_db.set_prospect_status(conn, "missing-prospect", "replied")


def test_daily_brief_reports_funnel_progress_against_targets(tmp_path):
    now = "2026-07-09T12:00:00Z"
    with crm_db.connect(Path("."), tmp_path / "crm.sqlite") as conn:
        crm_db.import_dataset(conn, _funnel_dataset(now))
        exported = crm_db.export_dataset(conn, now)

    brief = crm_db.daily_brief(exported)

    assert "Identified prospects: 9/50" in brief
    assert "Contacted: 7/30" in brief
    assert "Discovery booked: 3/5" in brief
    assert "Pilots proposed: 2/1" in brief
    assert "Documented call spend: $1.5106" in brief
    assert "Money-signal" not in brief


def test_funnel_metrics_survive_off_ramp_after_real_progress(tmp_path):
    # Finding #5: a prospect that genuinely reached pilot_proposed still had that
    # discovery call and pilot pitch happen even if the deal later goes cold. The
    # cumulative totals must not decrement when it off-ramps to lost.
    now = "2026-07-09T12:00:00Z"
    with crm_db.connect(Path("."), tmp_path / "crm.sqlite") as conn:
        crm_db.import_dataset(conn, _funnel_dataset(now))

        for status in ("contacted", "replied", "discovery_booked", "pilot_proposed"):
            crm_db.set_prospect_status(conn, "p1", status)
        before = crm_db.export_dataset(conn, now)
        metrics_before = {m["metric_name"]: m["metric_value"] for m in before["metrics"]}

        crm_db.set_prospect_status(conn, "p1", "lost")
        after = crm_db.export_dataset(conn, now)
        metrics_after = {m["metric_name"]: m["metric_value"] for m in after["metrics"]}

    assert next(p for p in after["prospects"] if p["id"] == "p1")["status"] == "lost"
    assert metrics_after["discovery_booked_total"] == metrics_before["discovery_booked_total"]
    assert metrics_after["pilot_proposed_total"] == metrics_before["pilot_proposed_total"]
    assert metrics_after["contacted_total"] == metrics_before["contacted_total"]


def test_not_fit_still_vetoes_funnel_metrics_even_after_real_progress(tmp_path):
    # not_fit is a disqualification, not an off-ramp: it must never inflate the
    # outreach numbers even if the prospect had prior recorded progress.
    now = "2026-07-09T12:00:00Z"
    with crm_db.connect(Path("."), tmp_path / "crm.sqlite") as conn:
        crm_db.import_dataset(conn, _funnel_dataset(now))
        for status in ("contacted", "discovery_booked", "pilot_proposed"):
            crm_db.set_prospect_status(conn, "p1", status)

        crm_db.set_prospect_status(conn, "p1", "not_fit")
        exported = crm_db.export_dataset(conn, now)

    metrics = {m["metric_name"]: m["metric_value"] for m in exported["metrics"]}
    p1 = next(p for p in exported["prospects"] if p["id"] == "p1")
    assert p1["status"] == "not_fit"
    # p1 starts (not_contacted) and ends (not_fit) excluded from every bucket, so
    # the totals match _funnel_dataset's other 8 prospects, untouched by p1's detour.
    assert metrics["contacted_total"] == 7
    assert metrics["discovery_booked_total"] == 3
    assert metrics["pilot_proposed_total"] == 2


def test_fresh_import_at_off_ramp_status_has_no_assumed_history(tmp_path):
    # A prospect imported directly at "lost" (no prior set_prospect_status calls)
    # has no known discovery/pilot history — it should count as contacted only,
    # same as today's baseline, not retroactively credited with deeper stages.
    now = "2026-07-09T12:00:00Z"
    dataset = _funnel_dataset(now)
    with crm_db.connect(Path("."), tmp_path / "crm.sqlite") as conn:
        crm_db.import_dataset(conn, dataset)
        exported = crm_db.export_dataset(conn, now)

    metrics = {m["metric_name"]: m["metric_value"] for m in exported["metrics"]}
    # p7 is imported directly as "lost" in _funnel_dataset with no write-path history.
    assert metrics["discovery_booked_total"] == 3
    assert metrics["pilot_proposed_total"] == 2


def test_loop_experiment_metrics_identify_next_best_move(tmp_path):
    private, _ = crm_generate.generate(ROOT, db_file=tmp_path / "crm.sqlite")
    metrics = {m["metric_name"]: m["metric_value"] for m in private["metrics"]}

    assert metrics["owner_operator_prospects"] > 0
    assert metrics["documented_call_spend"] > 0
    assert private["summary"]["next_best_move"] == private["next_actions"][0]["action"]
    assert "Owner-operated" in private["loops"][0]["name"]
