import base64
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

from unittest import mock

from claudeloop import setup
from claudeloop.config import INGRESS_ENV, load_config
from claudeloop.setup import STEPS, dump_toml, schema_payload

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

    def test_required_is_true_only_for_unconditionally_required_fields(self):
        # web_token, tasks_file and every jira.* key are only required in
        # some states (web_token: not loopback; tasks_file: source = "file";
        # jira.*: source = "jira") -- folding required_if into "required"
        # marked "Dashboard token *" as required even at the loopback
        # default, where it plainly is not. repo is the one field that is
        # unconditionally required.
        payload = json.loads(self.get("/api/setup/schema")[1])
        by_key = {field["key"]: field for field in payload["fields"]}
        self.assertTrue(by_key["repo"]["required"])
        self.assertFalse(by_key["web_token"]["required"])
        self.assertFalse(by_key["tasks_file"]["required"])
        self.assertFalse(by_key["jira.site"]["required"])

    def test_the_schema_route_says_this_is_a_first_run(self):
        payload = json.loads(self.get("/api/setup/schema")[1])
        self.assertFalse(payload["editing"])
        self.assertEqual(payload["values"], {})

    def test_an_unknown_route_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/nope")
        self.assertEqual(caught.exception.code, 404)
        caught.exception.close()


class IngressTest(SetupServerBase):
    """S5's two barriers are the loopback bind and the one-time console
    token. Under ingress both are replaced by supervisor authentication --
    which the operator has already passed, and which is the only reason the
    token can go: there is no query string to put it in on a sidebar link.
    See the S4 spec.

    The variable is set after serve(), because it is read per request; the
    bind half is pinned by IngressTest in tests/test_config.py."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.dict(os.environ, {INGRESS_ENV: "1"}))

    def test_the_page_is_served_with_no_token_at_all(self):
        code, _ = self.get("/", token=None)
        self.assertEqual(code, 200)

    def test_the_schema_route_is_served_with_no_token_at_all(self):
        code, body = self.get("/api/setup/schema", token=None)
        self.assertEqual(code, 200)
        self.assertIn("repo", [field["key"] for field in json.loads(body)["fields"]])

    def test_a_save_is_accepted_with_no_token_at_all(self):
        code, payload = self.post(
            "/api/setup/save",
            {"values": {"repo": str(self.repo), "tasks_file": str(self.tmp / "t.md")}},
            token=None,
        )
        self.assertEqual((code, payload.get("errors", {})), (200, {}))
        self.assertTrue(self.path.exists())

    def test_home_assistants_own_host_header_is_accepted(self):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5)
        self.addCleanup(connection.close)
        connection.request("GET", "/", headers={"Host": "homeassistant.local:8123"})
        self.assertEqual(connection.getresponse().status, 200)

    def test_both_barriers_come_back_without_the_variable(self):
        os.environ.pop(INGRESS_ENV)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/", token=None)
        self.assertEqual(caught.exception.code, 403)
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

    def test_a_json_array_body_is_answered_not_dropped(self):
        # _read_json's docstring promises "or None having already answered
        # with an error" -- the array branch used to return None without
        # writing anything, so the connection closed with zero bytes and the
        # browser's await response.json() rejected with nothing to show.
        code, payload = self.post("/api/setup/validate", None, raw=b"[1, 2, 3]")
        self.assertEqual(code, 400)
        self.assertTrue(payload.get("error"))

    def test_a_blank_required_repo_reports_required_not_remove(self):
        # The blank branch used to fire before required/required_if were
        # ever consulted, so a blank repo -- the one unconditionally
        # required field -- only ever got "remove the key or give it a
        # value". Removing repo is not a fix.
        code, payload = self.post("/api/setup/validate", {"values": {
            "repo": "   ", "tasks_file": f"{self.tmp}/tasks.md"}})
        self.assertEqual(code, 200)
        message = payload["errors"]["repo"]
        self.assertNotIn("remove the key", message)
        self.assertIn("required", message)

    def test_a_blank_web_token_when_exposed_reports_the_security_message(self):
        # web_host = "0.0.0.0" with a blank web_token used to get the same
        # "remove the key" advice -- actively wrong, since removing the key
        # still leaves the dashboard exposed. The message that matters is
        # required_error's, about a real credential being exposed.
        code, payload = self.post("/api/setup/validate", {"values": {
            **self.values(), "web_host": "0.0.0.0", "web_token": "   "}})
        self.assertEqual(code, 200)
        message = payload["errors"]["web_token"]
        self.assertNotIn("remove the key", message)
        self.assertIn("dashboard watches an agent", message)

    def test_a_blank_optional_field_still_reports_blank(self):
        # A blank field that is not required at all -- settings_file, here --
        # must keep today's message rather than picking up a required one.
        code, payload = self.post("/api/setup/validate", {"values": {
            **self.values(), "settings_file": "   "}})
        self.assertEqual(code, 200)
        message = payload["errors"]["settings_file"]
        self.assertIn("blank", message)
        self.assertIn("remove the key", message)

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

    def test_the_repo_check_asks_the_remote_when_repo_is_a_url(self):
        # Nothing is cloned during setup, so probe has nothing to look at:
        # the check has to reach the remote instead. file:// is a real URL to
        # git and needs no network.
        repo = make_repo(self.tmp / "remote")
        _, payload = self.post("/api/setup/test",
                               {"what": "repo", "values": {"repo": repo.as_uri()}})
        self.assertTrue(payload["ok"], payload["message"])
        self.assertIn("cloned", payload["message"])

    def test_the_repo_check_explains_a_url_it_cannot_reach(self):
        url = (self.tmp / "no-such-repo").as_uri()
        _, payload = self.post("/api/setup/test",
                               {"what": "repo", "values": {"repo": url}})
        self.assertFalse(payload["ok"])
        self.assertIn("cannot reach", payload["message"])

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

    def test_an_http_jira_site_is_refused_without_any_request(self):
        # _test() deliberately skips validate() before check_jira runs (see
        # check_jira's docstring), so config._https_site never gets a turn on
        # this route -- the one place in the project that actually puts the
        # Jira token on the wire. check_jira has to refuse http:// itself,
        # and refuse it before a single byte reaches the network: the fake
        # Jira below must see no request at all.
        jira = FakeJira({"POST /search/jql": (200, {"issues": []})})
        self.addCleanup(jira.close)
        evil_url = jira.url.replace("127.0.0.1", "evil.example")
        _, payload = self.post("/api/setup/test", {"what": "jira", "values": {
            "source": "jira",
            "jira": {"site": evil_url, "email": "a@b.c", "token": "SUPER-SECRET",
                     "project": "OPS"},
        }})
        self.assertFalse(payload["ok"])
        self.assertIn("https://", payload["message"])
        self.assertEqual(jira.requests, [])

    def test_a_malformed_jira_table_does_not_crash_the_jira_check(self):
        # merge_secrets already leaves a truthy non-dict "jira" section in
        # place rather than crashing (Task 6) -- but check_jira's own
        # `values.get("jira") or {}` only catches a falsy non-dict, so a
        # submitted "jira": "oops" still reached .get() on a str and killed
        # the request thread with no response at all.
        code, payload = self.post("/api/setup/test", {"what": "jira", "values": {
            "jira": "oops",
        }})
        self.assertEqual(code, 200)
        self.assertFalse(payload["ok"])

    def test_a_malformed_jira_table_does_not_crash_the_repo_check(self):
        # merge_secrets runs for every check, not just "jira" -- a submitted
        # "jira": "oops" must not take the request thread down before
        # check_repo ever runs. Pre-fix this drops the connection: no status,
        # no body, and even assertEqual(code, 200) never returns.
        repo = make_repo(self.tmp / "real2")
        code, payload = self.post("/api/setup/test", {"what": "repo", "values": {
            "repo": str(repo), "jira": "oops",
        }})
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"], payload["message"])

    def test_a_malformed_jira_table_is_reported_by_validate_not_silently_accepted(self):
        # The crash guard in merge_secrets must not turn into silent
        # acceptance. With source = "jira" the malformed table is equivalent
        # to an empty one, and every required jira.* key is then missing --
        # validate() still catches that, it just does not crash getting there.
        code, payload = self.post("/api/setup/validate", {"values": {
            "repo": str(self.repo), "tasks_file": f"{self.tmp}/tasks.md",
            "source": "jira", "jira": "oops",
        }})
        self.assertEqual(code, 200)
        self.assertIn("jira.site", payload["errors"])

    def test_the_claude_check_reports_a_non_object_json_answer_without_crashing(self):
        # json.loads accepts null, a list, a bare string -- none of them have
        # .get. A `claude` that is really a broken wrapper or a shell alias
        # can print any of these, and this check exists to catch exactly
        # that kind of misconfiguration, not crash on it.
        fake = self.tmp / "bin3"
        fake.mkdir()
        script = fake / "claude"
        script.write_text("#!/bin/sh\necho 'null'\n")
        script.chmod(0o755)
        old = os.environ["PATH"]
        os.environ["PATH"] = f"{fake}:{old}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", old))
        code, payload = self.post("/api/setup/test", {"what": "claude", "values": {}})
        self.assertEqual(code, 200)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["message"])


class CheckRouteSecretTest(SetupServerBase):
    """The stored Jira token, not the blank one just submitted, is what a
    live check actually authenticates with -- pinned separately from
    CheckRouteTest, whose fixture never has a stored secret to merge."""

    def existing_config(self) -> str:
        return (
            f'repo = "{self.repo}"\n'
            f'tasks_file = "{self.tmp}/tasks.md"\n'
            "[jira]\n"
            'token = "stored-secret-token"\n'
        )

    def test_a_blank_submitted_token_checks_with_the_stored_one(self):
        jira = FakeJira({"POST /search/jql": (200, {"issues": []})})
        self.addCleanup(jira.close)
        code, payload = self.post("/api/setup/test", {"what": "jira", "values": {
            "source": "jira",
            "jira": {"site": jira.url, "email": "a@b.c", "token": "",
                     "project": "OPS"},
        }})
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"], payload["message"])
        expected = "Basic " + base64.b64encode(b"a@b.c:stored-secret-token").decode()
        self.assertEqual(jira.authorizations[-1], expected)


class SetupBannerTest(unittest.TestCase):
    """The line an operator reads in their terminal before opening the wizard.

    Found by the live smoke test: --setup over a perfectly good config still
    announced "ClaudeLoop is not configured yet".
    """

    def banner(self, existing: dict) -> str:
        return (
            "Editing the ClaudeLoop configuration"
            if existing
            else "ClaudeLoop is not configured yet"
        )

    def test_a_first_run_says_it_is_not_configured(self):
        tmp = Path(tempfile.mkdtemp())
        server = setup.serve(tmp / "config.toml", tmp / "home", 0, "tok")
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.assertEqual(server.existing, {})

    def test_setup_over_an_existing_config_reports_it_as_editing(self):
        tmp = Path(tempfile.mkdtemp())
        path = tmp / "config.toml"
        path.write_text('repo = "/somewhere"\n')
        path.chmod(0o600)
        server = setup.serve(path, tmp / "home", 0, "tok")
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        # run_setup picks its wording off exactly this, so an existing
        # config must be visible here as a non-empty mapping.
        self.assertTrue(server.existing)
        self.assertEqual(server.existing["repo"], "/somewhere")


class WizardPageTest(SetupServerBase):
    def test_the_page_is_self_contained(self):
        page = self.get("/")[1].decode()
        self.assertNotIn("<script src=", page)
        self.assertNotIn("<link rel=\"stylesheet\"", page)
        self.assertNotIn("cdn.", page)
        self.assertNotIn("http://fonts", page)
        self.assertIn("#fd7c33", page.lower())  # the brand accent is used

    def test_the_page_names_every_step_and_route(self):
        page = self.get("/")[1].decode()
        for step in ("Repository", "Task source", "Dashboard", "Instructions",
                     "Advanced", "Review and save"):
            self.assertIn(step, page)
        for route in ("/api/setup/schema", "/api/setup/validate",
                      "/api/setup/test", "/api/setup/save"):
            self.assertIn(route, page)

    def test_the_page_carries_the_token_on_its_own_requests(self):
        # Every request needs the one-time token, and the page only ever has
        # it from its own URL.
        page = self.get("/")[1].decode()
        self.assertIn("location.search", page)

    def test_the_logo_and_favicon_are_never_requested_without_the_token(self):
        # A bare src/href attribute fires before any script runs, and every
        # route -- including /logo.png -- is token-gated in setup mode. A
        # static attribute pointing at the plain path 403s on every load.
        page = self.get("/")[1].decode()
        self.assertNotIn('src="/logo.png"', page)
        self.assertNotIn('href="/logo.png"', page)
        self.assertIn('url("/logo.png")', page)

    def test_the_review_step_can_render_errors_it_is_given(self):
        # A failed save answers with every error at once, and the Review
        # screen is the only one left to show them on -- renderReview must
        # accept and use them, not silently drop them like a no-arg
        # function would.
        page = self.get("/")[1].decode()
        self.assertIn("function renderReview(errors", page)
        self.assertIn("renderReview(errors)", page)

    def test_next_is_blocked_only_by_currently_rendered_fields(self):
        # A field's declared step is not the same as being on screen right
        # now: the [jira] block and tasks_file are also gated on `source`.
        # Judging Next's block on the declared step alone can wedge the
        # wizard on a field nothing renders.
        page = self.get("/")[1].decode()
        self.assertIn("visibleFields", page)
        self.assertNotIn("field.step === schema.steps[step].id", page)

    def test_a_save_error_with_no_field_is_shown_verbatim(self):
        # write_config failing (disk, permissions) answers {"error": ...},
        # not {"errors": {...}} -- there is no field to blame, so that
        # message must reach the operator rather than the generic line.
        page = self.get("/")[1].decode()
        self.assertIn("answer.error", page)

    def test_the_page_has_no_plugin_screen_left(self):
        page = (Path(__file__).parent.parent / "claudeloop" / "static"
                / "setup.html").read_text()
        self.assertNotIn("plugin", page.lower())
