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
from .config import LOOPBACK_HOSTS, Config
from .render import render_event
from .source import FileSource

log = logging.getLogger("claudeloop.web")

STATIC = Path(__file__).parent / "static"
TASK_ID_RE = re.compile(r"^[0-9a-f]{16}$")
"""task_id is interpolated into a filesystem path, so it is validated before
it reaches the disk. This is a traversal guard, not tidiness."""

STALE_AFTER_S = 90
"""A dedicated asyncio task inside main_loop refreshes status.heartbeat every
~10s (see loop.HEARTBEAT_S), independent of any task state transition -- so
it keeps advancing through a running session (up to session_timeout_s, 4h by
default) or a quota sleep (up to MAX_WAIT_S, 8 days), both long awaits with
no transition of their own. Three missed refreshes past that cadence means
the event loop itself has stopped spinning, not just that nothing changed."""

RECENT_TASKS = 50
TASK_LOG_ENTRIES = 2000

SSE_POLL_S = 0.5
REPLAY_ENTRIES = 200
PING_S = 15

TAIL_CAP_BYTES = 8 * 1024 * 1024
"""Read at most this many trailing bytes of an event log, however large the
file has grown over a multi-day run. Comfortably covers REPLAY_ENTRIES /
TASK_LOG_ENTRIES worth of ordinary events; a run with unusually large tool
output in its tail entries simply gets fewer of them replayed, rather than
the orchestrator's own process running out of memory to serve a viewer."""


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


def _cut(data: bytes) -> tuple[bytes, int]:
    """`data` up to its last complete line, and how many bytes that is.

    A write caught mid-line (the loop appends while this reads) must be left
    for the next read rather than rendered as a broken entry.
    """
    cut = data.rfind(b"\n") + 1
    return data[:cut], cut


def _tail(path: Path, cap: int) -> tuple[bytes, int]:
    """Up to the last `cap` bytes of `path`, cut to whole lines.

    Seeks from the end instead of reading the file whole: an events.jsonl
    appended to for days can run into the hundreds of MB, and a full read
    (worse, two -- one for the cut copy) is memory the orchestrator's own
    process cannot spare just to serve a dashboard viewer. A line the seek
    lands inside is dropped -- it belongs to data further back than the cap
    reaches, or is one giant line the cap can't hold either way -- rather
    than rendered as a broken fragment.

    Returns the whole-line bytes and the file offset just past them.
    """
    with open(path, "rb") as handle:
        size = handle.seek(0, 2)
        start = max(0, size - cap)
        handle.seek(start)
        data = handle.read()
    if start:
        nl = data.find(b"\n")
        if nl == -1:
            return b"", size  # the one held-back line alone exceeds the cap
        data = data[nl + 1 :]
        start += nl + 1
    data, cut = _cut(data)
    return data, start + cut


def _render(data: bytes) -> list[dict]:
    """Rendered entries for whole lines already cut by `_cut`/`_tail`."""
    entries: list[dict] = []
    for raw in data.splitlines():
        entries.extend(render_line(raw))
    return entries


def read_log(path: Path, limit: int) -> list[dict]:
    """The last `limit` rendered entries of an event log."""
    try:
        data, _ = _tail(path, TAIL_CAP_BYTES)
    except OSError:
        return []
    return _render(data)[-limit:]


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
            if task.id != snapshot.task_id
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
    timeout = 65
    """The page polls over keep-alive every 3s and SSE writes a ping every
    PING_S; either way a live peer generates traffic well inside this. A
    socket that never triggers it is a half-open TCP connection (the phone
    left wifi mid-idle, no FIN, no RST) that would otherwise park this
    thread -- and its fd -- for the life of the process. Without a timeout,
    StreamRequestHandler.setup() never calls settimeout() at all."""

    def log_message(self, fmt, *args):  # the stdlib default spams stderr
        log.debug("%s %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:
        if not self._host_allowed():
            self._json(403, {"error": "bad host"})
            return
        parsed = urlparse(self.path)
        if not self._authorized(parsed.query):
            self._json(403, {"error": "bad or missing token"})
            return
        route = parsed.path
        cfg = self.server.cfg
        if route == "/":
            self._file(STATIC / "index.html", "text/html; charset=utf-8")
        elif route in ("/logo.png", "/favicon.ico"):
            self._file(STATIC / "logo.png", "image/png", cache=True)
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

    def _host_allowed(self) -> bool:
        """Reject a Host header that does not name this server.

        Defends against DNS rebinding: a page open in a browser on this same
        machine can point an attacker-controlled hostname at 127.0.0.1, and
        the browser will still send that hostname in Host when it lands on
        this socket -- same-origin policy does not help, since as far as the
        browser is concerned every request still goes to the origin it
        started with. This is the only thing standing between an arbitrary
        website and /api/state at the loopback default, where web_token is
        empty by design.
        """
        cfg = self.server.cfg
        parsed = urlparse(f"//{self.headers.get('Host', '')}")
        allowed_hosts = LOOPBACK_HOSTS if cfg.web_host in LOOPBACK_HOSTS else (cfg.web_host,)
        return parsed.hostname in allowed_hosts and parsed.port == self.server.server_port

    def _authorized(self, query: str) -> bool:
        expected = self.server.cfg.web_token
        if not expected:
            return True
        given = (parse_qs(query).get("token") or [""])[0]
        # compare_digest requires ASCII-only str arguments; parse_qs happily
        # decodes a non-ASCII query value, and that mismatch used to escape
        # as an uncaught TypeError instead of a plain 403.
        if not given.isascii():
            return False
        return secrets.compare_digest(given, expected)

    def _json(self, code: int, payload: dict) -> None:
        self._body(code, "application/json", json.dumps(payload, default=str).encode())

    def _file(self, path: Path, content_type: str, cache: bool = False) -> None:
        try:
            data = path.read_bytes()
        except OSError:
            self._json(404, {"error": "not found"})
            return
        self._body(200, content_type, data, cache=cache)

    def _body(self, code: int, content_type: str, data: bytes, cache: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if cache:
            # The logo is a multi-MB asset served to a phone on every load;
            # it never changes at runtime, so it is worth paying for once.
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
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
        except OSError:
            # The viewer disconnected -- closed the tab (BrokenPipeError),
            # reset the connection (ConnectionResetError), or, most often,
            # just walked out of wifi range with no FIN and no RST, which
            # surfaces here as a plain TimeoutError on the next write.
            # EventSource will reconnect either way.
            pass
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
                if run_dir is not None:
                    # The loop can flip run_dir to None within milliseconds
                    # of the task finishing -- well inside one poll -- so
                    # whatever the run wrote since the last drain (closing
                    # prose, the cost/duration line) has to be flushed here
                    # or it is never seen at all.
                    self._drain(run_dir / "events.jsonl", offset)
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
                # raises OSError and the pump ends.
                self._sse({"kind": "ping"})
                last_ping = now
            time.sleep(SSE_POLL_S)

    def _replay(self, path: Path) -> int:
        """Emit up to REPLAY_ENTRIES existing lines; return the offset to
        resume tailing from.

        Bounded to the last TAIL_CAP_BYTES of the file (see `_tail`) rather
        than reading it whole: a multi-day run's events.jsonl only grows, and
        a full read here -- worse, two, one for the cut copy -- is exactly
        the kind of thing that must never be able to OOM the process this
        dashboard is supposed to be watching, not threatening.
        """
        try:
            data, offset = _tail(path, TAIL_CAP_BYTES)
        except OSError:
            return 0
        for entry in _render(data)[-REPLAY_ENTRIES:]:
            self._sse(entry)
        return offset

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
            raw = handle.read()
        # The loop appends while this reads, so a read can land mid-line.
        # Advance only to the last newline and leave the remainder for the
        # next pass.
        data, cut = _cut(raw)
        for entry in _render(data):
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
