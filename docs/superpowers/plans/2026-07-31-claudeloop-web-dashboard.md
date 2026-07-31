# ClaudeLoop Web Dashboard (S2a) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A live, read-only web dashboard for the orchestrator, reachable from a phone on the local network, showing whether the loop is alive, what it is working on, what the session is saying, what is queued, what finished, and how much quota is left.

**Architecture:** A `ThreadingHTTPServer` on a daemon thread inside the existing process. It reads `state.db` through its own read-only connection and tails `runs/<task-id>/events.jsonl` off disk, so it never touches the loop's objects. One frozen `Status` snapshot crosses the thread boundary by atomic reference swap. Live output goes out over Server-Sent Events. The page is a single no-build HTML file.

**Tech Stack:** Python 3.11+ standard library only — `http.server`, `sqlite3`, `threading`, `secrets`, `json`. Frontend is plain HTML/CSS/ES modules, no framework and no build step.

## Global Constraints

- **Python 3.11 or newer.**
- **No third-party packages, ever.** Not for the orchestrator, not for the tests, not for the frontend. `pip install` and `npm install` must both remain unnecessary.
- **No build step for the frontend.** One `claudeloop/static/index.html` with inline CSS and an inline `<script type="module">`.
- **S2a writes nothing.** No route mutates state, the task file, or the database. The only new files on disk are source files.
- **The web layer never touches the loop's objects.** Its sqlite connection is its own and opened read-only; event logs are read from disk.
- **Binding a non-loopback `web_host` with an empty `web_token` is a startup error, not a warning.**
- Tests run as `python -m unittest discover -s tests -t .` from the repository root. The suite stands at **72 tests** before this plan and should stand at **131** after it.
- Reference spec: `docs/superpowers/specs/2026-07-31-claudeloop-web-dashboard-design.md`.

## Deviations from the spec

Four, all deliberate:

0. **Task 7 carries no complete code block.** Every other task in this plan
   hands the implementer the exact code to write; Task 7 hands it the palette,
   the page structure, the behaviour, the hard constraints, and two required
   design skills. A 600-line HTML file transcribed through a plan is neither
   reviewable nor better than the skill-guided result, and this is the one
   task with genuine visual judgment in it.

1. **`render_event` returns `list[dict]`, not `dict | None`.** A single `assistant` message routinely carries prose *and* several `tool_use` blocks in one `content` array; a one-entry return would silently drop all but the first. The empty list replaces `None`.
2. **`web.serve(cfg)` is called from `main()`, not `main_loop()`.** `main_loop` is called directly by a dozen existing tests, which would then each bind a TCP port. Startup wiring belongs in `main()`; tests that want both call `serve()` themselves.
3. **`/api/tasks/<id>` validates the task id against `[0-9a-f]{16}` before touching the filesystem.** `task_id` is interpolated into a path, so without this `/api/tasks/..%2f..%2fetc` is a traversal. The spec did not mention it; it is not optional.

## File Structure

| File | Responsibility |
|---|---|
| `claudeloop/status.py` | The frozen `Status` snapshot, the module-level `current`, and `set_status`. |
| `claudeloop/render.py` | `render_event` — raw stream-json to compact display entries. Pure. |
| `claudeloop/web.py` | Request handler, routes, token check, SSE pump, `serve()`. |
| `claudeloop/static/index.html` | The entire frontend. |
| `claudeloop/static/logo.png` | Already committed. Served as both logo and favicon. |
| `claudeloop/config.py` | *Modify:* `web_host`, `web_port`, `web_token` + the non-loopback rule. |
| `claudeloop/loop.py` | *Modify:* `latest_rate_limit`, `set_status` at each transition, `serve()` in `main()`. |
| `tests/test_status.py` | Snapshot replacement semantics. |
| `tests/test_render.py` | Every branch of `render_event`. |
| `tests/test_web.py` | Routes, token, task detail, SSE, path traversal. |
| `tests/test_config.py` | *Modify:* the new keys and the non-loopback rule. |
| `tests/test_loop.py` | *Modify:* status transitions during a real run. |

---

### Task 1: Status snapshot

**Files:**
- Create: `claudeloop/status.py`
- Create: `tests/test_status.py`

**Interfaces:**
- Consumes: nothing.
- Produces: frozen dataclass `Status` with fields `state: str = "idle"`, `task_id: str | None = None`, `task_text: str | None = None`, `run_dir: Path | None = None`, `session_id: str | None = None`, `attempt: int = 0`, `started_at: float | None = None`, `wait_until: float | None = None`, `rate_limit: dict | None = None`, `last_error: str | None = None`, `heartbeat: float = 0.0`. Module-level `current: Status`. `set_status(**changes) -> Status`. `reset() -> Status`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_status.py`:

```python
import time
import unittest
from pathlib import Path

from claudeloop import status


class StatusTest(unittest.TestCase):
    def setUp(self):
        status.reset()

    def test_starts_idle_with_a_zero_heartbeat(self):
        self.assertEqual(status.current.state, "idle")
        self.assertIsNone(status.current.task_id)
        self.assertEqual(status.current.heartbeat, 0.0)

    def test_set_status_replaces_the_module_level_current(self):
        before = status.current
        after = status.set_status(state="running", task_id="abc")
        self.assertIs(status.current, after)
        self.assertIsNot(status.current, before)
        self.assertEqual(status.current.state, "running")

    def test_unnamed_fields_are_carried_over(self):
        status.set_status(task_id="abc", task_text="do it")
        status.set_status(state="waiting")
        self.assertEqual(status.current.task_id, "abc")
        self.assertEqual(status.current.task_text, "do it")
        self.assertEqual(status.current.state, "waiting")

    def test_every_transition_refreshes_the_heartbeat(self):
        status.set_status(state="running")
        self.assertAlmostEqual(status.current.heartbeat, time.time(), delta=1)

    def test_an_explicit_heartbeat_wins(self):
        status.set_status(state="running", heartbeat=123.0)
        self.assertEqual(status.current.heartbeat, 123.0)

    def test_the_snapshot_is_frozen(self):
        status.set_status(state="running")
        with self.assertRaises(Exception):
            status.current.state = "idle"

    def test_paths_survive_the_round_trip(self):
        status.set_status(run_dir=Path("/tmp/run"))
        self.assertEqual(status.current.run_dir, Path("/tmp/run"))

    def test_reset_returns_to_a_fresh_idle(self):
        status.set_status(state="running", task_id="abc")
        status.reset()
        self.assertEqual(status.current.state, "idle")
        self.assertIsNone(status.current.task_id)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_status -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'claudeloop.status'`

- [ ] **Step 3: Write the implementation**

Create `claudeloop/status.py`:

```python
"""The one value that crosses the loop/web-thread boundary.

The loop *replaces* `current` with a new frozen instance on every transition;
the web thread reads the reference. An atomic reference swap needs no lock and
cannot tear: a reader sees either the old snapshot whole or the new one whole.
Per-field assignment on a shared mutable object would not give that.
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Status:
    state: str = "idle"  # "idle" | "running" | "waiting" | "error"
    task_id: str | None = None
    task_text: str | None = None
    run_dir: Path | None = None
    session_id: str | None = None
    attempt: int = 0
    started_at: float | None = None
    wait_until: float | None = None  # set while sleeping off a quota block
    rate_limit: dict | None = None  # last rate_limit_info seen, for the gauge
    last_error: str | None = None
    heartbeat: float = 0.0


current = Status()


def set_status(**changes) -> Status:
    """Replace `current` with a copy carrying `changes`.

    Always refreshes the heartbeat unless one is passed explicitly: any
    transition at all is proof the loop is still alive. Fields not named are
    carried over, so a caller moving to a state that no longer has a task must
    clear those fields itself.
    """
    global current
    changes.setdefault("heartbeat", time.time())
    current = dataclasses.replace(current, **changes)
    return current


def reset() -> Status:
    """Back to a fresh idle snapshot. For tests."""
    global current
    current = Status()
    return current
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_status -v`
Expected: PASS, 8 tests.

- [ ] **Step 5: Commit**

```bash
git add claudeloop/status.py tests/test_status.py
git commit -m "feat: frozen status snapshot for the loop/web boundary"
```

---

### Task 2: Web configuration keys

**Files:**
- Modify: `claudeloop/config.py`
- Modify: `tests/test_config.py` (append a test class; leave the existing one alone)

**Interfaces:**
- Consumes: the existing `Config` and `load_config` from S1.
- Produces: `Config` gains `web_host: str = "127.0.0.1"`, `web_port: int = 8765`, `web_token: str = ""`. Module constant `LOOPBACK_HOSTS`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`, before the `if __name__` block:

```python
class WebConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)

    def write(self, extra: str = "") -> Path:
        path = self.tmp / "config.toml"
        path.write_text(
            f'repo = "{self.repo}"\n'
            f'tasks_file = "{self.tmp}/tasks.md"\n' + extra
        )
        return path

    def test_web_defaults_are_loopback(self):
        cfg = load_config(self.write(), home=self.tmp / "home")
        self.assertEqual(cfg.web_host, "127.0.0.1")
        self.assertEqual(cfg.web_port, 8765)
        self.assertEqual(cfg.web_token, "")

    def test_web_values_are_read(self):
        cfg = load_config(
            self.write('web_host = "0.0.0.0"\nweb_port = 9000\nweb_token = "s3cret"\n'),
            home=self.tmp / "home",
        )
        self.assertEqual(cfg.web_host, "0.0.0.0")
        self.assertEqual(cfg.web_port, 9000)
        self.assertEqual(cfg.web_token, "s3cret")

    def test_non_loopback_without_a_token_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            load_config(self.write('web_host = "0.0.0.0"\n'), home=self.tmp / "home")
        self.assertIn("web_token", str(caught.exception))

    def test_non_loopback_with_a_blank_token_is_refused(self):
        with self.assertRaises(ValueError):
            load_config(
                self.write('web_host = "192.168.1.5"\nweb_token = "   "\n'),
                home=self.tmp / "home",
            )

    def test_loopback_without_a_token_is_fine(self):
        for host in ("127.0.0.1", "localhost", "::1"):
            cfg = load_config(
                self.write(f'web_host = "{host}"\n'), home=self.tmp / "home"
            )
            self.assertEqual(cfg.web_host, host)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_config -v`
Expected: FAIL with `AttributeError: 'Config' object has no attribute 'web_host'`

- [ ] **Step 3: Write the implementation**

In `claudeloop/config.py`, add the constant after `REQUIRED_KEYS`:

```python
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
```

add three fields to `Config`, after `session_timeout_s` and before `home`:

```python
    web_host: str = "127.0.0.1"
    web_port: int = 8765
    web_token: str = ""
```

and in `load_config`, after the repo check and before the `return`:

```python
    web_host = str(data.get("web_host", "127.0.0.1"))
    web_token = str(data.get("web_token", "")).strip()
    if web_host not in LOOPBACK_HOSTS and not web_token:
        raise ValueError(
            f"{path}: web_host {web_host!r} is not loopback, so web_token must be"
            " set to a non-empty value. The dashboard watches an agent holding"
            " real credentials; exposing it beyond this machine has to be a"
            " deliberate act."
        )
```

then add to the `Config(...)` call, after `session_timeout_s=...`:

```python
        web_host=web_host,
        web_port=int(data.get("web_port", 8765)),
        web_token=web_token,
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_config -v`
Expected: PASS, 10 tests.

- [ ] **Step 5: Commit**

```bash
git add claudeloop/config.py tests/test_config.py
git commit -m "feat: web host/port/token config with a non-loopback guard"
```

---

### Task 3: Event rendering

**Files:**
- Create: `claudeloop/render.py`
- Create: `tests/test_render.py`

**Interfaces:**
- Consumes: nothing. Pure functions only.
- Produces: `render_event(event: dict) -> list[dict]`. Constants `SUMMARY_KEYS`, `PREVIEW_CHARS`.

**Entry shapes produced:**

| Input | Output entry |
|---|---|
| `assistant` block `{"type": "text", ...}` | `{"kind": "text", "text": str}` |
| `assistant` block `{"type": "tool_use", ...}` | `{"kind": "tool", "id": str, "name": str, "summary": str}` |
| `user` block `{"type": "tool_result", ...}` | `{"kind": "result", "id": str, "preview": str, "is_error": bool}` |
| `result` event | `{"kind": "done", "cost": float, "duration_ms": int, "subtype": str}` |
| `rate_limit_event`, `system`, anything else | *(no entries)* |

- [ ] **Step 1: Write the failing tests**

Create `tests/test_render.py`:

```python
import unittest

from claudeloop.render import PREVIEW_CHARS, render_event


def assistant(*blocks):
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def user(*blocks):
    return {"type": "user", "message": {"role": "user", "content": list(blocks)}}


class RenderTextTest(unittest.TestCase):
    def test_plain_text(self):
        self.assertEqual(
            render_event(assistant({"type": "text", "text": "Working on it."})),
            [{"kind": "text", "text": "Working on it."}],
        )

    def test_blank_text_produces_nothing(self):
        self.assertEqual(render_event(assistant({"type": "text", "text": "  \n "})), [])

    def test_text_is_stripped(self):
        entries = render_event(assistant({"type": "text", "text": "  hi  "}))
        self.assertEqual(entries[0]["text"], "hi")


class RenderToolTest(unittest.TestCase):
    def test_tool_use_summarises_its_first_recognisable_argument(self):
        entries = render_event(
            assistant(
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "Edit",
                    "input": {"file_path": "/repo/src/foo.py", "old_string": "a"},
                }
            )
        )
        self.assertEqual(
            entries, [{"kind": "tool", "id": "toolu_1", "name": "Edit", "summary": "/repo/src/foo.py"}]
        )

    def test_bash_summarises_its_command(self):
        entries = render_event(
            assistant({"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": "npm test"}})
        )
        self.assertEqual(entries[0]["summary"], "npm test")

    def test_a_newline_in_the_summary_becomes_a_space(self):
        entries = render_event(
            assistant({"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": "a\nb"}})
        )
        self.assertEqual(entries[0]["summary"], "a b")

    def test_an_unrecognised_input_summarises_to_empty(self):
        entries = render_event(
            assistant({"type": "tool_use", "id": "t", "name": "Mystery", "input": {"zzz": 1}})
        )
        self.assertEqual(entries[0]["summary"], "")

    def test_a_missing_input_does_not_raise(self):
        entries = render_event(assistant({"type": "tool_use", "id": "t", "name": "X"}))
        self.assertEqual(entries[0], {"kind": "tool", "id": "t", "name": "X", "summary": ""})

    def test_prose_and_tool_calls_in_one_message_all_survive(self):
        entries = render_event(
            assistant(
                {"type": "text", "text": "Editing two files."},
                {"type": "tool_use", "id": "a", "name": "Edit", "input": {"file_path": "one.py"}},
                {"type": "tool_use", "id": "b", "name": "Edit", "input": {"file_path": "two.py"}},
            )
        )
        self.assertEqual([e["kind"] for e in entries], ["text", "tool", "tool"])
        self.assertEqual([e.get("summary") for e in entries[1:]], ["one.py", "two.py"])


class RenderResultTest(unittest.TestCase):
    def test_a_string_tool_result(self):
        entries = render_event(
            user({"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"})
        )
        self.assertEqual(
            entries, [{"kind": "result", "id": "toolu_1", "preview": "ok", "is_error": False}]
        )

    def test_a_block_list_tool_result_is_flattened(self):
        entries = render_event(
            user(
                {
                    "type": "tool_result",
                    "tool_use_id": "t",
                    "content": [{"type": "text", "text": "line one"}, {"type": "text", "text": "line two"}],
                }
            )
        )
        self.assertEqual(entries[0]["preview"], "line one\nline two")

    def test_a_long_result_is_clipped(self):
        entries = render_event(
            user({"type": "tool_result", "tool_use_id": "t", "content": "x" * 5000})
        )
        self.assertEqual(len(entries[0]["preview"]), PREVIEW_CHARS)
        self.assertTrue(entries[0]["preview"].endswith("…"))

    def test_an_error_result_is_flagged(self):
        entries = render_event(
            user({"type": "tool_result", "tool_use_id": "t", "content": "boom", "is_error": True})
        )
        self.assertTrue(entries[0]["is_error"])


class RenderOtherTest(unittest.TestCase):
    def test_the_result_event(self):
        entries = render_event(
            {
                "type": "result",
                "subtype": "success",
                "total_cost_usd": 0.0248249,
                "duration_ms": 5587,
            }
        )
        self.assertEqual(entries[0]["kind"], "done")
        self.assertAlmostEqual(entries[0]["cost"], 0.0248249)
        self.assertEqual(entries[0]["duration_ms"], 5587)
        self.assertEqual(entries[0]["subtype"], "success")

    def test_a_result_event_missing_its_numbers(self):
        entries = render_event({"type": "result"})
        self.assertEqual(entries[0]["cost"], 0.0)
        self.assertEqual(entries[0]["duration_ms"], 0)

    def test_rate_limit_events_are_not_transcript_entries(self):
        self.assertEqual(
            render_event(
                {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "resetsAt": 1}}
            ),
            [],
        )

    def test_system_events_are_ignored(self):
        self.assertEqual(render_event({"type": "system", "subtype": "init"}), [])

    def test_a_message_with_no_content_list_is_ignored(self):
        self.assertEqual(render_event({"type": "assistant", "message": {}}), [])

    def test_a_malformed_event_does_not_raise(self):
        self.assertEqual(render_event({}), [])
        self.assertEqual(render_event({"type": "assistant", "message": {"content": "nope"}}), [])
        self.assertEqual(render_event({"type": "user", "message": {"content": [None, 3]}}), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_render -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'claudeloop.render'`

- [ ] **Step 3: Write the implementation**

Create `claudeloop/render.py`:

```python
"""Turn raw stream-json events into the compact entries the dashboard shows.

Pure, so it is testable against captured event shapes exactly the way
loop.decide() is. Raw output is unreadable on a phone: a single file read can
be tens of thousands of characters and would bury the prose entirely.
"""

from __future__ import annotations

SUMMARY_KEYS = ("file_path", "command", "pattern", "path", "url", "query", "prompt")
"""Tool inputs, in the order worth showing. The first one present becomes the
tool call's one-line summary."""

PREVIEW_CHARS = 400
SUMMARY_CHARS = 120


def _clip(text: str, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tool_summary(tool_input) -> str:
    if not isinstance(tool_input, dict):
        return ""
    for key in SUMMARY_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return _clip(" ".join(value.split()), SUMMARY_CHARS)
    return ""


def _flatten(content) -> str:
    """tool_result content is either a string or a list of typed blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _assistant_block(block: dict) -> dict | None:
    kind = block.get("type")
    if kind == "text":
        text = str(block.get("text") or "").strip()
        return {"kind": "text", "text": text} if text else None
    if kind == "tool_use":
        return {
            "kind": "tool",
            "id": str(block.get("id") or ""),
            "name": str(block.get("name") or "tool"),
            "summary": _tool_summary(block.get("input")),
        }
    return None


def _user_block(block: dict) -> dict | None:
    if block.get("type") != "tool_result":
        return None
    return {
        "kind": "result",
        "id": str(block.get("tool_use_id") or ""),
        "preview": _clip(_flatten(block.get("content")), PREVIEW_CHARS),
        "is_error": bool(block.get("is_error")),
    }


def _blocks(event: dict, render_block) -> list[dict]:
    content = (event.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    entries = []
    for block in content:
        if isinstance(block, dict):
            entry = render_block(block)
            if entry:
                entries.append(entry)
    return entries


def render_event(event: dict) -> list[dict]:
    """Zero or more display entries for one raw event.

    A list rather than a single entry because one assistant message routinely
    carries prose and several tool calls in the same content array.
    """
    kind = event.get("type")
    if kind == "assistant":
        return _blocks(event, _assistant_block)
    if kind == "user":
        return _blocks(event, _user_block)
    if kind == "result":
        return [
            {
                "kind": "done",
                "cost": float(event.get("total_cost_usd") or 0.0),
                "duration_ms": int(event.get("duration_ms") or 0),
                "subtype": str(event.get("subtype") or ""),
            }
        ]
    return []
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_render -v`
Expected: PASS, 19 tests.

- [ ] **Step 5: Commit**

```bash
git add claudeloop/render.py tests/test_render.py
git commit -m "feat: render stream-json events into display entries"
```

---

### Task 4: HTTP server and JSON routes

**Files:**
- Create: `claudeloop/web.py`
- Create: `tests/test_web.py`

**Interfaces:**
- Consumes: `Config` (Task 2), `status.current` (Task 1), `render_event` (Task 3), `FileSource` from `claudeloop.source`.
- Produces: `serve(cfg) -> ThreadingHTTPServer` (starts the daemon thread and returns the server, so callers can read `server.server_port` and call `shutdown()`), `api_state(cfg) -> dict`, `api_task(cfg, task_id) -> dict | None`, `read_log(path, limit) -> list[dict]`, `Handler`, and constants `STATIC`, `TASK_ID_RE`, `STALE_AFTER_S`, `RECENT_TASKS`, `TASK_LOG_ENTRIES`.

**Design notes for the implementer:**

1. **The handler reaches config through `self.server.cfg`.** `BaseHTTPRequestHandler.__init__` takes `(request, client_address, server)` and cannot be given extra arguments, so a `ThreadingHTTPServer` subclass carrying `cfg` is the standard way in.
2. **The sqlite connection is opened read-only per request and closed in a `finally`.** `with sqlite3.connect(...)` is a *transaction* context manager and does not close the connection — a common and silent leak. `mode=ro` fails outright if the file does not exist, so check `path.exists()` first: on a fresh install the loop has not created it yet.
3. **`TASK_ID_RE` is a security control, not tidiness.** `task_id` is interpolated into a filesystem path.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_web.py`:

```python
import json
import sqlite3
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from claudeloop import status, web
from claudeloop.config import Config
from claudeloop.state import State


class WebTestBase(unittest.TestCase):
    token = ""

    def setUp(self):
        status.reset()
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "repo" / ".git").mkdir(parents=True)
        self.tasks = self.tmp / "tasks.md"
        self.tasks.write_text("- [ ] first thing\n- [x] old thing\n")
        self.cfg = Config(
            repo=self.tmp / "repo",
            tasks_file=self.tasks,
            home=self.tmp / "home",
            web_host="127.0.0.1",
            web_port=0,
            web_token=self.token,
        )
        self.server = web.serve(self.cfg)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def get(self, path: str, token: str | None = None):
        url = self.base + path
        if token is not None:
            url += ("&" if "?" in path else "?") + f"token={token}"
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read()

    def get_json(self, path: str, token: str | None = None):
        _, body = self.get(path, token)
        return json.loads(body)


class RoutesTest(WebTestBase):
    def test_index_is_served(self):
        code, body = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn(b"<", body)

    def test_logo_is_served(self):
        code, body = self.get("/logo.png")
        self.assertEqual(code, 200)
        self.assertTrue(body.startswith(b"\x89PNG"))

    def test_unknown_route_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/nope")
        self.assertEqual(caught.exception.code, 404)


class StateRouteTest(WebTestBase):
    def test_reports_the_current_snapshot(self):
        status.set_status(state="running", task_id="abc", task_text="do it", attempt=2)
        payload = self.get_json("/api/state")
        self.assertEqual(payload["status"]["state"], "running")
        self.assertEqual(payload["status"]["task_id"], "abc")
        self.assertEqual(payload["status"]["attempt"], 2)
        self.assertFalse(payload["status"]["stale"])

    def test_a_cold_snapshot_is_stale(self):
        payload = self.get_json("/api/state")
        self.assertTrue(payload["status"]["stale"])

    def test_pending_comes_from_the_task_file(self):
        payload = self.get_json("/api/state")
        self.assertEqual([t["text"] for t in payload["pending"]], ["first thing"])

    def test_completed_comes_from_the_database(self):
        state = State(self.cfg.home / "state.db")
        state.start_task("abc", "file", "- [ ] done thing", "done thing")
        state.finish_task("abc", "done", "went fine", 1.5)
        payload = self.get_json("/api/state")
        self.assertEqual(len(payload["completed"]), 1)
        self.assertEqual(payload["completed"][0]["status"], "done")
        self.assertAlmostEqual(payload["completed"][0]["cost_usd"], 1.5)

    def test_a_running_task_is_not_in_the_completed_list(self):
        state = State(self.cfg.home / "state.db")
        state.start_task("abc", "file", "- [ ] x", "x")
        self.assertEqual(self.get_json("/api/state")["completed"], [])

    def test_a_missing_database_is_not_an_error(self):
        self.assertEqual(self.get_json("/api/state")["completed"], [])


class TaskRouteTest(WebTestBase):
    def seed(self):
        state = State(self.cfg.home / "state.db")
        state.start_task("0123456789abcdef", "file", "- [ ] x", "x")
        state.start_run("0123456789abcdef", "uuid-1", 0)
        state.finish_task("0123456789abcdef", "done", "fine", 0.25)
        run_dir = self.cfg.home / "runs" / "0123456789abcdef"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "events.jsonl").write_text(
            '{"type":"assistant","message":{"content":[{"type":"text","text":"hello"}]}}\n'
            "not json at all\n"
            '{"type":"result","subtype":"success","total_cost_usd":0.25}\n'
        )

    def test_returns_the_row_its_runs_and_its_log(self):
        self.seed()
        payload = self.get_json("/api/tasks/0123456789abcdef")
        self.assertEqual(payload["task"]["status"], "done")
        self.assertEqual(len(payload["runs"]), 1)
        self.assertEqual([e["kind"] for e in payload["log"]], ["text", "done"])

    def test_unknown_task_is_404(self):
        self.seed()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/api/tasks/ffffffffffffffff")
        self.assertEqual(caught.exception.code, 404)

    def test_a_task_id_that_is_not_a_hash_is_refused(self):
        for bad in ("..", "../../etc/passwd", "abc", "0123456789ABCDEF"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.get(f"/api/tasks/{bad}")
            self.assertEqual(caught.exception.code, 404)


class TokenTest(WebTestBase):
    token = "s3cret"

    def test_the_right_token_is_accepted(self):
        self.assertEqual(self.get("/api/state", token="s3cret")[0], 200)

    def test_a_missing_token_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/api/state")
        self.assertEqual(caught.exception.code, 403)

    def test_a_wrong_token_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/api/state", token="wrong")
        self.assertEqual(caught.exception.code, 403)


class ReadLogTest(unittest.TestCase):
    def test_a_missing_file_is_empty(self):
        self.assertEqual(web.read_log(Path("/nonexistent/events.jsonl"), 10), [])

    def test_only_the_tail_is_kept(self):
        path = Path(tempfile.mkdtemp()) / "events.jsonl"
        path.write_text(
            "".join(
                '{"type":"assistant","message":{"content":[{"type":"text","text":"%d"}]}}\n' % i
                for i in range(10)
            )
        )
        entries = web.read_log(path, 3)
        self.assertEqual([e["text"] for e in entries], ["7", "8", "9"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_web -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'claudeloop.web'`

- [ ] **Step 3: Write the implementation**

Create `claudeloop/web.py`:

```python
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
```

- [ ] **Step 4: Add a placeholder page so the index route has something to serve**

Create `claudeloop/static/index.html` with a single line; Task 7 replaces it entirely:

```html
<!doctype html><title>ClaudeLoop</title><p>Dashboard placeholder.</p>
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m unittest tests.test_web -v`
Expected: PASS, 17 tests.

- [ ] **Step 6: Commit**

```bash
git add claudeloop/web.py claudeloop/static/index.html tests/test_web.py
git commit -m "feat: dashboard http server, state and task routes"
```

---

### Task 5: Live output over SSE

**Files:**
- Modify: `claudeloop/web.py` (add the route and the pump; leave Task 4's code alone)
- Modify: `tests/test_web.py` (append a test class)

**Interfaces:**
- Consumes: everything from Task 4.
- Produces: the `GET /api/events` route, `Handler._stream_events`, `Handler._pump`, `Handler._drain`, `Handler._sse`. Constants `SSE_POLL_S = 0.5`, `REPLAY_ENTRIES = 200`, `PING_S = 15`.

**Design notes for the implementer:**

1. **One pump loop handles all three cases** — idle, a run in progress, and a switch between runs. Do not special-case idle with an early return: the client's `EventSource` would reconnect immediately and hammer the server.
2. **Only whole lines are consumed.** The loop appends to `events.jsonl` while this reads it, so a read can land mid-line. Advance the offset only to the last `\n` seen and leave the remainder for the next pass.
3. **A periodic ping is what detects a departed viewer.** Writing to a closed socket raises `BrokenPipeError`, which ends the pump; without traffic an idle stream would never notice.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_web.py`, before the `if __name__` block:

```python
class SseTest(WebTestBase):
    def read_entries(self, count: int, timeout: float = 10.0) -> list[dict]:
        """Read SSE `data:` payloads until `count` of them arrive."""
        entries: list[dict] = []
        deadline = time.time() + timeout
        response = urllib.request.urlopen(self.base + "/api/events", timeout=timeout)
        self.addCleanup(response.close)
        while len(entries) < count and time.time() < deadline:
            line = response.readline()
            if not line:
                break
            if line.startswith(b"data: "):
                entries.append(json.loads(line[len(b"data: ") :]))
        return entries

    def start_run(self, task_id: str = "0123456789abcdef") -> Path:
        run_dir = self.cfg.home / "runs" / task_id
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / "events.jsonl"
        path.write_text(
            '{"type":"assistant","message":{"content":[{"type":"text","text":"first"}]}}\n'
        )
        status.set_status(state="running", task_id=task_id, run_dir=run_dir)
        return path

    def test_an_idle_loop_still_gets_a_keepalive(self):
        entries = self.read_entries(1)
        self.assertEqual(entries[0]["kind"], "ping")

    def test_the_existing_log_is_replayed_on_connect(self):
        self.start_run()
        entries = self.read_entries(2)
        self.assertEqual(entries[0]["kind"], "run")
        self.assertEqual(entries[1], {"kind": "text", "text": "first"})

    def test_a_line_appended_after_connect_arrives(self):
        path = self.start_run()
        entries: list[dict] = []
        response = urllib.request.urlopen(self.base + "/api/events", timeout=10)
        self.addCleanup(response.close)
        deadline = time.time() + 10
        appended = False
        while time.time() < deadline:
            if not appended and len(entries) >= 2:
                with open(path, "a") as handle:
                    handle.write(
                        '{"type":"assistant","message":'
                        '{"content":[{"type":"text","text":"second"}]}}\n'
                    )
                appended = True
            line = response.readline()
            if line.startswith(b"data: "):
                entry = json.loads(line[len(b"data: ") :])
                entries.append(entry)
                if entry.get("text") == "second":
                    return
        self.fail(f"appended line never arrived; got {entries}")

    def test_a_partial_trailing_line_is_not_consumed_early(self):
        path = self.start_run()
        response = urllib.request.urlopen(self.base + "/api/events", timeout=10)
        self.addCleanup(response.close)

        def next_entry():
            while True:
                line = response.readline()
                if not line:
                    self.fail("stream ended early")
                if line.startswith(b"data: "):
                    return json.loads(line[len(b"data: ") :])

        # Drain the replay first: the run already has one line in it, and its
        # entry would otherwise look exactly like a leaked partial.
        self.assertEqual(next_entry()["kind"], "run")
        self.assertEqual(next_entry()["text"], "first")

        with open(path, "a") as handle:
            handle.write('{"type":"assistant","message":{"content":[{"type":"text",')
            handle.flush()
            time.sleep(SSE_SETTLE_S)  # give the pump a chance to read the fragment
            handle.write('"text":"whole"}]}}\n')

        deadline = time.time() + 10
        while time.time() < deadline:
            entry = next_entry()
            if entry.get("kind") == "ping":
                continue
            self.assertEqual(entry.get("text"), "whole", f"partial line leaked: {entry}")
            return
        self.fail("the completed line never arrived")
```

Add these to the imports at the top of `tests/test_web.py`:

```python
import time

SSE_SETTLE_S = 1.5  # comfortably longer than web.SSE_POLL_S
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_web.SseTest -v`
Expected: FAIL — `/api/events` currently falls through to the 404 branch, so `urlopen` raises `HTTPError: 404`.

- [ ] **Step 3: Write the implementation**

In `claudeloop/web.py`, add the constants next to the existing ones:

```python
SSE_POLL_S = 0.5
REPLAY_ENTRIES = 200
PING_S = 15
```

add the route to `do_GET`, immediately before the `elif route.startswith("/api/tasks/")` branch:

```python
        elif route == "/api/events":
            self._stream_events()
```

and add these methods to `Handler`:

```python
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
                    path = run_dir / "events.jsonl"
                    self._sse({"kind": "run", "task_id": run_dir.name})
                    for entry in read_log(path, REPLAY_ENTRIES):
                        self._sse(entry)
                    offset = path.stat().st_size if path.exists() else 0
            if run_dir is not None:
                offset = self._drain(run_dir / "events.jsonl", offset)
            now = time.time()
            if now - last_ping > PING_S:
                # Writing is how a departed viewer is noticed: the write
                # raises BrokenPipeError and the pump ends.
                self._sse({"kind": "ping"})
                last_ping = now
            time.sleep(SSE_POLL_S)

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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_web -v`
Expected: PASS, 21 tests.

- [ ] **Step 5: Commit**

```bash
git add claudeloop/web.py tests/test_web.py
git commit -m "feat: live session output over server-sent events"
```

---

### Task 6: Wire the loop to the status snapshot

**Files:**
- Modify: `claudeloop/loop.py`
- Modify: `tests/test_loop.py` (append a test class)

**Interfaces:**
- Consumes: `status.set_status` (Task 1), `web.serve` (Task 4).
- Produces: `latest_rate_limit(events: list[dict]) -> dict | None` in `loop.py`. No signature changes to `run_task` or `main_loop`.

**Design notes for the implementer:**

1. **`web.serve(cfg)` goes in `main()`, not `main_loop()`.** A dozen existing tests call `main_loop` directly and would each bind a TCP port.
2. **`set_status` carries unnamed fields over.** Moving to `idle` must therefore clear `task_id`, `task_text`, `run_dir`, `session_id`, `started_at`, and `wait_until` explicitly, or the dashboard shows a task that finished an hour ago.
3. **The idle branch must refresh the heartbeat every poll**, or `STALE_AFTER_S` marks a perfectly healthy idle loop as dead.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_loop.py`, before the `if __name__` block:

```python
class StatusWiringTest(unittest.TestCase):
    """Same fixture as MainLoopTest, deliberately duplicated rather than
    inherited: subclassing a TestCase re-runs every one of the parent's tests,
    which here means re-running seven subprocess-driven cases for nothing."""

    def setUp(self):
        from claudeloop import status

        status.reset()
        self.status = status
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "repo" / ".git").mkdir(parents=True)
        self.tasks = self.tmp / "tasks.md"
        self.tasks.write_text("- [ ] first thing\n- [ ] second thing\n")
        self.cfg = Config(
            repo=self.tmp / "repo",
            tasks_file=self.tasks,
            home=self.tmp / "home",
            max_resumes=3,
        )
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        shutil.copy(Path(__file__).parent / "fake_claude.sh", bin_dir / "claude")
        (bin_dir / "claude").chmod(0o755)
        self.old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{self.old_path}"

    def tearDown(self):
        os.environ["PATH"] = self.old_path
        os.environ.pop("FAKE_LIMIT_FLAG", None)

    def test_the_loop_ends_idle_with_the_task_fields_cleared(self):
        asyncio.run(loop.main_loop(self.cfg, once=True))
        self.assertEqual(self.status.current.state, "idle")
        self.assertIsNone(self.status.current.task_id)
        self.assertIsNone(self.status.current.run_dir)
        self.assertIsNone(self.status.current.session_id)

    def test_the_heartbeat_is_fresh_after_a_run(self):
        asyncio.run(loop.main_loop(self.cfg, once=True))
        self.assertAlmostEqual(self.status.current.heartbeat, time.time(), delta=5)

    def test_the_quota_reading_is_captured_from_the_stream(self):
        flag = self.tmp / "limit.flag"
        flag.write_text("")
        os.environ["FAKE_LIMIT_FLAG"] = str(flag)
        self.tasks.write_text("- [ ] first thing\n")
        asyncio.run(loop.main_loop(self.cfg, once=True))
        self.assertIsNotNone(self.status.current.rate_limit)
        self.assertEqual(self.status.current.rate_limit["rateLimitType"], "five_hour")

    def test_a_crash_is_recorded_as_the_error_state(self):
        os.environ["PATH"] = "/nonexistent"
        asyncio.run(loop.main_loop(self.cfg, once=True))
        self.assertEqual(self.status.current.state, "error")
        self.assertIn("claude", self.status.current.last_error or "")


class LatestRateLimitTest(unittest.TestCase):
    def test_returns_the_last_one(self):
        events = [
            {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}},
            {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}},
        ]
        self.assertEqual(loop.latest_rate_limit(events)["status"], "rejected")

    def test_none_when_absent_or_malformed(self):
        self.assertIsNone(loop.latest_rate_limit([]))
        self.assertIsNone(loop.latest_rate_limit([{"type": "result"}]))
        self.assertIsNone(
            loop.latest_rate_limit([{"type": "rate_limit_event", "rate_limit_info": "nope"}])
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_loop -v`
Expected: FAIL with `AttributeError: module 'claudeloop.loop' has no attribute 'latest_rate_limit'`

- [ ] **Step 3: Write the implementation**

In `claudeloop/loop.py`, add to the existing import block:

```python
from . import status as status_module
from . import web
```

add this function next to `blocking_reset`:

```python
def latest_rate_limit(events: list[dict]) -> dict | None:
    """The most recent quota reading in a run, for the dashboard's gauge."""
    for event in reversed(events):
        if event.get("type") == "rate_limit_event":
            info = event.get("rate_limit_info")
            return info if isinstance(info, dict) else None
    return None
```

In `run_task`, immediately after `log.info("task %s starting: %s", ...)`:

```python
    status_module.set_status(
        state="running",
        task_id=task.id,
        task_text=task.text,
        run_dir=run_dir,
        session_id=session_id,
        attempt=0,
        started_at=time.time(),
        wait_until=None,
        last_error=None,
    )
```

immediately after `run_id = state.start_run(task.id, session_id, attempt)`:

```python
        status_module.set_status(state="running", attempt=attempt, wait_until=None)
```

immediately after `cost += total_cost(events)`:

```python
        quota = latest_rate_limit(events)
        if quota is not None:
            status_module.set_status(rate_limit=quota)
```

and inside the `if action.wait_until:` branch, wrapping the sleep:

```python
            status_module.set_status(state="waiting", wait_until=action.wait_until)
            await asyncio.sleep(delay)
            status_module.set_status(state="running", wait_until=None)
```

In `main_loop`, replace the idle branch so it refreshes the heartbeat:

```python
        if not pending:
            if once:
                status_module.set_status(**IDLE_FIELDS)
                return
            status_module.set_status(**IDLE_FIELDS)
            await asyncio.sleep(POLL_S)
            continue
```

with this module-level constant next to `POLL_S`:

```python
IDLE_FIELDS = {
    "state": "idle",
    "task_id": None,
    "task_text": None,
    "run_dir": None,
    "session_id": None,
    "started_at": None,
    "wait_until": None,
}
"""set_status carries unnamed fields over, so going idle has to clear the
task fields explicitly or the dashboard shows a task that finished an hour
ago. Reasserted on every idle poll, which is also what keeps the heartbeat
fresh while nothing is running."""
```

and in `main_loop`'s `except Exception` handler, immediately after the
`log.exception(...)` call:

```python
            status_module.set_status(state="error", last_error=str(error))
```

Finally, replace `main()` in full — the dashboard starts after the config
validates and before the loop runs:

```python
def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        cfg = load_config()
    except FileNotFoundError:
        raise SystemExit(
            f"no config file at {DEFAULT_CONFIG} -- see README.md to set one up"
        )
    # After the config validates, so a non-loopback bind with no token fails
    # before anything is listening.
    web.serve(cfg)
    asyncio.run(main_loop(cfg))
```

- [ ] **Step 4: Run the whole suite**

Run: `python -m unittest discover -s tests -t . -v`
Expected: PASS, 131 tests.

- [ ] **Step 5: Commit**

```bash
git add claudeloop/loop.py tests/test_loop.py
git commit -m "feat: publish loop transitions to the dashboard status"
```

---

### Task 7: The dashboard page

**Files:**
- Modify: `claudeloop/static/index.html` (replace the Task 4 placeholder entirely)
- Modify: `tests/test_web.py` (append two assertions to `RoutesTest`)

**Interfaces:**
- Consumes: `GET /api/state`, `GET /api/events`, `GET /api/tasks/<id>`, `GET /logo.png`.
- Produces: nothing importable.

**REQUIRED SUB-SKILLS for this task:** load `frontend-design` before writing any markup, and `dataviz` before building the quota gauge or the cost/duration tiles. This is the one task in the plan with real visual design in it; do not treat it as markup transcription.

**Hard constraints:**

- One file. Inline `<style>`, inline `<script type="module">`. No CDN, no external font, no build step, no dependency.
- Mobile-first. Legible and usable at 380px wide.
- Both themes via `prefers-color-scheme`, plus an explicit toggle that persists in `localStorage`.
- Every request must carry the token from `location.search` when one is present, `EventSource` included — it cannot set headers, which is why the token is a query parameter.
- The status is always a coloured dot **and** a text label. Colour never carries meaning alone.

**Palette, from the project logo:**

| Token | Dark | Light |
|---|---|---|
| Background | `#1B222B` | `#FDF4EC` |
| Surface | `#242D37` | `#FFFFFF` |
| Text | `#F2E8E0` | `#242D37` |
| Muted text | `#9AA6B2` | `#6B7885` |
| Accent — fills, indicator | `#FD7C33` | `#FD7C33` |
| Accent — text, links | `#FD7C33` | `#C4551A` |

`#FD7C33` is about 2.6:1 on white and fails as text, which is why the light
theme has a separate, darker accent for type. On `#242D37` the brand orange is
about 5.5:1 and is used directly.

**Status indicator states:**

| State | Colour | Label |
|---|---|---|
| `running` | green, pulsing | "Running" |
| `waiting` | amber | "Waiting for quota" + a live countdown to `wait_until` |
| `idle` | grey | "Idle" |
| `error` | red | "Error" + `last_error` |

When `status.stale` is true, override the indicator with a red "Loop not
responding" regardless of `state` — a stale heartbeat means the loop died
while the web thread lived, and a cheerful green light over a dead
orchestrator is the one thing this dashboard must never show.

**Page structure, top to bottom:**

1. **Header** — the logo inside a rounded warm chip (the PNG has its
   background baked in with no alpha, so the chip makes that read as a
   deliberate lockup on either theme), the wordmark, the theme toggle.
2. **Status card** — indicator dot and label, the current task's text,
   attempt number when above zero, elapsed time since `started_at`, and the
   quota gauge built from `status.rate_limit` (`resetsAt` as a countdown,
   `rateLimitType` as the label).
3. **Live output** — the streamed entries. `text` entries render as prose;
   `tool` entries as a single line of name plus summary; `result` entries
   collapsed behind a disclosure that expands to the preview, tinted when
   `is_error`; `done` entries as a small cost-and-duration footer. Auto-scroll
   to the bottom, and stop auto-scrolling once the user scrolls up.
4. **Pending** — `pending[]` in file order.
5. **Completed** — `completed[]`, each with status, summary, and cost; tapping
   one fetches `/api/tasks/<id>` and shows its runs and log.

**Behaviour:**

- Poll `/api/state` every 3s. Hold one `EventSource` on `/api/events` for the
  page's life; `EventSource` handles reconnection itself.
- A `{"kind": "run"}` entry means a new run started: clear the output pane
  before appending what follows.
- Ignore `{"kind": "ping"}` entries; they exist to keep the connection alive.
- Escape every string from the API before inserting it. Task text, summaries,
  and tool output are attacker-adjacent — the session is reading files from a
  repository. Build nodes with `textContent`, never `innerHTML`.

- [ ] **Step 1: Load the design skills**

Load `frontend-design`, then `dataviz`. Follow them for the visual and chart
decisions below rather than defaulting to generic styling.

- [ ] **Step 2: Write the page**

Replace `claudeloop/static/index.html` in full, following the structure,
palette, constraints, and behaviour above.

- [ ] **Step 3: Extend the route test**

In `tests/test_web.py`, replace the body of `RoutesTest.test_index_is_served`
with:

```python
    def test_index_is_served(self):
        code, body = self.get("/")
        self.assertEqual(code, 200)
        page = body.decode()
        self.assertIn("<!doctype html", page.lower())
        # No build step and no CDN: everything the page needs is in the file.
        self.assertNotIn("<script src=", page)
        self.assertNotIn("cdn.", page)
        self.assertIn("/api/events", page)
        self.assertIn("#fd7c33", page.lower())  # the brand accent is actually used
```

- [ ] **Step 4: Run the whole suite**

Run: `python -m unittest discover -s tests -t . -v`
Expected: PASS, 131 tests.

- [ ] **Step 5: Verify it in a real browser**

Start the loop against the scratch setup from Step 6 below, open the page, and
check with your own eyes:

- the page is usable at 380px wide (device toolbar, iPhone SE)
- both themes render correctly, and the toggle persists across a reload
- the indicator turns green and prose streams in without a reload
- a tool call is one line; its result expands on click
- no console errors

- [ ] **Step 6: Commit**

```bash
git add claudeloop/static/index.html tests/test_web.py
git commit -m "feat: the dashboard page"
```

---

## Manual smoke test

After Task 7. The fake CLI cannot prove the page works against a real session.

- [ ] Reuse the scratch repository pattern from S1's smoke test: a throwaway
      git repo with a one-line `CLAUDE.md`, a config with `model = "haiku"`,
      and two or three trivial tasks in the task file.
- [ ] Run `python -m claudeloop` and open `http://127.0.0.1:8765` on the
      desktop and on a phone.
- [ ] Confirm: the indicator goes grey → green; the current task text is
      right; prose and tool lines stream in live; a finished task moves to the
      completed list with its real cost; opening it shows its runs and log.
- [ ] Set `web_host = "0.0.0.0"` with no `web_token` and confirm startup
      fails with a message naming `web_token`. Then set a token and confirm
      the dashboard is reachable from the phone with `?token=...` and refused
      without it.

## Spec coverage

| Spec requirement | Task |
|---|---|
| ThreadingHTTPServer on a daemon thread | 4 |
| Own read-only sqlite connection; event logs read from disk | 4 |
| Frozen `Status` snapshot, atomic replace, heartbeat | 1, 6 |
| `web_host` / `web_port` / `web_token` | 2 |
| Non-loopback with an empty token is a startup error | 2 |
| Token via query parameter, `secrets.compare_digest` | 4, 7 |
| `render_event` pure and fixture-tested | 3 |
| Assistant text plus tool names; results collapsed | 3, 7 |
| SSE with replay, byte-offset tailing, reconnect | 5 |
| `/`, `/logo.png`, `/api/state`, `/api/events`, `/api/tasks/<id>` | 4, 5 |
| Pending from the task file, completed from the database | 4 |
| Log tail capped at 2000 entries | 4 |
| Four indicator states, dot plus label | 7 |
| Stale heartbeat overrides the indicator | 7 |
| Both themes, `prefers-color-scheme` plus a toggle | 7 |
| Logo in a warm chip; doubles as favicon | 4, 7 |
| Mobile-first at 380px | 7 |
| No build step, no dependency | Global constraints; 7 |
| S2a writes nothing | Global constraints; every task |
| Acceptance criteria 1–7 | 7 + the manual smoke test |
