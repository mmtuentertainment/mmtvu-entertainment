import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("crm_generate", ROOT / "ops-crm" / "generate.py")
crm_generate = importlib.util.module_from_spec(SPEC)
sys.modules["crm_generate"] = crm_generate
assert SPEC.loader is not None
SPEC.loader.exec_module(crm_generate)


def test_generate_outputs_source_derived_core_records():
    private, public = crm_generate.generate(ROOT)

    assert len(private["prospects"]) >= 5
    assert len(private["assets"]) >= 5
    assert len(private["next_actions"]) >= 5
    assert len(private["evidence"]) >= 5
    assert private["next_actions"][0]["score"] >= private["next_actions"][1]["score"]
    assert private["next_actions"][0]["evidence_link"]
    assert "source_hashes" in private["next_actions"][0]
    assert private["next_actions"][0]["expected_revenue_path"]
    assert public["mode"] == "public"


def test_owner_operated_source_precedence_wins_and_preserves_sources():
    private, _ = crm_generate.generate(ROOT)
    halpin = next(p for p in private["prospects"] if p["id"] == "halpin-plumbing-inc-cincinnati-oh")

    assert halpin["owner_operator"] is True
    assert halpin["status"] == "contacted"
    assert "owner-operated-vapi-prospects.json won" in halpin["source_precedence"]
    assert "outreach/outreach-list-50.csv" in halpin["source_paths"]
    assert "outreach/owner-operated-vapi-prospects.json" in halpin["source_paths"]


def test_public_output_redacts_sensitive_contact_and_call_data():
    private, public = crm_generate.generate(ROOT)

    private_text = json.dumps(private)
    assert "phone" in private_text
    assert "transcript" not in json.dumps(public)
    public_text = json.dumps(public)
    assert "service@geteco.com" not in public_text
    assert not re.search(r"\b\d{3}[-.) ]+\d{3}[-. ]+\d{4}\b", public_text)
    assert "+151" not in public_text
    assert "\"phone\"" not in public_text
    assert "\"email\"" not in public_text
    assert "Halpin Plumbing Inc" not in public_text
    assert "Empire Contractors LLC" not in public_text
    assert "outreach/owner-operated-call-summaries.json" not in public_text
    assert "outreach/vapi-call-summaries.json" not in public_text
    assert "Local prospect" in public_text


def test_build_prospects_speaks_doc_funnel_vocabulary(tmp_path):
    csv_path = tmp_path / "outreach" / "outreach-list-50.csv"
    csv_path.parent.mkdir(parents=True)
    csv_path.write_text(
        "name,niche,city,website,priority,status\n"
        'A Co,HVAC,"X, OH",http://a.com,HIGH,Not contacted\n'
        'B Co,HVAC,"X, OH",http://b.com,HIGH,Contacted\n'
        'C Co,HVAC,"X, OH",http://c.com,LOW,Discovery booked\n',
        encoding="utf-8",
    )

    prospects = crm_generate.build_prospects(tmp_path, "2026-07-09T00:00:00Z")

    statuses = {p["company_name"]: p["status"] for p in prospects}
    assert statuses == {"A Co": "not_contacted", "B Co": "contacted", "C Co": "discovery_booked"}


def test_json_twin_does_not_clobber_csv_funnel_status(tmp_path):
    csv_path = tmp_path / "outreach" / "outreach-list-50.csv"
    csv_path.parent.mkdir(parents=True)
    csv_path.write_text(
        "name,niche,city,website,priority,status\n"
        'A Co,HVAC,"X, OH",http://a.com,HIGH,Contacted\n',
        encoding="utf-8",
    )
    json_path = tmp_path / "outreach" / "outreach-list-50.json"
    json_path.write_text(
        json.dumps([
            {
                "name": "A Co",
                "niche": "HVAC",
                "city": "X, OH",
                "website": "http://a.com",
                "priority": "HIGH",
                "status": "Contacted",
            }
        ]),
        encoding="utf-8",
    )

    prospects = crm_generate.build_prospects(tmp_path, "2026-07-09T00:00:00Z")

    assert prospects[0]["status"] == "contacted"


def test_build_prospects_rejects_status_outside_funnel(tmp_path):
    import pytest

    csv_path = tmp_path / "outreach" / "outreach-list-50.csv"
    csv_path.parent.mkdir(parents=True)
    csv_path.write_text(
        "name,niche,city,website,priority,status\n"
        'A Co,HVAC,"X, OH",http://a.com,HIGH,Maybe later\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Maybe later"):
        crm_generate.build_prospects(tmp_path, "2026-07-09T00:00:00Z")


def test_validation_rejects_orphan_next_action():
    private, _ = crm_generate.generate(ROOT)
    broken = json.loads(json.dumps(private))
    broken["next_actions"][0]["source_entity_id"] = "missing-prospect"

    try:
        crm_generate.validate_records(ROOT, broken)
    except ValueError as exc:
        assert "references missing" in str(exc)
    else:
        raise AssertionError("orphan next action should fail validation")
