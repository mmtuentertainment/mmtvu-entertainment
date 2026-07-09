"""Tests for the redaction seam: derive_public + assert_public_safe.

assert_public_safe is the backstop net: these tests prove it actually
catches planted leaks (it had never been shown to fail before).
derive_public is the publish seam: fail-closed, postcondition inside.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import redaction


def _minimal_private() -> dict:
    """Smallest dataset derive_public accepts: the 11 known collections."""
    return {
        "mode": "private",
        "generated_at": "2026-07-09T00:00:00Z",
        "offers": [],
        "prospects": [],
        "assets": [],
        "next_actions": [],
        "evidence": [],
        "loops": [],
        "experiments": [],
        "metrics": [],
        "summary": {"prospects": 0, "next_actions": 0, "top_action_id": None},
    }


# --- derive_public: fail-closed publish seam ---

def test_derive_public_rejects_unknown_collection():
    private = _minimal_private()
    private["call_recordings"] = [{"url": "recordings/rec1.wav"}]
    with pytest.raises(ValueError, match="call_recordings"):
        redaction.derive_public(private)


def test_loops_experiments_metrics_free_text_is_scrubbed():
    private = _minimal_private()
    private["experiments"] = [{"id": "e1", "hypothesis": "h", "learning": "owner asked to call (555) 123-4567", "status": "active"}]
    private["metrics"] = [{"id": "m1", "metric_name": "x", "metric_value": 1, "unit": "count", "source": "mail bad@example.com"}]
    private["loops"] = [{"id": "l1", "name": "outreach", "goal": "collect api_key=sk-12345 notes", "status": "active"}]
    public = redaction.derive_public(private)
    text = json.dumps(public)
    assert "(555) 123-4567" not in text
    assert "bad@example.com" not in text
    assert "sk-12345" not in text


def test_derive_public_runs_backstop_internally():
    """A company name in loop free text isn't scrubbed by redact_record;
    the internal backstop must refuse to return the output."""
    private = _minimal_private()
    private["prospects"] = [{"id": "p1", "company_name": "Acme Plumbing", "priority": "high", "status": "new"}]
    private["loops"] = [{"id": "l1", "name": "outreach", "goal": "Close Acme Plumbing this week", "status": "active"}]
    with pytest.raises(ValueError, match="company-name"):
        redaction.derive_public(private)


# --- assert_public_safe: the backstop must catch planted leaks ---

def test_backstop_catches_planted_phone():
    with pytest.raises(ValueError, match="phone"):
        redaction.assert_public_safe({"note": "call me at (555) 123-4567"})


def test_backstop_catches_planted_email():
    with pytest.raises(ValueError, match="email"):
        redaction.assert_public_safe({"note": "reach sales@example.com today"})


def test_backstop_catches_sensitive_key():
    with pytest.raises(ValueError, match="phone"):
        redaction.assert_public_safe({"prospects": [{"phone": "[redacted]"}]})


def test_backstop_catches_company_name_and_slug():
    with pytest.raises(ValueError, match="company-name"):
        redaction.assert_public_safe(
            {"note": "Acme Plumbing said yes"},
            private_company_names=["Acme Plumbing"],
        )
    with pytest.raises(ValueError, match="company-slug"):
        redaction.assert_public_safe(
            {"id": "prospect-acme-plumbing"},
            private_company_names=["Acme Plumbing"],
        )


def test_backstop_catches_private_data_path():
    with pytest.raises(ValueError, match="private-data-path"):
        redaction.assert_public_safe({"src": "ops-crm/data/private/x.json"})


def test_redact_record_scrubs_hostile_fixture():
    hostile = {"next_actions": [{"reason": "Call 614-555-1212 or email bad@example.com token=abc"}]}
    redacted = redaction.redact_record(hostile)
    assert "614-555-1212" not in json.dumps(redacted)
    assert "bad@example.com" not in json.dumps(redacted)
    assert "token=abc" not in json.dumps(redacted)


def test_backstop_accepts_clean_output():
    redaction.assert_public_safe(
        {"note": "a local prospect asked for a follow-up"},
        private_company_names=["Acme Plumbing"],
    )
