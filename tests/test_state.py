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
