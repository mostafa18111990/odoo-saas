"""A tiny in-process HTTP server that speaks Odoo's JSON-2 URL shape (POST /json/2/<model>/<method>).

Backed by the in-memory FakeOdoo, it records every request so end-to-end tests can prove exactly which
Odoo model/method the real MCP server process calls. Test-only; binds to 127.0.0.1.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from odoo_accountant.models import ExecutionAuthorization, _INTERNAL_KEY
from tests.fakes import FakeOdoo

READ_METHODS = {"search_count", "search_read", "read", "formatted_read_group", "fields_get", "read_group"}
_TEST_AUTH = ExecutionAuthorization("apr_test_harness", "0" * 64, "test", _key=_INTERNAL_KEY)   # harness only; the code under test never sees it


class MockOdoo:
    def __init__(self, tables: dict, db: str = "e2edb"):
        self.fake = FakeOdoo(tables)
        self.db = db
        self.log: list = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                parts = self.path.strip("/").split("/")
                entry = {"path": self.path, "body": body, "db": self.headers.get("X-Odoo-Database"), "auth": self.headers.get("Authorization")}
                outer.log.append(entry)
                try:
                    assert parts[:2] == ["json", "2"] and len(parts) == 4, "bad path"
                    model, method = parts[2], parts[3]
                    entry.update(model=model, method=method)
                    if entry["db"] != outer.db:
                        raise PermissionError("wrong database header")
                    f = outer.fake
                    if method == "search_count":
                        res = f.search_count(model, body["domain"])
                    elif method == "search_read":
                        res = f.search_read(model, body.get("domain", []), body.get("fields"), body.get("limit"), body.get("order"), body.get("offset"))
                    elif method == "read":
                        res = f.read(model, body["ids"], body["fields"])
                    elif method in ("formatted_read_group", "read_group"):
                        res = f.formatted_read_group(model, body.get("domain", []), body.get("groupby"), body.get("aggregates"))
                    elif method == "fields_get":
                        res = f.fields_get(model, body.get("attributes"))
                    elif method in ("create", "write"):
                        res = f._mutate(model, method, body, _TEST_AUTH)
                    else:
                        raise LookupError(f"unsupported method {method}")
                    payload, status = json.dumps(res).encode(), 200
                except Exception as exc:  # noqa: BLE001
                    payload, status = json.dumps({"name": type(exc).__name__, "message": str(exc)}).encode(), 400
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.server.shutdown()
        self.server.server_close()

    @property
    def mutations(self):
        return [e for e in self.log if e.get("method") not in READ_METHODS]
