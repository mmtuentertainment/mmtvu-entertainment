import importlib.util
import json
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
DB_SPEC = importlib.util.spec_from_file_location("crm_db_record_tests", ROOT / "ops-crm" / "db.py")
assert DB_SPEC is not None and DB_SPEC.loader is not None
crm_db = importlib.util.module_from_spec(DB_SPEC)
sys.modules["crm_db_record_tests"] = crm_db
DB_SPEC.loader.exec_module(crm_db)


def load_cli():
    spec = importlib.util.spec_from_file_location(
        "crm_record_outreach_cli_tests", ROOT / "ops-crm" / "record_outreach.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["crm_record_outreach_cli_tests"] = module
    spec.loader.exec_module(module)
    return module


def seed_prospect(conn: sqlite3.Connection, prospect_id: str = "p1") -> None:
    crm_db.init_db(conn)
    now = "2026-07-09T12:00:00Z"
    conn.execute(
        """INSERT INTO prospects(
            id, company_name, priority, status, created_at, updated_at, payload_json
        ) VALUES(?,?,?,?,?,?,?)""",
        (prospect_id, "Test Roofing", "high", "not_contacted", now, now, "{}"),
    )
    conn.commit()


def valid_attempt(**overrides: Any) -> dict[str, Any]:
    attempt: dict[str, Any] = {
        "id": "attempt-valid",
        "prospect_id": "p1",
        "attempted_at": "2026-07-10T09:00:00-04:00",
        "channel": "phone",
        "dnc_checked": True,
        "disposition": "no_answer",
        "operator": "Matthew",
    }
    attempt.update(overrides)
    return attempt


def valid_attempt_for(disposition: str, channel: str) -> dict[str, Any]:
    attempt = valid_attempt(
        id=f"attempt-{channel}-{disposition}",
        channel=channel,
        disposition=disposition,
    )
    if disposition in {"human_no_time", "email_replied", "linkedin_replied", "opt_out"}:
        attempt.update(human_reached=True, contact_role="owner")
    if disposition == "substantive_conversation":
        attempt.update(
            human_reached=True,
            substantive_conversation=True,
            contact_role="owner",
            conversation_outcome="no_relevant_pain",
            pain_score=0,
            evidence_grade="B",
            evidence_text="Same-day exact quote confirms no relevant pain.",
        )
    return attempt


def test_attempt_consistency_matrix_covers_every_disposition_and_channel_pair():
    expected = {
        "phone": {
            "no_answer", "voicemail_left", "ivr_blocked", "wrong_number",
            "gatekeeper_no_transfer", "human_no_time", "substantive_conversation", "opt_out",
        },
        "email": {"email_sent", "email_replied", "email_bounced", "substantive_conversation", "opt_out"},
        "linkedin": {"linkedin_sent", "linkedin_replied", "substantive_conversation", "opt_out"},
    }
    assert set(crm_db.ATTEMPT_CONSISTENCY_MATRIX) == set(crm_db.ATTEMPT_DISPOSITIONS)
    for channel in crm_db.OUTREACH_CHANNELS:
        for disposition in crm_db.ATTEMPT_DISPOSITIONS:
            attempt = valid_attempt_for(disposition, channel)
            if disposition in expected[channel]:
                crm_db._validate_outreach_attempt(attempt)
            else:
                with pytest.raises(ValueError, match="not valid for channel"):
                    crm_db._validate_outreach_attempt(attempt)


@pytest.mark.parametrize(
    "contradiction",
    [
        {"disposition": "no_answer", "human_reached": True},
        {"disposition": "no_answer", "substantive_conversation": True, "human_reached": True},
        {"disposition": "substantive_conversation", "human_reached": False, "substantive_conversation": False},
        {"disposition": "human_no_time", "human_reached": False},
        {"disposition": "email_replied", "channel": "email", "human_reached": False},
        {"disposition": "no_answer", "conversation_outcome": "no_relevant_pain", "pain_score": 3},
    ],
)
def test_attempt_matrix_rejects_contradictory_facts_before_write(tmp_path, contradiction):
    attempt = valid_attempt(id="attempt-contradiction", **contradiction)
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)
        with pytest.raises(ValueError):
            crm_db.record_outreach_attempt(conn, attempt)
        assert conn.execute("SELECT COUNT(*) FROM outreach_attempts").fetchone()[0] == 0
        assert conn.execute("SELECT status FROM prospects WHERE id='p1'").fetchone()[0] == "not_contacted"


@pytest.mark.parametrize(
    ("outcome", "score"),
    [
        ("no_relevant_pain", 0),
        ("pain_unqualified", 1),
        ("pain_qualified_no_interest", 2),
        ("follow_up_requested", 2),
        ("paid_terms_requested", 2),
    ],
)
def test_every_supported_conversation_outcome_has_a_valid_evidence_qualified_combination(outcome, score):
    attempt = valid_attempt(
        disposition="substantive_conversation",
        human_reached=True,
        substantive_conversation=True,
        contact_role="owner",
        conversation_outcome=outcome,
        pain_score=score,
        evidence_grade="B",
        evidence_text="Same-day exact quote with role and economics.",
        eligible_unsold_estimates=10,
        ticket_value_band="$10k-$25k",
    )
    crm_db._validate_outreach_attempt(attempt)


@pytest.mark.parametrize("outcome", ["pain_qualified_no_interest", "follow_up_requested", "paid_terms_requested"])
def test_positive_buyer_movement_requires_evidence_role_and_economics(tmp_path, outcome):
    base = valid_attempt(
        disposition="substantive_conversation",
        human_reached=True,
        substantive_conversation=True,
        contact_role="owner",
        conversation_outcome=outcome,
        pain_score=2,
        evidence_grade="B",
        evidence_text="Same-day exact buyer quote.",
        eligible_unsold_estimates=10,
        ticket_value_band="$10k-$25k",
    )
    for index, field in enumerate(("evidence_grade", "evidence_text", "contact_role", "eligible_unsold_estimates", "ticket_value_band")):
        attempt = {**base, "id": f"attempt-missing-{index}"}
        attempt.pop(field)
        with crm_db.connect(ROOT, tmp_path / f"crm-{index}.sqlite") as conn:
            seed_prospect(conn)
            with pytest.raises(ValueError):
                crm_db.record_outreach_attempt(conn, attempt)
            assert conn.execute("SELECT COUNT(*) FROM outreach_attempts").fetchone()[0] == 0


def test_attempt_requires_strict_true_dnc_acknowledgement(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)
        for index, value in enumerate((None, False, "true", 1)):
            attempt = valid_attempt(id=f"attempt-dnc-{index}")
            if value is None:
                attempt.pop("dnc_checked")
            else:
                attempt["dnc_checked"] = value
            with pytest.raises(ValueError, match="dnc_checked"):
                crm_db.record_outreach_attempt(conn, attempt)
        assert conn.execute("SELECT COUNT(*) FROM outreach_attempts").fetchone()[0] == 0


def test_attempt_boolean_fields_are_strictly_typed(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)
        with pytest.raises(ValueError, match="human_reached"):
            crm_db.record_outreach_attempt(conn, valid_attempt(human_reached="false"))
        assert conn.execute("SELECT status FROM prospects WHERE id='p1'").fetchone()[0] == "not_contacted"


def test_grade_b_evidence_requires_contact_role(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)
        with pytest.raises(ValueError, match="contact_role"):
            crm_db.record_outreach_attempt(
                conn,
                valid_attempt(
                    disposition="substantive_conversation",
                    human_reached=True,
                    substantive_conversation=True,
                    conversation_outcome="pain_unqualified",
                    pain_score=1,
                    evidence_grade="B",
                    evidence_text="Exact same-day quote",
                ),
            )


def test_material_pain_rejects_grade_c_without_any_write(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)

        with pytest.raises(ValueError, match="pain_score"):
            crm_db.record_outreach_attempt(
                conn,
                {
                    "id": "attempt-weak-pain",
                    "prospect_id": "p1",
                    "attempted_at": "2026-07-10T09:00:00-04:00",
                    "channel": "phone",
                    "dnc_checked": True,
                    "disposition": "substantive_conversation",
                    "human_reached": True,
                    "substantive_conversation": True,
                    "conversation_outcome": "paid_terms_requested",
                    "pain_score": 2,
                    "contact_role": "owner",
                    "evidence_grade": "C",
                    "evidence_text": "I remember they seemed interested",
                    "operator": "Matthew",
                },
            )

        assert conn.execute("SELECT COUNT(*) FROM outreach_attempts").fetchone()[0] == 0
        prospect = conn.execute("SELECT status, max_stage_rank FROM prospects WHERE id='p1'").fetchone()
        assert prospect["status"] == "not_contacted"
        assert prospect["max_stage_rank"] == 0



def test_unsuppress_requires_reason_before_future_attempt(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)
        conn.execute(
            """UPDATE prospects SET contact_suppressed_at=?, contact_suppression_reason=?
               WHERE id='p1'""",
            ("2026-07-10T09:00:00-04:00", "explicit_opt_out"),
        )
        conn.commit()

        with pytest.raises(ValueError, match="reason"):
            crm_db.unsuppress_prospect(conn, "p1", "")

        crm_db.unsuppress_prospect(conn, "p1", "written re-consent received")
        row = conn.execute(
            "SELECT contact_suppressed_at, contact_suppression_reason FROM prospects WHERE id='p1'"
        ).fetchone()

    assert row["contact_suppressed_at"] is None
    assert row["contact_suppression_reason"] == "unsuppressed: written re-consent received"



def test_gated_commercial_milestone_rejects_grade_b_note(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)

        with pytest.raises(ValueError, match="grade A"):
            crm_db.record_commercial_event(
                conn,
                {
                    "id": "event-acceptance-weak",
                    "prospect_id": "p1",
                    "event_type": "paid_pilot_accepted",
                    "occurred_at": "2026-07-10T09:00:00-04:00",
                    "evidence_grade": "B",
                    "evidence_text": "Same-day note: buyer sounded committed",
                    "operator": "Matthew",
                },
            )

        assert conn.execute("SELECT COUNT(*) FROM commercial_events").fetchone()[0] == 0
        assert conn.execute("SELECT status FROM prospects WHERE id='p1'").fetchone()[0] == "not_contacted"



def test_payment_event_requires_direct_artifact_and_advances_to_won(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)

        event_id = crm_db.record_commercial_event(
            conn,
            {
                "id": "event-payment-1",
                "prospect_id": "p1",
                "event_type": "payment_received",
                "occurred_at": "2026-07-10T09:00:00-04:00",
                "offer_version": "founding-pilot-v1",
                "price_amount": 500,
                "price_currency": "USD",
                "evidence_grade": "A",
                "artifact_ref": "private-receipt-001",
                "operator": "Matthew",
            },
        )

        event = conn.execute("SELECT * FROM commercial_events WHERE id = ?", (event_id,)).fetchone()
        prospect = conn.execute("SELECT * FROM prospects WHERE id = 'p1'").fetchone()

    assert event["event_type"] == "payment_received"
    assert event["price_amount"] == 500
    assert prospect["status"] == "won"
    assert prospect["max_stage_rank"] == crm_db.STAGE_RANK["won"]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"price_amount": 1}, "exactly"),
        ({"price_amount": 499.99}, "exactly"),
        ({"price_amount": True}, "exactly"),
        ({"price_amount": 0}, "exactly"),
        ({"price_amount": -500}, "exactly"),
        ({"price_amount": float("nan")}, "exactly"),
        ({"price_amount": float("inf")}, "exactly"),
        ({"price_amount": "500"}, "exactly"),
        ({"price_currency": "EUR"}, "USD"),
        ({"price_currency": ""}, "USD"),
        ({"price_currency": None}, "USD"),
        ({"offer_version": "founding-pilot-v2"}, "offer_version"),
        ({"offer_version": ""}, "offer_version"),
        ({"offer_version": None}, "offer_version"),
    ],
)
def test_noncontract_payment_is_rejected_atomically(tmp_path, overrides, message):
    event = {
        "id": "event-invalid-payment",
        "prospect_id": "p1",
        "event_type": "payment_received",
        "occurred_at": "2026-07-10T09:00:00-04:00",
        "offer_version": "founding-pilot-v1",
        "price_amount": 500,
        "price_currency": "USD",
        "evidence_grade": "A",
        "artifact_ref": "private-receipt-invalid",
        "operator": "Matthew",
    }
    event.update(overrides)
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)
        with pytest.raises(ValueError, match=message):
            crm_db.record_commercial_event(conn, event)
        assert conn.execute("SELECT COUNT(*) FROM commercial_events").fetchone()[0] == 0
        row = conn.execute(
            "SELECT status, max_stage_rank FROM prospects WHERE id='p1'"
        ).fetchone()
        assert row["status"] == "not_contacted"
        assert row["max_stage_rank"] == 0


def test_commercial_event_attempt_must_belong_to_same_prospect(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn, "p1")
        seed_prospect(conn, "p2")
        crm_db.record_outreach_attempt(conn, valid_attempt(id="attempt-p1", prospect_id="p1"))
        with pytest.raises(ValueError, match="same prospect"):
            crm_db.record_commercial_event(
                conn,
                {
                    "id": "event-cross-prospect",
                    "prospect_id": "p2",
                    "attempt_id": "attempt-p1",
                    "event_type": "discovery_completed",
                    "occurred_at": "2026-07-10T12:00:00-04:00",
                    "evidence_grade": "B",
                    "evidence_text": "Same-day completion note",
                    "operator": "Matthew",
                },
            )
        assert conn.execute("SELECT COUNT(*) FROM commercial_events").fetchone()[0] == 0


def test_suppression_survives_reimport_and_blocks_contact(tmp_path):
    now = "2026-07-10T12:00:00Z"
    dataset = {
        "generated_at": now,
        "prospects": [
            {"id": "p1", "company_name": "Test Roofing", "priority": "high", "status": "not_contacted"}
        ],
        "next_actions": [],
        "assets": [],
        "offers": [],
        "evidence": [],
    }
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        crm_db.import_dataset(conn, dataset)
        conn.execute(
            "UPDATE prospects SET contact_suppressed_at=?, contact_suppression_reason=? WHERE id='p1'",
            ("2026-07-10T09:00:00-04:00", "explicit_opt_out"),
        )
        conn.commit()
        crm_db.import_dataset(conn, dataset)

        row = conn.execute(
            "SELECT contact_suppressed_at, contact_suppression_reason FROM prospects WHERE id='p1'"
        ).fetchone()
        assert row["contact_suppressed_at"] == "2026-07-10T09:00:00-04:00"
        assert row["contact_suppression_reason"] == "explicit_opt_out"

        with pytest.raises(ValueError, match="contact-suppressed"):
            crm_db.record_outreach_attempt(
                conn,
                {
                    "id": "attempt-blocked",
                    "prospect_id": "p1",
                    "attempted_at": "2026-07-10T10:00:00-04:00",
                    "channel": "phone",
                    "dnc_checked": True,
                    "disposition": "no_answer",
                    "operator": "Matthew",
                },
            )
        assert conn.execute("SELECT COUNT(*) FROM outreach_attempts").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("event_type", "expected_status"),
    [
        ("discovery_scheduled", "discovery_booked"),
        ("paid_proposal_sent", "pilot_proposed"),
        ("paid_pilot_accepted", "pilot_proposed"),
        ("paid_pilot_declined", "lost"),
        ("payment_received", "won"),
    ],
)
def test_commercial_events_have_deterministic_stage_effects(tmp_path, event_type, expected_status):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)
        event = {
            "id": f"event-{event_type}",
            "prospect_id": "p1",
            "event_type": event_type,
            "occurred_at": "2026-07-10T09:00:00-04:00",
            "evidence_grade": "A",
            "artifact_ref": f"private-{event_type}-artifact",
            "operator": "Matthew",
        }
        if event_type == "payment_received":
            event.update(
                {
                    "offer_version": "founding-pilot-v1",
                    "price_amount": 500,
                    "price_currency": "USD",
                }
            )
        crm_db.record_commercial_event(conn, event)
        assert conn.execute("SELECT status FROM prospects WHERE id='p1'").fetchone()[0] == expected_status


def test_later_attempt_cannot_regress_evidence_stage(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)
        crm_db.set_prospect_status(conn, "p1", "discovery_booked")
        crm_db.record_outreach_attempt(
            conn,
            {
                "id": "attempt-after-booking",
                "prospect_id": "p1",
                "attempted_at": "2026-07-10T11:00:00-04:00",
                "channel": "phone",
                "dnc_checked": True,
                "disposition": "no_answer",
                "operator": "Matthew",
            },
        )
        row = conn.execute(
            "SELECT status, max_stage_rank FROM prospects WHERE id='p1'"
        ).fetchone()

    assert row["status"] == "discovery_booked"
    assert row["max_stage_rank"] == crm_db.STAGE_RANK["discovery_booked"]


def test_opt_out_preserves_stage_and_only_sets_suppression(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)
        crm_db.set_prospect_status(conn, "p1", "won")
        crm_db.record_outreach_attempt(
            conn,
            {
                "id": "attempt-opt-out-after-payment",
                "prospect_id": "p1",
                "attempted_at": "2026-07-10T11:00:00-04:00",
                "channel": "email",
                "dnc_checked": True,
                "disposition": "opt_out",
                "human_reached": True,
                "operator": "Matthew",
            },
        )
        row = conn.execute(
            "SELECT status, max_stage_rank, contact_suppressed_at FROM prospects WHERE id='p1'"
        ).fetchone()

    assert row["status"] == "won"
    assert row["max_stage_rank"] == crm_db.STAGE_RANK["won"]
    assert row["contact_suppressed_at"] == "2026-07-10T11:00:00-04:00"


def test_duplicate_commercial_event_rolls_back_stage_change(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)
        crm_db.record_commercial_event(
            conn,
            {
                "id": "event-duplicate",
                "prospect_id": "p1",
                "event_type": "discovery_scheduled",
                "occurred_at": "2026-07-10T09:00:00-04:00",
                "evidence_grade": "A",
                "artifact_ref": "private-calendar-1",
                "operator": "Matthew",
            },
        )
        with pytest.raises(sqlite3.IntegrityError):
            crm_db.record_commercial_event(
                conn,
                {
                    "id": "event-duplicate",
                    "prospect_id": "p1",
                    "event_type": "payment_received",
                    "occurred_at": "2026-07-10T10:00:00-04:00",
                    "offer_version": "founding-pilot-v1",
                    "price_amount": 500,
                    "price_currency": "USD",
                    "evidence_grade": "A",
                    "artifact_ref": "private-receipt-1",
                    "operator": "Matthew",
                },
            )
        assert conn.execute("SELECT status FROM prospects WHERE id='p1'").fetchone()[0] == "discovery_booked"
        assert conn.execute("SELECT COUNT(*) FROM commercial_events").fetchone()[0] == 1


def test_cli_records_attempt_from_private_json_and_refreshes_exports(tmp_path, monkeypatch):
    cli = load_cli()
    db_file = tmp_path / "crm.sqlite"
    with crm_db.connect(tmp_path, db_file) as conn:
        seed_prospect(conn)

    private_dir = tmp_path / "ops-crm" / "data" / "private"
    private_dir.mkdir(parents=True)
    input_file = private_dir / "attempt.json"
    input_file.write_text(
        json.dumps(
            {
                "id": "attempt-cli-1",
                "prospect_id": "p1",
                "attempted_at": "2026-07-10T11:00:00-04:00",
                "channel": "email",
                "dnc_checked": True,
                "disposition": "email_sent",
                "operator": "Matthew",
            }
        ),
        encoding="utf-8",
    )
    refreshed = []
    monkeypatch.setattr(cli.crm_generate, "refresh_exports", lambda root, db: refreshed.append((root, db)))
    output = []

    exit_code = cli.main(
        [
            "--root", str(tmp_path), "--db", str(db_file),
            "attempt", "--input-json", str(input_file),
        ],
        output=output.append,
    )

    assert exit_code == 0
    assert refreshed == [(tmp_path.resolve(), db_file.resolve())]
    assert all("evidence_text" not in line for line in output)
    with crm_db.connect(tmp_path, db_file) as conn:
        assert conn.execute("SELECT COUNT(*) FROM outreach_attempts").fetchone()[0] == 1
        assert conn.execute("SELECT status FROM prospects WHERE id='p1'").fetchone()[0] == "contacted"


def test_cli_records_commercial_event_from_private_json(tmp_path, monkeypatch):
    cli = load_cli()
    db_file = tmp_path / "crm.sqlite"
    with crm_db.connect(tmp_path, db_file) as conn:
        seed_prospect(conn)
    private_dir = tmp_path / "ops-crm" / "data" / "private"
    private_dir.mkdir(parents=True)
    input_file = private_dir / "event.json"
    input_file.write_text(
        json.dumps(
            {
                "id": "event-cli-1",
                "prospect_id": "p1",
                "event_type": "paid_proposal_sent",
                "occurred_at": "2026-07-10T12:00:00-04:00",
                "offer_version": "founding-pilot-v1",
                "price_amount": 500,
                "price_currency": "USD",
                "evidence_grade": "A",
                "artifact_ref": "private-proposal-1",
                "operator": "Matthew",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.crm_generate, "refresh_exports", lambda root, db: ({}, {}))

    assert cli.main(
        ["--root", str(tmp_path), "--db", str(db_file), "commercial-event", "--input-json", str(input_file)],
        output=lambda line: None,
    ) == 0
    with crm_db.connect(tmp_path, db_file) as conn:
        assert conn.execute("SELECT COUNT(*) FROM commercial_events").fetchone()[0] == 1
        assert conn.execute("SELECT status FROM prospects WHERE id='p1'").fetchone()[0] == "pilot_proposed"


def test_cli_rejects_input_json_outside_private_directory(tmp_path):
    cli = load_cli()
    db_file = tmp_path / "crm.sqlite"
    with crm_db.connect(tmp_path, db_file) as conn:
        seed_prospect(conn)
    unsafe_file = tmp_path / "attempt.json"
    unsafe_file.write_text("{}", encoding="utf-8")
    output = []

    assert cli.main(
        ["--root", str(tmp_path), "--db", str(db_file), "attempt", "--input-json", str(unsafe_file)],
        output=output.append,
    ) == 1
    assert output == ["Record rejected: input JSON must be stored under ops-crm/data/private"]
    with crm_db.connect(tmp_path, db_file) as conn:
        assert conn.execute("SELECT COUNT(*) FROM outreach_attempts").fetchone()[0] == 0


def test_guided_attempt_collection_is_conditional_and_typed():
    cli = load_cli()
    answers = iter(
        [
            "attempt-guided-1", "p1", "2026-07-10T13:00:00-04:00", "phone", "y", "owner",
            "substantive_conversation", "y", "y", "Matthew", "paid_terms_requested",
            "2", "lead-follow-up", "12", "$10k-$25k", "office manager", "timing",
            "B", "", "Exact same-day buyer quote", "send paid proposal",
        ]
    )
    payload = cli.collect_attempt(lambda prompt: next(answers))

    assert payload["pain_score"] == 2
    assert payload["eligible_unsold_estimates"] == 12
    assert payload["evidence_grade"] == "B"
    assert payload["evidence_text"] == "Exact same-day buyer quote"
    assert "artifact_ref" not in payload


def test_outreach_metrics_keep_zero_denominators_and_omit_undefined_rates(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)
        exported = crm_db.export_dataset(conn, "2026-07-10T17:00:00Z")

    metrics = {row["metric_name"]: row for row in exported["metrics"]}
    assert metrics["outreach_attempts_total"]["metric_value"] == 0
    assert metrics["outreach_human_reach_rate_numerator"]["metric_value"] == 0
    assert metrics["outreach_human_reach_rate_denominator"]["metric_value"] == 0
    assert "outreach_human_reach_rate" not in metrics
    assert metrics["outreach_payment_rate_denominator"]["metric_value"] == 0
    assert "outreach_payment_rate" not in metrics
    brief = crm_db.daily_brief(exported)
    assert "Human reach rate: not enough evidence (0/0)" in brief
    assert "Payment rate: not enough evidence (0/0)" in brief


def test_outreach_metrics_derive_rates_from_persisted_evidence(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)
        crm_db.verify_prospect_for_outreach(
            conn, "p1", endpoint_type="phone", planned_attempts=1
        )
        crm_db.record_outreach_attempt(
            conn,
            {
                "id": "attempt-full-funnel",
                "prospect_id": "p1",
                "attempted_at": "2026-07-10T09:00:00-04:00",
                "channel": "phone",
                "dnc_checked": True,
                "contact_role": "owner",
                "disposition": "substantive_conversation",
                "human_reached": True,
                "substantive_conversation": True,
                "conversation_outcome": "paid_terms_requested",
                "pain_score": 2,
                "eligible_unsold_estimates": 12,
                "ticket_value_band": "$10k-$25k",
                "evidence_grade": "B",
                "evidence_text": "Buyer described twelve unsold estimates and requested paid terms.",
                "operator": "Matthew",
            },
        )
        for index, event_type in enumerate(
            ["discovery_scheduled", "paid_proposal_sent", "paid_pilot_accepted", "payment_received"]
        ):
            event: dict[str, Any] = {
                "id": f"event-full-{index}",
                "prospect_id": "p1",
                "attempt_id": "attempt-full-funnel",
                "event_type": event_type,
                "occurred_at": f"2026-07-10T{10 + index:02d}:00:00-04:00",
                "offer_version": "founding-pilot-v1",
                "evidence_grade": "A",
                "artifact_ref": f"private-artifact-{index}",
                "operator": "Matthew",
            }
            if event_type in {"paid_proposal_sent", "paid_pilot_accepted", "payment_received"}:
                event.update({"price_amount": 500, "price_currency": "USD"})
            crm_db.record_commercial_event(conn, event)
        exported = crm_db.export_dataset(conn, "2026-07-10T18:00:00Z")

    metrics = {row["metric_name"]: row for row in exported["metrics"]}
    for name in [
        "outreach_human_reach_rate", "outreach_substantive_conversation_rate",
        "outreach_pain_qualification_rate", "outreach_discovery_booking_rate",
        "outreach_paid_proposal_rate", "outreach_paid_acceptance_rate", "outreach_payment_rate",
    ]:
        assert metrics[name]["metric_value"] == 1.0
        assert metrics[f"{name}_numerator"]["metric_value"] == 1
        assert metrics[f"{name}_denominator"]["metric_value"] == 1
    assert "Payment rate: 100.0% (1/1)" in crm_db.daily_brief(exported)


def test_outreach_metrics_count_distinct_businesses_and_emit_diagnostics(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn, "p1")
        seed_prospect(conn, "p2")
        crm_db.verify_prospect_for_outreach(
            conn, "p1", endpoint_type="phone", planned_attempts=2
        )
        crm_db.verify_prospect_for_outreach(
            conn, "p2", endpoint_type="phone", planned_attempts=1
        )
        crm_db.record_outreach_attempt(
            conn,
            valid_attempt(
                id="attempt-p1-first",
                prospect_id="p1",
                disposition="substantive_conversation",
                human_reached=True,
                substantive_conversation=True,
                conversation_outcome="paid_terms_requested",
                pain_score=2,
                contact_role="owner",
                eligible_unsold_estimates=10,
                ticket_value_band="$10k-$25k",
                evidence_grade="B",
                evidence_text="Exact quote about four stale estimates",
            ),
        )
        crm_db.record_outreach_attempt(
            conn,
            valid_attempt(
                id="attempt-p1-repeat",
                prospect_id="p1",
                disposition="substantive_conversation",
                human_reached=True,
                substantive_conversation=True,
                conversation_outcome="paid_terms_requested",
                pain_score=2,
                contact_role="owner",
                eligible_unsold_estimates=10,
                ticket_value_band="$10k-$25k",
                evidence_grade="B",
                evidence_text="Second exact same-day quote",
            ),
        )
        crm_db.record_outreach_attempt(
            conn,
            valid_attempt(id="attempt-p2-ivr", prospect_id="p2", disposition="ivr_blocked"),
        )
        exported = crm_db.export_dataset(conn, "2026-07-10T18:00:00Z")

    metrics = {row["metric_name"]: row["metric_value"] for row in exported["metrics"]}
    assert metrics["outreach_attempted_businesses_total"] == 2
    assert metrics["outreach_human_reached_total"] == 1
    assert metrics["outreach_substantive_conversations_total"] == 1
    assert metrics["outreach_pain_qualified_total"] == 1
    assert metrics["outreach_paid_terms_requested_total"] == 1
    assert metrics["outreach_attempts_total"] == 3
    assert metrics["outreach_ivr_blocks_total"] == 1
    assert metrics["outreach_human_reach_rate"] == 0.5
    assert metrics["outreach_substantive_conversation_rate"] == 1.0
    assert metrics["outreach_pain_qualification_rate"] == 1.0


def test_connect_sets_five_second_sqlite_busy_timeout(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_attempt_waits_for_suppression_transaction_then_rejects_contact(tmp_path):
    db_file = tmp_path / "crm.sqlite"
    with crm_db.connect(ROOT, db_file) as blocker:
        seed_prospect(blocker)
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute(
            "UPDATE prospects SET contact_suppressed_at='2026-07-10T08:00:00-04:00' WHERE id='p1'"
        )
        outcomes: list[Exception] = []

        def record_in_thread() -> None:
            try:
                with crm_db.connect(ROOT, db_file) as conn:
                    crm_db.record_outreach_attempt(conn, valid_attempt(id="attempt-race"))
            except Exception as exc:  # captured for assertion in the test thread
                outcomes.append(exc)

        worker = threading.Thread(target=record_in_thread)
        worker.start()
        time.sleep(0.2)
        assert worker.is_alive()
        blocker.commit()
        worker.join(timeout=2)

    assert not worker.is_alive()
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], ValueError)
    assert "contact-suppressed" in str(outcomes[0])
    with crm_db.connect(ROOT, db_file) as conn:
        assert conn.execute("SELECT COUNT(*) FROM outreach_attempts").fetchone()[0] == 0


def test_real_database_lock_waits_about_five_seconds_before_failing(tmp_path):
    db_file = tmp_path / "crm.sqlite"
    with crm_db.connect(ROOT, db_file) as blocker:
        seed_prospect(blocker)
        blocker.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        with crm_db.connect(ROOT, db_file) as contender:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                crm_db.record_outreach_attempt(contender, valid_attempt(id="attempt-timeout"))
        elapsed = time.monotonic() - started
        blocker.rollback()

    assert 4.5 <= elapsed < 7.0


def test_opt_out_rolls_back_attempt_when_suppression_update_fails(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)
        conn.execute(
            """CREATE TRIGGER reject_suppression BEFORE UPDATE OF contact_suppressed_at ON prospects
               BEGIN SELECT RAISE(ABORT, 'forced suppression failure'); END"""
        )
        with pytest.raises(sqlite3.IntegrityError, match="forced suppression failure"):
            crm_db.record_outreach_attempt(
                conn,
                valid_attempt(
                    id="attempt-atomic-opt-out",
                    channel="email",
                    disposition="opt_out",
                    human_reached=True,
                ),
            )
        assert conn.execute("SELECT COUNT(*) FROM outreach_attempts").fetchone()[0] == 0
        row = conn.execute(
            "SELECT status, contact_suppressed_at FROM prospects WHERE id='p1'"
        ).fetchone()
        assert row["status"] == "not_contacted"
        assert row["contact_suppressed_at"] is None


def test_cli_reports_post_commit_export_failure_without_losing_record(tmp_path, monkeypatch):
    cli = load_cli()
    db_file = tmp_path / "crm.sqlite"
    with crm_db.connect(tmp_path, db_file) as conn:
        seed_prospect(conn)
    private_dir = tmp_path / "ops-crm" / "data" / "private"
    private_dir.mkdir(parents=True)
    input_file = private_dir / "attempt-export-failure.json"
    input_file.write_text(
        json.dumps(
            {
                "id": "attempt-export-failure",
                "prospect_id": "p1",
                "attempted_at": "2026-07-10T15:00:00-04:00",
                "channel": "phone",
                "dnc_checked": True,
                "disposition": "no_answer",
                "operator": "Matthew",
            }
        ),
        encoding="utf-8",
    )

    def fail_refresh(root, db):
        raise RuntimeError("private quote must never be printed")

    monkeypatch.setattr(cli.crm_generate, "refresh_exports", fail_refresh)
    output = []
    exit_code = cli.main(
        ["--root", str(tmp_path), "--db", str(db_file), "attempt", "--input-json", str(input_file)],
        output=output.append,
    )

    assert exit_code == 2
    assert any("record committed" in line.lower() and " refresh failed" in line.lower() for line in output)
    assert "private quote" not in "\n".join(output)
    assert " refresh" in output[-1]
    with crm_db.connect(tmp_path, db_file) as conn:
        assert conn.execute("SELECT COUNT(*) FROM outreach_attempts").fetchone()[0] == 1


def test_cli_turns_locked_database_into_safe_retry_message(tmp_path, monkeypatch):
    cli = load_cli()
    db_file = tmp_path / "crm.sqlite"
    with crm_db.connect(tmp_path, db_file) as conn:
        seed_prospect(conn)
    output = []
    monkeypatch.setattr(cli.crm_db, "record_outreach_attempt", lambda conn, payload: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
    monkeypatch.setattr(cli, "collect_attempt", lambda input_fn: {"id": "attempt-lock"})

    exit_code = cli.main(
        ["--root", str(tmp_path), "--db", str(db_file), "attempt"], output=output.append
    )

    assert exit_code == 1
    assert output == ["Database is busy. No partial write was committed; wait five seconds and retry."]
