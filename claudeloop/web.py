"""The dashboard's HTTP surface: a ThreadingHTTPServer on a daemon thread.

It reads state.db through its own read-only connection and tails event logs
off disk. It never touches the loop's objects, so nothing here can corrupt
loop state, and S2a never writes anything.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import status as status_module
from .config import Config
from .render import render_event
from .source import FileSource

log = logging.getLogger("claudeloop.web")

STATIC = Path(__file__).parent / "static"
TASK_ID_RE = re.compile(r"^[0-9a-f]{16}$")
"""task_id is interpolated into a filesystem path, so it is validated before
it reaches the disk. This is a traversal guard, not tidiness."""

STALE_AFTER_S = 90
"""The loop refreshes the heartbeat at least every POLL_S (30s) even when
idle, so three missed refreshes means it is not running."""

RECENT_TASKS = 50
TASK_LOG_ENTRIES = 2000

SSE_POLL_S = 0.5
REPLAY_ENTRIES = 200
PING_S = 15


def _connect(cfg: Config) -> sqlite3.Connection | None:
    """A read-only connection of this request's own.

    The loop's connection belongs to the loop's thread and must not be shared.
    Returns None before the loop has created the database.
    """
    path = cfg.home / "state.db"
    if not path.exists():
        return None
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def read_log(path: Path, limit: int) -> list[dict]:
    """The last `limit` rendered entries of an event log."""
    entries: list[dict] = []
    try:
        with open(path, "rb") as handle:
            for raw in handle:
                entries.extend(render_line(raw))
    except OSError:
        return []
    # ponytail: reads the whole log to keep its tail. Seek backwards from the
    # end if run logs ever get big enough for that to matter.
    return entries[-limit:]


def render_line(raw: bytes) -> list[dict]:
    try:
        return render_event(json.loads(raw))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []


def api_state(cfg: Config) -> dict:
    snapshot = status_module.current
    db = _connect(cfg)
    completed: list[dict] = []
    if db is not None:
        try:
            completed = [
                dict(row)
                for row in db.execute(
                    "SELECT id, text, status, summary, question, cost_usd,"
                    " started_at, finished_at FROM tasks WHERE status != 'running'"
                    " ORDER BY COALESCE(finished_at, started_at) DESC LIMIT ?",
                    (RECENT_TASKS,),
                )
            ]
        finally:
            db.close()
    return {
        "status": {
            "state": snapshot.state,
            "task_id": snapshot.task_id,
            "task_text": snapshot.task_text,
            "session_id": snapshot.session_id,
            "attempt": snapshot.attempt,
            "started_at": snapshot.started_at,
            "wait_until": snapshot.wait_until,
            "rate_limit": snapshot.rate_limit,
            "last_error": snapshot.last_error,
            "heartbeat": snapshot.heartbeat,
            "stale": time.time() - snapshot.heartbeat > STALE_AFTER_S,
        },
        "pending": [
            {"id": task.id, "text": task.text}
            for task in FileSource(cfg.tasks_file).pending()
        ],
        "completed": completed,
        "now": time.time(),
    }


def api_task(cfg: Config, task_id: str) -> dict | None:
    if not TASK_ID_RE.match(task_id):
        return None
    db = _connect(cfg)
    if db is None:
        return None
    try:
        row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            return None
        runs = [
            dict(run)
            for run in db.execute(
                "SELECT id, session_id, started_at, ended_at, exit_reason,"
                " resume_count FROM runs WHERE task_id=? ORDER BY id",
                (task_id,),
            )
        ]
    finally:
        db.close()
    return {
        "task": dict(row),
        "runs": runs,
        "log": read_log(cfg.home / "runs" / task_id / "events.jsonl", TASK_LOG_ENTRIES),
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ClaudeLoop"

    def log_message(self, fmt, *args):  # the stdlib default spams stderr
        log.debug("%s %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if not self._authorized(parsed.query):
            self._json(403, {"error": "bad or missing token"})
            return
        route = parsed.path
        cfg = self.server.cfg
        if route == "/":
            self._file(STATIC / "index.html", "text/html; charset=utf-8")
        elif route in ("/logo.png", "/favicon.ico"):
            self._file(STATIC / "logo.png", "image/png")
        elif route == "/api/state":
            self._json(200, api_state(cfg))
        elif route == "/api/events":
            self._stream_events()
        elif route.startswith("/api/tasks/"):
            payload = api_task(cfg, route[len("/api/tasks/") :])
            if payload is None:
                self._json(404, {"error": "no such task"})
            else:
                self._json(200, payload)
        else:
            self._json(404, {"error": "not found"})

    def _authorized(self, query: str) -> bool:
        expected = self.server.cfg.web_token
        if not expected:
            return True
        given = (parse_qs(query).get("token") or [""])[0]
        return secrets.compare_digest(given, expected)

    def _json(self, code: int, payload: dict) -> None:
        self._body(code, "application/json", json.dumps(payload, default=str).encode())

    def _file(self, path: Path, content_type: str) -> None:
        try:
            data = path.read_bytes()
        except OSError:
            self._json(404, {"error": "not found"})
            return
        self._body(200, content_type, data)

    def _body(self, code: int, content_type: str, data: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _stream_events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self._pump()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the viewer closed the tab; EventSource will reconnect
        self.close_connection = True

    def _pump(self) -> None:
        """Follow whichever run is current, forever.

        One loop covers idle, an active run, and a switch between runs. An
        early return on idle would make EventSource reconnect immediately and
        hammer the server.
        """
        run_dir = None
        offset = 0
        last_ping = 0.0
        while True:
            live = status_module.current.run_dir
            if live != run_dir:
                run_dir = live
                offset = 0
                if run_dir is not None:
                    self._sse({"kind": "run", "task_id": run_dir.name})
                    offset = self._replay(run_dir / "events.jsonl")
            if run_dir is not None:
                offset = self._drain(run_dir / "events.jsonl", offset)
            now = time.time()
            if now - last_ping > PING_S:
                # Writing is how a departed viewer is noticed: the write
                # raises BrokenPipeError and the pump ends.
                self._sse({"kind": "ping"})
                last_ping = now
            time.sleep(SSE_POLL_S)

    def _replay(self, path: Path) -> int:
        """Emit up to REPLAY_ENTRIES existing lines; return the offset to
        resume tailing from.

        Reads the file once and derives both the replayed entries and the
        offset from that single snapshot, cut to the last complete line --
        the same rule _drain applies. A second, later stat() call here would
        race with a concurrent append (the run may already be writing): a
        line that lands between the read and the stat would be counted in
        the offset without ever having been read, and so would be silently
        skipped by both the replay and the first _drain pass.
        """
        try:
            data = path.read_bytes()
        except OSError:
            return 0
        cut = data.rfind(b"\n") + 1
        entries: list[dict] = []
        for raw in data[:cut].splitlines():
            entries.extend(render_line(raw))
        for entry in entries[-REPLAY_ENTRIES:]:
            self._sse(entry)
        return cut

    def _drain(self, path: Path, offset: int) -> int:
        """Emit every whole line past `offset`; return the new offset."""
        try:
            size = path.stat().st_size
        except OSError:
            return offset
        if size <= offset:
            return offset
        with open(path, "rb") as handle:
            handle.seek(offset)
            data = handle.read()
        # The loop appends while this reads, so a read can land mid-line.
        # Advance only to the last newline and leave the remainder for the
        # next pass.
        cut = data.rfind(b"\n") + 1
        for raw in data[:cut].splitlines():
            for entry in render_line(raw):
                self._sse(entry)
        return offset + cut

    def _sse(self, entry: dict) -> None:
        self.wfile.write(f"data: {json.dumps(entry, default=str)}\n\n".encode())
        self.wfile.flush()


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, cfg: Config):
        # BaseHTTPRequestHandler cannot take extra constructor arguments, so
        # config reaches the handler through the server it is bound to.
        self.cfg = cfg
        super().__init__(address, handler)


def serve(cfg: Config) -> ThreadingHTTPServer:
    """Start the dashboard on a daemon thread and return its server."""
    server = _Server((cfg.web_host, cfg.web_port), Handler, cfg)
    threading.Thread(
        target=server.serve_forever, name="claudeloop-web", daemon=True
    ).start()
    log.info("dashboard on http://%s:%s", cfg.web_host, server.server_port)
    return server
