#!/usr/bin/env python3
"""Serve the operator dashboard and write status changes into SQLite.

Run this instead of `python3 -m http.server`:

  uv run python ops-crm/serve.py [--port 8000] [--root /path/to/repo]

GET serves repo files exactly like http.server. POST /api/action-status and
POST /api/prospect-status (JSON body: {"id": ..., "status": ...}) write through
db.set_action_status / db.set_prospect_status and re-export the dashboard JSON,
so the browser, SQLite, and data/ never disagree about status.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db as crm_db
import generate as crm_generate

# ponytail: one global lock serializes write+re-export so rapid double-clicks
# can't interleave JSON file writes; per-file locking if this ever needs throughput.
WRITE_LOCK = threading.Lock()

ROUTES = {
    "/api/action-status": crm_db.set_action_status,
    "/api/prospect-status": crm_db.set_prospect_status,
}


class OperatorHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: object, root: Path, **kwargs: object) -> None:
        self.root = root
        super().__init__(*args, directory=str(root), **kwargs)  # type: ignore[arg-type]

    def send_head(self):
        if (self.headers.get("Host") or "") not in self._allowed_hosts():
            self.send_error(403, "host not allowed")
            return None
        return super().send_head()

    def do_POST(self) -> None:
        setter = ROUTES.get(self.path)
        if setter is None:
            self.send_error(404, "unknown endpoint")
            return
        if not self._origin_ok():
            self._respond(403, {"ok": False, "error": "cross-origin request rejected"})
            return
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            self._respond(400, {"ok": False, "error": "Content-Type must be application/json"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            record_id, status = body["id"], body["status"]
        except (ValueError, KeyError):
            self._respond(400, {"ok": False, "error": "body must be JSON with id and status"})
            return
        try:
            with WRITE_LOCK:
                with crm_db.connect(self.root) as conn:
                    setter(conn, record_id, status)
                crm_generate.refresh_exports(self.root)
        except Exception as exc:
            # ponytail: broad except so a post-commit export failure (bad legacy row
            # elsewhere, locked file) always 400s instead of killing the connection
            # silently. The status write is already committed to SQLite when this
            # fires, so data/ can stay briefly stale until the underlying row is
            # fixed and a write re-triggers export — a real fix needs refresh_exports
            # to validate before this commits, sharing one transaction instead of
            # reconnecting. See review finding #3 (Tier 1).
            self._respond(400, {"ok": False, "error": str(exc)})
            return
        self._respond(200, {"ok": True})

    def _allowed_hosts(self) -> set[str]:
        port = self.server.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        return origin in {f"http://{host}" for host in self._allowed_hosts()}

    def _respond(self, code: int, payload: dict[str, object]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def make_server(root: Path, port: int) -> ThreadingHTTPServer:
    # ponytail: binds 127.0.0.1 with no auth — single-operator local tool; add auth
    # only if this ever listens beyond loopback.
    return ThreadingHTTPServer(("127.0.0.1", port), partial(OperatorHandler, root=root))


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the MMTVU Operator CRM with a SQLite status write path")
    parser.add_argument("--root", default=Path(__file__).resolve().parents[1], type=Path)
    parser.add_argument("--port", default=8000, type=int)
    args = parser.parse_args()
    server = make_server(args.root.resolve(), args.port)
    print(f"Operator CRM at http://127.0.0.1:{args.port}/ops-crm/ (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
