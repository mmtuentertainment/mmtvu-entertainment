"""Targeted smoke tests for Phase 1 P0 fixes: P0.1, P0.6, P0.7."""

import pytest
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db as crm_db


@pytest.fixture
def fresh_db(tmp_path):
    """Create a fresh in-memory-like SQLite database for testing."""
    db_file = tmp_path / "test_smoke.sqlite"
    conn = crm_db.connect(Path("."), db_file)
    crm_db.init_db(conn)
    yield conn
    conn.close()


@pytest.fixture
def seed_dataset():
    """Minimal dataset with one prospect and one action."""
    now = "2026-07-09T12:00:00Z"
    return {
        "prospects": [
            {
                "id": "prospect-smoke",
                "company_name": "Test Co",
                "city": "City",
                "niche": "Niche",
                "website": "http://test.com",
                "priority": "high",
                "status": "new",
                "owner_operator": False,
            }
        ],
        "next_actions": [
            {
                "id": "action-smoke",
                "owner": "Hermes",
                "action": "call",
                "status": "open",
                "priority": "high",
                "score": 1.0,
                "due_at": now,
                "reason": "reason",
                "expected_revenue_path": "reply",
                "source_entity_type": "prospect",
                "source_entity_id": "prospect-smoke",
            }
        ],
        "assets": [],
        "offers": [],
        "evidence": [],
        "generated_at": now,
    }


def test_p01_prospect_status_preserved(fresh_db, seed_dataset):
    """P0.1: Operator-set prospect status survives re-import."""
    now = "2026-07-09T12:00:00Z"

    # Initial import
    crm_db.import_dataset(fresh_db, seed_dataset)

    # Operator manually changes status
    fresh_db.execute('UPDATE prospects SET status = "qualified" WHERE id = ?', ("prospect-smoke",))
    fresh_db.execute('UPDATE actions SET status = "done" WHERE id = ?', ("action-smoke",))
    fresh_db.commit()

    # Regenerate with original dataset (status=new, status=open)
    crm_db.import_dataset(fresh_db, seed_dataset)

    # Assert operator-set status preserved
    p_status = fresh_db.execute('SELECT status FROM prospects WHERE id = ?', ("prospect-smoke",)).fetchone()["status"]
    a_status = fresh_db.execute('SELECT status FROM actions WHERE id = ?', ("action-smoke",)).fetchone()["status"]

    assert p_status == "qualified", f"Prospect status was {p_status}, expected 'qualified'"
    assert a_status == "done", f"Action status was {a_status}, expected 'done'"


def test_p06_stale_records_archived(fresh_db, seed_dataset):
    """P0.6: Records not in current dataset get archived (is_current=0, archived_at set)."""
    now = "2026-07-09T12:00:00Z"

    # Initial import
    crm_db.import_dataset(fresh_db, seed_dataset)

    # Add a fake record not in the dataset
    fresh_db.execute(
        """INSERT INTO prospects(id, company_name, city, niche, website, priority, status, owner_operator, created_at, updated_at, payload_json, is_current, archived_at)
           VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL)""",
        ("fake-prospect", "Fake Co", "City", "Niche", "http://fake.com", "low", "new", 0, now, now, "{}"),
    )
    fresh_db.commit()

    # Regenerate WITHOUT fake-prospect in dataset
    crm_db.import_dataset(fresh_db, seed_dataset)

    # Assert fake record archived
    fake = fresh_db.execute('SELECT is_current, archived_at FROM prospects WHERE id = ?', ("fake-prospect",)).fetchone()
    assert fake["is_current"] == 0, f"Stale prospect is_current={fake['is_current']}, expected 0"
    assert fake["archived_at"] is not None, f"Stale prospect archived_at={fake['archived_at']}, expected not null"


def test_p07_table_whitelist_rejects_invalid(fresh_db, seed_dataset):
    """P0.7: _existing_created_at rejects tables not in ALLOWED_TABLES."""
    now = "2026-07-09T12:00:00Z"

    with pytest.raises(ValueError, match="unsupported table for created_at lookup"):
        crm_db._existing_created_at(fresh_db, "prospects; DROP TABLE prospects", "x", now)

    with pytest.raises(ValueError, match="unsupported table for created_at lookup"):
        crm_db._existing_created_at(fresh_db, "nonexistent", "x", now)

    # Valid tables should work
    assert crm_db._existing_created_at(fresh_db, "prospects", "x", now) == now
    assert crm_db._existing_created_at(fresh_db, "actions", "x", now) == now
    assert crm_db._existing_created_at(fresh_db, "artifacts", "x", now) == now


def test_p06_archive_empty_dataset(fresh_db, seed_dataset):
    """P0.6: Empty current_ids archives all current records safely."""
    now = "2026-07-09T12:00:00Z"

    # Initial import
    crm_db.import_dataset(fresh_db, seed_dataset)

    # Regenerate with empty prospects/actions
    empty_dataset = {
        "prospects": [],
        "next_actions": [],
        "assets": [],
        "offers": [],
        "evidence": [],
        "generated_at": now,
    }
    crm_db.import_dataset(fresh_db, empty_dataset)

    # All prospects should be archived
    prospects = fresh_db.execute("SELECT is_current FROM prospects WHERE is_current = 1").fetchall()
    assert len(prospects) == 0, "Expected all prospects archived when empty dataset provided"


def test_p01_export_dataset_filters_archived(fresh_db, seed_dataset):
    """P0.1 + P0.6: export_dataset only returns is_current=1 records."""
    now = "2026-07-09T12:00:00Z"

    crm_db.import_dataset(fresh_db, seed_dataset)

    # Manually archive one record
    fresh_db.execute('UPDATE prospects SET is_current = 0, archived_at = ? WHERE id = ?', (now, "prospect-smoke"))
    fresh_db.commit()

    exported = crm_db.export_dataset(fresh_db, generated_at=now)
    assert len(exported["prospects"]) == 0, f"Export returned {len(exported['prospects'])} prospects, expected 0 (archived)"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])