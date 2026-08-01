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
    cost_usd    REAL,
    question    TEXT
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
        # CREATE TABLE IF NOT EXISTS is a no-op against a database created
        # before `question` was added to SCHEMA, so it needs its own
        # migration. Guarded because it's a straight error on a database
        # that already has the column.
        try:
            self.db.execute("ALTER TABLE tasks ADD COLUMN question TEXT")
        except sqlite3.OperationalError:
            pass
        # A 'running' row can only mean the previous process died mid-task
        # (crash, SIGKILL, power loss): nothing else leaves it in that state.
        # Left alone it would misreport as in-progress forever.
        self.db.execute("UPDATE tasks SET status='interrupted' WHERE status='running'")

    def start_task(self, task_id: str, source: str, source_ref: str, text: str) -> None:
        now = time.time()
        self.db.execute(
            "INSERT OR REPLACE INTO tasks"
            " (id, source, source_ref, text, status, created_at, started_at)"
            " VALUES (?, ?, ?, ?, 'running', ?, ?)",
            (task_id, source, source_ref, text, now, now),
        )

    def finish_task(
        self,
        task_id: str,
        status: str,
        summary: str,
        cost_usd: float,
        question: str | None = None,
    ) -> None:
        self.db.execute(
            "UPDATE tasks SET status=?, summary=?, cost_usd=?, finished_at=?, question=?"
            " WHERE id=?",
            (status, summary, cost_usd, time.time(), question, task_id),
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

    def terminal_ids(self) -> set[str]:
        """Task ids that reached a verdict, for a source that needs a backstop
        against re-running finished work.

        'interrupted' is excluded on purpose: State.__init__ writes it when a
        previous process died mid-task, and that task never finished.
        """
        rows = self.db.execute(
            "SELECT id FROM tasks WHERE status IN ('done', 'failed', 'blocked')"
        )
        return {row["id"] for row in rows}
