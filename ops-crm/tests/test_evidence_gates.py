import importlib.util
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
DB_SPEC = importlib.util.spec_from_file_location("crm_db_gate_tests", ROOT / "ops-crm" / "db.py")
assert DB_SPEC is not None and DB_SPEC.loader is not None
crm_db = importlib.util.module_from_spec(DB_SPEC)
sys.modules["crm_db_gate_tests"] = crm_db
DB_SPEC.loader.exec_module(crm_db)


def seed_prospect(conn: sqlite3.Connection, prospect_id: str) -> None:
    crm_db.init_db(conn)
    now = "2026-07-10T12:00:00Z"
    conn.execute(
        """INSERT INTO prospects(id, company_name, priority, status, created_at, updated_at, payload_json)
           VALUES(?,?,?,?,?,?,?)""",
        (prospect_id, f"Test Roofing {prospect_id}", "high", "not_contacted", now, now, "{}"),
    )
    conn.commit()


def attempt(prospect_id: str, disposition: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": f"attempt-{prospect_id}-{disposition}",
        "prospect_id": prospect_id,
        "attempted_at": "2026-07-10T09:00:00-04:00",
        "channel": "phone",
        "dnc_checked": True,
        "disposition": disposition,
        "operator": "Matthew",
    }
    if disposition == "human_no_time":
        payload.update(human_reached=True, contact_role="owner")
    if disposition == "substantive_conversation":
        payload.update(
            human_reached=True,
            substantive_conversation=True,
            contact_role="owner",
            conversation_outcome="no_relevant_pain",
            pain_score=0,
            evidence_grade="B",
            evidence_text="Same-day exact buyer quote.",
        )
    payload.update(overrides)
    return payload


def metric_map(exported: dict[str, Any]) -> dict[str, float]:
    return {row["metric_name"]: row["metric_value"] for row in exported["metrics"]}


def test_init_db_adds_explicit_contactability_and_sequence_state(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        crm_db.init_db(conn)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(prospects)")}
    assert {
        "icp_fit_verified_at",
        "contact_endpoint_verified_at",
        "contact_endpoint_type",
        "sequence_planned_attempts",
        "sequence_completed_at",
        "sequence_completion_reason",
    }.issubset(columns)


def test_verified_contactable_and_completed_sequence_are_explicit_not_inferred(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn, "p1")
        seed_prospect(conn, "p2")
        crm_db.verify_prospect_for_outreach(conn, "p1", endpoint_type="phone", planned_attempts=1)
        crm_db.record_outreach_attempt(conn, attempt("p1", "no_answer"))
        crm_db.record_outreach_attempt(conn, attempt("p2", "no_answer"))
        before = metric_map(crm_db.export_dataset(conn, "2026-07-10T17:00:00Z"))
        crm_db.complete_outreach_sequence(conn, "p1", reason="planned_attempts_completed")
        after = metric_map(crm_db.export_dataset(conn, "2026-07-10T17:01:00Z"))

    assert before["outreach_verified_contactable_businesses_total"] == 1
    assert before["outreach_attempted_businesses_total"] == 1
    assert before["outreach_completed_sequences_total"] == 0
    assert after["outreach_completed_sequences_total"] == 1


def populate_calibration(conn: sqlite3.Connection, *, humans: int, substantive: int, qualified: int, buyer: bool) -> None:
    for index in range(10):
        pid = f"p{index}"
        seed_prospect(conn, pid)
        crm_db.verify_prospect_for_outreach(conn, pid, endpoint_type="phone", planned_attempts=1)
        if index < substantive:
            outcome = "paid_terms_requested" if index < qualified and buyer else (
                "pain_qualified_no_interest" if index < qualified else "no_relevant_pain"
            )
            crm_db.record_outreach_attempt(
                conn,
                attempt(
                    pid,
                    "substantive_conversation",
                    conversation_outcome=outcome,
                    pain_score=2 if index < qualified else 0,
                    eligible_unsold_estimates=10 if index < qualified else None,
                    ticket_value_band="$10k-$25k" if index < qualified else None,
                ),
            )
        elif index < humans:
            crm_db.record_outreach_attempt(conn, attempt(pid, "human_no_time"))
        else:
            crm_db.record_outreach_attempt(conn, attempt(pid, "no_answer"))
        crm_db.complete_outreach_sequence(conn, pid, reason="planned_attempts_completed")


def test_exact_ten_business_gate_thresholds_expand(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        populate_calibration(conn, humans=5, substantive=3, qualified=2, buyer=True)
        exported = crm_db.export_dataset(conn, "2026-07-10T18:00:00Z")

    metrics = metric_map(exported)
    assert metrics["outreach_relevant_human_reached_total"] == 5
    assert metrics["outreach_substantive_conversations_total"] == 3
    assert metrics["outreach_pain_qualified_total"] == 2
    assert metrics["outreach_paid_terms_requested_total"] == 2
    brief = crm_db.daily_brief(exported)
    assert "Reach gate: PASS" in brief
    assert "Pain/economics gate: PASS" in brief
    assert "Buyer-movement gate: PASS" in brief
    assert "Recommendation: EXPAND" in brief


@pytest.mark.parametrize(
    ("humans", "substantive", "qualified", "buyer", "failed_gate"),
    [
        (4, 3, 2, True, "Reach gate: FAIL"),
        (5, 2, 2, True, "Reach gate: FAIL"),
        (5, 3, 1, True, "Pain/economics gate: FAIL"),
        (5, 3, 2, False, "Buyer-movement gate: FAIL"),
    ],
)
def test_gate_boundaries_immediately_below_threshold_stop(
    tmp_path, humans, substantive, qualified, buyer, failed_gate
):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        populate_calibration(
            conn, humans=humans, substantive=substantive, qualified=qualified, buyer=buyer
        )
        exported = crm_db.export_dataset(conn, "2026-07-10T18:00:00Z")
    brief = crm_db.daily_brief(exported)
    assert failed_gate in brief
    assert "Recommendation: STOP" in brief


def test_zero_population_gates_are_understandable_and_continue(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        exported = crm_db.export_dataset(conn, "2026-07-10T18:00:00Z")
    brief = crm_db.daily_brief(exported)
    assert "Reach gate: NOT ENOUGH EVIDENCE" in brief
    assert "0/10" in brief
    assert "Recommendation: CONTINUE" in brief


def test_endpoint_failure_and_active_suppression_metrics_use_correct_populations(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        for pid, channel, disposition in [
            ("p-phone", "phone", "wrong_number"),
            ("p-email", "email", "email_bounced"),
            ("p-good", "linkedin", "linkedin_sent"),
        ]:
            seed_prospect(conn, pid)
            crm_db.verify_prospect_for_outreach(conn, pid, endpoint_type=channel, planned_attempts=1)
            crm_db.record_outreach_attempt(
                conn,
                attempt(pid, disposition, channel=channel),
            )
        conn.execute(
            "UPDATE prospects SET contact_suppressed_at=?, contact_suppression_reason=? WHERE id='p-good'",
            ("2026-07-10T12:00:00Z", "compliance_suppression"),
        )
        conn.commit()
        metrics = metric_map(crm_db.export_dataset(conn, "2026-07-10T18:00:00Z"))

    assert metrics["outreach_invalid_endpoints_total"] == 2
    assert metrics["outreach_endpoint_attempts_total"] == 3
    assert metrics["outreach_invalid_endpoint_rate"] == pytest.approx(2 / 3, abs=0.0001)
    assert metrics["outreach_suppressed_businesses_total"] == 1
