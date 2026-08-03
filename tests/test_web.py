import http.client
import json
import os
import socket
import sqlite3
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from claudeloop import config, status, web
from claudeloop.config import Config
from claudeloop.source import task_id
from claudeloop.state import State

SSE_SETTLE_S = 1.5  # comfortably longer than web.SSE_POLL_S


class WebTestBase(unittest.TestCase):
    token = ""
    web_host = "127.0.0.1"

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
            web_host=self.web_host,
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
            url += ("&" if "?" in path else "?") + "token=" + urllib.parse.quote(token)
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
        caught.exception.close()

    def test_the_logo_is_cached_long_lived(self):
        # It's 1.66MB served to a phone on every page load; worth paying for
        # once rather than every 3s poll cycle's page load.
        with urllib.request.urlopen(self.base + "/logo.png", timeout=5) as response:
            cache_control = response.headers.get("Cache-Control", "")
        self.assertIn("max-age", cache_control)

    def test_the_index_page_is_not_cached(self):
        with urllib.request.urlopen(self.base + "/", timeout=5) as response:
            self.assertIsNone(response.headers.get("Cache-Control"))

    def test_the_status_vocabulary_matches_what_the_database_produces(self):
        # state.py's tasks.status can be done/failed/blocked/interrupted;
        # the page used to only know question (dead -- never produced) and
        # was silent on blocked and interrupted.
        page = self.get("/")[1].decode()
        self.assertIn('data-status="blocked"', page)
        self.assertIn('data-status="interrupted"', page)
        self.assertNotIn('data-status="question"', page)
        self.assertIn("blocked:", page)
        self.assertIn("interrupted:", page)


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

    def test_pending_comes_from_the_status_snapshot(self):
        # Not the task file: under the Jira source cfg.tasks_file is None, so
        # web must never re-read a task source itself. The loop publishes the
        # list it already computed onto the snapshot each poll.
        status.set_status(pending=(("aaa", "first thing"), ("bbb", "second thing")))
        payload = self.get_json("/api/state")
        self.assertEqual(
            [t["text"] for t in payload["pending"]], ["first thing", "second thing"]
        )

    def test_the_running_task_is_not_listed_as_pending(self):
        # Otherwise the same task shows as both in-flight (the beacon) and
        # queued (position 01), and inflates "Pending N" for the whole run.
        status.set_status(
            state="running",
            task_id=task_id("first thing"),
            pending=((task_id("first thing"), "first thing"),),
        )
        payload = self.get_json("/api/state")
        self.assertEqual(payload["pending"], [])

    def test_completed_comes_from_the_database(self):
        state = State(self.cfg.home / "state.db", str(self.cfg.repo))
        state.start_task("abc", "file", "- [ ] done thing", "done thing")
        state.finish_task("abc", "done", "went fine", 1.5)
        payload = self.get_json("/api/state")
        self.assertEqual(len(payload["completed"]), 1)
        self.assertEqual(payload["completed"][0]["status"], "done")
        self.assertAlmostEqual(payload["completed"][0]["cost_usd"], 1.5)

    def test_another_repositorys_finished_tasks_are_not_listed(self):
        # One state.db per machine, one dashboard per repository: a fresh
        # config pointed at a new repository must not open onto the history
        # of whatever ran here before it.
        other = State(self.cfg.home / "state.db", str(self.tmp / "other-repo"))
        other.start_task("abc", "file", "- [ ] old thing", "old thing")
        other.finish_task("abc", "done", "went fine", 1.5)
        self.assertEqual(self.get_json("/api/state")["completed"], [])

    def test_a_database_without_the_repo_column_is_not_an_error(self):
        # The dashboard is served before main_loop opens State, so a database
        # written by a version without `repo` can be queried before the
        # migration has run.
        path = self.cfg.home / "state.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT)")
        conn.commit()
        conn.close()
        self.assertEqual(self.get_json("/api/state")["completed"], [])

    def test_a_running_task_is_not_in_the_completed_list(self):
        state = State(self.cfg.home / "state.db", str(self.cfg.repo))
        state.start_task("abc", "file", "- [ ] x", "x")
        self.assertEqual(self.get_json("/api/state")["completed"], [])

    def test_a_missing_database_is_not_an_error(self):
        self.assertEqual(self.get_json("/api/state")["completed"], [])

    def test_a_blocked_task_never_ages_out_of_the_completed_list(self):
        # state.blocked() keeps a parked task forever and the loop will
        # still resume it, but api_state only returns the most recent
        # RECENT_TASKS rows -- so a task parked before RECENT_TASKS newer
        # ones finished used to fall off the dashboard and become
        # unanswerable under the file source, whose only channel is the
        # answer box in this same list.
        state = State(self.cfg.home / "state.db", str(self.cfg.repo))
        state.start_task("old-blocked", "file", "- [ ] old", "old")
        state.finish_task("old-blocked", "blocked", "waiting", 0.1, question="ok?")
        for i in range(web.RECENT_TASKS):
            tid = f"newer-{i}"
            state.start_task(tid, "file", f"- [ ] {i}", str(i))
            state.finish_task(tid, "done", "fine", 0.1)
        completed = self.get_json("/api/state")["completed"]
        self.assertEqual(len(completed), web.RECENT_TASKS)
        self.assertIn("old-blocked", [t["id"] for t in completed])


class JiraSourceStateTest(unittest.TestCase):
    """Regression: under source = "jira", cfg.tasks_file is None. api_state
    used to build a FileSource(None) to compute pending, and
    FileSource.pending() only catches FileNotFoundError -- so this raised
    AttributeError on every request and took the whole dashboard down
    whenever Jira was the backlog. Fails against the pre-fix code."""

    def setUp(self):
        status.reset()
        self.addCleanup(status.reset)
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "repo" / ".git").mkdir(parents=True)
        self.cfg = Config(
            repo=self.tmp / "repo",
            tasks_file=None,
            home=self.tmp / "home",
            source="jira",
        )

    def test_api_state_does_not_raise_and_pending_is_empty(self):
        payload = web.api_state(self.cfg)
        self.assertEqual(payload["pending"], [])

    def test_pending_published_by_the_loop_still_comes_through(self):
        status.set_status(pending=(("abc", "do the jira thing"),))
        payload = web.api_state(self.cfg)
        self.assertEqual([t["text"] for t in payload["pending"]], ["do the jira thing"])


class TaskRouteTest(WebTestBase):
    def seed(self):
        state = State(self.cfg.home / "state.db", str(self.cfg.repo))
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
        caught.exception.close()

    def test_a_task_id_that_is_not_a_hash_is_refused(self):
        for bad in ("..", "../../etc/passwd", "abc", "0123456789ABCDEF"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.get(f"/api/tasks/{bad}")
            self.assertEqual(caught.exception.code, 404)
            caught.exception.close()


class TokenTest(WebTestBase):
    token = "s3cret"

    def test_the_right_token_is_accepted(self):
        self.assertEqual(self.get("/api/state", token="s3cret")[0], 200)

    def test_a_missing_token_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/api/state")
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()

    def test_a_wrong_token_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/api/state", token="wrong")
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()

    def test_a_non_ascii_token_is_refused_not_raised(self):
        # secrets.compare_digest requires ASCII-only str; parse_qs happily
        # decodes a non-ASCII query value, which used to escape as an
        # uncaught TypeError instead of a plain 403.
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/api/state", token="é")
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()


class HostHeaderTest(WebTestBase):
    def _request(self, host_header: str) -> int:
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("GET", "/api/state", headers={"Host": host_header})
        response = conn.getresponse()
        response.read()
        return response.status

    def test_the_configured_host_and_port_are_accepted(self):
        self.assertEqual(self._request(f"127.0.0.1:{self.server.server_port}"), 200)

    def test_a_rebound_hostname_is_rejected(self):
        # The scenario this defends: a page open in a browser on this same
        # machine gets DNS-rebound so "evil.example" resolves to 127.0.0.1.
        # The browser still sends the hostname it started with in Host.
        self.assertEqual(self._request(f"evil.example:{self.server.server_port}"), 403)

    def test_the_right_host_with_the_wrong_port_is_rejected(self):
        self.assertEqual(self._request(f"127.0.0.1:{self.server.server_port + 1}"), 403)

    def test_a_non_numeric_port_is_rejected_not_raised(self):
        # urlparse(...).port raises ValueError on "abc"; that used to escape
        # _host_allowed as an uncaught exception instead of a plain 403.
        self.assertEqual(self._request("localhost:abc"), 403)

    def test_an_out_of_range_port_is_rejected_not_raised(self):
        # Same ValueError, from a port outside 0-65535.
        self.assertEqual(self._request("127.0.0.1:99999"), 403)


class WildcardHostHeaderTest(WebTestBase):
    # web_host = "0.0.0.0" means the token (config.py refuses to start
    # otherwise) is the guard, not Host -- a real remote client's Host names
    # its own address, never the wildcard, so Host can't be compared against
    # a single allowed value the way it is for a specific bind.
    web_host = "0.0.0.0"

    def _request(self, host_header: str) -> int:
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("GET", "/api/state", headers={"Host": host_header})
        response = conn.getresponse()
        response.read()
        return response.status

    def test_any_host_at_the_right_port_is_accepted(self):
        self.assertEqual(self._request(f"192.168.1.5:{self.server.server_port}"), 200)

    def test_the_wrong_port_is_still_rejected(self):
        self.assertEqual(self._request(f"192.168.1.5:{self.server.server_port + 1}"), 403)


class IngressTest(WebTestBase):
    """Behind Home Assistant's ingress proxy the three guards that assume a
    browser talking to this server directly are all replaced -- see the S4
    spec. The variable is set *after* serve(), because it is read per request:
    binding here would mean binding the real ingress port, and the point of
    reading it late is that a test does not have to."""

    token = "a-token-nobody-sends"

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.dict(os.environ, {config.INGRESS_ENV: "1"}))

    def _request(self, host_header: str, path: str = "/api/state") -> int:
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("GET", path, headers={"Host": host_header})
        response = conn.getresponse()
        response.read()
        return response.status

    def test_home_assistants_own_host_header_is_accepted(self):
        # What the supervisor forwards: the Host the browser sent to Home
        # Assistant, which names neither this server nor its port.
        self.assertEqual(self._request("homeassistant.local:8123"), 200)

    def test_the_token_is_not_required(self):
        # web_token is set and no request here carries it: the supervisor
        # authenticated the user before this request existed, and a sidebar
        # entry has no query string to put it in.
        self.assertEqual(self._request("homeassistant.local:8123", "/"), 200)

    def test_both_guards_come_back_without_the_variable(self):
        # The same server, the same request: only ingress mode relaxes it.
        os.environ.pop(config.INGRESS_ENV)
        self.assertEqual(self._request("homeassistant.local:8123"), 403)


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

    def test_a_log_far_larger_than_the_cap_still_yields_the_correct_tail(self):
        # Regression for the full-file read this used to do: with a cap far
        # smaller than the file, the seek is forced to land inside a line,
        # and the result must still be exactly the trailing entries -- no
        # broken/partial entry from the line the seek cut into.
        path = Path(tempfile.mkdtemp()) / "events.jsonl"
        path.write_text(
            "".join(
                '{"type":"assistant","message":{"content":[{"type":"text","text":"%d"}]}}\n' % i
                for i in range(500)
            )
        )
        old_cap = web.TAIL_CAP_BYTES
        web.TAIL_CAP_BYTES = 500  # ~7 lines' worth; file is ~37KB
        try:
            entries = web.read_log(path, 5)
        finally:
            web.TAIL_CAP_BYTES = old_cap
        self.assertEqual([e["text"] for e in entries], ["495", "496", "497", "498", "499"])


class TailTest(unittest.TestCase):
    """web._tail is the primitive both read_log and _replay lean on to avoid
    reading a multi-day event log whole just to serve its last few entries."""

    def test_a_cap_larger_than_the_file_reads_it_whole(self):
        path = Path(tempfile.mkdtemp()) / "log.jsonl"
        path.write_bytes(b"a\nb\nc\n")
        data, offset = web._tail(path, 1_000_000)
        self.assertEqual(data, b"a\nb\nc\n")
        self.assertEqual(offset, 6)

    def test_a_trailing_partial_line_is_left_for_the_next_read(self):
        path = Path(tempfile.mkdtemp()) / "log.jsonl"
        path.write_bytes(b"a\nb\npartial")
        data, offset = web._tail(path, 1_000_000)
        self.assertEqual(data, b"a\nb\n")
        self.assertEqual(offset, 4)

    def test_seeking_into_the_middle_of_a_line_drops_only_that_fragment(self):
        path = Path(tempfile.mkdtemp()) / "log.jsonl"
        path.write_bytes(b"".join(b"line-%03d\n" % i for i in range(100)))  # 9 bytes/line
        data, offset = web._tail(path, 50)
        self.assertEqual(offset, path.stat().st_size)  # file ends mid-newline, cleanly
        lines = data.splitlines()
        self.assertTrue(lines)
        for line in lines:
            self.assertTrue(line.startswith(b"line-"), f"broken fragment leaked: {line!r}")
        # The last line is always whole and present.
        self.assertEqual(lines[-1], b"line-099")

    def test_a_single_line_longer_than_the_cap_yields_nothing_usable(self):
        path = Path(tempfile.mkdtemp()) / "log.jsonl"
        path.write_bytes(b"x" * 1000)  # one giant line, no newline anywhere
        data, offset = web._tail(path, 100)
        self.assertEqual(data, b"")
        self.assertEqual(offset, 1000)

    def test_a_missing_file_raises_oserror(self):
        with self.assertRaises(OSError):
            web._tail(Path("/nonexistent/events.jsonl"), 100)


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

    def test_the_tail_of_a_finished_run_is_not_dropped(self):
        # main_loop clears run_dir to None within milliseconds of a task
        # finishing -- well inside one 0.5s poll -- so the pump used to skip
        # draining the last write entirely: the closing prose and the
        # cost/duration line were simply never seen.
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

        self.assertEqual(next_entry()["kind"], "run")
        self.assertEqual(next_entry()["text"], "first")

        # Write the closing line and flip run_dir to None back to back, with
        # no sleep -- reproducing the finish happening inside one poll tick.
        with open(path, "a") as handle:
            handle.write(
                '{"type":"assistant","message":'
                '{"content":[{"type":"text","text":"final"}]}}\n'
            )
        status.set_status(run_dir=None)

        deadline = time.time() + 10
        while time.time() < deadline:
            entry = next_entry()
            if entry.get("kind") == "ping":
                continue
            self.assertEqual(entry.get("text"), "final", f"the closing line was dropped: {entry}")
            return
        self.fail("the closing line never arrived")


class AnswerRouteTest(WebTestBase):
    def setUp(self):
        super().setUp()
        self.state = State(self.cfg.home / "state.db", str(self.cfg.repo))
        self.task_id = task_id("ambiguous thing")
        self.state.start_task(self.task_id, "file", "- [ ] ambiguous thing",
                              "ambiguous thing")
        self.state.finish_task(self.task_id, "blocked", "stuck", 0.1, "which currency?")

    def post(self, path: str, body, content_type="application/json", token=None):
        """Returns (status, decoded body). http.client, not urllib: urllib
        raises on a 4xx and the status code is the thing under test."""
        url = path + (("?token=" + urllib.parse.quote(token)) if token else "")
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            raw = body if isinstance(body, (bytes, str)) else json.dumps(body)
            headers = {"Content-Type": content_type} if content_type else {}
            conn.request("POST", url, raw, headers)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def answer_file(self) -> Path:
        return self.cfg.home / "runs" / self.task_id / "answer.json"

    def answer_path(self) -> str:
        """The route, carrying the token when the subclass sets one."""
        path = f"/api/tasks/{self.task_id}/answer"
        return path + (f"?token={urllib.parse.quote(self.token)}" if self.token else "")

    def test_an_answer_is_written_where_the_loop_looks_for_it(self):
        code, _ = self.post(f"/api/tasks/{self.task_id}/answer", {"answer": "use EUR"})

        self.assertEqual(code, 200)
        payload = json.loads(self.answer_file().read_text())
        self.assertEqual(payload["answer"], "use EUR")
        self.assertIsInstance(payload["at"], float)

    def test_the_answer_is_stripped(self):
        self.post(f"/api/tasks/{self.task_id}/answer", {"answer": "  use EUR \n"})

        self.assertEqual(json.loads(self.answer_file().read_text())["answer"], "use EUR")

    def test_a_task_that_is_not_blocked_is_refused(self):
        self.state.finish_task(self.task_id, "done", "did it", 0.1)

        code, _ = self.post(f"/api/tasks/{self.task_id}/answer", {"answer": "use EUR"})

        self.assertEqual(code, 409)
        self.assertFalse(self.answer_file().exists())

    def test_an_unknown_task_is_refused(self):
        code, _ = self.post("/api/tasks/" + ("0" * 16) + "/answer", {"answer": "x"})

        self.assertEqual(code, 409)

    def test_a_bad_task_id_never_reaches_the_filesystem(self):
        code, _ = self.post("/api/tasks/..%2f..%2fetc/answer", {"answer": "x"})

        self.assertEqual(code, 404)

    def test_a_form_content_type_is_refused(self):
        # A cross-origin fetch sending application/json triggers a CORS
        # preflight this server never answers, and an HTML form cannot set
        # that content type. This is what stops a drive-by submission.
        code, _ = self.post(f"/api/tasks/{self.task_id}/answer",
                            "answer=use+EUR",
                            content_type="application/x-www-form-urlencoded")

        self.assertEqual(code, 415)
        self.assertFalse(self.answer_file().exists())

    def test_an_empty_answer_is_refused(self):
        code, _ = self.post(f"/api/tasks/{self.task_id}/answer", {"answer": "   "})

        self.assertEqual(code, 400)
        self.assertFalse(self.answer_file().exists())

    def test_a_body_that_is_not_an_answer_object_is_refused(self):
        code, _ = self.post(f"/api/tasks/{self.task_id}/answer", {"nope": "x"})

        self.assertEqual(code, 400)

    def test_an_oversized_answer_is_refused(self):
        code, _ = self.post(f"/api/tasks/{self.task_id}/answer",
                            {"answer": "x" * (web.ANSWER_MAX_BYTES + 1)})

        self.assertEqual(code, 413)
        self.assertFalse(self.answer_file().exists())

    def test_an_answer_that_is_not_a_string_is_refused(self):
        code, _ = self.post(f"/api/tasks/{self.task_id}/answer", {"answer": None})

        self.assertEqual(code, 400)
        self.assertFalse(self.answer_file().exists())

    def test_an_unknown_post_route_is_a_404(self):
        code, _ = self.post("/api/nonsense", {"answer": "x"})

        self.assertEqual(code, 404)

    def test_a_rejected_post_cannot_smuggle_a_second_request(self):
        # A rejection that does not consume the request body leaves those
        # bytes to be parsed as the next request on the same keep-alive
        # connection -- and the body is attacker-controlled. One
        # CORS-safelisted text/plain POST carrying a well-formed JSON POST
        # in its body would otherwise clear every guard on the second pass.
        smuggled_body = json.dumps({"answer": "SMUGGLED PAST"})
        smuggled = (
            f"POST {self.answer_path()} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.server.server_port}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(smuggled_body)}\r\n"
            "\r\n"
            f"{smuggled_body}"
        )
        outer = (
            f"POST {self.answer_path()} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.server.server_port}\r\n"
            "Content-Type: text/plain;charset=UTF-8\r\n"
            f"Content-Length: {len(smuggled)}\r\n"
            "\r\n"
            f"{smuggled}"
        )

        sock = socket.create_connection(
            ("127.0.0.1", self.server.server_port), timeout=5)
        self.addCleanup(sock.close)
        sock.sendall(outer.encode())
        sock.settimeout(2)
        received = b""
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                received += chunk
        except (TimeoutError, OSError):
            pass

        self.assertNotIn(b"200 OK", received, received)
        self.assertFalse(self.answer_file().exists(), "the smuggled POST was served")


class AnswerRouteTokenTest(AnswerRouteTest):
    token = "s3cret"

    def post(self, path, body, content_type="application/json", token="s3cret"):
        return super().post(path, body, content_type, token)

    def test_the_answer_route_needs_the_token(self):
        code, _ = super().post(f"/api/tasks/{self.task_id}/answer",
                               {"answer": "use EUR"}, token=None)

        self.assertEqual(code, 403)
        self.assertFalse(self.answer_file().exists())

    def test_the_right_token_is_accepted_on_the_answer_route(self):
        code, _ = self.post(f"/api/tasks/{self.task_id}/answer",
                            {"answer": "use EUR"}, token="s3cret")

        self.assertEqual(code, 200)


if __name__ == "__main__":
    unittest.main()
