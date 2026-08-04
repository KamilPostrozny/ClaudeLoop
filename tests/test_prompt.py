import sys
import tempfile
import unittest
from pathlib import Path

from claudeloop import prompt
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
        self.assertIn("small set of invariants", text)
        self.assertIn("this repository's own instructions come first", text)

    def test_the_protocol_carries_the_task_file_guard(self):
        # It used to live in BUILTIN_DEFINITION_OF_DONE, which compose() drops
        # whenever the repository's own CLAUDE.md defines done -- so the better
        # a repository documented itself, the fewer of ClaudeLoop's own guards
        # survived. This is not a definition of done; it is ClaudeLoop's
        # bookkeeping, and it holds whatever the repository says.
        self.assertIn("task-tracking file", PROTOCOL)
        self.assertIn("git add -A", PROTOCOL)

    def test_the_guards_survive_a_repo_that_fully_defines_done(self):
        (self.repo / "CLAUDE.md").write_text(
            "# rules\n\nDone means: committed and pushed to main.\n"
        )
        self.assertIn("task-tracking file", compose(self.cfg()))

    def test_the_builtin_no_longer_carries_the_guards(self):
        self.assertNotIn("task-tracking file", BUILTIN_DEFINITION_OF_DONE)
        self.assertNotIn("Never check out the default branch",
                         BUILTIN_DEFINITION_OF_DONE)

    def test_the_builtin_defers_to_the_repository_on_where_work_lands(self):
        self.assertIn("as this repository's instructions direct",
                      BUILTIN_DEFINITION_OF_DONE)

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

    def test_the_definition_of_done_does_not_ask_the_session_to_branch(self):
        # ClaudeLoop creates the branch now, so the instruction a live smoke
        # test measured at ~50% compliance is gone rather than reworded.
        self.assertNotIn("Create that branch before your first commit",
                         BUILTIN_DEFINITION_OF_DONE)
        self.assertIn("branch you are already on", BUILTIN_DEFINITION_OF_DONE)

    def test_the_definition_of_done_allows_a_rename(self):
        self.assertIn("git branch -m", BUILTIN_DEFINITION_OF_DONE)

    def test_the_builtin_qualifies_the_tests_requirement(self):
        # The target case for this feature -- a scratch repo -- very likely
        # has no test suite at all; a literal "the tests pass" sends the
        # session hunting indefinitely or writing speculative tests.
        self.assertIn("if it has any", BUILTIN_DEFINITION_OF_DONE)

    def test_the_protocol_forbids_touching_the_task_list(self):
        # Sequence this guards against: a `git add -A` sweeps ClaudeLoop's
        # own tasks file into a commit, then a later session's branch
        # cleanup (`git checkout -- .` / `git stash`) discards the `- [x]`
        # mark, and main_loop re-reads the file and repeats the task forever.
        self.assertIn("task-tracking file", PROTOCOL)
        self.assertIn("git checkout -- .", PROTOCOL)
        self.assertIn("git stash", PROTOCOL)

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

    def test_precedence_puts_the_repository_above_claudeloops_fallback(self):
        for text in (precedence(has_operator=True), precedence(has_operator=False)):
            self.assertIn("this repository's own instructions come first", text)
            self.assertIn("only a fallback", text)

    def test_precedence_no_longer_calls_the_builtin_the_base(self):
        # S1's framing, reversed by this slice: ClaudeLoop's definition of done
        # was the base and the repository's file was pointed at from inside it.
        # A repository that says "push to main" was then arguing with a layer
        # that outranked it.
        for text in (precedence(has_operator=True), precedence(has_operator=False)):
            self.assertNotIn("definition of done is the base", text)

    def test_precedence_states_the_facts_layer_cannot_be_overridden(self):
        for text in (precedence(has_operator=True), precedence(has_operator=False)):
            self.assertIn("fact about this machine", text)

    def test_the_repo_pointer_says_the_repository_comes_first(self):
        (self.repo / "CLAUDE.md").write_text("# rules")
        text = compose(self.cfg())
        self.assertIn("They come first", text)
        self.assertIn("Use what follows only for what that file does not say",
                      text)

    def test_precedence_never_misnames_the_base_layer(self):
        # The base is whichever of built-in / definition_of_done_file /
        # repo CLAUDE.md actually supplied it -- "the repository's own
        # documentation" was wrong whenever it wasn't a repo CLAUDE.md.
        self.assertNotIn("repository's own documentation", precedence(has_operator=True))
        self.assertNotIn("repository's own documentation", precedence(has_operator=False))

    def test_the_definition_of_done_names_the_tree_the_session_works_in(self):
        # Under worktrees the session's cwd is not cfg.repo, and pointing a
        # literal-minded agent at a CLAUDE.md outside its own working
        # directory invites it to edit the wrong copy.
        (self.repo / "CLAUDE.md").write_text("repo rules\n")
        tree = self.tmp / "worktrees" / "abc123"
        tree.mkdir(parents=True)
        (tree / "CLAUDE.md").write_text("repo rules\n")

        text = compose(self.cfg(), tree)

        self.assertIn(str(tree / "CLAUDE.md"), text)
        self.assertNotIn(str(self.repo / "CLAUDE.md"), text)


class WorkingTreeSectionTest(unittest.TestCase):
    """Fact about the machine, not policy. A session that has to infer any of
    this infers it wrong: the defect that produced this slice was a session
    running `git push origin main` from a worktree, where it pushes that
    branch's own ref, says "Everything up-to-date", exits 0 and ships
    nothing."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.tree = self.tmp / "worktrees" / "abc123"
        self.tree.mkdir(parents=True)

    def cfg(self, **overrides) -> Config:
        base = {
            "repo": self.repo,
            "tasks_file": self.tmp / "tasks.md",
            "home": self.tmp / "home",
        }
        return Config(**{**base, **overrides})

    def test_the_section_names_the_tree_and_the_default_branch(self):
        text = compose(self.cfg(), self.tree, default_branch="trunk")
        self.assertIn(str(self.tree), text)
        self.assertIn("trunk", text)

    def test_it_gives_both_publish_commands_naming_head(self):
        text = compose(self.cfg(), self.tree, default_branch="main")
        self.assertIn("git push origin HEAD:main", text)
        self.assertIn("git push -u origin HEAD", text)

    def test_it_warns_that_pushing_the_branch_name_ships_nothing(self):
        text = compose(self.cfg(), self.tree, default_branch="main")
        self.assertIn("git push origin main", text)
        self.assertIn("Everything up-to-date", text)
        self.assertIn("ships nothing", text)

    def test_it_explains_the_default_branch_cannot_be_checked_out(self):
        # Stated as the mechanical fact it is, with git's actual error, rather
        # than as a rule -- a rule invites a session to try it once.
        text = compose(self.cfg(), self.tree, default_branch="main")
        self.assertIn("git checkout main", text)
        self.assertIn("already used by worktree", text)

    def test_it_leaves_the_choice_to_the_repository(self):
        text = compose(self.cfg(), self.tree, default_branch="main")
        self.assertIn("this repository's decision, not ClaudeLoop's", text)

    def test_it_is_present_whether_or_not_the_repo_documents_itself(self):
        (self.tree / "CLAUDE.md").write_text("# fully documented\n")
        with_md = compose(self.cfg(), self.tree, default_branch="main")
        without_md = compose(self.cfg(), self.tmp, default_branch="main")
        for text in (with_md, without_md):
            self.assertIn("## Your working tree", text)

    def test_absent_when_either_fact_is_unknown(self):
        # A guessed default branch would hand a literal-minded session a push
        # command aimed at a branch that may not exist.
        self.assertNotIn("## Your working tree", compose(self.cfg()))
        self.assertNotIn("## Your working tree", compose(self.cfg(), self.tree))
        self.assertNotIn(
            "## Your working tree", compose(self.cfg(), None, default_branch="main")
        )

    def test_it_sits_above_the_definition_of_done(self):
        text = compose(self.cfg(), self.tree, default_branch="main")
        self.assertLess(text.index("## Your working tree"),
                        text.index("## Definition of done"))


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


class NoPluginLayerTest(unittest.TestCase):
    """S8 dropped the plugin usage layer: a repository's own
    .claude/settings.json decides which plugins a session gets, and the
    plugins state their own rules. Nothing in the prompt may claim
    ClaudeLoop installed tools for the session."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)

    def test_no_prompt_mentions_plugins_at_all(self):
        cfg = Config(repo=self.repo, tasks_file=self.tmp / "tasks.md",
                     home=self.tmp / "home")
        self.assertNotIn("plugin", compose(cfg).lower())

    def test_precedence_names_only_the_layers_that_exist(self):
        self.assertNotIn("plugin", precedence(has_operator=True).lower())
        self.assertNotIn("operator", precedence(has_operator=False).lower())


class OversizedTest(unittest.TestCase):
    """The composed prompt travels as one --append-system-prompt argument,
    and Linux caps a single argv element at 128 KiB. Past it, execve fails on
    every task with an errno the CLI reports as something unrelated."""

    def test_an_ordinary_prompt_is_fine(self):
        self.assertIsNone(prompt.oversized("x" * 1000))

    def test_a_prompt_at_the_limit_is_still_fine(self):
        self.assertIsNone(prompt.oversized("x" * prompt.MAX_ARG_BYTES))

    def test_a_prompt_past_the_limit_is_named_with_its_size(self):
        message = prompt.oversized("x" * (prompt.MAX_ARG_BYTES + 1))

        self.assertIsNotNone(message)
        self.assertIn(str(prompt.MAX_ARG_BYTES + 1), message)
        self.assertIn("--append-system-prompt", message)

    def test_the_limit_is_measured_in_bytes_not_characters(self):
        # execve counts bytes. A prompt of multi-byte characters that fits as
        # a str would otherwise pass the check and still fail to start.
        text = "é" * (prompt.MAX_ARG_BYTES // 2 + 1)

        self.assertLess(len(text), prompt.MAX_ARG_BYTES)
        self.assertIsNotNone(prompt.oversized(text))


if __name__ == "__main__":
    unittest.main()
