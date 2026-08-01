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
