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
