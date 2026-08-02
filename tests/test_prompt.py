import sys
import tempfile
import unittest
from pathlib import Path

from claudeloop.config import Config, JiraConfig
from claudeloop.jira import task_text
from claudeloop.prompt import (
    BUILTIN_DEFINITION_OF_DONE,
    PROTOCOL,
    compose,
    precedence,
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

    def test_protocol_and_base_precedence_are_always_present(self):
        text = compose(self.cfg())
        self.assertIn(PROTOCOL, text)
        self.assertIn("ClaudeLoop protocol above is invariant", text)
        self.assertIn("definition of done is the base", text)

    def test_protocol_still_names_the_result_contract(self):
        for token in ("CLAUDELOOP_RESULT", "done", "failed", "blocked"):
            self.assertIn(token, PROTOCOL)

    def test_protocol_no_longer_names_claude_md(self):
        # That sentence moved to the definition-of-done layer, which only
        # points at the repository's file when the repository has one.
        self.assertNotIn("CLAUDE.md", PROTOCOL)

    def test_protocol_distinguishes_blocked_from_failed(self):
        # PROTOCOL named both statuses without ever saying what tells them
        # apart, and separately told the session to "decide open questions
        # yourself rather than waiting" -- in tension with a status that
        # waits on a human. This resolves both: blocked is reserved for what
        # only a human can decide.
        self.assertIn("means a human must decide something", PROTOCOL)
        self.assertIn("means you tried and could not finish", PROTOCOL)
        self.assertIn("decide open questions yourself rather than waiting", PROTOCOL)

    def test_a_repo_claude_md_is_pointed_at(self):
        (self.repo / "CLAUDE.md").write_text("# rules")
        text = compose(self.cfg())
        self.assertIn(str(self.repo / "CLAUDE.md"), text)

    def test_a_repo_claude_md_still_carries_the_builtin_as_a_fallback(self):
        # Most CLAUDE.md files are architecture/style notes that never say
        # when work is finished -- without this, those repositories get the
        # pointer and no fallback, reopening the gap this branch exists to
        # close.
        (self.repo / "CLAUDE.md").write_text("# rules, no mention of done")
        text = compose(self.cfg())
        self.assertIn(BUILTIN_DEFINITION_OF_DONE, text)

    def test_a_dot_claude_claude_md_is_found(self):
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude" / "CLAUDE.md").write_text("# rules")
        self.assertEqual(
            repo_claude_md(self.repo), self.repo / ".claude" / "CLAUDE.md"
        )

    def test_an_agents_md_is_found(self):
        # AGENTS.md is auto-loaded into the session's context by Claude Code
        # itself; without this, a repository with only AGENTS.md would get
        # both that file and the built-in definition of done, with no stated
        # precedence between them.
        (self.repo / "AGENTS.md").write_text("# agent rules")
        self.assertEqual(repo_claude_md(self.repo), self.repo / "AGENTS.md")

    def test_no_claude_md_anywhere_returns_none(self):
        self.assertIsNone(repo_claude_md(self.repo))

    def test_the_builtin_is_used_when_repo_and_file_are_both_silent(self):
        self.assertIn(BUILTIN_DEFINITION_OF_DONE, compose(self.cfg()))

    def test_the_builtin_covers_the_no_remote_case(self):
        self.assertIn("no remote", BUILTIN_DEFINITION_OF_DONE)

    def test_the_builtin_also_covers_missing_credentials_and_forge_cli(self):
        # A literal reading of "no remote configured" gives no permission to
        # stop when the remote exists but push credentials or a forge CLI
        # are the thing actually missing -- the case the S4 box hits. The
        # session must also be told to say what was missing.
        self.assertIn("push credentials", BUILTIN_DEFINITION_OF_DONE)
        self.assertIn("forge CLI", BUILTIN_DEFINITION_OF_DONE)
        self.assertIn("name in your summary exactly what was missing", BUILTIN_DEFINITION_OF_DONE)

    def test_the_escape_hatch_names_done_and_rules_out_blocked(self):
        # The smoke test found two sessions that both finished the work and
        # both stopped short of a PR for lack of a remote, and neither wrote
        # "done" -- one asked a human in prose and burned its result file
        # entirely, the other wrote "blocked" and posed the missing remote
        # as a question. The old text said "stop after committing" and never
        # named a status, so both readings were defensible. This pins the
        # fix: the status to write is spelled out, and "blocked" is
        # explicitly ruled out with the reason (supplying a remote or
        # credentials is not a decision anyone can hand you mid-run).
        self.assertIn("that is not blocked", BUILTIN_DEFINITION_OF_DONE)
        self.assertIn(
            "is not a decision anyone can hand you mid-run", BUILTIN_DEFINITION_OF_DONE
        )
        self.assertIn(
            'write status "done" (not "blocked")', BUILTIN_DEFINITION_OF_DONE
        )

    def test_the_escape_hatch_closes_the_stop_and_ask_reading(self):
        # PROTOCOL already says writing the result file is what ends the
        # task and that nobody is watching -- the old escape-hatch text
        # ("stop after committing") nonetheless read, to one session, as
        # permission to stop and ask a human in prose instead of writing the
        # file. The fix restates the point inline rather than assuming the
        # reader connects it back to PROTOCOL.
        self.assertIn("Write that result file and stop there", BUILTIN_DEFINITION_OF_DONE)
        self.assertIn(
            "do not instead end your turn by asking a human what to do next",
            BUILTIN_DEFINITION_OF_DONE,
        )

    def test_the_builtin_requires_a_new_branch_from_the_default(self):
        # "committed on a branch" was satisfiable by committing to main
        # itself, or by branching a second task off the first task's branch.
        self.assertIn("new branch", BUILTIN_DEFINITION_OF_DONE)
        self.assertIn("default branch", BUILTIN_DEFINITION_OF_DONE)

    def test_the_builtin_qualifies_the_tests_requirement(self):
        # The target case for this feature -- a scratch repo -- very likely
        # has no test suite at all; a literal "the tests pass" sends the
        # session hunting indefinitely or writing speculative tests.
        self.assertIn("if it has any", BUILTIN_DEFINITION_OF_DONE)

    def test_the_builtin_forbids_touching_the_task_list(self):
        # Sequence this guards against: a `git add -A` sweeps ClaudeLoop's
        # own tasks file into a commit, then a later session's branch
        # cleanup (`git checkout -- .` / `git stash`) discards the `- [x]`
        # mark, and main_loop re-reads the file and repeats the task forever.
        self.assertIn("task-tracking file", BUILTIN_DEFINITION_OF_DONE)
        self.assertIn("git add -A", BUILTIN_DEFINITION_OF_DONE)

    def test_a_definition_of_done_file_wins_over_the_builtin(self):
        dod = self.tmp / "dod.md"
        dod.write_text("Done means the customer said so.")
        text = compose(self.cfg(definition_of_done_file=dod))
        self.assertIn("Done means the customer said so.", text)
        self.assertNotIn(BUILTIN_DEFINITION_OF_DONE, text)

    def test_a_repo_claude_md_is_still_pointed_at_when_a_definition_of_done_file_is_set(self):
        (self.repo / "CLAUDE.md").write_text("# rules")
        dod = self.tmp / "dod.md"
        dod.write_text("Done means the customer said so.")
        text = compose(self.cfg(definition_of_done_file=dod))
        self.assertIn(str(self.repo / "CLAUDE.md"), text)

    def test_a_definition_of_done_file_is_the_fallback_not_the_builtin(self):
        # The operator outranks the repository. An architecture-only
        # CLAUDE.md never says when work is finished, so the fallback used
        # in its place must be whatever the operator configured -- the
        # operator's own box may have no forge access, so the built-in's
        # "and a pull request is open" cannot silently override them.
        (self.repo / "CLAUDE.md").write_text("# architecture notes only")
        dod = self.tmp / "dod.md"
        dod.write_text("Done means: commit and stop, never open a pull request.")
        text = compose(self.cfg(definition_of_done_file=dod))
        self.assertIn(
            "Done means: commit and stop, never open a pull request.", text
        )
        self.assertNotIn(BUILTIN_DEFINITION_OF_DONE, text)

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

    def test_precedence_names_the_operator_layer_when_present(self):
        self.assertIn("operator instructions outrank", precedence(has_operator=True))

    def test_precedence_omits_the_operator_layer_when_absent(self):
        self.assertNotIn("outrank", precedence(has_operator=False))

    def test_composed_precedence_reflects_whether_the_operator_layer_ran(self):
        instructions = self.tmp / "mine.md"
        instructions.write_text("Never push to main.")
        with_operator = compose(self.cfg(instructions_file=instructions))
        without_operator = compose(self.cfg(instructions_file=self.tmp / "nope.md"))
        self.assertIn("outrank", with_operator)
        self.assertNotIn("outrank", without_operator)

    def test_precedence_never_misnames_the_base_layer(self):
        # The base is whichever of built-in / definition_of_done_file /
        # repo CLAUDE.md actually supplied it -- "the repository's own
        # documentation" was wrong whenever it wasn't a repo CLAUDE.md.
        self.assertNotIn("repository's own documentation", precedence(has_operator=True))
        self.assertNotIn("repository's own documentation", precedence(has_operator=False))


class TaskSourceSectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)

    def cfg(self, **kwargs):
        return Config(repo=self.repo, tasks_file=self.tmp / "t.md",
                      home=self.tmp, **kwargs)

    def jira_cfg(self):
        return self.cfg(source="jira", jira=JiraConfig(
            "https://example.atlassian.net", "me@example.com", "secret",
            "project = OPS"))

    def test_absent_for_the_file_source(self):
        self.assertNotIn("Task source", compose(self.cfg()))

    def test_present_for_the_jira_source(self):
        text = compose(self.jira_cfg())
        self.assertIn("## Task source", text)
        self.assertIn("claudeloop.jira show", text)
        self.assertIn("claudeloop.jira comment", text)

    def test_names_this_interpreter_not_bare_python(self):
        self.assertIn(sys.executable, compose(self.jira_cfg()))

    def test_the_key_instruction_matches_how_task_text_is_built(self):
        text = compose(self.jira_cfg())
        # The prompt's worked example must be true of the real builder.
        self.assertIn("OPS-42: Fix the widget", text)
        self.assertEqual(task_text("OPS-42", "Fix the widget", None),
                         "OPS-42: Fix the widget")
        self.assertIn("the key is OPS-42", text)

    def test_forbids_the_session_transitioning_or_relabelling(self):
        text = compose(self.jira_cfg())
        self.assertIn("Do not transition", text)
        self.assertIn("labels", text)

    def test_says_a_comment_is_not_how_a_task_ends(self):
        # A session told it may talk on the ticket is exactly the session
        # that ends its turn with a comment instead of the result file.
        self.assertIn("Commenting is not how a task ends", compose(self.jira_cfg()))

    def test_does_not_leave_a_long_step_undefined(self):
        # "or before a long step" gave a literal-minded session no way to
        # tell how long is long, so it could read every file edit as
        # qualifying and bill a Jira round trip before each one.
        self.assertNotIn("a long step", compose(self.jira_cfg()))
        self.assertIn("Comment when you find something a human should see.",
                      compose(self.jira_cfg()))

    def test_sits_below_the_protocol(self):
        text = compose(self.jira_cfg())
        self.assertLess(text.index("unattended under ClaudeLoop"),
                        text.index("## Task source"))


class BlockedWordingTest(unittest.TestCase):
    """These pin specific sentences on purpose. Every live failure this
    project has had traced back to prompt text that could be read two ways,
    so a reworded claim must break a test and get looked at."""

    def test_the_protocol_no_longer_claims_nobody_can_answer(self):
        from claudeloop.prompt import PROTOCOL

        self.assertNotIn("Nobody is watching, so", PROTOCOL)

    def test_the_protocol_says_blocking_parks_the_task_and_costs_time(self):
        from claudeloop.prompt import PROTOCOL

        self.assertIn("parks this task until a human", PROTOCOL)
        self.assertIn("may be hours", PROTOCOL)

    def test_the_protocol_says_the_answer_comes_back_to_this_session(self):
        from claudeloop.prompt import PROTOCOL

        self.assertIn("this same session is resumed with their answer", PROTOCOL)

    def test_the_protocol_still_reserves_blocked_for_a_human_decision(self):
        from claudeloop.prompt import PROTOCOL

        self.assertIn("an ordinary judgment call is not that", PROTOCOL)

    def test_the_protocol_names_blocked_where_it_sets_the_bar(self):
        # "Reserve it for the narrow case" left the nearest antecedent as
        # "your patience". The status has to be named at the point the bar
        # is set, not four clauses earlier.
        from claudeloop.prompt import PROTOCOL

        self.assertIn('Reserve "blocked" for the narrow case', PROTOCOL)


if __name__ == "__main__":
    unittest.main()
