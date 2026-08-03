import tempfile
import tomllib
import unittest
from pathlib import Path

from claudeloop import setup
from claudeloop.config import load_config


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
