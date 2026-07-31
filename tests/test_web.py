import json
import sqlite3
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from claudeloop import status, web
from claudeloop.config import Config
from claudeloop.state import State

SSE_SETTLE_S = 1.5  # comfortably longer than web.SSE_POLL_S


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
        # shutdown() only stops the serve_forever loop; it leaves the
        # listening socket open. Close it too, or every test in the module
        # leaks one fd for the life of the process.
        self.addCleanup(self.server.server_close)
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
        page = body.decode()
        self.assertIn("<!doctype html", page.lower())
        # No build step and no CDN: everything the page needs is in the file.
        self.assertNotIn("<script src=", page)
        self.assertNotIn("cdn.", page)
        self.assertIn("/api/events", page)
        self.assertIn("#fd7c33", page.lower())  # the brand accent is actually used

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


if __name__ == "__main__":
    unittest.main()
