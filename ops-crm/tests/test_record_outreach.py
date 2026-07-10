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


def valid_commercial_event(**overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "id": "event-valid",
        "prospect_id": "p1",
        "event_type": "discovery_completed",
        "occurred_at": "2026-07-10T10:00:00-04:00",
        "evidence_grade": "B",
        "evidence_text": "Same-day commercial milestone note.",
        "operator": "Matthew",
    }
    event.update(overrides)
    return event


def commercial_write_state(conn: sqlite3.Connection) -> tuple[int, tuple[Any, ...]]:
    event_count = conn.execute("SELECT COUNT(*) FROM commercial_events").fetchone()[0]
    prospect = conn.execute(
        "SELECT status, max_stage_rank, updated_at FROM prospects WHERE id='p1'"
    ).fetchone()
    return event_count, tuple(prospect)


def assert_commercial_rejection_was_atomic(
    conn: sqlite3.Connection, before: tuple[int, tuple[Any, ...]]
) -> None:
    assert commercial_write_state(conn) == before


def test_commercial_event_contract_covers_every_event_type():
    expected = {
        "discovery_scheduled": ({"A"}, False, False, "discovery_booked", False),
        "discovery_completed": ({"A", "B"}, False, False, None, False),
        "discovery_no_show": ({"A", "B"}, False, False, None, False),
        "paid_proposal_sent": ({"A"}, True, True, "pilot_proposed", False),
        "paid_pilot_accepted": ({"A"}, True, True, "pilot_proposed", False),
        "paid_pilot_declined": ({"A", "B"}, True, True, "lost", False),
        "payment_received": ({"A"}, True, True, "won", False),
    }
    assert set(crm_db.COMMERCIAL_EVENT_CONTRACT) == set(crm_db.COMMERCIAL_EVENT_TYPES)
    for event_type, contract in crm_db.COMMERCIAL_EVENT_CONTRACT.items():
        assert (
            set(contract["evidence_grades"]),
            contract["offer_version_required"],
            contract["price_required"],
            contract["stage"],
            contract["attempt_required"],
        ) == expected[event_type]


@pytest.mark.parametrize("event_type", sorted(crm_db.COMMERCIAL_EVENT_TYPES))
def test_each_commercial_event_policy_allows_nullable_originating_attempt(tmp_path, event_type):
    contract = crm_db.COMMERCIAL_EVENT_CONTRACT[event_type]
    event = valid_commercial_event(event_type=event_type)
    if contract["evidence_grades"] == frozenset({"A"}):
        event.update(
            evidence_grade="A",
            evidence_text=None,
            artifact_ref=f"private/{event_type}/artifact-1",
        )
    if contract["offer_version_required"]:
        event.update(
            offer_version="founding-pilot-v1",
            price_amount=500,
            price_currency="USD",
        )
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)
        crm_db.record_commercial_event(conn, event)
        row = conn.execute(
            "SELECT attempt_id FROM commercial_events WHERE id='event-valid'"
        ).fetchone()
        assert row["attempt_id"] is None


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



def test_commercial_event_and_concurrent_archive_serialize_without_validation_gap(tmp_path):
    database = tmp_path / "crm.sqlite"
    with crm_db.connect(ROOT, database) as conn:
        seed_prospect(conn)

    prospect_read = threading.Event()
    archive_started = threading.Event()
    results: dict[str, Any] = {}
    errors: list[BaseException] = []
    trace_failures: list[BaseException] = []

    def record_event() -> None:
        try:
            with crm_db.connect(ROOT, database) as conn:
                def trace(statement: str) -> None:
                    if "SELECT * FROM prospects" in statement:
                        prospect_read.set()
                        if not archive_started.wait(2):
                            trace_failures.append(
                                AssertionError("archiver did not reach the protected boundary")
                            )

                conn.set_trace_callback(trace)
                results["event_id"] = crm_db.record_commercial_event(
                    conn, valid_commercial_event()
                )
        except BaseException as exc:  # surfaced in the main test thread
            errors.append(exc)

    def archive_prospect() -> None:
        try:
            if not prospect_read.wait(2):
                raise AssertionError("event transaction did not read the prospect")
            with crm_db.connect(ROOT, database) as conn:
                archive_started.set()
                conn.execute("BEGIN IMMEDIATE")
                results["events_visible_before_archive"] = conn.execute(
                    "SELECT COUNT(*) FROM commercial_events"
                ).fetchone()[0]
                conn.execute("UPDATE prospects SET is_current=0 WHERE id='p1'")
                conn.commit()
        except BaseException as exc:  # surfaced in the main test thread
            errors.append(exc)

    event_thread = threading.Thread(target=record_event)
    archive_thread = threading.Thread(target=archive_prospect)
    event_thread.start()
    archive_thread.start()
    event_thread.join(6)
    archive_thread.join(6)

    assert not event_thread.is_alive()
    assert not archive_thread.is_alive()
    assert trace_failures == []
    assert errors == []
    assert results == {
        "event_id": "event-valid",
        "events_visible_before_archive": 1,
    }
    with crm_db.connect(ROOT, database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM commercial_events").fetchone()[0] == 1
        assert conn.execute("SELECT is_current FROM prospects WHERE id='p1'").fetchone()[0] == 0


def test_commercial_event_rejects_missing_prospect_without_writes(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        crm_db.init_db(conn)
        with pytest.raises(KeyError, match="no current prospect"):
            crm_db.record_commercial_event(conn, valid_commercial_event())
        assert conn.execute("SELECT COUNT(*) FROM commercial_events").fetchone()[0] == 0


def test_commercial_event_rejects_archived_prospect_atomically(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)
        conn.execute("UPDATE prospects SET is_current=0 WHERE id='p1'")
        conn.commit()
        before = commercial_write_state(conn)
        with pytest.raises(KeyError, match="no current prospect"):
            crm_db.record_commercial_event(conn, valid_commercial_event())
        assert_commercial_rejection_was_atomic(conn, before)


def test_commercial_event_rejects_suppressed_prospect_atomically(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)
        conn.execute(
            "UPDATE prospects SET contact_suppressed_at=?, contact_suppression_reason=? WHERE id='p1'",
            ("2026-07-10T09:00:00-04:00", "explicit_opt_out"),
        )
        conn.commit()
        before_updated_at = conn.execute(
            "SELECT updated_at FROM prospects WHERE id='p1'"
        ).fetchone()[0]

        with pytest.raises(ValueError, match="contact-suppressed"):
            crm_db.record_commercial_event(conn, valid_commercial_event())

        assert conn.execute("SELECT COUNT(*) FROM commercial_events").fetchone()[0] == 0
        row = conn.execute(
            "SELECT status, max_stage_rank, updated_at FROM prospects WHERE id='p1'"
        ).fetchone()
        assert tuple(row) == ("not_contacted", 0, before_updated_at)


def test_commercial_event_rejects_artifact_reference_with_surrounding_whitespace(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)
        before = commercial_write_state(conn)
        event = valid_commercial_event(
            event_type="discovery_scheduled",
            evidence_grade="A",
            evidence_text=None,
            artifact_ref=" private/calendar/event-1 ",
        )

        with pytest.raises(ValueError, match="artifact_ref"):
            crm_db.record_commercial_event(conn, event)

        assert_commercial_rejection_was_atomic(conn, before)


@pytest.mark.parametrize(
    "artifact_ref",
    [
        "",
        "   ",
        "private/evidence\nraw",
        "private/evidence\x00raw",
        "x" * (crm_db.MAX_PRIVATE_ARTIFACT_REF_LENGTH + 1),
        123,
        '{"raw":"evidence"}',
        "private/evidence?token=abc123",
    ],
)
def test_commercial_event_rejects_malformed_private_artifact_references_atomically(
    tmp_path, artifact_ref
):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)
        before = commercial_write_state(conn)
        event = valid_commercial_event(
            event_type="discovery_scheduled",
            evidence_grade="A",
            evidence_text=None,
            artifact_ref=artifact_ref,
        )

        with pytest.raises(ValueError, match="artifact_ref"):
            crm_db.record_commercial_event(conn, event)

        assert_commercial_rejection_was_atomic(conn, before)


@pytest.mark.parametrize(
    "evidence_text",
    [123, "x" * (crm_db.MAX_EVIDENCE_TEXT_LENGTH + 1)],
)
def test_grade_b_commercial_event_rejects_malformed_evidence_text_atomically(
    tmp_path, evidence_text
):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)
        before = commercial_write_state(conn)

        with pytest.raises(ValueError, match="evidence_text"):
            crm_db.record_commercial_event(
                conn, valid_commercial_event(evidence_text=evidence_text)
            )

        assert_commercial_rejection_was_atomic(conn, before)


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


def test_paid_proposal_rejects_wrong_offer_version_atomically(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)
        before = commercial_write_state(conn)
        event = valid_commercial_event(
            event_type="paid_proposal_sent",
            evidence_grade="A",
            artifact_ref="private/proposal/proposal-1",
            offer_version="founding-pilot-v2",
            price_amount=500,
            price_currency="USD",
        )

        with pytest.raises(ValueError, match="offer_version"):
            crm_db.record_commercial_event(conn, event)

        assert_commercial_rejection_was_atomic(conn, before)


@pytest.mark.parametrize(
    "event_type",
    ["paid_proposal_sent", "paid_pilot_accepted", "paid_pilot_declined"],
)
@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"offer_version": None}, "offer_version"),
        ({"offer_version": "founding-pilot-v2"}, "offer_version"),
        ({"price_amount": None}, "price_amount"),
        ({"price_amount": True}, "price_amount"),
        ({"price_amount": "500"}, "price_amount"),
        ({"price_amount": float("nan")}, "price_amount"),
        ({"price_currency": None}, "price_currency"),
        ({"price_currency": "EUR"}, "price_currency"),
    ],
)
def test_paid_commercial_events_enforce_founding_offer_atomically(
    tmp_path, event_type, overrides, message
):
    event = valid_commercial_event(
        event_type=event_type,
        evidence_grade="A",
        evidence_text=None,
        artifact_ref=f"private/{event_type}/artifact-1",
        offer_version="founding-pilot-v1",
        price_amount=500,
        price_currency="USD",
    )
    event.update(overrides)
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)
        before = commercial_write_state(conn)
        with pytest.raises(ValueError, match=message):
            crm_db.record_commercial_event(conn, event)
        assert_commercial_rejection_was_atomic(conn, before)


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


def test_commercial_event_rejects_reversed_attempt_chronology_by_instant(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)
        crm_db.record_outreach_attempt(
            conn,
            valid_attempt(attempted_at="2026-07-10T10:00:00-04:00"),
        )
        before = commercial_write_state(conn)
        event = valid_commercial_event(
            attempt_id="attempt-valid",
            occurred_at="2026-07-10T14:30:00+01:00",
        )

        with pytest.raises(ValueError, match="before its originating attempt"):
            crm_db.record_commercial_event(conn, event)

        assert_commercial_rejection_was_atomic(conn, before)


def test_commercial_event_rejects_missing_attempt_atomically(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)
        before = commercial_write_state(conn)
        with pytest.raises(KeyError, match="no outreach attempt"):
            crm_db.record_commercial_event(
                conn, valid_commercial_event(attempt_id="attempt-missing")
            )
        assert_commercial_rejection_was_atomic(conn, before)


def test_commercial_event_accepts_equal_attempt_and_event_instants(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)
        crm_db.record_outreach_attempt(
            conn,
            valid_attempt(attempted_at="2026-07-10T10:00:00-04:00"),
        )
        event_id = crm_db.record_commercial_event(
            conn,
            valid_commercial_event(
                attempt_id="attempt-valid",
                occurred_at="2026-07-10T15:00:00+01:00",
            ),
        )
        assert event_id == "event-valid"
        assert conn.execute("SELECT COUNT(*) FROM commercial_events").fetchone()[0] == 1


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
        if event_type.startswith("paid_") or event_type == "payment_received":
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
        crm_db.set_prospect_status(
            conn, "p1", "discovery_booked", changed_at="2026-07-10T10:00:00-04:00"
        )
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


def test_weak_attempt_never_reopens_lost_off_ramp(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)
        crm_db.set_prospect_status(
            conn, "p1", "lost", changed_at="2026-07-10T10:00:00-04:00"
        )
        crm_db.record_outreach_attempt(
            conn,
            valid_attempt(
                id="attempt-after-lost", attempted_at="2026-07-10T11:00:00-04:00"
            ),
        )
        row = conn.execute(
            "SELECT status, max_stage_rank FROM prospects WHERE id='p1'"
        ).fetchone()
    assert row["status"] == "lost"
    assert row["max_stage_rank"] == 0


@pytest.mark.parametrize(
    ("outcome", "expected_status"),
    [
        ("pain_qualified_no_interest", "lost"),
        ("follow_up_requested", "follow_up_later"),
        ("confirmed_not_fit", "not_fit"),
    ],
)
def test_typed_conversation_outcomes_derive_each_off_ramp(tmp_path, outcome, expected_status):
    payload = valid_attempt(
        disposition="substantive_conversation",
        human_reached=True,
        substantive_conversation=True,
        contact_role="owner",
        conversation_outcome=outcome,
        evidence_grade="B",
        evidence_text="Same-day exact quote supports the typed outcome.",
    )
    if outcome != "confirmed_not_fit":
        payload.update(
            pain_score=2,
            eligible_unsold_estimates=10,
            ticket_value_band="$10k-$25k",
        )
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)
        crm_db.record_outreach_attempt(conn, payload)
        row = conn.execute(
            "SELECT status, max_stage_rank FROM prospects WHERE id='p1'"
        ).fetchone()
    assert row["status"] == expected_status
    assert row["max_stage_rank"] == crm_db.STAGE_RANK["replied"]


def test_confirmed_not_fit_requires_qualified_evidence_before_write(tmp_path):
    payload = valid_attempt(
        id="attempt-evidence-free-not-fit",
        disposition="substantive_conversation",
        human_reached=True,
        substantive_conversation=True,
        contact_role="owner",
        conversation_outcome="confirmed_not_fit",
    )
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)
        with pytest.raises(ValueError, match="confirmed_not_fit.*grade A or B evidence"):
            crm_db.record_outreach_attempt(conn, payload)
        attempt_count = conn.execute("SELECT COUNT(*) FROM outreach_attempts").fetchone()[0]
        status = conn.execute("SELECT status FROM prospects WHERE id = 'p1'").fetchone()[0]

    assert attempt_count == 0
    assert status == "not_contacted"


def test_explicit_reopening_requires_auditable_grade_a_evidence(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)
        crm_db.set_prospect_status(conn, "p1", "pilot_proposed")
        crm_db.set_prospect_status(conn, "p1", "lost")
        with pytest.raises(ValueError, match="grade A"):
            crm_db.reopen_prospect(
                conn,
                "p1",
                reason="buyer asked to restart",
                evidence_grade="B",
                artifact_ref="private-email-1",
                operator="Matthew",
            )
        crm_db.reopen_prospect(
            conn,
            "p1",
            reason="buyer asked to restart",
            evidence_grade="A",
            artifact_ref="private/email/reopen-1",
            operator="Matthew",
            reopened_at="2026-07-10T15:00:00-04:00",
        )
        prospect = conn.execute(
            "SELECT status, max_stage_rank FROM prospects WHERE id='p1'"
        ).fetchone()
        audit = conn.execute("SELECT * FROM prospect_reopenings").fetchone()

    assert prospect["status"] == "pilot_proposed"
    assert prospect["max_stage_rank"] == crm_db.STAGE_RANK["pilot_proposed"]
    assert audit["from_status"] == "lost"
    assert audit["to_status"] == "pilot_proposed"
    assert audit["reason"] == "buyer asked to restart"
    assert audit["artifact_ref"] == "private/email/reopen-1"
    assert audit["reopened_at"] == "2026-07-10T15:00:00-04:00"


def test_reopening_rejects_credential_bearing_artifact_atomically(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)
        crm_db.set_prospect_status(conn, "p1", "lost")
        before = conn.execute(
            "SELECT status, max_stage_rank, updated_at FROM prospects WHERE id='p1'"
        ).fetchone()

        with pytest.raises(ValueError, match="artifact_ref"):
            crm_db.reopen_prospect(
                conn, "p1", reason="buyer asked to restart", evidence_grade="A",
                artifact_ref="https://example.test/evidence?secret=credential-value",
                operator="Matthew",
            )

        after = conn.execute(
            "SELECT status, max_stage_rank, updated_at FROM prospects WHERE id='p1'"
        ).fetchone()
        audit_count = conn.execute("SELECT COUNT(*) FROM prospect_reopenings").fetchone()[0]

    assert tuple(after) == tuple(before)
    assert audit_count == 0


def test_stale_equal_rank_commercial_event_cannot_regress_prospect_chronology(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)
        crm_db.record_commercial_event(
            conn,
            valid_commercial_event(
                id="event-new", event_type="discovery_scheduled",
                occurred_at="2026-07-10T12:00:00-04:00", evidence_grade="A",
                evidence_text=None, artifact_ref="private/calendar/new",
            ),
        )
        with pytest.raises(ValueError, match="older than current prospect chronology"):
            crm_db.record_commercial_event(
                conn,
                valid_commercial_event(
                    id="event-old", event_type="discovery_scheduled",
                    occurred_at="2026-07-10T10:00:00-04:00", evidence_grade="A",
                    evidence_text=None, artifact_ref="private/calendar/old",
                ),
            )
        row = conn.execute(
            "SELECT status, updated_at FROM prospects WHERE id='p1'"
        ).fetchone()
        event_count = conn.execute(
            "SELECT COUNT(*) FROM commercial_events WHERE id='event-old'"
        ).fetchone()[0]

    assert tuple(row) == ("discovery_booked", "2026-07-10T12:00:00-04:00")
    assert event_count == 0


def test_stale_attempt_and_off_ramp_fact_do_not_overwrite_newer_lifecycle_state(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)
        crm_db.record_commercial_event(
            conn,
            valid_commercial_event(
                id="event-new", event_type="discovery_scheduled",
                occurred_at="2026-07-10T12:00:00-04:00", evidence_grade="A",
                evidence_text=None, artifact_ref="private/calendar/new",
            ),
        )
        with pytest.raises(ValueError, match="older than current prospect chronology"):
            crm_db.record_outreach_attempt(
                conn,
                valid_attempt(
                    id="attempt-stale-off-ramp", attempted_at="2026-07-10T10:00:00-04:00",
                    disposition="substantive_conversation", human_reached=True,
                    substantive_conversation=True, contact_role="owner",
                    conversation_outcome="pain_qualified_no_interest", pain_score=2,
                    eligible_unsold_estimates=10, ticket_value_band="$10k-$25k",
                    evidence_grade="B", evidence_text="Same-day exact buyer quote.",
                ),
            )
        row = conn.execute(
            "SELECT status, max_stage_rank, updated_at FROM prospects WHERE id='p1'"
        ).fetchone()
        attempt_count = conn.execute(
            "SELECT COUNT(*) FROM outreach_attempts WHERE id='attempt-stale-off-ramp'"
        ).fetchone()[0]

    assert tuple(row) == (
        "discovery_booked", crm_db.STAGE_RANK["discovery_booked"],
        "2026-07-10T12:00:00-04:00",
    )
    assert attempt_count == 0


def test_stale_verification_is_rejected_without_resetting_current_sequence(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)
        crm_db.verify_prospect_for_outreach(
            conn, "p1", endpoint_type="phone", planned_attempts=1,
            verified_at="2026-07-10T12:00:00-04:00",
        )
        before = conn.execute(
            """SELECT contact_endpoint_type, sequence_planned_attempts,
                      sequence_completed_at, updated_at
               FROM prospects WHERE id='p1'"""
        ).fetchone()

        with pytest.raises(ValueError, match="older than current prospect chronology"):
            crm_db.verify_prospect_for_outreach(
                conn, "p1", endpoint_type="email", planned_attempts=3,
                verified_at="2026-07-10T10:00:00-04:00",
            )

        after = conn.execute(
            """SELECT contact_endpoint_type, sequence_planned_attempts,
                      sequence_completed_at, updated_at
               FROM prospects WHERE id='p1'"""
        ).fetchone()

    assert tuple(after) == tuple(before)


def test_stale_reopening_is_rejected_without_audit_or_lifecycle_change(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)
        crm_db.set_prospect_status(conn, "p1", "pilot_proposed")
        crm_db.set_prospect_status(conn, "p1", "lost")
        before = conn.execute(
            "SELECT status, max_stage_rank, updated_at FROM prospects WHERE id='p1'"
        ).fetchone()

        with pytest.raises(ValueError, match="older than current prospect chronology"):
            crm_db.reopen_prospect(
                conn, "p1", reason="late evidence", evidence_grade="A",
                artifact_ref="private/email/stale-reopen", operator="Matthew",
                reopened_at="2020-01-01T00:00:00Z",
            )

        after = conn.execute(
            "SELECT status, max_stage_rank, updated_at FROM prospects WHERE id='p1'"
        ).fetchone()
        audit_count = conn.execute("SELECT COUNT(*) FROM prospect_reopenings").fetchone()[0]

    assert tuple(after) == tuple(before)
    assert audit_count == 0


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE prospect_reopenings SET reason = 'rewritten'",
        "DELETE FROM prospect_reopenings",
    ],
)
def test_reopening_audit_rows_are_immutable(tmp_path, mutation):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)
        crm_db.set_prospect_status(conn, "p1", "lost")
        crm_db.reopen_prospect(
            conn,
            "p1",
            reason="artifact-backed restart",
            evidence_grade="A",
            artifact_ref="private/email/immutable-reopen",
            operator="Matthew",
        )

        with pytest.raises(sqlite3.IntegrityError, match="prospect_reopenings are immutable"):
            conn.execute(mutation)

        rows = conn.execute(
            "SELECT prospect_id, from_status, reason FROM prospect_reopenings"
        ).fetchall()

    assert [tuple(row) for row in rows] == [("p1", "lost", "artifact-backed restart")]


def test_reopening_rejects_nonintegral_high_water_rank_atomically(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)
        conn.execute(
            "UPDATE prospects SET status = 'lost', max_stage_rank = 1.5 WHERE id = 'p1'"
        )
        conn.commit()
        before = conn.execute(
            "SELECT status, max_stage_rank, updated_at FROM prospects WHERE id = 'p1'"
        ).fetchone()

        with pytest.raises(ValueError, match="invalid max_stage_rank"):
            crm_db.reopen_prospect(
                conn,
                "p1",
                reason="buyer asked to restart",
                evidence_grade="A",
                artifact_ref="private/email/reopen-invalid-rank",
                operator="Matthew",
            )

        after = conn.execute(
            "SELECT status, max_stage_rank, updated_at FROM prospects WHERE id = 'p1'"
        ).fetchone()
        audit_count = conn.execute("SELECT COUNT(*) FROM prospect_reopenings").fetchone()[0]

    assert tuple(after) == tuple(before)
    assert audit_count == 0


def test_cumulative_funnel_metrics_survive_off_ramp_and_explicit_reopening(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)
        crm_db.set_prospect_status(conn, "p1", "pilot_proposed")
        crm_db.set_prospect_status(conn, "p1", "lost")
        during = crm_db.export_dataset(conn, "2026-07-10T14:00:00Z")
        crm_db.reopen_prospect(
            conn,
            "p1",
            reason="artifact-backed re-engagement",
            evidence_grade="A",
            artifact_ref="private/email/reopen-2",
            operator="Matthew",
        )
        after = crm_db.export_dataset(conn, "2026-07-10T15:00:00Z")
    for dataset in (during, after):
        metrics = {m["metric_name"]: m["metric_value"] for m in dataset["metrics"]}
        assert metrics["discovery_booked_total"] == 1
        assert metrics["pilot_proposed_total"] == 1


def test_opt_out_preserves_stage_and_only_sets_suppression(tmp_path):
    with crm_db.connect(ROOT, tmp_path / "crm.sqlite") as conn:
        seed_prospect(conn)
        crm_db.set_prospect_status(
            conn, "p1", "won", changed_at="2026-07-10T10:00:00-04:00"
        )
        attempted_at = "2026-07-10T11:00:00-04:00"
        crm_db.record_outreach_attempt(
            conn,
            {
                "id": "attempt-opt-out-after-payment",
                "prospect_id": "p1",
                "attempted_at": attempted_at,
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
    assert row["contact_suppressed_at"] == attempted_at


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
            conn, "p1", endpoint_type="phone", planned_attempts=1,
            verified_at="2026-07-10T08:00:00-04:00",
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
        crm_db.complete_outreach_sequence(
            conn,
            "p1",
            reason="planned_attempts_completed",
            completed_at="2026-07-10T09:30:00-04:00",
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
            conn, "p1", endpoint_type="phone", planned_attempts=2,
            verified_at="2026-07-10T08:00:00-04:00",
        )
        crm_db.verify_prospect_for_outreach(
            conn, "p2", endpoint_type="phone", planned_attempts=1,
            verified_at="2026-07-10T08:00:00-04:00",
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
        for prospect_id in ("p1", "p2"):
            crm_db.complete_outreach_sequence(
                conn,
                prospect_id,
                reason="planned_attempts_completed",
                completed_at="2026-07-10T10:00:00-04:00",
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
