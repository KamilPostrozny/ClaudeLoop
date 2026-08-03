import os
import shutil
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from claudeloop.plugins import (
    PROPOSED,
    Plugin,
    by_name,
    reconcile,
    usage_section,
)


class TableTest(unittest.TestCase):
    def test_the_proposed_set_is_two_plugins_in_a_fixed_order(self):
        self.assertEqual(
            [plugin.name for plugin in PROPOSED],
            ["caveman", "ponytail"],
        )

    def test_every_proposed_plugin_carries_an_id_marketplace_and_reason(self):
        for plugin in PROPOSED:
            self.assertIn("@", plugin.plugin_id, plugin.name)
            self.assertTrue(plugin.marketplace, plugin.name)
            # The wizard renders this next to the checkbox; a blank one is a
            # checkbox with no argument for ticking it.
            self.assertTrue(plugin.reason, plugin.name)

    def test_no_proposed_plugin_carries_usage_text(self):
        # caveman and ponytail contribute nothing to the prompt: both already
        # state their own rules, and a second copy here would drift out of
        # step with the plugin. The layer is the operator's to fill.
        self.assertEqual([plugin.name for plugin in PROPOSED if plugin.usage], [])

    def test_by_name_finds_a_proposed_plugin_and_nothing_else(self):
        self.assertEqual(by_name("caveman").plugin_id, "caveman@caveman")
        self.assertIsNone(by_name("nonesuch"))
        self.assertIsNone(by_name(""))

    def test_by_name_also_resolves_the_fully_qualified_plugin_id(self):
        # `claude plugin list` prints ids in this form, and README.md tells
        # operators to spell anything outside the built-in three this way --
        # it must resolve to the same proposed plugin as the short name.
        self.assertIs(by_name("caveman@caveman"), by_name("caveman"))


class UsageSectionTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())

    def test_no_selection_renders_nothing(self):
        self.assertEqual(usage_section((), self.home), "")

    def test_a_selection_with_no_usage_text_renders_nothing(self):
        # No empty "## Plugin usage" header over two plugins that say nothing.
        self.assertEqual(usage_section(("caveman", "ponytail"), self.home), "")

    def test_a_plugin_with_text_renders_a_header_and_its_block(self):
        one = Plugin("aaa", "aaa@m", "m", reason="r", usage="A text")
        text = usage_section(("aaa",), self.home, proposed=(one,))
        self.assertTrue(text.startswith("## Plugin usage\n\n### aaa\n\n"))
        self.assertIn("A text", text)

    def test_a_fully_qualified_selection_still_composes_its_block(self):
        # plugins = ["aaa@m"] is the exact spelling `claude plugin list`
        # prints and README.md invites for anything outside the built-in
        # set -- it must not silently lose the fourth prompt layer.
        one = Plugin("aaa", "aaa@m", "m", reason="r", usage="A text")
        text = usage_section(("aaa@m",), self.home, proposed=(one,))
        self.assertTrue(text.startswith("## Plugin usage\n\n### aaa\n\n"))
        self.assertIn("A text", text)

    def test_blocks_follow_proposed_order_not_selection_order(self):
        one = Plugin("aaa", "aaa@m", "m", reason="r", usage="A text")
        two = Plugin("zzz", "zzz@m", "m", reason="r", usage="Z text")
        text = usage_section(("zzz", "aaa"), self.home, proposed=(one, two))
        self.assertLess(text.index("### aaa"), text.index("### zzz"))

    def test_a_plugin_usage_file_replaces_the_built_in_text(self):
        one = Plugin("aaa", "aaa@m", "m", reason="r", usage="A text")
        directory = self.home / "plugin-usage"
        directory.mkdir()
        (directory / "aaa.md").write_text("operator's own wording\n")
        text = usage_section(("aaa",), self.home, proposed=(one,))
        self.assertIn("operator's own wording", text)
        self.assertNotIn("A text", text)

    def test_a_usage_file_gives_a_proposed_plugin_its_only_block(self):
        # No proposed plugin ships usage text now, so this file is the whole
        # fourth layer for one of them.
        directory = self.home / "plugin-usage"
        directory.mkdir()
        (directory / "caveman.md").write_text("keep it terse\n")
        text = usage_section(("caveman", "ponytail"), self.home)
        self.assertEqual(text, "## Plugin usage\n\n### caveman\n\nkeep it terse")

    def test_a_usage_file_gives_a_plugin_outside_the_set_a_block(self):
        directory = self.home / "plugin-usage"
        directory.mkdir()
        (directory / "mine@market.md").write_text("how to use mine\n")
        text = usage_section(("mine@market",), self.home)
        self.assertIn("### mine@market\n\nhow to use mine", text)

    def test_an_unreadable_usage_file_falls_back_to_the_built_in(self):
        # Same rule prompt._read already follows: a layer is optional, and a
        # session must not fail to start over a permissions mistake.
        one = Plugin("aaa", "aaa@m", "m", reason="r", usage="A text")
        directory = self.home / "plugin-usage"
        directory.mkdir()
        path = directory / "aaa.md"
        path.write_text("unreadable")
        path.chmod(0o000)
        self.addCleanup(path.chmod, 0o600)
        self.assertIn("A text", usage_section(("aaa",), self.home, proposed=(one,)))


class ReconcileTest(unittest.TestCase):
    """Against a fake `claude` on PATH, the same harness tests/test_loop.py
    uses for the real CLI."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        shutil.copy(Path(__file__).parent / "fake_claude_plugin.sh",
                    bin_dir / "claude")
        (bin_dir / "claude").chmod(0o755)
        self.state = self.tmp / "state.txt"
        self.calls = self.tmp / "calls.txt"
        self.state.write_text("")
        patch = unittest.mock.patch.dict(os.environ, {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_PLUGIN_STATE": str(self.state),
            "FAKE_PLUGIN_CALLS": str(self.calls),
        })
        patch.start()
        self.addCleanup(patch.stop)

    def calls_made(self) -> list[str]:
        if not self.calls.exists():
            return []
        return [line for line in self.calls.read_text().splitlines() if line]

    def test_an_empty_selection_runs_no_command_at_all(self):
        self.assertIsNone(reconcile(()))
        self.assertEqual(self.calls_made(), [])

    def test_an_already_installed_and_enabled_plugin_touches_nothing(self):
        self.state.write_text("ponytail@ponytail\n")
        self.assertIsNone(reconcile(("ponytail",)))
        # One read, and no network: this is the steady state on every start,
        # so a marketplace outage must not be able to stop the loop.
        self.assertEqual(self.calls_made(), ["plugin list --json"])

    def test_a_disabled_plugin_is_enabled_not_reinstalled(self):
        self.state.write_text("!caveman@caveman\n")
        self.assertIsNone(reconcile(("caveman",)))
        self.assertIn("plugin enable caveman@caveman --scope user",
                      self.calls_made())
        self.assertNotIn("install", " ".join(self.calls_made()))

    def test_a_missing_plugin_adds_its_marketplace_then_installs_it(self):
        self.assertIsNone(reconcile(("ponytail",)))
        calls = self.calls_made()
        self.assertIn("plugin marketplace add DietrichGebert/ponytail --scope user", calls)
        self.assertIn("plugin install ponytail@ponytail --scope user", calls)
        self.assertLess(
            calls.index("plugin marketplace add DietrichGebert/ponytail --scope user"),
            calls.index("plugin install ponytail@ponytail --scope user"))

    def test_a_fully_qualified_proposed_plugin_installs_exactly_once(self):
        # plugins = ["caveman@caveman"]: by_name must resolve this to the
        # real proposed plugin (with its marketplace), not fall through to
        # reconcile's unknown-plugin fallback, which would install the same
        # id a second time under a fake Plugin with no marketplace.
        self.assertIsNone(reconcile(("caveman@caveman",)))
        calls = self.calls_made()
        self.assertIn("plugin marketplace add JuliusBrussee/caveman --scope user",
                      calls)
        self.assertEqual(
            calls.count("plugin install caveman@caveman --scope user"), 1)

    def test_a_plugin_outside_the_set_installs_without_a_marketplace_add(self):
        self.assertIsNone(reconcile(("mine@market",)))
        calls = self.calls_made()
        self.assertIn("plugin install mine@market --scope user", calls)
        self.assertNotIn("marketplace", " ".join(calls))

    def test_a_failing_marketplace_add_is_reported_and_stops_the_loop(self):
        with unittest.mock.patch.dict(os.environ, {"FAKE_PLUGIN_FAIL": "marketplace"}):
            problem = reconcile(("ponytail",))
        self.assertIsNotNone(problem)
        self.assertIn("DietrichGebert/ponytail", problem)
        self.assertIn("fake failure", problem)

    def test_an_install_that_reports_success_and_does_nothing_is_caught(self):
        # The re-check exists for exactly this: a CLI exiting 0 having
        # installed nothing must not read as success, or the session runs
        # with a prompt describing skills it does not have.
        with unittest.mock.patch.dict(os.environ, {"FAKE_PLUGIN_NOOP_INSTALL": "1"}):
            problem = reconcile(("caveman",))
        self.assertIsNotNone(problem)
        self.assertIn("caveman@caveman", problem)

    def test_a_claude_that_is_not_on_path_is_reported_not_raised(self):
        with unittest.mock.patch.dict(os.environ, {"PATH": str(self.tmp / "empty")}):
            problem = reconcile(("caveman",))
        self.assertIsNotNone(problem)
        self.assertIn("claude", problem)

    def test_unparseable_output_is_reported_not_raised(self):
        bad = self.tmp / "bin" / "claude"
        bad.write_text("#!/usr/bin/env bash\necho not json\n")
        bad.chmod(0o755)
        problem = reconcile(("caveman",))
        self.assertIsNotNone(problem)
        self.assertIn("could not read", problem)

    def test_the_dict_shaped_list_output_is_accepted_too(self):
        # `claude plugin list --json` returns a bare list; the --available
        # variant returns {"installed": [...], "available": [...]}. Accepting
        # both is one line against CLI version drift.
        bad = self.tmp / "bin" / "claude"
        bad.write_text(
            "#!/usr/bin/env bash\n"
            "echo '{\"installed\":[{\"id\":\"caveman@caveman\",\"scope\":\"user\","
            "\"enabled\":true}]}'\n"
        )
        bad.chmod(0o755)
        self.assertIsNone(reconcile(("caveman",)))

    def test_a_user_scope_row_wins_over_a_later_non_user_row_for_the_same_id(self):
        # The real CLI emits one row per scope a plugin is installed in --
        # the same id repeated. A project-scope row landing after the
        # user-scope row for the same id must not make _installed forget
        # the user-scope one just because the CLI happened to emit it last.
        bad = self.tmp / "bin" / "claude"
        bad.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$*\" >> \"$FAKE_PLUGIN_CALLS\"\n"
            "echo '[{\"id\":\"caveman@caveman\",\"scope\":\"user\",\"enabled\":true},"
            "{\"id\":\"caveman@caveman\",\"scope\":\"project\",\"enabled\":true}]'\n"
        )
        bad.chmod(0o755)
        self.assertIsNone(reconcile(("caveman",)))
        self.assertNotIn("install", " ".join(self.calls_made()))

    def test_a_plugin_installed_only_in_another_scope_is_installed_at_user(self):
        # Project and local scope are per-repository and cannot be used here,
        # so a project-scope row is not evidence this box has it.
        bad = self.tmp / "bin" / "claude"
        shutil.copy(Path(__file__).parent / "fake_claude_plugin.sh", bad)
        self.state.write_text("caveman@caveman\n")
        with unittest.mock.patch("claudeloop.plugins._installed",
                                 side_effect=[{"caveman@caveman": ("project", True)},
                                              {"caveman@caveman": ("user", True)}]):
            self.assertIsNone(reconcile(("caveman",)))
        self.assertIn("plugin install caveman@caveman --scope user",
                      self.calls_made())
