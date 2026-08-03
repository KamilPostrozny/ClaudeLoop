import tempfile
import unittest
from pathlib import Path

from claudeloop.plugins import (
    PROPOSED,
    SUPERPOWERS_USAGE,
    Plugin,
    by_name,
    usage_section,
)


class TableTest(unittest.TestCase):
    def test_the_proposed_set_is_three_plugins_in_a_fixed_order(self):
        self.assertEqual(
            [plugin.name for plugin in PROPOSED],
            ["superpowers", "caveman", "ponytail"],
        )

    def test_every_proposed_plugin_carries_an_id_marketplace_and_reason(self):
        for plugin in PROPOSED:
            self.assertIn("@", plugin.plugin_id, plugin.name)
            self.assertTrue(plugin.marketplace, plugin.name)
            # The wizard renders this next to the checkbox; a blank one is a
            # checkbox with no argument for ticking it.
            self.assertTrue(plugin.reason, plugin.name)

    def test_only_superpowers_carries_usage_text(self):
        # caveman and ponytail were selected for the set but contribute
        # nothing to the prompt: both already state their own rules, and a
        # second copy here would drift out of step with the plugin.
        self.assertEqual(
            [plugin.name for plugin in PROPOSED if plugin.usage],
            ["superpowers"],
        )

    def test_by_name_finds_a_proposed_plugin_and_nothing_else(self):
        self.assertEqual(by_name("caveman").plugin_id, "caveman@caveman")
        self.assertIsNone(by_name("nonesuch"))
        self.assertIsNone(by_name(""))


class SuperpowersUsageTest(unittest.TestCase):
    """Prompt text is the product here: these pin the two rules the text
    exists to carry, so a rewrite that drops one fails rather than passing
    quietly."""

    def test_it_states_the_question_discipline(self):
        self.assertIn("go and read it, and never ask", SUPERPOWERS_USAGE)
        self.assertIn("solely in the operator's head", SUPERPOWERS_USAGE)

    def test_it_refuses_a_subagent_as_a_way_to_dodge_a_question(self):
        self.assertIn("for breadth, not for dodging a question", SUPERPOWERS_USAGE)

    def test_it_resolves_the_approval_gate(self):
        self.assertIn("approved this work when they queued it", SUPERPOWERS_USAGE)

    def test_the_approval_licence_is_bounded(self):
        # A literal-minded agent told "approval already happened" will
        # generalise it to every gate it meets unless this sentence is here.
        self.assertIn("licenses nothing about tests, verification", SUPERPOWERS_USAGE)


class UsageSectionTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())

    def test_no_selection_renders_nothing(self):
        self.assertEqual(usage_section((), self.home), "")

    def test_a_selection_with_no_usage_text_renders_nothing(self):
        # No empty "## Plugin usage" header over two plugins that say nothing.
        self.assertEqual(usage_section(("caveman", "ponytail"), self.home), "")

    def test_a_plugin_with_text_renders_a_header_and_its_block(self):
        text = usage_section(("superpowers",), self.home)
        self.assertTrue(text.startswith("## Plugin usage\n\n### superpowers\n\n"))
        self.assertIn(SUPERPOWERS_USAGE, text)

    def test_blocks_follow_proposed_order_not_selection_order(self):
        one = Plugin("aaa", "aaa@m", "m", reason="r", usage="A text")
        two = Plugin("zzz", "zzz@m", "m", reason="r", usage="Z text")
        text = usage_section(("zzz", "aaa"), self.home, proposed=(one, two))
        self.assertLess(text.index("### aaa"), text.index("### zzz"))

    def test_a_plugin_usage_file_replaces_the_built_in_text(self):
        directory = self.home / "plugin-usage"
        directory.mkdir()
        (directory / "superpowers.md").write_text("operator's own wording\n")
        text = usage_section(("superpowers",), self.home)
        self.assertIn("operator's own wording", text)
        self.assertNotIn(SUPERPOWERS_USAGE, text)

    def test_a_usage_file_gives_a_plugin_outside_the_set_a_block(self):
        directory = self.home / "plugin-usage"
        directory.mkdir()
        (directory / "mine@market.md").write_text("how to use mine\n")
        text = usage_section(("mine@market",), self.home)
        self.assertIn("### mine@market\n\nhow to use mine", text)

    def test_an_unreadable_usage_file_falls_back_to_the_built_in(self):
        # Same rule prompt._read already follows: a layer is optional, and a
        # session must not fail to start over a permissions mistake.
        directory = self.home / "plugin-usage"
        directory.mkdir()
        path = directory / "superpowers.md"
        path.write_text("unreadable")
        path.chmod(0o000)
        self.addCleanup(path.chmod, 0o600)
        self.assertIn(SUPERPOWERS_USAGE, usage_section(("superpowers",), self.home))
