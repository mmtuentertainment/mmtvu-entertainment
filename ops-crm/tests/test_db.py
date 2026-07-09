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
    }.issubset(tables)


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


def test_loop_experiment_metrics_identify_next_best_move(tmp_path):
    private, _ = crm_generate.generate(ROOT, db_file=tmp_path / "crm.sqlite")
    metrics = {m["metric_name"]: m["metric_value"] for m in private["metrics"]}

    assert metrics["owner_operator_prospects"] > 0
    assert metrics["documented_call_spend"] > 0
    assert private["summary"]["next_best_move"] == private["next_actions"][0]["action"]
    assert "Owner-operated" in private["loops"][0]["name"]
