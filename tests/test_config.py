import tempfile
import unittest
from pathlib import Path

from claudeloop.config import Config, load_config


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)

    def write(self, body: str) -> Path:
        path = self.tmp / "config.toml"
        path.write_text(body)
        path.chmod(0o600)
        return path

    def test_reads_values_and_applies_defaults(self):
        path = self.write(
            f'repo = "{self.repo}"\n'
            f'tasks_file = "{self.tmp}/tasks.md"\n'
        )
        cfg = load_config(path, home=self.tmp / "home")
        self.assertEqual(cfg.repo, self.repo)
        self.assertEqual(cfg.tasks_file, self.tmp / "tasks.md")
        self.assertEqual(cfg.model, "opus")
        self.assertEqual(cfg.max_resumes, 20)
        self.assertEqual(cfg.max_waits, 200)
        self.assertEqual(cfg.session_timeout_s, 4 * 3600)
        self.assertEqual(cfg.home, self.tmp / "home")

    def test_overrides_defaults(self):
        path = self.write(
            f'repo = "{self.repo}"\n'
            f'tasks_file = "{self.tmp}/tasks.md"\n'
            'model = "sonnet"\n'
            'max_resumes = 3\n'
            'max_waits = 50\n'
            'session_timeout_s = 60\n'
        )
        cfg = load_config(path, home=self.tmp / "home")
        self.assertEqual(cfg.model, "sonnet")
        self.assertEqual(cfg.max_resumes, 3)
        self.assertEqual(cfg.max_waits, 50)
        self.assertEqual(cfg.session_timeout_s, 60.0)

    def test_rejects_missing_required_key(self):
        path = self.write(f'repo = "{self.repo}"\n')
        with self.assertRaises(ValueError) as caught:
            load_config(path, home=self.tmp / "home")
        self.assertIn("tasks_file", str(caught.exception))

    def test_rejects_repo_that_is_not_a_git_checkout(self):
        path = self.write(
            f'repo = "{self.tmp}/nope"\n'
            f'tasks_file = "{self.tmp}/tasks.md"\n'
        )
        with self.assertRaises(ValueError) as caught:
            load_config(path, home=self.tmp / "home")
        self.assertIn("git repository", str(caught.exception))

    def test_config_is_frozen(self):
        with self.assertRaises(Exception):
            Config(repo=Path("/a"), tasks_file=Path("/b")).model = "x"


class WebConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)

    def write(self, extra: str = "") -> Path:
        path = self.tmp / "config.toml"
        path.write_text(
            f'repo = "{self.repo}"\n'
            f'tasks_file = "{self.tmp}/tasks.md"\n' + extra
        )
        path.chmod(0o600)
        return path

    def test_web_defaults_are_loopback(self):
        cfg = load_config(self.write(), home=self.tmp / "home")
        self.assertEqual(cfg.web_host, "127.0.0.1")
        self.assertEqual(cfg.web_port, 8765)
        self.assertEqual(cfg.web_token, "")

    def test_web_values_are_read(self):
        cfg = load_config(
            self.write('web_host = "0.0.0.0"\nweb_port = 9000\nweb_token = "s3cret"\n'),
            home=self.tmp / "home",
        )
        self.assertEqual(cfg.web_host, "0.0.0.0")
        self.assertEqual(cfg.web_port, 9000)
        self.assertEqual(cfg.web_token, "s3cret")

    def test_non_loopback_without_a_token_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            load_config(self.write('web_host = "0.0.0.0"\n'), home=self.tmp / "home")
        self.assertIn("web_token", str(caught.exception))

    def test_non_loopback_with_a_blank_token_is_refused(self):
        with self.assertRaises(ValueError):
            load_config(
                self.write('web_host = "192.168.1.5"\nweb_token = "   "\n'),
                home=self.tmp / "home",
            )

    def test_a_non_ascii_token_is_refused_at_load(self):
        # secrets.compare_digest, used to check web_token on every request,
        # requires ASCII-only str and raises TypeError otherwise. Catching
        # this here means a bad config fails loudly once at startup instead
        # of on every single request forever.
        with self.assertRaises(ValueError) as caught:
            load_config(
                self.write('web_token = "pässwort"\n'), home=self.tmp / "home"
            )
        self.assertIn("web_token", str(caught.exception))

    def test_loopback_without_a_token_is_fine(self):
        for host in ("127.0.0.1", "localhost", "::1"):
            cfg = load_config(
                self.write(f'web_host = "{host}"\n'), home=self.tmp / "home"
            )
            self.assertEqual(cfg.web_host, host)


class SessionEnvironmentConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.home = self.tmp / "home"

    def write(self, extra: str = "", mode: int = 0o600) -> Path:
        path = self.tmp / "config.toml"
        path.write_text(
            f'repo = "{self.repo}"\n'
            f'tasks_file = "{self.tmp}/tasks.md"\n' + extra
        )
        path.chmod(mode)
        return path

    def test_instruction_paths_default_under_home(self):
        cfg = load_config(self.write(), home=self.home)
        self.assertEqual(cfg.instructions_file, self.home / "instructions.md")
        self.assertEqual(cfg.definition_of_done_file, self.home / "definition-of-done.md")

    def test_instruction_paths_can_be_overridden(self):
        cfg = load_config(
            self.write(
                f'instructions_file = "{self.tmp}/mine.md"\n'
                f'definition_of_done_file = "{self.tmp}/dod.md"\n'
            ),
            home=self.home,
        )
        self.assertEqual(cfg.instructions_file, self.tmp / "mine.md")
        self.assertEqual(cfg.definition_of_done_file, self.tmp / "dod.md")

    def test_plugin_and_mcp_keys_default_to_unset(self):
        cfg = load_config(self.write(), home=self.home)
        self.assertIsNone(cfg.settings_file)
        self.assertIsNone(cfg.mcp_config)
        self.assertFalse(cfg.strict_mcp)

    def test_plugin_and_mcp_keys_are_read(self):
        (self.tmp / "settings.json").write_text("{}")
        (self.tmp / "mcp.json").write_text("{}")
        cfg = load_config(
            self.write(
                f'settings_file = "{self.tmp}/settings.json"\n'
                f'mcp_config = "{self.tmp}/mcp.json"\n'
                "strict_mcp = true\n"
            ),
            home=self.home,
        )
        self.assertEqual(cfg.settings_file, self.tmp / "settings.json")
        self.assertEqual(cfg.mcp_config, self.tmp / "mcp.json")
        self.assertTrue(cfg.strict_mcp)

    def test_strict_mcp_without_mcp_config_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            load_config(self.write("strict_mcp = true\n"), home=self.home)
        self.assertIn("mcp_config", str(caught.exception))

    def test_a_settings_file_that_does_not_exist_is_refused(self):
        # load_config validates repo up front precisely so a typo surfaces
        # at startup rather than making `claude` exit immediately on every
        # single task, with main_loop retrying forever and never marking the
        # task -- check settings_file the same way.
        with self.assertRaises(ValueError) as caught:
            load_config(
                self.write(f'settings_file = "{self.tmp}/nope-settings.json"\n'),
                home=self.home,
            )
        self.assertIn("settings_file", str(caught.exception))

    def test_an_mcp_config_that_does_not_exist_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            load_config(
                self.write(f'mcp_config = "{self.tmp}/nope-mcp.json"\n'), home=self.home
            )
        self.assertIn("mcp_config", str(caught.exception))

    def test_session_env_defaults_empty(self):
        self.assertEqual(load_config(self.write(), home=self.home).session_env, {})

    def test_session_env_is_read_as_strings(self):
        cfg = load_config(
            self.write(
                "[session_env]\n"
                'GH_TOKEN = "ghp_abc"\n'
                'GIT_CONFIG_COUNT = 1\n'
            ),
            home=self.home,
        )
        self.assertEqual(cfg.session_env, {"GH_TOKEN": "ghp_abc", "GIT_CONFIG_COUNT": "1"})

    def test_session_env_rejects_a_nested_table(self):
        with self.assertRaises(ValueError) as caught:
            load_config(
                self.write("[session_env.nested]\nA = \"b\"\n"), home=self.home
            )
        self.assertIn("session_env", str(caught.exception))

    def test_a_group_readable_config_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            load_config(self.write(mode=0o640), home=self.home)
        message = str(caught.exception)
        self.assertIn("chmod 600", message)

    def test_a_world_readable_config_is_refused(self):
        with self.assertRaises(ValueError):
            load_config(self.write(mode=0o644), home=self.home)

    def test_an_owner_only_config_is_accepted(self):
        cfg = load_config(self.write(mode=0o600), home=self.home)
        self.assertEqual(cfg.repo, self.repo)


if __name__ == "__main__":
    unittest.main()
