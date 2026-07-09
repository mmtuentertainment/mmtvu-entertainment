import importlib.util
import json
import shutil
import sys
import threading
from http.client import HTTPConnection
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SPEC = importlib.util.spec_from_file_location("crm_serve", ROOT / "ops-crm" / "serve.py")
assert SPEC is not None and SPEC.loader is not None
crm_serve = importlib.util.module_from_spec(SPEC)
sys.modules["crm_serve"] = crm_serve
SPEC.loader.exec_module(crm_serve)
crm_db = crm_serve.crm_db

NOW = "2026-07-09T12:00:00Z"


def _record_base():
    return {
        "source_paths": ["outreach/example.json"],
        "evidence_link": "outreach/example.json",
        "generated_at": NOW,
        "source_hashes": {},
        "source_precedence": "test seed",
    }


def _scaffold_root(tmp_path):
    """A minimal repo root: schemas + a seeded SQLite, no source artifacts."""
    (tmp_path / "ops-crm").mkdir()
    shutil.copytree(ROOT / "ops-crm" / "schemas", tmp_path / "ops-crm" / "schemas")
    dataset = {
        "generated_at": NOW,
        "prospects": [
            {
                "id": "p1",
                "company_name": "Alpha Plumbing Co",
                "city": "X, OH",
                "niche": "Plumbing",
                "priority": "high",
                "status": "not_contacted",
                "owner_operator": True,
                **_record_base(),
            }
        ],
        "next_actions": [
            {
                "id": "action-p1",
                "owner": "Hermes",
                "action": "Call the seeded prospect",
                "priority": "high",
                "score": 50.0,
                "due_at": "today",
                "reason": "seed",
                "expected_revenue_path": "seed path",
                "status": "open",
                "source_entity_type": "prospect",
                "source_entity_id": "p1",
                **_record_base(),
            }
        ],
        "assets": [],
        "offers": [],
        "evidence": [],
    }
    with crm_db.connect(tmp_path) as conn:
        crm_db.import_dataset(conn, dataset)
    return tmp_path


def _post(client, path, payload):
    client.request("POST", path, json.dumps(payload), {"Content-Type": "application/json"})
    resp = client.getresponse()
    body = resp.read()
    try:
        return resp.status, json.loads(body)
    except json.JSONDecodeError:
        return resp.status, {}


def test_post_rejects_cross_origin_request(tmp_path):
    root = _scaffold_root(tmp_path)
    server = crm_serve.make_server(root, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        client.request(
            "POST",
            "/api/action-status",
            json.dumps({"id": "action-p1", "status": "done"}),
            {"Content-Type": "application/json", "Origin": "http://evil.example"},
        )
        resp = client.getresponse()
        resp.read()
        assert resp.status == 403
        with crm_db.connect(root) as conn:
            assert conn.execute("SELECT status FROM actions WHERE id='action-p1'").fetchone()["status"] == "open"
    finally:
        server.shutdown()


def test_post_rejects_non_json_content_type(tmp_path):
    root = _scaffold_root(tmp_path)
    server = crm_serve.make_server(root, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        client.request(
            "POST",
            "/api/action-status",
            json.dumps({"id": "action-p1", "status": "done"}),
            {"Content-Type": "text/plain"},
        )
        resp = client.getresponse()
        resp.read()
        assert resp.status == 400
        with crm_db.connect(root) as conn:
            assert conn.execute("SELECT status FROM actions WHERE id='action-p1'").fetchone()["status"] == "open"
    finally:
        server.shutdown()


def _raw_get(port, path, host_header):
    conn = HTTPConnection("127.0.0.1", port, timeout=10)
    conn.putrequest("GET", path, skip_host=True)
    conn.putheader("Host", host_header)
    conn.endheaders()
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, body


def test_get_rejects_dns_rebound_host_header(tmp_path):
    root = _scaffold_root(tmp_path)
    server = crm_serve.make_server(root, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        status, body = _raw_get(port, "/ops-crm/schemas/prospect.schema.json", "evil.example")
        assert status == 403
        assert b"Prospect" not in body

        status, body = _raw_get(port, "/ops-crm/schemas/prospect.schema.json", f"127.0.0.1:{port}")
        assert status == 200
        assert b"Prospect" in body
    finally:
        server.shutdown()


def test_post_returns_400_when_refresh_exports_raises_unexpected_error(tmp_path, monkeypatch):
    root = _scaffold_root(tmp_path)

    def boom(*_args, **_kwargs):
        raise OSError("simulated locked-file failure")

    monkeypatch.setattr(crm_serve.crm_generate, "refresh_exports", boom)
    server = crm_serve.make_server(root, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        status, body = _post(client, "/api/action-status", {"id": "action-p1", "status": "done"})
        assert status == 400
        assert "simulated locked-file failure" in body["error"]
    finally:
        server.shutdown()


def test_post_rejects_non_dict_json_body(tmp_path):
    # Finding #8: a syntactically valid but non-dict JSON body (null, a list, a bare
    # value) raised an uncaught TypeError at body["id"], killing the connection with
    # no HTTP response instead of a clean 400.
    root = _scaffold_root(tmp_path)
    server = crm_serve.make_server(root, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        for bad_body in (None, [], "just a string", 42):
            status, body = _post(client, "/api/action-status", bad_body)
            assert status == 400
            assert "error" in body
        with crm_db.connect(root) as conn:
            assert conn.execute("SELECT status FROM actions WHERE id='action-p1'").fetchone()["status"] == "open"
    finally:
        server.shutdown()


def test_post_returns_400_against_a_db_with_no_tables(tmp_path):
    # Finding #8's other half: running serve.py before generate.py ever ran leaves
    # a stray empty crm.sqlite (connect()'s mkdir/connect creates the file but no
    # tables) — the setter's sqlite3.OperationalError must 400, not kill the
    # connection. Already covered by Tier 1's broad `except Exception` around the
    # setter+refresh_exports call; this test locks that in for finding #8 too.
    (tmp_path / "ops-crm").mkdir()
    (tmp_path / "ops-crm" / "crm.sqlite").touch()
    server = crm_serve.make_server(tmp_path, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        status, body = _post(client, "/api/action-status", {"id": "x", "status": "done"})
        assert status == 400
        assert "error" in body
    finally:
        server.shutdown()


def test_post_status_writes_sqlite_and_refreshes_exports(tmp_path):
    root = _scaffold_root(tmp_path)
    server = crm_serve.make_server(root, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)

        status, body = _post(client, "/api/action-status", {"id": "action-p1", "status": "done"})
        assert (status, body["ok"]) == (200, True)
        with crm_db.connect(root) as conn:
            assert conn.execute("SELECT status FROM actions WHERE id='action-p1'").fetchone()["status"] == "done"
        summary_path = root / "ops-crm" / "data" / "private" / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        assert next(a for a in summary["next_actions"] if a["id"] == "action-p1")["status"] == "done"

        status, body = _post(client, "/api/prospect-status", {"id": "p1", "status": "replied"})
        assert (status, body["ok"]) == (200, True)
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        metrics = {m["metric_name"]: m for m in summary["metrics"]}
        assert metrics["contacted_total"]["metric_value"] == 1

        status, body = _post(client, "/api/action-status", {"id": "action-p1", "status": "finished"})
        assert status == 400
        assert "finished" in body["error"]

        status, body = _post(client, "/api/prospect-status", {"id": "nope", "status": "replied"})
        assert status == 400

        status, _ = _post(client, "/api/unknown", {"id": "x", "status": "y"})
        assert status == 404
    finally:
        server.shutdown()
