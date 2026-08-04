import asyncio
import gc
import os
import sqlite3
import tempfile
import time
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

    def test_finish_task_stores_the_question(self):
        self.state.start_task("abc", "file", "- [ ] do it", "do it")
        self.state.finish_task("abc", "blocked", "stuck", 0.0, "which currency?")
        row = self.state.task("abc")
        self.assertEqual(row["status"], "blocked")
        self.assertEqual(row["question"], "which currency?")

    def test_finish_task_question_defaults_to_none(self):
        self.state.start_task("abc", "file", "- [ ] do it", "do it")
        self.state.finish_task("abc", "done", "worked fine", 1.25)
        self.assertIsNone(self.state.task("abc")["question"])

    def test_reopening_marks_orphaned_running_tasks_interrupted(self):
        # No clean shutdown path ever leaves a row at 'running': it can only
        # mean the previous process died mid-task.
        self.state.start_task("abc", "file", "- [ ] do it", "do it")
        reopened = State(self.tmp / "nested" / "state.db")
        self.assertEqual(reopened.task("abc")["status"], "interrupted")

    def test_reopening_does_not_touch_finished_tasks(self):
        self.state.start_task("abc", "file", "- [ ] do it", "do it")
        self.state.finish_task("abc", "done", "worked fine", 1.25)
        reopened = State(self.tmp / "nested" / "state.db")
        self.assertEqual(reopened.task("abc")["status"], "done")

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

    def test_opening_a_pre_migration_database_adds_the_question_column(self):
        # Simulates a state.db created before `question` was added to
        # SCHEMA: CREATE TABLE IF NOT EXISTS is a no-op against it, so
        # without the guarded ALTER TABLE every finish_task() call would
        # raise "no such column: question" on a database this old.
        path = self.tmp / "pre-migration.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE tasks (
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
            """
        )
        conn.close()

        state = State(path)
        columns = {row[1] for row in state.db.execute("PRAGMA table_info(tasks)")}
        self.assertIn("question", columns)

        state.start_task("abc", "file", "- [ ] do it", "do it")
        state.finish_task("abc", "blocked", "stuck", 0.0, "which currency?")
        self.assertEqual(state.task("abc")["question"], "which currency?")

    def test_reopening_an_existing_database_keeps_rows(self):
        self.state.start_task("abc", "file", "- [ ] do it", "do it")
        reopened = State(self.tmp / "nested" / "state.db")
        self.assertEqual(reopened.task("abc")["text"], "do it")

    def test_the_pre_migration_database_also_gains_the_repo_column(self):
        path = self.tmp / "pre-repo.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE tasks (
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
            INSERT INTO tasks (id, source, source_ref, text, status, created_at)
            VALUES ('old', 'file', '- [ ] old', 'old', 'done', 1.0);
            """
        )
        conn.close()

        state = State(path, "/repo")
        columns = {row[1] for row in state.db.execute("PRAGMA table_info(tasks)")}
        self.assertIn("repo", columns)
        # A row from before the column existed carries NULL and belongs to no
        # repository, so it is nobody's history rather than everybody's.
        self.assertEqual(state.terminal_ids(), set())

    def test_records_the_repository_it_was_opened_for(self):
        state = State(self.tmp / "scoped.db", "/repo/one")
        state.start_task("abc", "file", "- [ ] do it", "do it")
        self.assertEqual(state.task("abc")["repo"], "/repo/one")

    def test_another_repositorys_running_row_is_left_alone_on_startup(self):
        # Two loops over two repositories share one state.db. One starting up
        # must not rewrite the other's live row to 'interrupted'.
        path = self.tmp / "shared.db"
        one = State(path, "/repo/one")
        one.start_task("abc", "file", "- [ ] do it", "do it")
        State(path, "/repo/two")
        self.assertEqual(one.task("abc")["status"], "running")


class WasInterruptedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "state.db"

    def test_a_task_left_running_by_a_dead_process_reads_as_interrupted(self):
        dying = State(self.path, "/repo")
        dying.start_task("abc", "file", "- [ ] do it", "do it")

        self.assertTrue(State(self.path, "/repo").was_interrupted("abc"))

    def test_a_finished_task_does_not(self):
        state = State(self.path, "/repo")
        state.start_task("abc", "file", "- [ ] do it", "do it")
        state.finish_task("abc", "done", "shipped", 0.1)

        self.assertFalse(State(self.path, "/repo").was_interrupted("abc"))

    def test_an_errored_task_does_not(self):
        state = State(self.path, "/repo")
        state.start_task("abc", "file", "- [ ] do it", "do it")
        state.finish_task("abc", "error", "no disk", 0.0)

        self.assertFalse(State(self.path, "/repo").was_interrupted("abc"))

    def test_a_task_that_never_ran_does_not(self):
        self.assertFalse(State(self.path, "/repo").was_interrupted("abc"))

    def test_another_repositorys_interruption_does_not_count(self):
        # `id` is the primary key on its own, so two repositories whose file
        # sources hold identical task text share a row. Answering yes here
        # would resume a session id belonging to the other repository.
        dying = State(self.path, "/repo/one")
        dying.start_task("abc", "file", "- [ ] do it", "do it")
        State(self.path, "/repo/one")  # flips /repo/one's row to interrupted

        self.assertFalse(State(self.path, "/repo/two").was_interrupted("abc"))


class TerminalIdsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state = State(self.tmp / "state.db")

    def finished(self, task_id: str, status: str) -> None:
        self.state.start_task(task_id, "jira", "OPS-1", "text")
        self.state.finish_task(task_id, status, "summary", 0.0)

    def test_collects_every_terminal_status(self):
        self.finished("aaaa", "done")
        self.finished("bbbb", "failed")
        self.finished("cccc", "blocked")
        self.assertEqual(self.state.terminal_ids(), {"aaaa", "bbbb", "cccc"})

    def test_running_and_interrupted_are_not_terminal(self):
        self.state.start_task("dddd", "jira", "OPS-2", "text")
        self.state.db.execute("UPDATE tasks SET status='interrupted' WHERE id='dddd'")
        self.state.start_task("eeee", "jira", "OPS-3", "text")
        self.assertEqual(self.state.terminal_ids(), set())

    def test_is_empty_on_a_fresh_database(self):
        self.assertEqual(self.state.terminal_ids(), set())

    def test_another_repositorys_finished_work_is_not_a_backstop(self):
        # One state.db, two repositories: a ticket finished elsewhere must not
        # suppress this loop's copy of it.
        other = State(self.tmp / "state.db", "/repo/other")
        other.start_task("aaaa", "jira", "OPS-1", "text")
        other.finish_task("aaaa", "done", "summary", 0.0)
        self.assertEqual(self.state.terminal_ids(), set())

    def test_terminal_ids_works_from_a_different_thread(self):
        # The loop calls terminal_ids() through asyncio.to_thread, i.e. from
        # a worker thread other than the one that created this State.
        # sqlite3 connections opened without check_same_thread=False raise
        # sqlite3.ProgrammingError when used off their creating thread -- the
        # live smoke test found this made the re-run backstop silently
        # inert, because JiraSource.pending() caught the error and carried
        # on as if state.db were unreadable.
        self.finished("aaaa", "done")
        result = asyncio.run(asyncio.to_thread(self.state.terminal_ids))
        self.assertEqual(result, {"aaaa"})


class BlockedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state = State(self.tmp / "state.db")

    def test_blocked_returns_what_a_task_can_be_rebuilt_from(self):
        self.state.start_task("aaaa", "jira", "OPS-1", "OPS-1: do a thing")
        self.state.finish_task("aaaa", "blocked", "stuck", 0.25, "which currency?")

        rows = self.state.blocked()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "aaaa")
        self.assertEqual(rows[0]["source"], "jira")
        self.assertEqual(rows[0]["source_ref"], "OPS-1")
        self.assertEqual(rows[0]["text"], "OPS-1: do a thing")
        self.assertEqual(rows[0]["question"], "which currency?")

    def test_blocked_ignores_every_other_status(self):
        for index, status in enumerate(("done", "failed", "error", "running")):
            self.state.start_task(f"id{index}", "file", "- [ ] x", "x")
            if status != "running":
                self.state.finish_task(f"id{index}", status, "", 0.0)

        self.assertEqual(self.state.blocked(), [])

    def test_blocked_ignores_another_repositorys_parked_task(self):
        # Otherwise this loop resumes a task parked against a different
        # repository, in a worktree cut from its own.
        other = State(self.tmp / "state.db", "/repo/other")
        other.start_task("aaaa", "jira", "OPS-1", "OPS-1: do a thing")
        other.finish_task("aaaa", "blocked", "stuck", 0.0, "which currency?")

        self.assertEqual(self.state.blocked(), [])

    def test_blocked_is_oldest_first(self):
        for key in ("first", "second"):
            self.state.start_task(key, "file", f"- [ ] {key}", key)
            self.state.finish_task(key, "blocked", "", 0.0, "?")
            time.sleep(0.01)

        self.assertEqual([row["id"] for row in self.state.blocked()],
                         ["first", "second"])

    def test_last_session_is_the_most_recent_run(self):
        self.state.start_task("aaaa", "file", "- [ ] x", "x")
        self.state.start_run("aaaa", "session-one", 0)
        self.state.start_run("aaaa", "session-two", 1)

        self.assertEqual(self.state.last_session("aaaa"), "session-two")

    def test_last_session_is_none_when_the_task_never_ran(self):
        self.state.start_task("aaaa", "file", "- [ ] x", "x")

        self.assertIsNone(self.state.last_session("aaaa"))

    def test_blocked_ignores_a_task_interrupted_by_a_dead_process(self):
        # State.__init__ rewrites 'running' to 'interrupted' when a previous
        # process died mid-task. That task never finished, so it is not
        # parked on a question.
        self.state.start_task("aaaa", "file", "- [ ] x", "x")

        reopened = State(self.tmp / "state.db")

        status = reopened.db.execute(
            "SELECT status FROM tasks WHERE id='aaaa'").fetchone()["status"]
        self.assertEqual(status, "interrupted")
        self.assertEqual(reopened.blocked(), [])


class TaskIdentityTest(unittest.TestCase):
    """`id` alone used to be the primary key, so two repositories whose file
    sources hold identical task text shared one row and start_task's INSERT OR
    REPLACE silently overwrote the other's."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "state.db"

    def test_two_repositories_keep_separate_rows_for_the_same_task_id(self):
        one = State(self.path, "/repo/one")
        two = State(self.path, "/repo/two")
        one.start_task("abc", "file", "- [ ] do it", "do it")
        one.finish_task("abc", "done", "one finished", 0.5)

        two.start_task("abc", "file", "- [ ] do it", "do it")

        self.assertEqual(one.task("abc")["status"], "done")
        self.assertEqual(one.task("abc")["summary"], "one finished")
        self.assertEqual(two.task("abc")["status"], "running")

    def test_task_reads_only_this_repositorys_row(self):
        one = State(self.path, "/repo/one")
        one.start_task("abc", "file", "- [ ] do it", "do it")

        self.assertIsNone(State(self.path, "/repo/two").task("abc"))

    def test_last_session_ignores_another_repositorys_runs(self):
        # What the caller does with a session id is --resume it, and that
        # session's transcript belongs to the other repository's worktree.
        one = State(self.path, "/repo/one")
        one.start_task("abc", "file", "- [ ] do it", "do it")
        one.start_run("abc", "session-one", 0)

        two = State(self.path, "/repo/two")
        two.start_task("abc", "file", "- [ ] do it", "do it")

        self.assertIsNone(two.last_session("abc"))
        self.assertEqual(one.last_session("abc"), "session-one")

    def test_a_database_with_the_old_single_column_key_is_rebuilt(self):
        conn = sqlite3.connect(self.path)
        conn.executescript(
            """
            CREATE TABLE tasks (
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
                question    TEXT,
                repo        TEXT
            );
            CREATE TABLE runs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id      TEXT NOT NULL REFERENCES tasks(id),
                session_id   TEXT NOT NULL,
                started_at   REAL NOT NULL,
                ended_at     REAL,
                exit_reason  TEXT,
                resume_count INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO tasks (id, source, source_ref, text, status, created_at,
                               cost_usd, repo)
            VALUES ('abc', 'file', '- [ ] x', 'x', 'blocked', 1.0, 0.25, '/repo/one');
            INSERT INTO runs (task_id, session_id, started_at)
            VALUES ('abc', 'session-one', 1.0);
            """
        )
        conn.close()

        state = State(self.path, "/repo/one")

        keyed = [
            row[1] for row in state.db.execute("PRAGMA table_info(tasks)") if row[5]
        ]
        self.assertEqual(sorted(keyed), ["id", "repo"])
        # The row and everything hanging off it survives the rebuild.
        row = state.task("abc")
        self.assertEqual(row["status"], "blocked")
        self.assertAlmostEqual(row["cost_usd"], 0.25)
        # runs gained `repo`, backfilled from the task it belongs to -- without
        # that a task parked across the upgrade loses the session it resumes.
        self.assertEqual(state.last_session("abc"), "session-one")

    def test_a_pre_repo_column_row_still_belongs_to_no_repository(self):
        conn = sqlite3.connect(self.path)
        conn.executescript(
            """
            CREATE TABLE tasks (
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
            INSERT INTO tasks (id, source, source_ref, text, status, created_at)
            VALUES ('old', 'file', '- [ ] old', 'old', 'done', 1.0);
            """
        )
        conn.close()

        state = State(self.path, "/repo")

        self.assertEqual(state.terminal_ids(), set())


class PriorCostTest(unittest.TestCase):
    """A task that parks and is later answered spans two run_task calls, and
    the second one used to overwrite the first one's cost with its own."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state = State(self.tmp / "state.db", "/repo")

    def test_prior_cost_is_what_the_task_has_already_spent(self):
        self.state.start_task("abc", "file", "- [ ] x", "x")
        self.state.finish_task("abc", "blocked", "stuck", 0.0395, "which currency?")

        self.assertAlmostEqual(self.state.prior_cost("abc"), 0.0395)

    def test_prior_cost_is_zero_for_a_task_that_never_ran(self):
        self.assertEqual(self.state.prior_cost("abc"), 0.0)

    def test_prior_cost_is_zero_when_no_cost_was_recorded(self):
        self.state.start_task("abc", "file", "- [ ] x", "x")

        self.assertEqual(self.state.prior_cost("abc"), 0.0)

    def test_prior_cost_ignores_another_repositorys_row(self):
        other = State(self.tmp / "state.db", "/repo/other")
        other.start_task("abc", "file", "- [ ] x", "x")
        other.finish_task("abc", "blocked", "stuck", 9.99, "?")

        self.assertEqual(self.state.prior_cost("abc"), 0.0)


class PermissionsTest(unittest.TestCase):
    """state.db carries task text, session summaries and the questions a
    session parked on. config.toml's own secrets guard refuses a world-readable
    file one step earlier; this is the same reasoning applied to the database
    and the directory holding it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def mode(self, path: Path) -> int:
        return path.stat().st_mode & 0o777

    @unittest.skipUnless(os.name == "posix", "posix modes")
    def test_the_database_is_owner_only(self):
        state = State(self.tmp / "home" / "state.db", "/repo")

        self.assertEqual(self.mode(self.tmp / "home" / "state.db"), 0o600)

    @unittest.skipUnless(os.name == "posix", "posix modes")
    def test_the_home_directory_is_owner_only(self):
        State(self.tmp / "home" / "state.db", "/repo")

        self.assertEqual(self.mode(self.tmp / "home"), 0o700)

    @unittest.skipUnless(os.name == "posix", "posix modes")
    def test_an_existing_world_readable_home_is_narrowed(self):
        home = self.tmp / "home"
        home.mkdir()
        home.chmod(0o755)

        State(home / "state.db", "/repo")

        self.assertEqual(self.mode(home), 0o700)


class CloseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_close_releases_the_connection(self):
        state = State(self.tmp / "state.db", "/repo")
        state.close()

        with self.assertRaises(sqlite3.ProgrammingError):
            state.db.execute("SELECT 1")

    def test_close_is_idempotent(self):
        state = State(self.tmp / "state.db", "/repo")
        state.close()
        state.close()  # a second close must not raise

    def test_a_dropped_state_closes_its_own_connection(self):
        # The suite used to emit ~80 ResourceWarnings about unclosed sqlite
        # connections, one per State that nobody closed. Every one of those
        # call sites is a test, so the fix belongs on State rather than on
        # eighty edits.
        state = State(self.tmp / "state.db", "/repo")
        connection = state.db
        del state
        gc.collect()

        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
