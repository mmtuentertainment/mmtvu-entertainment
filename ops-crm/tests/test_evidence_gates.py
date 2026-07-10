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
        crm_db.verify_prospect_for_outreach(
            conn, "p1", endpoint_type="phone", planned_attempts=1,
            verified_at="2026-07-10T08:00:00-04:00",
        )
        crm_db.record_outreach_attempt(conn, attempt("p1", "no_answer"))
        crm_db.record_outreach_attempt(conn, attempt("p2", "no_answer"))
        before = metric_map(crm_db.export_dataset(conn, "2026-07-10T17:00:00Z"))
        crm_db.complete_outreach_sequence(
            conn,
            "p1",
            reason="planned_attempts_completed",
            completed_at="2026-07-10T14:00:00Z",
        )
        after = metric_map(crm_db.export_dataset(conn, "2026-07-10T17:01:00Z"))

    assert before["outreach_verified_contactable_businesses_total"] == 1
    assert before["outreach_attempted_businesses_total"] == 1
    assert before["outreach_completed_sequences_total"] == 0
    assert after["outreach_completed_sequences_total"] == 1


def populate_calibration(conn: sqlite3.Connection, *, humans: int, substantive: int, qualified: int, buyer: bool) -> None:
    for index in range(10):
        pid = f"p{index}"
        seed_prospect(conn, pid)
        crm_db.verify_prospect_for_outreach(
            conn, pid, endpoint_type="phone", planned_attempts=1,
            verified_at="2026-07-10T08:00:00-04:00",
        )
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
        crm_db.complete_outreach_sequence(
            conn,
            pid,
            reason="planned_attempts_completed",
            completed_at="2026-07-10T14:00:00Z",
        )


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


def test_incomplete_strong_sequences_cannot_supply_completed_cohort_gates(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        populate_calibration(conn, humans=0, substantive=0, qualified=0, buyer=False)
        for index in range(5):
            pid = f"incomplete-{index}"
            seed_prospect(conn, pid)
            crm_db.verify_prospect_for_outreach(
                conn, pid, endpoint_type="phone", planned_attempts=2,
                verified_at="2026-07-10T08:00:00-04:00",
            )
            crm_db.record_outreach_attempt(
                conn,
                attempt(
                    pid,
                    "substantive_conversation",
                    conversation_outcome="paid_terms_requested",
                    pain_score=2,
                    eligible_unsold_estimates=10,
                    ticket_value_band="$10k-$25k",
                ),
            )
        exported = crm_db.export_dataset(conn, "2026-07-10T18:00:00Z")

    metrics = metric_map(exported)
    assert metrics["outreach_completed_sequences_total"] == 10
    assert metrics["outreach_relevant_human_reached_total"] == 0
    assert metrics["outreach_substantive_conversations_total"] == 0
    assert metrics["outreach_pain_qualified_total"] == 0
    assert metrics["outreach_paid_terms_requested_total"] == 0
    brief = crm_db.daily_brief(exported)
    assert "Recommendation: STOP" in brief
    assert "Recommendation: EXPAND" not in brief


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
            crm_db.verify_prospect_for_outreach(
                conn, pid, endpoint_type=channel, planned_attempts=1,
                verified_at="2026-07-10T08:00:00-04:00",
            )
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


def test_attempt_metrics_remain_bound_to_original_sequence_after_reverification(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn, "p1")
        crm_db.verify_prospect_for_outreach(
            conn, "p1", endpoint_type="phone", planned_attempts=1,
            verified_at="2026-07-10T08:00:00-04:00",
        )
        crm_db.record_outreach_attempt(conn, attempt("p1", "no_answer"))
        crm_db.complete_outreach_sequence(
            conn, "p1", reason="planned_attempts_completed",
            completed_at="2026-07-10T10:00:00-04:00",
        )
        before = crm_db.compute_outreach_metrics(conn)

        crm_db.verify_prospect_for_outreach(
            conn, "p1", endpoint_type="email", planned_attempts=1,
            verified_at="2026-07-10T11:00:00-04:00",
        )
        after = crm_db.compute_outreach_metrics(conn)

    assert before["outreach_attempted_businesses_total"][0] == 1
    assert before["outreach_completed_sequences_total"][0] == 1
    assert after["outreach_attempted_businesses_total"][0] == 1
    assert after["outreach_completed_sequences_total"][0] == 1


def test_sequence_completion_uses_only_attempts_from_current_plan_and_chronology(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn, "p1")
        crm_db.verify_prospect_for_outreach(
            conn, "p1", endpoint_type="phone", planned_attempts=1,
            verified_at="2026-07-10T08:00:00-04:00",
        )
        crm_db.record_outreach_attempt(conn, attempt("p1", "no_answer"))

        with pytest.raises(ValueError, match="before.*attempt"):
            crm_db.complete_outreach_sequence(
                conn, "p1", reason="planned_attempts_completed",
                completed_at="2026-07-10T08:30:00-04:00",
            )

        crm_db.verify_prospect_for_outreach(
            conn, "p1", endpoint_type="phone", planned_attempts=1,
            verified_at="2026-07-10T10:00:00-04:00",
        )
        with pytest.raises(ValueError, match="planned attempts are not complete"):
            crm_db.complete_outreach_sequence(
                conn, "p1", reason="planned_attempts_completed",
                completed_at="2026-07-10T11:00:00-04:00",
            )


def test_commercial_gate_metrics_require_a_completed_eligible_sequence(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn, "outside")
        crm_db.record_commercial_event(
            conn,
            {
                "id": "event-outside", "prospect_id": "outside",
                "event_type": "discovery_scheduled",
                "occurred_at": "2026-07-10T10:00:00-04:00",
                "evidence_grade": "A", "artifact_ref": "private/calendar/outside",
                "operator": "Matthew",
            },
        )
        outside = crm_db.compute_outreach_metrics(conn)

        seed_prospect(conn, "inside")
        crm_db.verify_prospect_for_outreach(
            conn, "inside", endpoint_type="phone", planned_attempts=1,
            verified_at="2026-07-10T08:00:00-04:00",
        )
        crm_db.record_outreach_attempt(
            conn, attempt("inside", "no_answer", id="attempt-inside")
        )
        crm_db.complete_outreach_sequence(
            conn, "inside", reason="planned_attempts_completed",
            completed_at="2026-07-10T10:00:00-04:00",
        )
        crm_db.record_commercial_event(
            conn,
            {
                "id": "event-inside", "prospect_id": "inside",
                "attempt_id": "attempt-inside", "event_type": "discovery_scheduled",
                "occurred_at": "2026-07-10T11:00:00-04:00",
                "evidence_grade": "A", "artifact_ref": "private/calendar/inside",
                "operator": "Matthew",
            },
        )
        inside = crm_db.compute_outreach_metrics(conn)

    assert outside["outreach_completed_sequences_total"][0] == 0
    assert outside["outreach_discovery_booked_total"][0] == 0
    assert inside["outreach_completed_sequences_total"][0] == 1
    assert inside["outreach_discovery_booked_total"][0] == 1


def test_gatekeeper_block_rate_counts_gatekeepers_as_live_call_connects(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        for index, disposition in enumerate(
            ("gatekeeper_no_transfer", "gatekeeper_no_transfer", "human_no_time")
        ):
            pid = f"p{index}"
            seed_prospect(conn, pid)
            crm_db.verify_prospect_for_outreach(
                conn, pid, endpoint_type="phone", planned_attempts=1,
                verified_at="2026-07-10T08:00:00-04:00",
            )
            crm_db.record_outreach_attempt(
                conn,
                attempt(
                    pid, disposition, id=f"attempt-{index}",
                    human_reached=disposition == "human_no_time",
                    contact_role="owner" if disposition == "human_no_time" else None,
                ),
            )
        metrics = crm_db.compute_outreach_metrics(conn)

    assert metrics["outreach_gatekeeper_blocks_total"][0] == 2
    assert metrics["outreach_live_call_connects_total"][0] == 3
    assert metrics["outreach_gatekeeper_block_rate"][0] == pytest.approx(2 / 3, abs=0.0001)


def test_sequence_and_fact_provenance_cannot_be_rewritten(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn, "p1")
        crm_db.verify_prospect_for_outreach(
            conn, "p1", endpoint_type="phone", planned_attempts=1,
            verified_at="2026-07-10T08:00:00-04:00",
        )
        crm_db.record_outreach_attempt(conn, attempt("p1", "no_answer"))
        crm_db.complete_outreach_sequence(
            conn, "p1", reason="planned_attempts_completed",
            completed_at="2026-07-10T10:00:00-04:00",
        )
        crm_db.record_commercial_event(
            conn,
            {
                "id": "event-immutable", "prospect_id": "p1",
                "attempt_id": "attempt-p1-no_answer", "event_type": "discovery_scheduled",
                "occurred_at": "2026-07-10T11:00:00-04:00",
                "evidence_grade": "A", "artifact_ref": "private/calendar/immutable",
                "operator": "Matthew",
            },
        )
        sequence_id = conn.execute(
            "SELECT current_sequence_id FROM prospects WHERE id='p1'"
        ).fetchone()[0]

        with pytest.raises(sqlite3.IntegrityError, match="attempt provenance is immutable"):
            conn.execute("UPDATE outreach_attempts SET sequence_id = NULL WHERE id='attempt-p1-no_answer'")
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="outreach attempts are append-only"):
            conn.execute("DELETE FROM outreach_attempts WHERE id='attempt-p1-no_answer'")
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="commercial event provenance is immutable"):
            conn.execute("UPDATE commercial_events SET sequence_id = NULL WHERE id='event-immutable'")
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="commercial events are append-only"):
            conn.execute("DELETE FROM commercial_events WHERE id='event-immutable'")
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="completion is append-only"):
            conn.execute(
                "UPDATE outreach_sequences SET completed_at = NULL WHERE id = ?",
                (sequence_id,),
            )


@pytest.mark.parametrize(
    ("table", "record_id", "mutation"),
    [
        ("outreach_attempts", "attempt-p1-no_answer", "disposition = 'wrong_number'"),
        ("outreach_attempts", "attempt-p1-no_answer", "evidence_text = 'rewritten fact'"),
        ("commercial_events", "event-immutable", "event_type = 'payment_received'"),
        ("commercial_events", "event-immutable", "occurred_at = '2026-07-11T00:00:00Z'"),
        ("commercial_events", "event-immutable", "price_amount = 500"),
    ],
)
def test_recorded_attempt_and_commercial_facts_are_fully_immutable(
    tmp_path, table, record_id, mutation
):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn, "p1")
        crm_db.verify_prospect_for_outreach(
            conn, "p1", endpoint_type="phone", planned_attempts=1,
            verified_at="2026-07-10T08:00:00-04:00",
        )
        crm_db.record_outreach_attempt(conn, attempt("p1", "no_answer"))
        crm_db.complete_outreach_sequence(
            conn, "p1", reason="planned_attempts_completed",
            completed_at="2026-07-10T10:00:00-04:00",
        )
        crm_db.record_commercial_event(
            conn,
            {
                "id": "event-immutable", "prospect_id": "p1",
                "attempt_id": "attempt-p1-no_answer", "event_type": "discovery_scheduled",
                "occurred_at": "2026-07-10T11:00:00-04:00",
                "evidence_grade": "A", "artifact_ref": "private/calendar/immutable",
                "operator": "Matthew",
            },
        )

        with pytest.raises(sqlite3.IntegrityError, match="immutable|append-only"):
            conn.execute(f"UPDATE {table} SET {mutation} WHERE id = ?", (record_id,))
