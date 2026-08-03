import http.client
import json
import os
import socket
import tempfile
import tomllib
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from claudeloop import setup
from claudeloop.config import load_config

from .gitrepo import make_repo      # a real repo, one commit on main, gpgsign off
from .jira_fake import FakeJira     # routes {"POST /search/jql": (status, body)}


class DumpTomlTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)

    def roundtrip(self, data: dict) -> dict:
        text = setup.dump_toml(data)
        return tomllib.loads(text)

    def test_a_minimal_config_round_trips(self):
        data = {"repo": str(self.repo), "tasks_file": f"{self.tmp}/tasks.md"}
        self.assertEqual(self.roundtrip(data), data)

    def test_types_survive_the_trip(self):
        data = {
            "repo": str(self.repo),
            "tasks_file": f"{self.tmp}/tasks.md",
            "web_port": 8765,
            "session_timeout_s": 14400.0,
            "strict_mcp": False,
        }
        back = self.roundtrip(data)
        self.assertEqual(back["web_port"], 8765)
        self.assertIsInstance(back["web_port"], int)
        self.assertIs(back["strict_mcp"], False)
        self.assertEqual(back["session_timeout_s"], 14400.0)

    def test_tables_are_emitted(self):
        data = {
            "repo": str(self.repo),
            "source": "jira",
            "jira": {"site": "https://x.atlassian.net", "email": "a@b.c",
                     "token": "t", "project": "OPS"},
            "session_env": {"GH_TOKEN": "ghp_x"},
        }
        back = self.roundtrip(data)
        self.assertEqual(back["jira"]["project"], "OPS")
        self.assertEqual(back["session_env"]["GH_TOKEN"], "ghp_x")

    def test_a_value_with_quotes_and_backslashes_survives(self):
        # A Windows-shaped path or a JQL with a quoted status would break a
        # naive f'"{value}"'.
        nasty = 'a "quoted" \\ value\twith a tab'
        data = {"repo": str(self.repo), "tasks_file": f"{self.tmp}/t.md",
                "session_env": {"WEIRD": nasty}}
        self.assertEqual(self.roundtrip(data)["session_env"]["WEIRD"], nasty)

    def test_empty_values_are_omitted_not_emitted_blank(self):
        # An emitted `settings_file = ""` would be read back as a path that
        # does not exist, and load_config would then refuse the file the
        # wizard just wrote.
        data = {"repo": str(self.repo), "tasks_file": f"{self.tmp}/t.md",
                "settings_file": "", "web_token": ""}
        text = setup.dump_toml(data)
        self.assertNotIn("settings_file", text)
        self.assertNotIn("web_token", text)

    def test_help_text_is_emitted_as_comments(self):
        text = setup.dump_toml({"repo": str(self.repo),
                                "tasks_file": f"{self.tmp}/t.md"})
        self.assertIn("# ", text)
        self.assertIn("worktree", text)  # repo's help text

    def test_a_non_bmp_character_survives_the_trip(self):
        # json.dumps(ensure_ascii=True) encodes an emoji as a UTF-16 surrogate
        # pair of \uXXXX escapes, which TOML rejects as not a Unicode scalar
        # value -- and tomllib then fails on the whole file, not just this key.
        data = {"repo": str(self.repo), "tasks_file": f"{self.tmp}/t.md",
                "session_env": {"EMOJI": "hello \U0001F600 world"}}
        self.assertEqual(
            self.roundtrip(data)["session_env"]["EMOJI"], "hello \U0001F600 world"
        )

    def test_a_del_character_survives_the_trip(self):
        # ensure_ascii=False alone emits U+007F raw, which TOML forbids in a
        # basic string.
        data = {"repo": str(self.repo), "tasks_file": f"{self.tmp}/t.md",
                "session_env": {"DEL": "a\x7fb"}}
        self.assertEqual(self.roundtrip(data)["session_env"]["DEL"], "a\x7fb")

    def test_session_env_names_needing_quotes_round_trip_as_that_exact_key(self):
        # These names are operator input, not schema data. A bare key with a
        # space or a non-ASCII character breaks the whole file; a dot in a
        # bare key silently parses as a nested table instead of the literal
        # name the operator typed.
        data = {"repo": str(self.repo), "tasks_file": f"{self.tmp}/t.md",
                "session_env": {"a b": "1", "a.b": "2", "café": "3"}}
        back = self.roundtrip(data)["session_env"]
        self.assertEqual(back["a b"], "1")
        self.assertEqual(back["a.b"], "2")
        self.assertNotIn("a", back)  # not a nested {"a": {"b": "2"}}
        self.assertEqual(back["café"], "3")

    def test_what_the_wizard_writes_is_what_load_config_reads(self):
        # The whole claim of this slice in one assertion.
        data = {"repo": str(self.repo), "tasks_file": f"{self.tmp}/tasks.md",
                "model": "haiku", "web_port": 9000}
        path = self.tmp / "config.toml"
        path.write_text(setup.dump_toml(data))
        path.chmod(0o600)
        cfg = load_config(path, home=self.tmp / "home")
        self.assertEqual(cfg.repo, self.repo)
        self.assertEqual(cfg.model, "haiku")
        self.assertEqual(cfg.web_port, 9000)


class SetupServerBase(unittest.TestCase):
    """Fixture only -- no tests of its own.

    Deliberately not a test case other classes subclass: unittest would run
    every inherited test again in each subclass, and the first-run assertions
    below are false by construction in the editing subclass.
    """

    def existing_config(self) -> str:
        """config.toml as it stands before the wizard opens. "" is a first
        run. A method, not a class attribute, because the interesting cases
        interpolate paths setUp has only just created."""
        return ""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        # A bare `.git` is all validate() looks for. The live repo check in
        # CheckRouteTest makes a real repository of its own.
        (self.repo / ".git").mkdir(parents=True)
        self.path = self.tmp / "config.toml"
        body = self.existing_config()
        if body:
            self.path.write_text(body)
            self.path.chmod(0o600)
        self.token = "one-time-token"
        self.server = setup.serve(self.path, self.tmp / "home", 0, self.token)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def get(self, route, token="one-time-token"):
        url = self.base + route
        if token is not None:
            url += ("&" if "?" in route else "?") + "token=" + urllib.parse.quote(token)
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read()

    def post(self, route, payload, content_type="application/json",
             token="one-time-token", raw=None):
        url = self.base + route
        if token is not None:
            url += "?token=" + urllib.parse.quote(token)
        body = raw if raw is not None else json.dumps(payload).encode()
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            payload = json.loads(error.read() or b"{}")
            error.close()
            return error.code, payload


class FirstRunTest(SetupServerBase):
    def test_the_page_is_served(self):
        code, body = self.get("/")
        self.assertEqual(code, 200)
        page = body.decode()
        self.assertIn("<!doctype html", page.lower())
        # No build step and no CDN: everything the page needs is in the file.
        self.assertNotIn("<script src=", page)
        self.assertNotIn("cdn.", page)

    def test_the_one_time_token_is_required(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/", token=None)
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()

    def test_a_wrong_token_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/", token="guess")
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()

    def test_a_foreign_host_header_is_refused(self):
        # DNS rebinding: a page in a browser on this machine can point an
        # attacker-controlled hostname at 127.0.0.1 and still reach here.
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5)
        self.addCleanup(connection.close)
        connection.request("GET", f"/?token={self.token}",
                           headers={"Host": "evil.example:80"})
        self.assertEqual(connection.getresponse().status, 403)

    def test_the_schema_route_describes_every_field(self):
        _, body = self.get("/api/setup/schema")
        payload = json.loads(body)
        keys = [field["key"] for field in payload["fields"]]
        self.assertIn("repo", keys)
        self.assertIn("jira.site", keys)
        self.assertIn("strict_mcp", keys)
        for field in payload["fields"]:
            self.assertTrue(field["label"])
            self.assertTrue(field["help"])
            self.assertIn(field["step"], [step["id"] for step in payload["steps"]])

    def test_the_schema_route_says_this_is_a_first_run(self):
        payload = json.loads(self.get("/api/setup/schema")[1])
        self.assertFalse(payload["editing"])
        self.assertEqual(payload["values"], {})

    def test_an_unknown_route_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/nope")
        self.assertEqual(caught.exception.code, 404)
        caught.exception.close()


class EditingTest(SetupServerBase):
    def existing_config(self) -> str:
        return (
            f'repo = "{self.repo}"\n'
            'model = "haiku"\n'
            'web_token = "hunter2"\n'
            "[jira]\n"
            'token = "jira-secret"\n'
            "[session_env]\n"
            'GH_TOKEN = "ghp_secret"\n'
        )

    def test_existing_values_are_prefilled(self):
        payload = json.loads(self.get("/api/setup/schema")[1])
        self.assertTrue(payload["editing"])
        self.assertEqual(payload["values"]["model"], "haiku")

    def test_no_secret_ever_reaches_the_browser(self):
        # The wizard is exactly the screen an operator screenshots when
        # asking for help, and under S4 it is reached through Home Assistant
        # ingress, which logs.
        _, body = self.get("/api/setup/schema")
        self.assertNotIn(b"hunter2", body)
        self.assertNotIn(b"jira-secret", body)
        self.assertNotIn(b"ghp_secret", body)
        payload = json.loads(body)
        self.assertIn("web_token", payload["secrets_set"])
        self.assertIn("jira.token", payload["secrets_set"])
        self.assertEqual(payload["session_env"], {"GH_TOKEN": ""})


class ValidateRouteTest(SetupServerBase):
    def values(self, **extra) -> dict:
        return {"repo": str(self.repo), "tasks_file": f"{self.tmp}/tasks.md", **extra}

    def test_a_good_config_validates_clean(self):
        code, payload = self.post("/api/setup/validate", {"values": self.values()})
        self.assertEqual(code, 200)
        self.assertEqual(payload["errors"], {})

    def test_errors_come_back_keyed_by_field(self):
        code, payload = self.post("/api/setup/validate", {"values": {
            "repo": str(self.tmp / "nope"), "web_host": "0.0.0.0"}})
        self.assertEqual(code, 200)
        self.assertIn("repo", payload["errors"])
        self.assertIn("web_token", payload["errors"])
        self.assertIn("tasks_file", payload["errors"])

    def test_validate_writes_nothing(self):
        self.post("/api/setup/validate", {"values": self.values()})
        self.assertFalse(self.path.exists())

    def test_a_post_without_the_json_content_type_is_refused(self):
        code, _ = self.post("/api/setup/validate", {"values": self.values()},
                            content_type="text/plain")
        self.assertEqual(code, 415)

    def test_a_rejected_post_cannot_smuggle_a_second_request(self):
        # Inherited from web.Handler's do_POST, and pinned here because this
        # server writes config.toml: a cross-origin page could otherwise send
        # one CORS-safelisted text/plain POST whose body is a well-formed
        # application/json POST, and the second pass would clear every guard.
        inner_body = json.dumps({"values": self.values()})
        smuggled = (
            f"POST /api/setup/save?token={self.token} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.server.server_port}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(inner_body)}\r\n\r\n{inner_body}"
        )
        outer = (
            f"POST /api/setup/validate?token={self.token} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.server.server_port}\r\n"
            "Content-Type: text/plain;charset=UTF-8\r\n"
            f"Content-Length: {len(smuggled)}\r\n\r\n{smuggled}"
        )
        sock = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=5)
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
        self.assertFalse(self.path.exists(), "the smuggled POST wrote a config")


class SaveRouteTest(SetupServerBase):
    def values(self, **extra) -> dict:
        return {"repo": str(self.repo), "tasks_file": f"{self.tmp}/tasks.md", **extra}

    def test_saving_writes_a_config_load_config_accepts(self):
        code, payload = self.post("/api/setup/save", {"values": self.values(model="haiku")})
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])
        cfg = load_config(self.path, home=self.tmp / "home")
        self.assertEqual(cfg.model, "haiku")

    def test_the_file_is_written_0600(self):
        # It holds web_token, the Jira API token and every [session_env]
        # credential, and load_config refuses to read it at any other mode.
        self.post("/api/setup/save", {"values": self.values()})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_saving_releases_the_waiter(self):
        self.assertFalse(self.server.saved.is_set())
        self.post("/api/setup/save", {"values": self.values()})
        self.assertTrue(self.server.saved.wait(timeout=5))

    def test_an_invalid_config_is_not_written(self):
        code, payload = self.post("/api/setup/save",
                                  {"values": {"repo": str(self.tmp / "nope")}})
        self.assertEqual(code, 400)
        self.assertIn("repo", payload["errors"])
        self.assertFalse(self.path.exists())
        self.assertFalse(self.server.saved.is_set())


class SaveSecretsTest(SetupServerBase):
    def existing_config(self) -> str:
        return (
            f'repo = "{self.repo}"\n'
            f'tasks_file = "{self.tmp}/tasks.md"\n'
            'web_host = "0.0.0.0"\n'
            'web_token = "hunter2"\n'
            "[session_env]\n"
            'GH_TOKEN = "ghp_secret"\n'
        )

    def values(self, **extra) -> dict:
        return {"repo": str(self.repo), "tasks_file": f"{self.tmp}/tasks.md",
                "web_host": "0.0.0.0", **extra}

    def test_a_blank_secret_keeps_the_stored_value(self):
        # The browser was never told the token, so blank means "unchanged",
        # not "clear it" -- and web_host is non-loopback here, so clearing it
        # would fail validation outright.
        code, _ = self.post("/api/setup/save",
                            {"values": self.values(web_token="",
                                                   session_env={"GH_TOKEN": ""})})
        self.assertEqual(code, 200)
        cfg = load_config(self.path, home=self.tmp / "home")
        self.assertEqual(cfg.web_token, "hunter2")
        self.assertEqual(cfg.session_env["GH_TOKEN"], "ghp_secret")

    def test_a_new_secret_replaces_the_stored_one(self):
        code, _ = self.post("/api/setup/save",
                            {"values": self.values(web_token="rotated")})
        self.assertEqual(code, 200)
        cfg = load_config(self.path, home=self.tmp / "home")
        self.assertEqual(cfg.web_token, "rotated")

    def test_a_removed_session_env_name_is_dropped(self):
        # Blank keeps a value; omitting the name entirely removes the entry.
        code, _ = self.post("/api/setup/save",
                            {"values": self.values(web_token="", session_env={})})
        self.assertEqual(code, 200)
        cfg = load_config(self.path, home=self.tmp / "home")
        self.assertEqual(cfg.session_env, {})


class TypedWriteTest(SetupServerBase):
    """A browser form posts every field as a string. Saving the submission
    verbatim -- rather than validate()'s coerced values -- would put
    `web_port = "9999"` and `strict_mcp = "false"` in the file: quoted
    strings load_config only survives because its own coercion is lenient,
    and that come back to the wizard JS-truthy on the next --setup."""

    def values(self, **extra) -> dict:
        return {"repo": str(self.repo), "tasks_file": f"{self.tmp}/tasks.md", **extra}

    def test_browser_shaped_numbers_and_bools_are_written_unquoted(self):
        code, payload = self.post("/api/setup/save", {"values": self.values(
            web_port="9999", session_timeout_s="100", strict_mcp="false")})
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])
        text = self.path.read_text()
        self.assertIn("web_port = 9999", text)
        self.assertIn("strict_mcp = false", text)
        cfg = load_config(self.path, home=self.tmp / "home")
        self.assertEqual(cfg.web_port, 9999)
        self.assertIsInstance(cfg.web_port, int)
        self.assertIs(cfg.strict_mcp, False)

    def test_a_resaved_bool_prefills_as_a_real_bool_not_a_string(self):
        # The Task 7 landmine: schema_payload feeds the wizard's next
        # --setup run, and a JS-truthy "false" string would render a
        # checkbox checked for a field that is actually False.
        self.post("/api/setup/save", {"values": self.values(strict_mcp="false")})
        existing = setup._read_existing(self.path)
        payload = setup.schema_payload(existing)
        self.assertIs(payload["values"]["strict_mcp"], False)


class SaveOverExistingModeTest(SetupServerBase):
    def existing_config(self) -> str:
        return f'repo = "{self.repo}"\ntasks_file = "{self.tmp}/tasks.md"\n'

    def test_a_preexisting_0644_config_is_narrowed_to_0600(self):
        # os.open's mode argument only applies to a file it creates --
        # rewriting an existing config sitting at a looser mode must not
        # leave the secrets it is about to hold on disk world-readable, not
        # even for the length of the write.
        self.path.chmod(0o644)
        code, _ = self.post("/api/setup/save", {"values": {
            "repo": str(self.repo), "tasks_file": f"{self.tmp}/tasks.md",
            "model": "haiku"}})
        self.assertEqual(code, 200)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)


class JiraRetentionTest(SetupServerBase):
    def existing_config(self) -> str:
        return (
            f'repo = "{self.repo}"\n'
            f'tasks_file = "{self.tmp}/tasks.md"\n'
            "[jira]\n"
            'token = "jira-secret"\n'
        )

    def test_a_save_with_no_jira_key_keeps_the_stored_jira_token(self):
        # source = "file" here, so an ordinary save's submission never
        # mentions Jira at all -- that must not be read as "clear it".
        code, _ = self.post("/api/setup/save", {"values": {
            "repo": str(self.repo), "tasks_file": f"{self.tmp}/tasks.md"}})
        self.assertEqual(code, 200)
        data = tomllib.loads(self.path.read_text())
        self.assertEqual(data["jira"]["token"], "jira-secret")


class CheckRouteTest(SetupServerBase):
    def test_the_repo_check_passes_on_a_real_repository(self):
        # A real repository, not a bare .git directory: worktree.probe shells
        # out to `git worktree prune` and then resolves the default branch.
        repo = make_repo(self.tmp / "real")
        code, payload = self.post("/api/setup/test",
                                  {"what": "repo", "values": {"repo": str(repo)}})
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"], payload["message"])

    def test_the_repo_check_explains_a_directory_that_is_not_a_repository(self):
        (self.tmp / "plain").mkdir()
        _, payload = self.post("/api/setup/test",
                               {"what": "repo", "values": {"repo": str(self.tmp / "plain")}})
        self.assertFalse(payload["ok"])
        self.assertIn("worktree", payload["message"])

    def test_an_unknown_check_is_refused(self):
        code, payload = self.post("/api/setup/test",
                                  {"what": "astrology", "values": {}})
        self.assertEqual(code, 400)

    def test_the_claude_check_reports_what_the_cli_says(self):
        # A fake `claude` on PATH, the same technique the session tests use.
        fake = self.tmp / "bin"
        fake.mkdir()
        script = fake / "claude"
        script.write_text(
            "#!/bin/sh\n"
            'echo \'{"loggedIn": true, "authMethod": "claude.ai",'
            ' "subscriptionType": "pro"}\'\n'
        )
        script.chmod(0o755)
        old = os.environ["PATH"]
        os.environ["PATH"] = f"{fake}:{old}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", old))
        _, payload = self.post("/api/setup/test", {"what": "claude", "values": {}})
        self.assertTrue(payload["ok"], payload["message"])
        self.assertIn("claude.ai", payload["message"])

    def test_the_claude_check_says_so_when_the_cli_is_missing(self):
        old = os.environ["PATH"]
        os.environ["PATH"] = str(self.tmp / "empty")
        self.addCleanup(lambda: os.environ.__setitem__("PATH", old))
        _, payload = self.post("/api/setup/test", {"what": "claude", "values": {}})
        self.assertFalse(payload["ok"])
        self.assertIn("claude", payload["message"])

    def test_the_claude_check_applies_session_env(self):
        # A stray ANTHROPIC_API_KEY in [session_env] moves the session off
        # subscription billing, so the rate_limit_events the whole recovery
        # path is built on stop arriving. The check must see what a session
        # would see, not what this process happens to have.
        fake = self.tmp / "bin2"
        fake.mkdir()
        script = fake / "claude"
        script.write_text(
            "#!/bin/sh\n"
            'echo "{\\"loggedIn\\": true, \\"authMethod\\": \\"$ANTHROPIC_API_KEY\\"}"\n'
        )
        script.chmod(0o755)
        old = os.environ["PATH"]
        os.environ["PATH"] = f"{fake}:{old}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", old))
        _, payload = self.post("/api/setup/test", {
            "what": "claude",
            "values": {"session_env": {"ANTHROPIC_API_KEY": "leaked"}},
        })
        self.assertIn("leaked", payload["message"])

    def test_the_jira_check_reports_the_matching_issue_count(self):
        jira = FakeJira({"POST /search/jql": (
            200, {"issues": [{"key": "OPS-1"}, {"key": "OPS-2"}]})})
        self.addCleanup(jira.close)
        _, payload = self.post("/api/setup/test", {"what": "jira", "values": {
            "source": "jira",
            "jira": {"site": jira.url, "email": "a@b.c", "token": "t",
                     "project": "OPS"},
        }})
        self.assertTrue(payload["ok"], payload["message"])
        self.assertIn("2", payload["message"])

    def test_the_jira_check_sends_the_composed_query(self):
        # The label guard is spliced on by compose_jql. A check that reported
        # on a different query than the loop will actually poll with would be
        # worse than no check.
        jira = FakeJira({"POST /search/jql": (200, {"issues": []})})
        self.addCleanup(jira.close)
        self.post("/api/setup/test", {"what": "jira", "values": {
            "source": "jira",
            "jira": {"site": jira.url, "email": "a@b.c", "token": "t",
                     "project": "OPS", "status": "To Do"},
        }})
        _, _, body = jira.requests[-1]
        self.assertIn('project = "OPS"', body["jql"])
        self.assertIn("claudeloop-done", body["jql"])

    def test_the_jira_check_reports_a_rejected_token(self):
        jira = FakeJira({"POST /search/jql": (401, {"errorMessages": ["nope"]})})
        self.addCleanup(jira.close)
        _, payload = self.post("/api/setup/test", {"what": "jira", "values": {
            "source": "jira",
            "jira": {"site": jira.url, "email": "a@b.c", "token": "wrong",
                     "project": "OPS"},
        }})
        self.assertFalse(payload["ok"])
        self.assertIn("401", payload["message"])
