# ClaudeLoop Core Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An unattended orchestrator that runs one headless Claude Code session per task from a markdown checklist, survives subscription rate limits by sleeping until `resetsAt` and resuming the same session, and records every outcome.

**Architecture:** A single Python process. `config.py` loads a TOML file, `source.py` reads and checks off a markdown task list, `session.py` spawns and streams one `claude -p` invocation, `loop.py` holds a pure decision function plus the orchestration around it, `state.py` records outcomes in SQLite. The orchestrator contains no workflow logic — the target repository's own `CLAUDE.md` defines what "done" means, and the per-task instruction just points at it.

**Tech Stack:** Python 3.11+ standard library only — `asyncio`, `sqlite3`, `tomllib`, `hashlib`, `unittest`. The Claude Code CLI is invoked as a subprocess.

## Global Constraints

- **Python 3.11 or newer.** `tomllib` entered the standard library in 3.11.
- **No third-party packages, ever.** Not for the orchestrator, not for the tests. `pip install` must never be required to run this project.
- **No `pyproject.toml`, no packaging.** The program runs as `python -m claudeloop` from the repository root; tests run as `python -m unittest discover -s tests -t .` from the repository root.
- **Strictly serial.** One task at a time, one Claude session at a time. No concurrency beyond `asyncio` draining two pipes of one subprocess.
- **Never write to the target repository.** The result file, event log, and database all live under `~/.claudeloop/`. The only file ClaudeLoop writes outside that directory is the user's own task list.
- **Every stdout line is written to `events.jsonl` verbatim before it is parsed.** A parser bug must never lose the record.
- Reference spec: `docs/superpowers/specs/2026-07-31-claudeloop-core-loop-design.md`.

## Deviation from the spec

The spec's `session.py` command line includes `--include-partial-messages`. This plan **omits that flag**. Complete `assistant` events already carry each message as it is produced, which is enough granularity for S2's live output panel; partial messages add token-by-token deltas that would multiply `events.jsonl` size for no S1 benefit and no visible S2 benefit. Adding the flag later is a one-line change in `build_command`. Everything else follows the spec as written.

## File Structure

| File | Responsibility |
|---|---|
| `claudeloop/__init__.py` | Empty package marker. |
| `claudeloop/config.py` | Load and validate `~/.claudeloop/config.toml` into a frozen `Config`. |
| `claudeloop/state.py` | SQLite schema and the handful of writes against it. |
| `claudeloop/source.py` | `Task`, the `TaskSource` protocol, and `FileSource` over a markdown checklist. |
| `claudeloop/session.py` | Build the `claude` command line; spawn it; tee stdout to `events.jsonl`; return parsed events. |
| `claudeloop/loop.py` | Pure `decide()` state machine, per-task orchestration, the outer polling loop, `main()`. |
| `claudeloop/__main__.py` | Three lines: call `loop.main()`. |
| `tests/test_config.py` | Config loading and its validation failures. |
| `tests/test_state.py` | Task and run rows survive a round trip. |
| `tests/test_source.py` | Pending parsing; checking off the right line under concurrent edits. |
| `tests/test_loop.py` | Every row of the `decide()` table; `read_result` against malformed files. |
| `tests/test_session.py` | Command construction, and one end-to-end run against a fake `claude`. |
| `tests/fake_claude.sh` | Stand-in for the real CLI: emits a canned event stream, optionally a rate-limit event. |

---

### Task 1: Package skeleton and configuration

**Files:**
- Create: `claudeloop/__init__.py`
- Create: `tests/__init__.py` (required: the tasks below run single modules as `python -m unittest tests.test_x`, which needs `tests` to be a package)
- Create: `claudeloop/config.py`
- Create: `tests/test_config.py`
- Create: `.gitignore`

**Interfaces:**
- Consumes: nothing.
- Produces: `Config` — a frozen dataclass with fields `repo: Path`, `tasks_file: Path`, `model: str`, `max_resumes: int`, `home: Path`. `load_config(path: Path = DEFAULT_CONFIG, home: Path = HOME) -> Config`. Module constant `HOME = Path.home() / ".claudeloop"`.

- [ ] **Step 1: Create the package marker and gitignore**

```bash
mkdir -p claudeloop tests
touch claudeloop/__init__.py tests/__init__.py
printf '__pycache__/\n*.pyc\n' > .gitignore
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_config.py`:

```python
import tempfile
import unittest
from pathlib import Path

from claudeloop.config import Config, load_config


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)

    def write(self, body: str) -> Path:
        path = self.tmp / "config.toml"
        path.write_text(body)
        return path

    def test_reads_values_and_applies_defaults(self):
        path = self.write(
            f'repo = "{self.repo}"\n'
            f'tasks_file = "{self.tmp}/tasks.md"\n'
        )
        cfg = load_config(path, home=self.tmp / "home")
        self.assertEqual(cfg.repo, self.repo)
        self.assertEqual(cfg.tasks_file, self.tmp / "tasks.md")
        self.assertEqual(cfg.model, "opus")
        self.assertEqual(cfg.max_resumes, 20)
        self.assertEqual(cfg.home, self.tmp / "home")

    def test_overrides_defaults(self):
        path = self.write(
            f'repo = "{self.repo}"\n'
            f'tasks_file = "{self.tmp}/tasks.md"\n'
            'model = "sonnet"\n'
            'max_resumes = 3\n'
        )
        cfg = load_config(path, home=self.tmp / "home")
        self.assertEqual(cfg.model, "sonnet")
        self.assertEqual(cfg.max_resumes, 3)

    def test_rejects_missing_required_key(self):
        path = self.write(f'repo = "{self.repo}"\n')
        with self.assertRaises(ValueError) as caught:
            load_config(path, home=self.tmp / "home")
        self.assertIn("tasks_file", str(caught.exception))

    def test_rejects_repo_that_is_not_a_git_checkout(self):
        path = self.write(
            f'repo = "{self.tmp}/nope"\n'
            f'tasks_file = "{self.tmp}/tasks.md"\n'
        )
        with self.assertRaises(ValueError) as caught:
            load_config(path, home=self.tmp / "home")
        self.assertIn("git repository", str(caught.exception))

    def test_config_is_frozen(self):
        with self.assertRaises(Exception):
            Config(repo=Path("/a"), tasks_file=Path("/b")).model = "x"


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m unittest discover -s tests -t . -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'claudeloop.config'`

- [ ] **Step 4: Write the implementation**

Create `claudeloop/config.py`:

```python
"""Load and validate the ClaudeLoop configuration file."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

HOME = Path.home() / ".claudeloop"
DEFAULT_CONFIG = HOME / "config.toml"
REQUIRED_KEYS = ("repo", "tasks_file")


@dataclass(frozen=True)
class Config:
    repo: Path
    tasks_file: Path
    model: str = "opus"
    max_resumes: int = 20
    home: Path = HOME


def load_config(path: Path = DEFAULT_CONFIG, home: Path = HOME) -> Config:
    """Read `path` into a Config.

    The config file is user input, so both the required keys and the repo path
    are validated here rather than failing much later inside a subprocess.
    """
    with open(path, "rb") as handle:
        data = tomllib.load(handle)

    missing = [key for key in REQUIRED_KEYS if key not in data]
    if missing:
        raise ValueError(f"{path}: missing required key(s): {', '.join(missing)}")

    repo = Path(data["repo"]).expanduser()
    if not (repo / ".git").exists():
        raise ValueError(f"{path}: repo {repo} is not a git repository")

    return Config(
        repo=repo,
        tasks_file=Path(data["tasks_file"]).expanduser(),
        model=str(data.get("model", "opus")),
        max_resumes=int(data.get("max_resumes", 20)),
        home=home,
    )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m unittest discover -s tests -t . -v`
Expected: PASS, 5 tests.

- [ ] **Step 6: Commit**

```bash
git add claudeloop/__init__.py tests/__init__.py claudeloop/config.py tests/test_config.py .gitignore
git commit -m "feat: config loading with validation"
```

---

### Task 2: State database

**Files:**
- Create: `claudeloop/state.py`
- Create: `tests/test_state.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `State(db_path: Path)` with methods `start_task(task_id: str, source: str, source_ref: str, text: str) -> None`, `finish_task(task_id: str, status: str, summary: str, cost_usd: float) -> None`, `start_run(task_id: str, session_id: str, resume_count: int) -> int`, `finish_run(run_id: int, exit_reason: str) -> None`, `task(task_id: str) -> sqlite3.Row | None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_state.py`:

```python
import sqlite3
import tempfile
import unittest
from pathlib import Path

from claudeloop.state import State


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state = State(self.tmp / "nested" / "state.db")

    def test_creates_parent_directory(self):
        self.assertTrue((self.tmp / "nested" / "state.db").exists())

    def test_task_round_trip(self):
        self.state.start_task("abc", "file", "- [ ] do it", "do it")
        row = self.state.task("abc")
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["text"], "do it")
        self.assertIsNotNone(row["started_at"])
        self.assertIsNone(row["finished_at"])

        self.state.finish_task("abc", "done", "worked fine", 1.25)
        row = self.state.task("abc")
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["summary"], "worked fine")
        self.assertAlmostEqual(row["cost_usd"], 1.25)
        self.assertIsNotNone(row["finished_at"])

    def test_rerunning_a_task_replaces_the_previous_row(self):
        self.state.start_task("abc", "file", "- [ ] do it", "do it")
        self.state.finish_task("abc", "failed", "nope", 0.0)
        self.state.start_task("abc", "file", "- [ ] do it", "do it")
        self.assertEqual(self.state.task("abc")["status"], "running")

    def test_run_round_trip(self):
        self.state.start_task("abc", "file", "- [ ] do it", "do it")
        first = self.state.start_run("abc", "uuid-1", 0)
        second = self.state.start_run("abc", "uuid-1", 1)
        self.assertNotEqual(first, second)

        self.state.finish_run(first, "Resume")
        row = self.state.db.execute(
            "SELECT * FROM runs WHERE id=?", (first,)
        ).fetchone()
        self.assertEqual(row["exit_reason"], "Resume")
        self.assertEqual(row["resume_count"], 0)
        self.assertIsNotNone(row["ended_at"])

    def test_unknown_task_is_none(self):
        self.assertIsNone(self.state.task("missing"))

    def test_reopening_an_existing_database_keeps_rows(self):
        self.state.start_task("abc", "file", "- [ ] do it", "do it")
        reopened = State(self.tmp / "nested" / "state.db")
        self.assertEqual(reopened.task("abc")["text"], "do it")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_state -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'claudeloop.state'`

- [ ] **Step 3: Write the implementation**

Create `claudeloop/state.py`:

```python
"""SQLite record of what the loop did. Not the source of truth for what is
pending — the task source is."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    source_ref  TEXT NOT NULL,
    text        TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  REAL NOT NULL,
    started_at  REAL,
    finished_at REAL,
    summary     TEXT,
    cost_usd    REAL
);
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL REFERENCES tasks(id),
    session_id   TEXT NOT NULL,
    started_at   REAL NOT NULL,
    ended_at     REAL,
    exit_reason  TEXT,
    resume_count INTEGER NOT NULL DEFAULT 0
);
"""


class State:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None is autocommit: a crash never loses the last write.
        self.db = sqlite3.connect(db_path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        # WAL so S2's web UI can read while the loop writes.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def start_task(self, task_id: str, source: str, source_ref: str, text: str) -> None:
        now = time.time()
        self.db.execute(
            "INSERT OR REPLACE INTO tasks"
            " (id, source, source_ref, text, status, created_at, started_at)"
            " VALUES (?, ?, ?, ?, 'running', ?, ?)",
            (task_id, source, source_ref, text, now, now),
        )

    def finish_task(self, task_id: str, status: str, summary: str, cost_usd: float) -> None:
        self.db.execute(
            "UPDATE tasks SET status=?, summary=?, cost_usd=?, finished_at=? WHERE id=?",
            (status, summary, cost_usd, time.time(), task_id),
        )

    def start_run(self, task_id: str, session_id: str, resume_count: int) -> int:
        cursor = self.db.execute(
            "INSERT INTO runs (task_id, session_id, started_at, resume_count)"
            " VALUES (?, ?, ?, ?)",
            (task_id, session_id, time.time(), resume_count),
        )
        return cursor.lastrowid

    def finish_run(self, run_id: int, exit_reason: str) -> None:
        self.db.execute(
            "UPDATE runs SET ended_at=?, exit_reason=? WHERE id=?",
            (time.time(), exit_reason, run_id),
        )

    def task(self, task_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_state -v`
Expected: PASS, 6 tests.

- [ ] **Step 5: Commit**

```bash
git add claudeloop/state.py tests/test_state.py
git commit -m "feat: sqlite state for tasks and runs"
```

---

### Task 3: File task source

**Files:**
- Create: `claudeloop/source.py`
- Create: `tests/test_source.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Task` — frozen dataclass with `id: str`, `text: str`, `source: str`, `source_ref: str`. `TaskSource` — a `Protocol` with `pending(self) -> list[Task]` and `mark(self, task: Task, status: str, summary: str) -> None`. `FileSource(path: Path)` implementing it. `task_id(text: str) -> str`.

**Design note for the implementer:** `mark()` writes `- [x]` for a `done` task and `- [!]` for anything else. Both are ignored by `pending()`, so a failed or blocked task does not get picked up forever, and `- [!]` is visibly different to the human reading the file. `source_ref` is the stripped line text, not a line number, so a user editing the file while a task runs cannot cause the wrong line to be checked off.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_source.py`:

```python
import tempfile
import unittest
from pathlib import Path

from claudeloop.source import FileSource, Task, task_id


class TaskIdTest(unittest.TestCase):
    def test_is_stable_and_distinct(self):
        self.assertEqual(task_id("do it"), task_id("do it"))
        self.assertNotEqual(task_id("do it"), task_id("do it twice"))
        self.assertEqual(len(task_id("do it")), 16)


class FileSourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "tasks.md"

    def source(self, body: str) -> FileSource:
        self.path.write_text(body)
        return FileSource(self.path)

    def test_pending_returns_unchecked_lines_in_order(self):
        source = self.source(
            "# My tasks\n"
            "- [ ] first thing\n"
            "- [x] already done\n"
            "- [!] failed earlier\n"
            "- [ ] second thing\n"
            "\n"
            "some prose\n"
        )
        self.assertEqual([t.text for t in source.pending()], ["first thing", "second thing"])

    def test_pending_handles_indentation_and_empty_items(self):
        source = self.source("  - [ ] indented\n- [ ] \n- [ ]\n")
        self.assertEqual([t.text for t in source.pending()], ["indented"])

    def test_pending_on_missing_file_is_empty(self):
        self.assertEqual(FileSource(self.tmp / "absent.md").pending(), [])

    def test_mark_done_checks_the_line_off(self):
        source = self.source("- [ ] first thing\n- [ ] second thing\n")
        source.mark(source.pending()[0], "done", "went fine")
        self.assertEqual(self.path.read_text(), "- [x] first thing\n- [ ] second thing\n")

    def test_mark_failed_uses_the_attention_marker(self):
        source = self.source("- [ ] first thing\n")
        source.mark(source.pending()[0], "failed", "broke")
        self.assertEqual(self.path.read_text(), "- [!] first thing\n")
        self.assertEqual(source.pending(), [])

    def test_mark_preserves_indentation(self):
        source = self.source("  - [ ] indented\n")
        source.mark(source.pending()[0], "done", "")
        self.assertEqual(self.path.read_text(), "  - [x] indented\n")

    def test_mark_finds_the_line_after_the_file_was_edited_underneath(self):
        source = self.source("- [ ] first thing\n- [ ] second thing\n")
        task = source.pending()[0]
        # The user inserts a task above while the first one is still running.
        self.path.write_text("- [ ] urgent thing\n- [ ] first thing\n- [ ] second thing\n")
        source.mark(task, "done", "")
        self.assertEqual(
            self.path.read_text(),
            "- [ ] urgent thing\n- [x] first thing\n- [ ] second thing\n",
        )

    def test_mark_is_a_no_op_when_the_line_is_gone(self):
        source = self.source("- [ ] first thing\n")
        task = source.pending()[0]
        self.path.write_text("- [ ] something else\n")
        source.mark(task, "done", "")
        self.assertEqual(self.path.read_text(), "- [ ] something else\n")

    def test_mark_without_trailing_newline(self):
        source = self.source("- [ ] only thing")
        source.mark(source.pending()[0], "done", "")
        self.assertEqual(self.path.read_text(), "- [x] only thing")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_source -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'claudeloop.source'`

- [ ] **Step 3: Write the implementation**

Create `claudeloop/source.py`:

```python
"""Where tasks come from. S1 ships one implementation, over a markdown
checklist; S3 adds a Jira one behind the same protocol."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

UNCHECKED = "- [ ]"
DONE = "- [x]"
ATTENTION = "- [!]"


@dataclass(frozen=True)
class Task:
    id: str
    text: str
    source: str
    source_ref: str


class TaskSource(Protocol):
    def pending(self) -> list[Task]: ...
    def mark(self, task: Task, status: str, summary: str) -> None: ...


def task_id(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class FileSource:
    """A markdown checklist. `- [ ]` is pending, `- [x]` succeeded, `- [!]`
    needs a human. Only `- [ ]` is ever picked up."""

    def __init__(self, path: Path):
        self.path = path

    def pending(self) -> list[Task]:
        try:
            body = self.path.read_text()
        except FileNotFoundError:
            return []
        tasks = []
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped.startswith(UNCHECKED):
                continue
            text = stripped[len(UNCHECKED):].strip()
            if text:
                tasks.append(Task(task_id(text), text, "file", stripped))
        return tasks

    def mark(self, task: Task, status: str, summary: str) -> None:
        """Rewrite the task's line.

        Matched on exact line text rather than index, so a user editing the
        file while the task ran cannot cause the wrong line to be marked. A
        line that has since vanished is left alone; the database still holds
        the record.
        """
        marker = DONE if status == "done" else ATTENTION
        lines = self.path.read_text().splitlines(keepends=True)
        for index, line in enumerate(lines):
            if line.strip() != task.source_ref:
                continue
            body = line.rstrip("\r\n")
            indent = line[: len(line) - len(line.lstrip())]
            eol = line[len(body):]
            lines[index] = f"{indent}{marker} {task.text}{eol}"
            self.path.write_text("".join(lines))
            return
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_source -v`
Expected: PASS, 10 tests.

- [ ] **Step 5: Commit**

```bash
git add claudeloop/source.py tests/test_source.py
git commit -m "feat: markdown checklist task source"
```

---

### Task 4: The decision state machine

**Files:**
- Create: `claudeloop/loop.py`
- Create: `tests/test_loop.py`
- Create: `tests/fixtures/rate_limited.jsonl`
- Create: `tests/fixtures/completed.jsonl`

**Interfaces:**
- Consumes: nothing.
- Produces: `ReadResult` (no fields), `Resume(wait_until: float = 0.0)`, `Fail(reason: str)` — all frozen dataclasses. `blocking_reset(events: list[dict]) -> float | None`. `decide(events: list[dict], result_exists: bool, resume_count: int, max_resumes: int) -> ReadResult | Resume | Fail`. `read_result(path: Path) -> dict` returning `{"status": str, "summary": str}`. `total_cost(events: list[dict]) -> float`. Constant `RESET_PAD_S = 30`.

**Design note for the implementer:** this task creates `loop.py` containing only the pure functions. Task 6 appends the orchestration to the same file. Do not write the orchestration here.

The decision table, from the spec:

| Condition | Action |
|---|---|
| result file exists | `ReadResult()` |
| last `rate_limit_event` of the just-exited run is not `"allowed"` | `Resume(wait_until=resetsAt + 30)` |
| `resume_count < max_resumes` | `Resume()` |
| otherwise | `Fail("no_result")` |

The result file is checked first so that a session which finished its work and then tripped the limit on a trailing turn is recorded as done rather than parked until the reset.

- [ ] **Step 1: Create the fixtures**

Create `tests/fixtures/rate_limited.jsonl` — a real stream shape, truncated:

```
{"type":"system","subtype":"init","session_id":"53b506d2-bc68-4d0f-a7ed-d35a2ff50b47","model":"claude-opus-5"}
{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","resetsAt":1785516000,"rateLimitType":"five_hour","overageStatus":"rejected","overageDisabledReason":"out_of_credits","isUsingOverage":false},"session_id":"53b506d2-bc68-4d0f-a7ed-d35a2ff50b47"}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Working on it."}]},"session_id":"53b506d2-bc68-4d0f-a7ed-d35a2ff50b47"}
{"type":"rate_limit_event","rate_limit_info":{"status":"rejected","resetsAt":1785516000,"rateLimitType":"five_hour","overageStatus":"rejected","overageDisabledReason":"out_of_credits","isUsingOverage":false},"session_id":"53b506d2-bc68-4d0f-a7ed-d35a2ff50b47"}
```

Create `tests/fixtures/completed.jsonl`:

```
{"type":"system","subtype":"init","session_id":"53b506d2-bc68-4d0f-a7ed-d35a2ff50b47","model":"claude-opus-5"}
{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","resetsAt":1785516000,"rateLimitType":"five_hour"},"session_id":"53b506d2-bc68-4d0f-a7ed-d35a2ff50b47"}
{"type":"result","subtype":"success","is_error":false,"terminal_reason":"completed","total_cost_usd":0.0248249,"result":"ok","session_id":"53b506d2-bc68-4d0f-a7ed-d35a2ff50b47"}
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_loop.py`:

```python
import json
import tempfile
import unittest
from pathlib import Path

from claudeloop.loop import (
    RESET_PAD_S,
    Fail,
    ReadResult,
    Resume,
    blocking_reset,
    decide,
    read_result,
    total_cost,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> list[dict]:
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines() if line]


class BlockingResetTest(unittest.TestCase):
    def test_returns_reset_time_when_the_latest_report_is_blocking(self):
        self.assertEqual(blocking_reset(load("rate_limited.jsonl")), 1785516000.0)

    def test_returns_none_when_the_latest_report_is_allowed(self):
        self.assertIsNone(blocking_reset(load("completed.jsonl")))

    def test_returns_none_when_there_is_no_report(self):
        self.assertIsNone(blocking_reset([{"type": "assistant"}]))

    def test_an_earlier_block_does_not_outvote_a_later_allow(self):
        events = [
            {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 1}},
            {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "resetsAt": 2}},
        ]
        self.assertIsNone(blocking_reset(events))

    def test_blocked_without_a_reset_time_falls_back_to_a_short_wait(self):
        events = [{"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}}]
        self.assertGreater(blocking_reset(events), 0)


class DecideTest(unittest.TestCase):
    def test_result_file_wins_even_over_a_rate_limit(self):
        action = decide(load("rate_limited.jsonl"), True, 0, 20)
        self.assertIsInstance(action, ReadResult)

    def test_rate_limit_waits_until_the_reset(self):
        action = decide(load("rate_limited.jsonl"), False, 0, 20)
        self.assertEqual(action, Resume(wait_until=1785516000.0 + RESET_PAD_S))

    def test_clean_exit_without_a_result_is_nudged(self):
        action = decide(load("completed.jsonl"), False, 0, 20)
        self.assertEqual(action, Resume(wait_until=0.0))

    def test_exhausted_resumes_fails(self):
        action = decide(load("completed.jsonl"), False, 20, 20)
        self.assertEqual(action, Fail("no_result"))

    def test_exhausted_resumes_fails_even_when_rate_limited(self):
        action = decide(load("rate_limited.jsonl"), False, 20, 20)
        self.assertEqual(action, Fail("no_result"))

    def test_empty_stream_is_nudged(self):
        self.assertEqual(decide([], False, 0, 20), Resume(wait_until=0.0))


class ReadResultTest(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "result.json"

    def test_reads_a_good_file(self):
        self.path.write_text('{"status": "done", "summary": "all green"}')
        self.assertEqual(read_result(self.path), {"status": "done", "summary": "all green"})

    def test_blocked_folds_the_question_into_the_summary(self):
        self.path.write_text(
            '{"status": "blocked", "summary": "stuck", "question": "which currency?"}'
        )
        result = read_result(self.path)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("which currency?", result["summary"])

    def test_malformed_json_becomes_a_failure(self):
        self.path.write_text("{not json")
        self.assertEqual(read_result(self.path)["status"], "failed")

    def test_missing_file_becomes_a_failure(self):
        self.assertEqual(read_result(self.path / "nope")["status"], "failed")

    def test_unknown_status_becomes_a_failure(self):
        self.path.write_text('{"status": "vibes", "summary": "hm"}')
        result = read_result(self.path)
        self.assertEqual(result["status"], "failed")
        self.assertIn("vibes", result["summary"])

    def test_non_object_json_becomes_a_failure(self):
        self.path.write_text("[1, 2, 3]")
        self.assertEqual(read_result(self.path)["status"], "failed")


class TotalCostTest(unittest.TestCase):
    def test_sums_result_events_only(self):
        events = load("completed.jsonl") + [{"type": "assistant", "total_cost_usd": 99.0}]
        self.assertAlmostEqual(total_cost(events), 0.0248249)

    def test_no_result_event_is_zero(self):
        self.assertEqual(total_cost([]), 0.0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m unittest tests.test_loop -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'claudeloop.loop'`

- [ ] **Step 4: Write the implementation**

Create `claudeloop/loop.py`:

```python
"""The decision state machine and the orchestration around it."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

RESET_PAD_S = 30
"""Slack past resetsAt, to absorb clock skew between this host and the API."""

FALLBACK_WAIT_S = 300
"""Used when a blocking rate-limit event arrives without a resetsAt."""

VALID_STATUSES = ("done", "failed", "blocked")


@dataclass(frozen=True)
class ReadResult:
    """The session wrote a result file; take its verdict."""


@dataclass(frozen=True)
class Resume:
    """Run the session again. wait_until is a unix time, 0 meaning now."""

    wait_until: float = 0.0


@dataclass(frozen=True)
class Fail:
    reason: str


def blocking_reset(events: list[dict]) -> float | None:
    """The resetsAt of the most recent rate_limit_event, if it was blocking.

    These events arrive continuously, including while the quota is fine, so
    only the last one describes the state the run ended in.
    """
    for event in reversed(events):
        if event.get("type") != "rate_limit_event":
            continue
        info = event.get("rate_limit_info") or {}
        if info.get("status") == "allowed":
            return None
        return float(info.get("resetsAt") or time.time() + FALLBACK_WAIT_S)
    return None


def decide(
    events: list[dict], result_exists: bool, resume_count: int, max_resumes: int
) -> ReadResult | Resume | Fail:
    """Decide what to do after a claude invocation exits.

    `events` is the stream from the invocation that just exited, not the task's
    whole history: a rate-limit event from an earlier attempt must not
    re-trigger a wait after a later attempt exits for another reason.
    """
    if result_exists:
        return ReadResult()
    if resume_count >= max_resumes:
        return Fail("no_result")
    reset_at = blocking_reset(events)
    if reset_at is not None:
        return Resume(wait_until=reset_at + RESET_PAD_S)
    return Resume()


def read_result(path: Path) -> dict:
    """Read the session's result file, tolerating anything it might contain."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        return {"status": "failed", "summary": f"unreadable result file: {error}"}
    if not isinstance(data, dict):
        return {"status": "failed", "summary": f"result file is not an object: {data!r:.200}"}
    status = data.get("status")
    if status not in VALID_STATUSES:
        return {"status": "failed", "summary": f"result file has bad status {status!r}"}
    summary = str(data.get("summary", ""))
    question = data.get("question")
    if status == "blocked" and question:
        summary = f"{summary}\n\nQuestion: {question}"
    return {"status": status, "summary": summary}


def total_cost(events: list[dict]) -> float:
    return sum(
        float(event.get("total_cost_usd", 0.0))
        for event in events
        if event.get("type") == "result"
    )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m unittest tests.test_loop -v`
Expected: PASS, 19 tests.

- [ ] **Step 6: Commit**

```bash
git add claudeloop/loop.py tests/test_loop.py tests/fixtures
git commit -m "feat: rate-limit-aware decision state machine"
```

---

### Task 5: Session subprocess

**Files:**
- Create: `claudeloop/session.py`
- Create: `tests/test_session.py`
- Create: `tests/fake_claude.sh`

**Interfaces:**
- Consumes: `Config` from Task 1.
- Produces: `PROTOCOL: str`, `MAX_LINE: int`, `build_command(cfg: Config, session_id: str, prompt: str, resume: bool) -> list[str]`, `async run(cfg: Config, run_dir: Path, session_id: str, prompt: str, resume: bool) -> list[dict]`.

**Design notes for the implementer, all load-bearing:**

1. **`limit=MAX_LINE` on `create_subprocess_exec` is required.** The asyncio stream reader defaults to a 64 KiB line buffer, and a single `stream-json` line carrying a large tool result will exceed it and raise `ValueError`. Set 16 MiB.
2. **stdout and stderr must be drained concurrently.** `--verbose` puts diagnostics on stderr; if stderr fills its 64 KiB pipe buffer while this code is still reading stdout, the child blocks forever and so does the loop. Use `asyncio.gather`.
3. **On resume, pass `--resume <uuid>` and *not* `--session-id`.** They are alternative ways to name the session; sending both is a conflict.
4. **Write each raw line to the log before parsing it**, so a malformed line is still on disk.

- [ ] **Step 1: Create the fake CLI**

Create `tests/fake_claude.sh`:

```bash
#!/usr/bin/env bash
# Stand-in for the real `claude` CLI. Emits a canned stream-json stream.
#
# FAKE_LIMIT_FLAG: if set and the named file exists, delete it, emit a blocking
# rate_limit_event whose reset is already in the past, and exit non-zero
# without writing a result. The next invocation therefore succeeds, which is
# how the end-to-end test exercises the recover-and-resume path.
# FAKE_ARGS_OUT: if set, the invocation's arguments are appended to that file.
set -u

if [ -n "${FAKE_ARGS_OUT:-}" ]; then
  printf '%s\n' "$*" >> "$FAKE_ARGS_OUT"
fi

echo '{"type":"system","subtype":"init","session_id":"fake"}'
echo 'diagnostic noise on stderr' >&2

if [ -n "${FAKE_LIMIT_FLAG:-}" ] && [ -f "$FAKE_LIMIT_FLAG" ]; then
  rm -f "$FAKE_LIMIT_FLAG"
  past=$(( $(date +%s) - 120 ))
  echo "{\"type\":\"rate_limit_event\",\"rate_limit_info\":{\"status\":\"rejected\",\"resetsAt\":${past},\"rateLimitType\":\"five_hour\"}}"
  exit 1
fi

echo 'this line is not json'
printf '%s' '{"status":"done","summary":"fake work"}' > "$CLAUDELOOP_RESULT"
echo '{"type":"result","subtype":"success","total_cost_usd":0.5,"result":"ok"}'
```

```bash
chmod +x tests/fake_claude.sh
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_session.py`:

```python
import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from claudeloop import session
from claudeloop.config import Config

FAKE = Path(__file__).parent / "fake_claude.sh"


def fake_path_dir(tmp: Path) -> Path:
    """A directory containing an executable named `claude` that is our fake."""
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    shutil.copy(FAKE, bin_dir / "claude")
    (bin_dir / "claude").chmod(0o755)
    return bin_dir


class BuildCommandTest(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(repo=Path("/repo"), tasks_file=Path("/tasks.md"), model="sonnet")

    def test_first_run_assigns_the_session_id(self):
        cmd = session.build_command(self.cfg, "uuid-1", "do it", resume=False)
        self.assertEqual(cmd[:4], ["claude", "-p", "do it", "--session-id"])
        self.assertEqual(cmd[4], "uuid-1")
        self.assertNotIn("--resume", cmd)

    def test_resume_uses_resume_and_not_session_id(self):
        cmd = session.build_command(self.cfg, "uuid-1", "Continue.", resume=True)
        self.assertIn("--resume", cmd)
        self.assertNotIn("--session-id", cmd)

    def test_carries_the_flags_the_loop_depends_on(self):
        cmd = session.build_command(self.cfg, "uuid-1", "do it", resume=False)
        self.assertIn("--output-format", cmd)
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "stream-json")
        self.assertIn("--verbose", cmd)
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "bypassPermissions")
        self.assertEqual(cmd[cmd.index("--model") + 1], "sonnet")
        self.assertEqual(cmd[cmd.index("--append-system-prompt") + 1], session.PROTOCOL)

    def test_protocol_names_the_result_variable_and_every_status(self):
        for token in ("CLAUDELOOP_RESULT", "CLAUDE.md", "done", "failed", "blocked"):
            self.assertIn(token, session.PROTOCOL)


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "repo" / ".git").mkdir(parents=True)
        self.cfg = Config(
            repo=self.tmp / "repo",
            tasks_file=self.tmp / "tasks.md",
            home=self.tmp / "home",
        )
        self.run_dir = self.tmp / "home" / "runs" / "abc"
        self.old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{fake_path_dir(self.tmp)}{os.pathsep}{self.old_path}"

    def tearDown(self):
        os.environ["PATH"] = self.old_path

    def run_once(self, resume: bool = False) -> list[dict]:
        return asyncio.run(
            session.run(self.cfg, self.run_dir, "uuid-1", "do it", resume=resume)
        )

    def test_returns_parsed_events_and_skips_non_json_lines(self):
        events = self.run_once()
        types = [event.get("type") for event in events]
        self.assertEqual(types, ["system", "result"])

    def test_logs_every_raw_line_including_the_unparseable_one(self):
        self.run_once()
        lines = (self.run_dir / "events.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("this line is not json", lines)

    def test_sets_the_result_path_in_the_environment(self):
        self.run_once()
        result = json.loads((self.run_dir / "result.json").read_text())
        self.assertEqual(result["status"], "done")

    def test_captures_stderr(self):
        self.run_once()
        self.assertIn("diagnostic noise", (self.run_dir / "stderr.log").read_text())

    def test_appends_to_the_event_log_across_invocations(self):
        self.run_once()
        self.run_once(resume=True)
        lines = (self.run_dir / "events.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 6)

    def test_survives_a_non_zero_exit(self):
        flag = self.tmp / "limit.flag"
        flag.write_text("")
        os.environ["FAKE_LIMIT_FLAG"] = str(flag)
        try:
            events = self.run_once()
        finally:
            del os.environ["FAKE_LIMIT_FLAG"]
        self.assertEqual(events[-1]["type"], "rate_limit_event")
        self.assertFalse((self.run_dir / "result.json").exists())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m unittest tests.test_session -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'claudeloop.session'`

- [ ] **Step 4: Write the implementation**

Create `claudeloop/session.py`:

```python
"""Spawn one headless Claude Code invocation and stream its output."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from .config import Config

MAX_LINE = 16 * 1024 * 1024
"""asyncio's default 64 KiB line buffer is too small: a single stream-json line
carrying a large tool result overruns it and raises ValueError."""

PROTOCOL = (
    "You are running unattended under ClaudeLoop. Follow this repository's "
    "CLAUDE.md end to end — it defines what \"done\" means here, including its "
    "testing and verification requirements. Nobody is watching, so decide open "
    "questions yourself rather than waiting. When the task is fully complete, "
    "or provably cannot be completed, write a JSON object to the path in the "
    "CLAUDELOOP_RESULT environment variable with keys \"status\" (one of "
    "\"done\", \"failed\", \"blocked\"), \"summary\" (one paragraph on what you "
    "did), and, when blocked, \"question\" (the one thing a human must answer). "
    "Writing that file is what ends the task; do not stop without it."
)


def build_command(cfg: Config, session_id: str, prompt: str, resume: bool) -> list[str]:
    command = ["claude", "-p", prompt]
    # --resume and --session-id are alternative ways to name the session;
    # passing both is a conflict.
    command += ["--resume", session_id] if resume else ["--session-id", session_id]
    command += [
        "--append-system-prompt", PROTOCOL,
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", "bypassPermissions",
        "--model", cfg.model,
    ]
    return command


async def _read_events(stream: asyncio.StreamReader, path: Path, out: list[dict]) -> None:
    with open(path, "ab") as log:
        while True:
            try:
                raw = await stream.readline()
            except ValueError:
                continue  # over-long line, already discarded by the reader
            if not raw:
                return
            log.write(raw)  # verbatim first: a parser bug never loses the record
            log.flush()
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                pass  # non-JSON noise on stdout, already on disk


async def _drain(stream: asyncio.StreamReader, path: Path) -> None:
    with open(path, "ab") as log:
        async for chunk in stream:
            log.write(chunk)


async def run(
    cfg: Config, run_dir: Path, session_id: str, prompt: str, resume: bool
) -> list[dict]:
    """Run one invocation to completion. Returns only this invocation's events."""
    run_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ | {"CLAUDELOOP_RESULT": str(run_dir / "result.json")}
    process = await asyncio.create_subprocess_exec(
        *build_command(cfg, session_id, prompt, resume),
        cwd=cfg.repo,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=MAX_LINE,
    )
    events: list[dict] = []
    # Both pipes must be drained concurrently: --verbose writes diagnostics to
    # stderr, and a full stderr pipe buffer would deadlock the child.
    await asyncio.gather(
        _read_events(process.stdout, run_dir / "events.jsonl", events),
        _drain(process.stderr, run_dir / "stderr.log"),
    )
    await process.wait()
    return events
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m unittest tests.test_session -v`
Expected: PASS, 10 tests.

- [ ] **Step 6: Commit**

```bash
git add claudeloop/session.py tests/test_session.py tests/fake_claude.sh
git commit -m "feat: headless claude session subprocess"
```

---

### Task 6: Orchestration and entry point

**Files:**
- Modify: `claudeloop/loop.py` (append; do not change the pure functions)
- Create: `claudeloop/__main__.py`
- Create: `README.md`
- Modify: `tests/test_loop.py` (append a new test class)

**Interfaces:**
- Consumes: `Config`/`load_config` (Task 1), `State` (Task 2), `FileSource`/`Task` (Task 3), `ReadResult`/`Resume`/`Fail`/`decide`/`read_result`/`total_cost` (Task 4), `session.run` (Task 5).
- Produces: `async run_task(cfg, state, source, task) -> dict`, `async main_loop(cfg, once: bool = False) -> None`, `main() -> None`. Constant `POLL_S = 30`.

**Design notes for the implementer:**

1. **Delete a stale result file before starting a task.** A previous attempt's verdict sitting in the run directory would make `decide()` finish the task instantly on its first invocation.
2. **Accumulate cost across resumes.** `session.run` returns only the last invocation's events, so `total_cost` must be summed into a running total inside the loop, not called once at the end.
3. `main_loop(once=True)` drains the currently pending tasks and returns, so the test does not need to cancel an infinite loop.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_loop.py` — add these imports at the top of the file:

```python
import asyncio
import os
import shutil

from claudeloop import loop
from claudeloop.config import Config
from claudeloop.source import FileSource, task_id
from claudeloop.state import State
```

and append this class before the `if __name__` block:

```python
class MainLoopTest(unittest.TestCase):
    """End to end against the fake CLI, including one rate-limit recovery."""

    def setUp(self):
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

    def test_runs_every_task_and_checks_it_off(self):
        asyncio.run(loop.main_loop(self.cfg, once=True))
        self.assertEqual(self.tasks.read_text(), "- [x] first thing\n- [x] second thing\n")

    def test_records_status_and_cost(self):
        asyncio.run(loop.main_loop(self.cfg, once=True))
        state = State(self.cfg.home / "state.db")
        rows = state.db.execute("SELECT * FROM tasks ORDER BY started_at").fetchall()
        self.assertEqual([row["status"] for row in rows], ["done", "done"])
        self.assertEqual([row["summary"] for row in rows], ["fake work", "fake work"])
        self.assertAlmostEqual(rows[0]["cost_usd"], 0.5)

    def test_recovers_from_a_rate_limit_and_finishes_the_task(self):
        flag = self.tmp / "limit.flag"
        flag.write_text("")
        os.environ["FAKE_LIMIT_FLAG"] = str(flag)
        self.tasks.write_text("- [ ] first thing\n")

        asyncio.run(loop.main_loop(self.cfg, once=True))

        self.assertEqual(self.tasks.read_text(), "- [x] first thing\n")
        state = State(self.cfg.home / "state.db")
        runs = state.db.execute("SELECT * FROM runs ORDER BY id").fetchall()
        self.assertEqual(len(runs), 2, "expected one limited run and one resume")
        self.assertEqual(runs[0]["exit_reason"], "Resume")
        self.assertEqual(runs[1]["exit_reason"], "ReadResult")
        # The resume must reuse the session, not start a fresh one.
        self.assertEqual(runs[0]["session_id"], runs[1]["session_id"])

    def test_gives_up_after_max_resumes_and_marks_for_attention(self):
        # A CLI that never writes a result: every invocation is a nudge.
        fake = self.tmp / "bin" / "claude"
        fake.write_text('#!/usr/bin/env bash\necho \'{"type":"result"}\'\n')
        fake.chmod(0o755)
        self.tasks.write_text("- [ ] doomed thing\n")

        asyncio.run(loop.main_loop(self.cfg, once=True))

        self.assertEqual(self.tasks.read_text(), "- [!] doomed thing\n")
        state = State(self.cfg.home / "state.db")
        row = state.db.execute("SELECT * FROM tasks").fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn("no_result", row["summary"])
        runs = state.db.execute("SELECT COUNT(*) AS n FROM runs").fetchone()
        self.assertEqual(runs["n"], self.cfg.max_resumes + 1)

    def test_stale_result_from_a_previous_attempt_is_discarded(self):
        stale = self.cfg.home / "runs" / task_id("first thing")
        stale.mkdir(parents=True)
        (stale / "result.json").write_text('{"status": "failed", "summary": "old news"}')
        self.tasks.write_text("- [ ] first thing\n")

        asyncio.run(loop.main_loop(self.cfg, once=True))

        state = State(self.cfg.home / "state.db")
        row = state.db.execute("SELECT * FROM tasks").fetchone()
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["summary"], "fake work")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_loop -v`
Expected: FAIL with `AttributeError: module 'claudeloop.loop' has no attribute 'main_loop'`

- [ ] **Step 3: Write the implementation**

Append to `claudeloop/loop.py` — add these imports to the existing import block at the top:

```python
import asyncio
import logging
import uuid

from . import session
from .config import Config, load_config
from .source import FileSource, Task, TaskSource
from .state import State
```

and append to the end of the file:

```python
POLL_S = 30
"""How long to idle when the task list is empty, so appended tasks get picked
up without a restart."""

log = logging.getLogger("claudeloop")


async def run_task(cfg: Config, state: State, source: TaskSource, task: Task) -> dict:
    """Run one task to a terminal status, resuming through rate limits."""
    run_dir = cfg.home / "runs" / task.id
    result_path = run_dir / "result.json"
    run_dir.mkdir(parents=True, exist_ok=True)
    # A previous attempt's verdict would otherwise end this one immediately.
    result_path.unlink(missing_ok=True)

    session_id = str(uuid.uuid4())
    state.start_task(task.id, task.source, task.source_ref, task.text)
    log.info("task %s starting: %s", task.id, task.text)

    resume_count = 0
    cost = 0.0
    while True:
        run_id = state.start_run(task.id, session_id, resume_count)
        events = await session.run(
            cfg,
            run_dir,
            session_id,
            prompt="Continue." if resume_count else task.text,
            resume=bool(resume_count),
        )
        # session.run returns only this invocation's events, so cost has to
        # accumulate here rather than being read once at the end.
        cost += total_cost(events)
        action = decide(events, result_path.exists(), resume_count, cfg.max_resumes)
        state.finish_run(run_id, type(action).__name__)

        if isinstance(action, ReadResult):
            result = read_result(result_path)
            break
        if isinstance(action, Fail):
            result = {"status": "failed", "summary": f"ClaudeLoop gave up: {action.reason}"}
            break
        if action.wait_until:
            delay = max(0.0, action.wait_until - time.time())
            log.info("task %s rate limited, sleeping %.0fs", task.id, delay)
            await asyncio.sleep(delay)
        resume_count += 1

    state.finish_task(task.id, result["status"], result["summary"], cost)
    source.mark(task, result["status"], result["summary"])
    log.info("task %s %s ($%.4f): %s", task.id, result["status"], cost, result["summary"])
    return result


async def main_loop(cfg: Config, once: bool = False) -> None:
    """Run pending tasks one at a time, forever.

    `once` drains the tasks pending right now and returns, for tests.
    """
    state = State(cfg.home / "state.db")
    source = FileSource(cfg.tasks_file)
    while True:
        pending = source.pending()
        if not pending:
            if once:
                return
            await asyncio.sleep(POLL_S)
            continue
        # Re-read after every task: the file may have been edited meanwhile.
        await run_task(cfg, state, source, pending[0])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    asyncio.run(main_loop(load_config()))
```

Create `claudeloop/__main__.py`:

```python
from .loop import main

main()
```

- [ ] **Step 4: Run the whole suite**

Run: `python -m unittest discover -s tests -t . -v`
Expected: PASS, 55 tests.

- [ ] **Step 5: Write the README**

Create `README.md`:

````markdown
# ClaudeLoop

An unattended orchestrator for Claude Code. It takes tasks one at a time from a
markdown checklist, runs a headless Claude Code session per task in a target
repository, sleeps through subscription rate limits and resumes the same
session afterwards, and records every outcome.

ClaudeLoop holds no workflow logic of its own. The target repository's
`CLAUDE.md` defines what "done" means there — testing, verification, review,
whatever it says — and ClaudeLoop's per-task instruction just points at it.

## Requirements

Python 3.11 or newer, and the Claude Code CLI on `PATH`, already authenticated
(`claude setup-token` for an unattended host). No Python packages to install.

## Configure

`~/.claudeloop/config.toml`:

```toml
repo        = "/home/you/Projects/yourrepo"
tasks_file  = "/home/you/Projects/yourrepo/.claudeloop-tasks.md"
model       = "opus"        # optional, default "opus"
max_resumes = 20            # optional, default 20
```

One instance serves one repository. For a second repository, run a second
instance with its own config.

## Tasks

A markdown checklist. Unchecked items run in file order.

```markdown
- [ ] Fix the cart total rounding on the store grid
- [x] Add Money serialization to the admin SPA
- [!] Migrate the renderer to Containers
```

`- [x]` succeeded. `- [!]` needs a human — it failed, was blocked on a
question, or exhausted its resume budget. Neither is picked up again. Append
new tasks at any time; the loop re-reads the file after each one.

## Run

```bash
python -m claudeloop
```

## Where things go

```
~/.claudeloop/
  config.toml
  state.db                      # what happened: status, summary, cost, timings
  runs/<task-id>/
    events.jsonl                # the raw stream-json stream, appended per attempt
    result.json                 # the session's own verdict
    stderr.log
```

## Warning

Sessions run with `--permission-mode bypassPermissions` and no human present.
Whatever credentials the target repository's workflow uses, an unattended agent
is using them — including, if that repository authorizes it, pushing to `main`
and triggering a production deploy. Point ClaudeLoop only at repositories whose
`CLAUDE.md` you are willing to have executed without review.

## Tests

```bash
python -m unittest discover -s tests -t .
```
````

- [ ] **Step 6: Commit**

```bash
git add claudeloop/loop.py claudeloop/__main__.py tests/test_loop.py README.md
git commit -m "feat: task orchestration, entry point, README"
```

---

## Manual smoke test

After Task 6, before declaring S1 done. This is the one thing the fake CLI
cannot prove: that the real `claude` accepts these flags and writes the result
file when asked.

- [ ] Create a scratch git repository with a one-line `CLAUDE.md` saying that
      done means the change is committed.
- [ ] Point `~/.claudeloop/config.toml` at it, with `model = "haiku"` and
      `max_resumes = 2`.
- [ ] Put one trivial task in the task file, e.g. `- [ ] Add a LICENSE file (MIT, 2026 Kamil Postrożny)`.
- [ ] Run `python -m claudeloop`, confirm the task is checked off, the file
      exists and is committed, and `runs/<id>/result.json` contains a `done`
      status.
- [ ] Confirm `events.jsonl` contains at least one `rate_limit_event` with a
      `resetsAt`, proving the recovery path's input is really present in the
      live stream.

## Spec coverage

| Spec requirement | Task |
|---|---|
| Python, standard library only | Global constraints; every task |
| Config file, `repo`/`tasks_file`/`model`/`max_resumes` | 1 |
| `sqlite3` state, `tasks` + `runs` tables | 2 |
| `TaskSource` protocol; `FileSource` over markdown checkboxes | 3 |
| `mark()` matches on line text, not index | 3 |
| Rate-limit detection via `rate_limit_event.resetsAt` | 4 |
| `decide()` as a pure, separately testable function | 4 |
| Result file schema, `blocked` carries a question | 4 |
| Result file checked before the rate-limit branch | 4 |
| `resetsAt + 30s` pad | 4 |
| Pre-assigned session UUID; `--resume` on later attempts | 5, 6 |
| Protocol in `--append-system-prompt`, not the prompt | 5 |
| `bypassPermissions`, `stream-json`, `cwd` = repo | 5 |
| Result file outside the repository, via `CLAUDELOOP_RESULT` | 5, 6 |
| Every stdout line logged verbatim before parsing | 5 |
| Strictly serial execution | 6 |
| `max_resumes` bounds nudges and rate-limit waits together | 4, 6 |
| Blocked task recorded, loop continues | 6 |
| Security warning stated for the operator | 6 (README) |
| Acceptance criteria 1–5 | Task 6 tests + manual smoke test |
