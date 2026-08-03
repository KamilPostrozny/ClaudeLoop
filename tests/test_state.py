import asyncio
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


if __name__ == "__main__":
    unittest.main()
