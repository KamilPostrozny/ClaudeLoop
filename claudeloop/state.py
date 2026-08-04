"""SQLite record of what the loop did. Not the source of truth for what is
pending — the task source is."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .config import narrow

TASK_COLUMNS = (
    "id", "source", "source_ref", "text", "status", "created_at", "started_at",
    "finished_at", "summary", "cost_usd", "question", "repo",
)
"""Named so the (id, repo) migration below can copy rows without SELECT *,
whose column order is whatever the old database happened to have."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT NOT NULL,
    source      TEXT NOT NULL,
    source_ref  TEXT NOT NULL,
    text        TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  REAL NOT NULL,
    started_at  REAL,
    finished_at REAL,
    summary     TEXT,
    cost_usd    REAL,
    question    TEXT,
    repo        TEXT,
    PRIMARY KEY (id, repo)
);
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    started_at   REAL NOT NULL,
    ended_at     REAL,
    exit_reason  TEXT,
    resume_count INTEGER NOT NULL DEFAULT 0,
    repo         TEXT
);
"""
"""`tasks` is keyed on (id, repo), not id alone. `id` is a hash of the task
text, so two repositories whose file sources hold identical task text produce
the same id -- and under the old single-column key start_task's INSERT OR
REPLACE silently overwrote the other repository's row.

`repo` stays nullable rather than defaulting to '': rows written before the
column existed carry NULL, and SQLite does not enforce uniqueness across a
NULL key part, so they keep belonging to no repository exactly as they did.
Nothing this code writes ever leaves it NULL.

`runs` lost its REFERENCES clause, which named a `tasks(id)` that is no longer
a key on its own. Foreign keys were never enforced here (the pragma is off by
default and nothing turns it on), so this only removes a declaration that had
stopped being true."""


class State:
    """One database per machine, many repositories: `~/.claudeloop/state.db`
    is shared by every config ever run on the box, so every read that means
    "this loop's work" is scoped by `repo`. Without it a fresh config points
    at a repository whose dashboard lists another repository's finished
    tasks, the Jira backstop suppresses a ticket a different loop finished,
    and `blocked()` offers a parked task belonging to somewhere else.

    Rows written before the column existed carry NULL and match no scope.
    """

    def __init__(self, db_path: Path, repo: str = ""):
        self.repo = repo
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # This database holds task text, session summaries and the questions a
        # session parked on, and it sits beside config.toml -- whose own guard
        # refuses to load a file readable beyond its owner. Applied
        # unconditionally rather than only on creation: mkdir(exist_ok=True)
        # leaves an existing directory's mode alone, and the common case is a
        # ~/.claudeloop created at the default umask by a version without this.
        narrow(db_path.parent, 0o700)
        # isolation_level=None is autocommit: a crash never loses the last write.
        # check_same_thread=False: the loop is strictly serial and awaits every
        # asyncio.to_thread call before starting the next, so although this
        # connection gets used from a different worker thread each time, it is
        # never touched by two threads at once. That is single-writer access
        # from a varying thread, not concurrent access, so sqlite3's default
        # same-thread guard is a false positive here -- without this, a call
        # like JiraSource.pending() reaching State.terminal_ids() through
        # to_thread raises sqlite3.ProgrammingError on every call.
        self.db = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        # WAL so S2's web UI can read while the loop writes.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        # CREATE TABLE IF NOT EXISTS is a no-op against a database created
        # before `question` was added to SCHEMA, so it needs its own
        # migration. Guarded because it's a straight error on a database
        # that already has the column.
        for column in ("question TEXT", "repo TEXT"):
            try:
                self.db.execute(f"ALTER TABLE tasks ADD COLUMN {column}")
            except sqlite3.OperationalError:
                pass
        try:
            self.db.execute("ALTER TABLE runs ADD COLUMN repo TEXT")
        except sqlite3.OperationalError:
            pass
        # After the ALTER above, not in SCHEMA: on a database predating
        # runs.repo the column does not exist yet when SCHEMA runs.
        self.db.execute("CREATE INDEX IF NOT EXISTS runs_by_task ON runs (task_id, repo)")
        self._rekey()
        # Same reasoning as the directory above, and it has to come after the
        # connection: sqlite creates the file itself, at the default umask.
        # -wal and -shm are created beside it and carry the same content.
        for suffix in ("", "-wal", "-shm"):
            narrow(Path(str(db_path) + suffix), 0o600)
        # A 'running' row can only mean the previous process died mid-task
        # (crash, SIGKILL, power loss): nothing else leaves it in that state.
        # Left alone it would misreport as in-progress forever. Scoped to this
        # repository: two loops over two repositories can run at once, and one
        # starting up must not rewrite the other's live row.
        self.db.execute(
            "UPDATE tasks SET status='interrupted' WHERE status='running' AND repo IS ?",
            (self.repo,),
        )

    def _rekey(self) -> None:
        """Rebuild `tasks` on (id, repo) if it still carries the old key.

        SQLite cannot alter a primary key in place, so this is the copy-drop-
        rename dance. It runs once per database: the check below is what makes
        it a no-op on every subsequent start.
        """
        keyed = {row["name"] for row in self.db.execute("PRAGMA table_info(tasks)")
                 if row["pk"]}
        if keyed == {"id", "repo"}:
            return
        columns = ", ".join(TASK_COLUMNS)
        # Before the rebuild, while `id` is still unique in `tasks`: a run row
        # written by a version without runs.repo has to learn which repository
        # it belongs to, or last_session() stops finding the session a task
        # parked across this upgrade needs to resume.
        self.db.execute(
            "UPDATE runs SET repo = (SELECT repo FROM tasks WHERE tasks.id = runs.task_id)"
            " WHERE repo IS NULL"
        )
        self.db.executescript(
            f"""
            BEGIN;
            CREATE TABLE tasks_rekeyed (
                id          TEXT NOT NULL,
                source      TEXT NOT NULL,
                source_ref  TEXT NOT NULL,
                text        TEXT NOT NULL,
                status      TEXT NOT NULL,
                created_at  REAL NOT NULL,
                started_at  REAL,
                finished_at REAL,
                summary     TEXT,
                cost_usd    REAL,
                question    TEXT,
                repo        TEXT,
                PRIMARY KEY (id, repo)
            );
            INSERT OR IGNORE INTO tasks_rekeyed ({columns})
                SELECT {columns} FROM tasks;
            DROP TABLE tasks;
            ALTER TABLE tasks_rekeyed RENAME TO tasks;
            COMMIT;
            """
        )

    def start_task(self, task_id: str, source: str, source_ref: str, text: str) -> None:
        now = time.time()
        self.db.execute(
            "INSERT OR REPLACE INTO tasks"
            " (id, source, source_ref, text, status, created_at, started_at, repo)"
            " VALUES (?, ?, ?, ?, 'running', ?, ?, ?)",
            (task_id, source, source_ref, text, now, now, self.repo),
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
            " WHERE id=? AND repo IS ?",
            (status, summary, cost_usd, time.time(), question, task_id, self.repo),
        )

    def start_run(self, task_id: str, session_id: str, resume_count: int) -> int:
        cursor = self.db.execute(
            "INSERT INTO runs (task_id, session_id, started_at, resume_count, repo)"
            " VALUES (?, ?, ?, ?, ?)",
            (task_id, session_id, time.time(), resume_count, self.repo),
        )
        return cursor.lastrowid

    def finish_run(self, run_id: int, exit_reason: str) -> None:
        self.db.execute(
            "UPDATE runs SET ended_at=?, exit_reason=? WHERE id=?",
            (time.time(), exit_reason, run_id),
        )

    def task(self, task_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM tasks WHERE id=? AND repo IS ?", (task_id, self.repo)
        ).fetchone()

    def prior_cost(self, task_id: str) -> float:
        """What this task has already spent, before the run about to start.

        A task that parks on a question and is later answered spans two
        run_task calls, and the second one starts its own accumulator at zero
        -- so finish_task's `cost_usd=?` used to overwrite the money spent
        before the question was asked rather than adding to it. Measured in
        S6's live smoke test: $0.0395 parking plus $0.0162 finishing was
        recorded, reported and commented on the ticket as $0.0162.

        Read before start_task, which is INSERT OR REPLACE and puts cost_usd
        back to NULL -- the same ordering was_interrupted() needs.
        """
        row = self.db.execute(
            "SELECT cost_usd FROM tasks WHERE id=? AND repo IS ?", (task_id, self.repo)
        ).fetchone()
        if row is None or row["cost_usd"] is None:
            return 0.0
        return float(row["cost_usd"])

    def was_interrupted(self, task_id: str) -> bool:
        """Whether this task's previous run died mid-task.

        Scoped to this repository for the same reason terminal_ids() and
        blocked() are, and here it is load-bearing rather than tidy: `id` is
        the primary key on its own, so two repositories whose file sources
        hold identical task text share a row, and an unscoped read could
        answer yes on the strength of the *other* loop's interruption. What
        the caller does with a yes is resume a session id -- a session whose
        stored transcript belongs to a different repository's worktree.

        Must be called before start_task, which is INSERT OR REPLACE and puts
        the row back to 'running'.
        """
        row = self.db.execute(
            "SELECT 1 FROM tasks WHERE id=? AND status='interrupted' AND repo IS ?",
            (task_id, self.repo),
        ).fetchone()
        return row is not None

    def terminal_ids(self) -> set[str]:
        """Task ids that reached a verdict, for a source that needs a backstop
        against re-running finished work.

        'interrupted' is excluded on purpose: State.__init__ writes it when a
        previous process died mid-task, and that task never finished.
        """
        rows = self.db.execute(
            "SELECT id FROM tasks WHERE status IN ('done', 'failed', 'blocked')"
            " AND repo IS ?",
            (self.repo,),
        )
        return {row["id"] for row in rows}

    def blocked(self) -> list[sqlite3.Row]:
        """Tasks parked waiting for a human, oldest first.

        Returns enough to rebuild the Task the loop handed to the source, so
        a task parked before this process started can still be resumed.
        """
        return self.db.execute(
            "SELECT id, source, source_ref, text, question FROM tasks"
            " WHERE status='blocked' AND repo IS ? ORDER BY finished_at",
            (self.repo,),
        ).fetchall()

    def last_session(self, task_id: str) -> str | None:
        """The session id of this task's most recent run.

        An answered task resumes in the session that asked the question: it
        still holds the repository context and the name of the branch it
        created. None means there is no session to resume -- a database from
        before this slice, or a task whose runs were pruned.
        """
        row = self.db.execute(
            "SELECT session_id FROM runs WHERE task_id=? AND repo IS ?"
            " ORDER BY id DESC LIMIT 1",
            (task_id, self.repo),
        ).fetchone()
        return row["session_id"] if row is not None else None

    def close(self) -> None:
        """Release the connection. Idempotent, and safe to call twice."""
        db = getattr(self, "db", None)
        if db is not None:
            db.close()

    def __del__(self) -> None:
        # sqlite3 warns loudly (ResourceWarning, one per connection) when a
        # connection is finalized without being closed, and State owns its
        # connection outright -- so closing it here is what State owning it
        # means. Guarded because __init__ can raise before `db` is bound, and
        # because an interpreter shutdown can reach this with sqlite3 already
        # torn down.
        try:
            self.close()
        except Exception:
            pass
