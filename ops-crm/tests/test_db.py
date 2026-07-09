import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

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


def test_sqlite_init_is_idempotent_and_has_revenue_os_tables(tmp_path):
    conn = sqlite3.connect(tmp_path / "crm.sqlite")
    conn.row_factory = sqlite3.Row

    crm_db.init_db(conn)
    crm_db.init_db(conn)

    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "prospects",
        "interactions",
        "actions",
        "loops",
        "runs",
        "artifacts",
        "notes",
        "decisions",
        "experiments",
        "metrics",
        "schema_migrations",
    }.issubset(tables)
    assert conn.execute("SELECT count(*) FROM schema_migrations WHERE version = 1").fetchone()[0] == 1


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

    crm_generate.assert_public_safe(public)
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


def test_loop_experiment_metrics_identify_next_best_move(tmp_path):
    private, _ = crm_generate.generate(ROOT, db_file=tmp_path / "crm.sqlite")
    metrics = {m["metric_name"]: m["metric_value"] for m in private["metrics"]}

    assert metrics["owner_operator_prospects"] > 0
    assert metrics["documented_call_spend"] > 0
    assert private["summary"]["next_best_move"] == private["next_actions"][0]["action"]
    assert "Owner-operated" in private["loops"][0]["name"]
