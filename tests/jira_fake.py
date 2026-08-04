"""A stand-in Jira Cloud, over http.server. Real sockets, real urllib on the
other end -- the same choice tests/ already makes with the fake claude CLI."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "jira"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


class FakeJira:
    """routes maps "METHOD /path/suffix" to (status, payload) or to a list of
    them, consumed one per call so a test can make the first attempt fail and
    the second succeed."""

    def __init__(self, routes: dict):
        self.routes = {key: list(value) if isinstance(value, list) else [value]
                       for key, value in routes.items()}
        self.requests: list[tuple[str, str, dict | None]] = []
        # Kept separate from .requests rather than widening that tuple: two
        # existing tests unpack it as exactly (method, path, payload), and the
        # path in it is deliberately stripped of its query so a route key
        # stays stable.
        self.authorizations: list[str | None] = []
        self.raw_paths: list[str] = []
        """Every path as asked for, query string included -- which is where
        the comment reads carry their bound."""
        server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server = server
        self.url = f"http://127.0.0.1:{server.server_port}"
        self.thread = threading.Thread(target=server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep the test output clean
                pass

            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                payload = json.loads(raw) if raw else None
                path = self.path.split("?")[0].replace("/rest/api/2", "")
                fake.raw_paths.append(self.path.replace("/rest/api/2", ""))
                fake.requests.append((self.command, path, payload))
                fake.authorizations.append(self.headers.get("Authorization"))
                queue = fake.routes.get(f"{self.command} {path}")
                if not queue:
                    status, body = 404, {"errorMessages": ["no such route"]}
                else:
                    status, body = queue[0] if len(queue) == 1 else queue.pop(0)
                if "orderBy=-created" in self.path and isinstance(body, dict):
                    # Jira honours this, and JiraSource.answer depends on it:
                    # it asks for the newest comments so a bounded page still
                    # contains the boundary it looks for. A fake that ignored
                    # it would let a wrong assumption about the order pass.
                    body = dict(body)
                    comments = body.get("comments")
                    if isinstance(comments, list):
                        body["comments"] = list(reversed(comments))
                encoded = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            do_GET = do_POST = do_PUT = _serve

        return Handler
