import tempfile
import unittest
from pathlib import Path

from claudeloop.config import Config
from claudeloop.prompt import (
    BUILTIN_DEFINITION_OF_DONE,
    PRECEDENCE,
    PROTOCOL,
    compose,
    repo_claude_md,
)


class PromptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)

    def cfg(self, **overrides) -> Config:
        base = {
            "repo": self.repo,
            "tasks_file": self.tmp / "tasks.md",
            "home": self.tmp / "home",
        }
        return Config(**{**base, **overrides})

    def test_protocol_and_precedence_are_always_present(self):
        text = compose(self.cfg())
        self.assertIn(PROTOCOL, text)
        self.assertIn(PRECEDENCE, text)

    def test_protocol_still_names_the_result_contract(self):
        for token in ("CLAUDELOOP_RESULT", "done", "failed", "blocked"):
            self.assertIn(token, PROTOCOL)

    def test_protocol_no_longer_names_claude_md(self):
        # That sentence moved to the definition-of-done layer, which only
        # points at the repository's file when the repository has one.
        self.assertNotIn("CLAUDE.md", PROTOCOL)

    def test_a_repo_claude_md_is_pointed_at(self):
        (self.repo / "CLAUDE.md").write_text("# rules")
        text = compose(self.cfg())
        self.assertIn(str(self.repo / "CLAUDE.md"), text)
        self.assertNotIn(BUILTIN_DEFINITION_OF_DONE, text)

    def test_a_dot_claude_claude_md_is_found(self):
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude" / "CLAUDE.md").write_text("# rules")
        self.assertEqual(
            repo_claude_md(self.repo), self.repo / ".claude" / "CLAUDE.md"
        )

    def test_no_claude_md_anywhere_returns_none(self):
        self.assertIsNone(repo_claude_md(self.repo))

    def test_the_builtin_is_used_when_repo_and_file_are_both_silent(self):
        self.assertIn(BUILTIN_DEFINITION_OF_DONE, compose(self.cfg()))

    def test_the_builtin_covers_the_no_remote_case(self):
        self.assertIn("no remote", BUILTIN_DEFINITION_OF_DONE)

    def test_a_definition_of_done_file_wins_over_the_builtin(self):
        dod = self.tmp / "dod.md"
        dod.write_text("Done means the customer said so.")
        text = compose(self.cfg(definition_of_done_file=dod))
        self.assertIn("Done means the customer said so.", text)
        self.assertNotIn(BUILTIN_DEFINITION_OF_DONE, text)

    def test_a_repo_claude_md_wins_over_the_definition_of_done_file(self):
        (self.repo / "CLAUDE.md").write_text("# rules")
        dod = self.tmp / "dod.md"
        dod.write_text("Done means the customer said so.")
        text = compose(self.cfg(definition_of_done_file=dod))
        self.assertIn(str(self.repo / "CLAUDE.md"), text)
        self.assertNotIn("Done means the customer said so.", text)

    def test_operator_instructions_are_included(self):
        instructions = self.tmp / "mine.md"
        instructions.write_text("Never push to main.")
        self.assertIn("Never push to main.", compose(self.cfg(instructions_file=instructions)))

    def test_there_is_no_operator_layer_when_the_file_is_absent(self):
        text = compose(self.cfg(instructions_file=self.tmp / "nope.md"))
        self.assertNotIn("Operator instructions", text)

    def test_an_empty_operator_file_produces_no_layer(self):
        instructions = self.tmp / "mine.md"
        instructions.write_text("   \n\n")
        self.assertNotIn("Operator instructions", compose(self.cfg(instructions_file=instructions)))

    def test_none_paths_are_treated_as_absent(self):
        text = compose(self.cfg(instructions_file=None, definition_of_done_file=None))
        self.assertIn(BUILTIN_DEFINITION_OF_DONE, text)
        self.assertNotIn("Operator instructions", text)


if __name__ == "__main__":
    unittest.main()
