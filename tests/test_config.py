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

    def test_rejects_a_tasks_file_directly_inside_repo(self):
        # No trace of ClaudeLoop should live in a repository it works in --
        # a session's ordinary branch hygiene (`git checkout .`, `git
        # stash`, `git checkout main`) can revert ClaudeLoop's own `- [x]`
        # mark, and the loop then re-runs work it already finished.
        path = self.write(
            f'repo = "{self.repo}"\n'
            f'tasks_file = "{self.repo}/tasks.md"\n'
        )
        with self.assertRaises(ValueError) as caught:
            load_config(path, home=self.tmp / "home")
        self.assertIn("tasks_file", str(caught.exception))

    def test_rejects_a_tasks_file_that_escapes_and_returns_via_dotdot(self):
        # A naive string-prefix check would miss this; resolving first (and
        # using is_relative_to) does not.
        path = self.write(
            f'repo = "{self.repo}"\n'
            f'tasks_file = "{self.repo}/../repo/tasks.md"\n'
        )
        with self.assertRaises(ValueError) as caught:
            load_config(path, home=self.tmp / "home")
        self.assertIn("tasks_file", str(caught.exception))

    def test_accepts_a_tasks_file_genuinely_outside_repo(self):
        path = self.write(
            f'repo = "{self.repo}"\n'
            f'tasks_file = "{self.tmp}/tasks.md"\n'
        )
        cfg = load_config(path, home=self.tmp / "home")
        self.assertEqual(cfg.tasks_file, self.tmp / "tasks.md")

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


class JiraConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.home = self.tmp / "home"

    def write(self, body: str) -> Path:
        path = self.tmp / "config.toml"
        path.write_text(f'repo = "{self.repo}"\n{body}')
        path.chmod(0o600)
        return path

    JIRA = (
        'source = "jira"\n'
        "[jira]\n"
        'site = "https://example.atlassian.net"\n'
        'email = "me@example.com"\n'
        'token = "secret"\n'
        'jql = "project = OPS ORDER BY created"\n'
    )

    def test_loads_a_jira_source(self):
        cfg = load_config(self.write(self.JIRA), home=self.home)
        self.assertEqual(cfg.source, "jira")
        self.assertEqual(cfg.jira.site, "https://example.atlassian.net")
        self.assertEqual(cfg.jira.email, "me@example.com")
        self.assertEqual(cfg.jira.token, "secret")
        self.assertEqual(cfg.jira.jql, "project = OPS ORDER BY created")
        self.assertEqual(cfg.jira.transition_start, "")
        self.assertEqual(cfg.jira.transition_done, "")

    def test_jira_needs_no_tasks_file(self):
        cfg = load_config(self.write(self.JIRA), home=self.home)
        self.assertIsNone(cfg.tasks_file)

    def test_transitions_are_optional_and_carried_when_present(self):
        cfg = load_config(
            self.write(self.JIRA + 'transition_start = "In Progress"\n'
                                   'transition_done = "Done"\n'),
            home=self.home,
        )
        self.assertEqual(cfg.jira.transition_start, "In Progress")
        self.assertEqual(cfg.jira.transition_done, "Done")

    def test_defaults_to_the_file_source(self):
        tasks = self.tmp / "tasks.md"
        tasks.write_text("")
        cfg = load_config(self.write(f'tasks_file = "{tasks}"\n'), home=self.home)
        self.assertEqual(cfg.source, "file")
        self.assertEqual(cfg.tasks_file, tasks)
        self.assertIsNone(cfg.jira)

    def test_an_unknown_source_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            load_config(self.write('source = "github"\n'), home=self.home)
        self.assertIn("source", str(caught.exception))

    def test_the_file_source_still_requires_tasks_file(self):
        with self.assertRaises(ValueError) as caught:
            load_config(self.write('source = "file"\n'), home=self.home)
        self.assertIn("tasks_file", str(caught.exception))

    def test_the_jira_source_requires_the_jira_table(self):
        with self.assertRaises(ValueError) as caught:
            load_config(self.write('source = "jira"\n'), home=self.home)
        self.assertIn("[jira]", str(caught.exception))

    def test_project_composes_a_query_so_nobody_has_to_write_jql(self):
        cfg = load_config(self.write(
            'source = "jira"\n[jira]\n'
            'site = "https://example.atlassian.net"\n'
            'email = "me@example.com"\n'
            'token = "secret"\n'
            'project = "OPS"\n'
        ), home=self.home)
        self.assertEqual(cfg.jira.jql, 'project = "OPS" ORDER BY created ASC')

    def test_status_narrows_the_composed_query(self):
        cfg = load_config(self.write(
            'source = "jira"\n[jira]\n'
            'site = "https://example.atlassian.net"\n'
            'email = "me@example.com"\n'
            'token = "secret"\n'
            'project = "OPS"\nstatus = "To Do"\n'
        ), home=self.home)
        self.assertEqual(
            cfg.jira.jql,
            'project = "OPS" AND status = "To Do" ORDER BY created ASC',
        )

    def test_an_explicit_jql_wins_over_the_shorthand(self):
        cfg = load_config(self.write(self.JIRA + 'project = "OTHER"\n'), home=self.home)
        self.assertEqual(cfg.jira.jql, "project = OPS ORDER BY created")

    def test_neither_jql_nor_project_is_refused_by_name(self):
        with self.assertRaises(ValueError) as caught:
            load_config(self.write(
                'source = "jira"\n[jira]\n'
                'site = "https://example.atlassian.net"\n'
                'email = "me@example.com"\n'
                'token = "secret"\n'
            ), home=self.home)
        self.assertIn("jql", str(caught.exception))
        self.assertIn("project", str(caught.exception))

    def test_each_missing_jira_key_is_named(self):
        for key in ("site", "email", "token", "jql"):
            with self.subTest(key=key):
                body = "".join(line + "\n" for line in self.JIRA.splitlines()
                               if not line.startswith(f"{key} ="))
                with self.assertRaises(ValueError) as caught:
                    load_config(self.write(body), home=self.home)
                self.assertIn(key, str(caught.exception))

    def test_a_tasks_file_inside_the_repo_is_still_refused_under_jira(self):
        # tasks_file must come before [jira] opens -- TOML assigns any key
        # after a table header to that table, not the root, so appending it
        # after self.JIRA would silently make it jira.tasks_file instead of
        # the top-level key this guard checks.
        inside = self.repo / "tasks.md"
        inside.write_text("")
        with self.assertRaises(ValueError):
            load_config(self.write(f'tasks_file = "{inside}"\n' + self.JIRA),
                        home=self.home)


if __name__ == "__main__":
    unittest.main()
