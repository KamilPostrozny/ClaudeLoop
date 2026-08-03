import tempfile
import unittest
from pathlib import Path

from claudeloop.config import SCHEMA, Config, Field, JiraConfig, load_config, validate


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

    def test_load_config_reports_the_first_error_with_the_file_path(self):
        # Every message a human sees names the file it came from -- the
        # common install failure is a config.toml at the default umask, and
        # that operator must get the message, not a traceback.
        path = self.write(
            f'repo = "{self.tmp}/nope"\n'
            f'tasks_file = "{self.tmp}/tasks.md"\n'
        )
        with self.assertRaises(ValueError) as caught:
            load_config(path, home=self.tmp / "home")
        self.assertIn(str(path), str(caught.exception))

    def test_the_jira_shorthand_still_composes_a_query(self):
        path = self.write(
            f'repo = "{self.repo}"\n'
            'source = "jira"\n'
            "[jira]\n"
            'site = "https://x.atlassian.net"\n'
            'email = "a@b.c"\n'
            'token = "t"\n'
            'project = "OPS"\n'
            'status = "To Do"\n'
        )
        cfg = load_config(path, home=self.tmp / "home")
        self.assertEqual(cfg.jira.jql, 'project = "OPS" AND status = "To Do" ORDER BY created ASC')

    def test_session_timeout_s_default_is_a_float(self):
        # validate() writes field.default verbatim on the absent path, and
        # 4 * 3600 is a bare int -- 14400 == 14400.0 so assertEqual would not
        # catch a regression here, only assertIsInstance does.
        path = self.write(
            f'repo = "{self.repo}"\n'
            f'tasks_file = "{self.tmp}/tasks.md"\n'
        )
        cfg = load_config(path, home=self.tmp / "home")
        self.assertIsInstance(cfg.session_timeout_s, float)

    def test_plugins_defaults_to_nothing_selected(self):
        path = self.write(
            f'repo = "{self.repo}"\n'
            f'tasks_file = "{self.tmp}/tasks.md"\n'
        )
        cfg = load_config(path, home=self.tmp / "home")
        self.assertEqual(cfg.plugins, ())

    def test_plugins_reads_a_list(self):
        path = self.write(
            f'repo = "{self.repo}"\n'
            f'tasks_file = "{self.tmp}/tasks.md"\n'
            'plugins = ["superpowers", "caveman"]\n'
        )
        cfg = load_config(path, home=self.tmp / "home")
        self.assertEqual(cfg.plugins, ("superpowers", "caveman"))

    def test_plugins_accepts_a_comma_separated_string(self):
        # A hand-edited `plugins = "superpowers"` must not become a list of
        # eleven characters.
        values, errors = validate({"repo": str(self.repo),
                                   "tasks_file": str(self.tmp / "tasks.md"),
                                   "plugins": "superpowers, caveman"})
        self.assertEqual(errors, [])
        self.assertEqual(values["plugins"], ("superpowers", "caveman"))

    def test_plugins_drops_blank_entries(self):
        values, _ = validate({"repo": str(self.repo),
                              "tasks_file": str(self.tmp / "tasks.md"),
                              "plugins": ["superpowers", "", "  "]})
        self.assertEqual(values["plugins"], ("superpowers",))

    def test_plugins_rejects_a_name_outside_the_proposed_set(self):
        # Caught here rather than at startup hours later: the wizard can show
        # this while the operator is still looking at the screen.
        _, errors = validate({"repo": str(self.repo),
                              "tasks_file": str(self.tmp / "tasks.md"),
                              "plugins": ["superpowers", "nonesuch"]})
        self.assertEqual([key for key, _ in errors], ["plugins"])
        self.assertIn("nonesuch", errors[0][1])
        self.assertIn("plugin@marketplace", errors[0][1])

    def test_plugins_accepts_an_explicit_plugin_at_marketplace(self):
        values, errors = validate({"repo": str(self.repo),
                                   "tasks_file": str(self.tmp / "tasks.md"),
                                   "plugins": ["mine@market"]})
        self.assertEqual(errors, [])
        self.assertEqual(values["plugins"], ("mine@market",))

    def test_plugins_rejects_a_table(self):
        _, errors = validate({"repo": str(self.repo),
                              "tasks_file": str(self.tmp / "tasks.md"),
                              "plugins": {"superpowers": True}})
        self.assertEqual([key for key, _ in errors], ["plugins"])


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

    def test_a_blank_settings_file_is_refused_but_an_absent_one_is_not(self):
        # "" is what the wizard posts for every field left untouched, and
        # must keep meaning absent -- but "   " is a present, blank value:
        # silently dropping it to None would undercut _must_exist's whole
        # point, which exists to catch exactly this class of typo.
        with self.assertRaises(ValueError) as caught:
            load_config(self.write('settings_file = "   "\n'), home=self.home)
        message = str(caught.exception)
        self.assertIn("settings_file", message)
        self.assertIn("blank", message)

        cfg = load_config(self.write(), home=self.home)
        self.assertIsNone(cfg.settings_file)

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

    def test_a_non_https_site_is_refused(self):
        # urllib's redirect handler forwards the Authorization header across
        # hosts, so an http:// typo puts the Basic-auth API token on the
        # wire in cleartext the first time Jira redirects it.
        with self.assertRaises(ValueError) as caught:
            load_config(self.write(
                'source = "jira"\n[jira]\n'
                'site = "http://example.atlassian.net"\n'
                'email = "me@example.com"\n'
                'token = "secret"\n'
                'project = "OPS"\n'
            ), home=self.home)
        self.assertIn("https://", str(caught.exception))

    def test_an_unused_jira_table_does_not_block_a_file_source_config(self):
        # A [jira] table left behind after switching source back to "file"
        # -- or typed and abandoned while trying "jira" -- must not block a
        # config that never reads it. Same site, still refused under
        # source = "jira".
        tasks = self.tmp / "tasks.md"
        tasks.write_text("")
        cfg = load_config(self.write(
            f'source = "file"\ntasks_file = "{tasks}"\n'
            '[jira]\nsite = "http://x"\n'
        ), home=self.home)
        self.assertEqual(cfg.source, "file")
        with self.assertRaises(ValueError):
            load_config(self.write(
                'source = "jira"\n[jira]\n'
                'site = "http://x"\nemail = "a@b.c"\ntoken = "t"\nproject = "OPS"\n'
            ), home=self.home)

    def test_each_missing_jira_key_is_named(self):
        for key in ("site", "email", "token", "jql"):
            with self.subTest(key=key):
                body = "".join(line + "\n" for line in self.JIRA.splitlines()
                               if not line.startswith(f"{key} ="))
                with self.assertRaises(ValueError) as caught:
                    load_config(self.write(body), home=self.home)
                self.assertIn(key, str(caught.exception))

    def test_multiple_missing_jira_keys_are_all_named_in_one_message(self):
        # The old code reported every missing [jira] key in one message. An
        # operator fixing one key per run, only to discover the next on
        # re-run, is the regression this pins.
        with self.assertRaises(ValueError) as caught:
            load_config(self.write('source = "jira"\n[jira]\n'), home=self.home)
        message = str(caught.exception)
        self.assertIn("site", message)  # the lead error
        self.assertIn("jira.email", message)
        self.assertIn("jira.token", message)
        self.assertIn("jira.project", message)
        self.assertIn("more problem", message)

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


class SchemaTest(unittest.TestCase):
    """The table is the single source of truth for both load_config and the
    setup wizard. These pin the walk itself; the ConfigTest cases above pin
    what load_config does with it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)

    def minimal(self, **extra) -> dict:
        return {"repo": str(self.repo), "tasks_file": f"{self.tmp}/tasks.md", **extra}

    def test_a_valid_config_produces_no_errors_and_coerced_values(self):
        values, errors = validate(self.minimal(max_resumes="7", strict_mcp=False))
        self.assertEqual(errors, [])
        self.assertEqual(values["repo"], self.repo)
        self.assertIsInstance(values["repo"], Path)
        self.assertEqual(values["max_resumes"], 7)
        self.assertEqual(values["model"], "opus")  # the default landed

    def test_every_error_is_collected_not_just_the_first(self):
        # The wizard marks up a whole form at once; raising on the first
        # would make it a one-error-per-round-trip guessing game.
        values, errors = validate({
            "repo": str(self.tmp / "nope"),
            "source": "jira",
            "web_host": "0.0.0.0",
        })
        keys = [key for key, _ in errors]
        self.assertIn("repo", keys)
        self.assertIn("web_token", keys)
        self.assertIn("jira.site", keys)
        self.assertGreaterEqual(len(errors), 3)

    def test_errors_are_keyed_by_field_key(self):
        _, errors = validate(self.minimal(source="jira"))
        keys = dict(errors)
        self.assertIn("jira.site", keys)
        self.assertIn("[jira]", keys["jira.site"])

    def test_a_non_numeric_number_is_an_error_not_a_crash(self):
        _, errors = validate(self.minimal(max_waits="soon"))
        self.assertEqual([key for key, _ in errors], ["max_waits"])

    def test_a_bad_choice_names_the_alternatives(self):
        _, errors = validate(self.minimal(source="carrier pigeon"))
        key, message = errors[0]
        self.assertEqual(key, "source")
        self.assertIn("file", message)
        self.assertIn("jira", message)

    def test_an_empty_string_counts_as_absent(self):
        # The wizard submits "" for every field the operator left alone.
        values, errors = validate(self.minimal(model="", settings_file=""))
        self.assertEqual(errors, [])
        self.assertEqual(values["model"], "opus")
        self.assertIsNone(values["settings_file"])

    def test_a_false_boolean_is_not_absent(self):
        values, errors = validate(self.minimal(strict_mcp=False))
        self.assertEqual(errors, [])
        self.assertIs(values["strict_mcp"], False)

    def test_jira_needs_project_or_jql_and_says_so(self):
        _, errors = validate({"repo": str(self.repo), "source": "jira",
                              "jira": {"site": "https://x.atlassian.net",
                                       "email": "a@b.c", "token": "t"}})
        message = dict(errors)["jira.project"]
        self.assertIn("jql", message)
        self.assertIn("project", message)

    def test_an_explicit_jql_removes_the_project_requirement(self):
        _, errors = validate({"repo": str(self.repo), "source": "jira",
                              "jira": {"site": "https://x.atlassian.net",
                                       "email": "a@b.c", "token": "t",
                                       "jql": "project = OPS"}})
        self.assertEqual(errors, [])

    def test_a_condition_only_sees_fields_declared_before_it(self):
        # SCHEMA order is load-bearing: web_token's required_if reads
        # web_host, tasks_file's check reads repo, strict_mcp's check reads
        # mcp_config. Anything that reorders the table must fail here.
        order = [field.key for field in SCHEMA]
        for earlier, later in (("repo", "tasks_file"), ("source", "tasks_file"),
                               ("web_host", "web_token"), ("jira.jql", "jira.project"),
                               ("mcp_config", "strict_mcp")):
            self.assertLess(order.index(earlier), order.index(later),
                            f"{earlier} must be declared before {later}")

    def test_every_field_carries_help_text_for_the_wizard(self):
        for field in SCHEMA:
            self.assertTrue(field.help.strip(), f"{field.key} has no help text")
            self.assertTrue(field.label.strip(), f"{field.key} has no label")

    def test_the_table_and_the_Config_dataclass_agree(self):
        # Not a bijection, and the exceptions are named rather than left to
        # be rediscovered: jira.project and jira.status are composed away
        # into jira.jql by _compose_jql and never reach Config, and `home`
        # is a load_config parameter that is never a config key.
        composed_away = {"jira.project", "jira.status"}
        not_a_config_key = {"home", "jira"}
        top_level = {f.name for f in SCHEMA if not f.section}
        jira_keys = {f.name for f in SCHEMA if f.section == "jira"}
        self.assertEqual(
            top_level | {"session_env"},
            set(Config.__dataclass_fields__) - not_a_config_key,
        )
        self.assertEqual(
            {f"jira.{name}" for name in jira_keys} - composed_away,
            {f"jira.{name}" for name in JiraConfig.__dataclass_fields__},
        )

    def test_secret_fields_are_marked(self):
        secret = {field.key for field in SCHEMA if field.secret}
        self.assertEqual(secret, {"web_token", "jira.token"})


if __name__ == "__main__":
    unittest.main()
