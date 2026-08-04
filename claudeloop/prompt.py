"""Compose the system prompt a session carries.

Three layers with a stated precedence: ClaudeLoop's own protocol, which is
invariant; the operator's instructions, which outrank the repository because
the operator runs the machine; and the definition of done, which is the
repository's own CLAUDE.md when it has one. Pure, so every combination is
testable without spawning anything.

PROTOCOL and BUILTIN_DEFINITION_OF_DONE are not documentation -- they are
instructions a capable but literal-minded agent executes unattended for
hours with bypassed permissions. Ambiguity here is a defect the same way
a bug in loop.decide() would be.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .config import Config

PROTOCOL = (
    "You are running unattended under ClaudeLoop. Nobody is watching in real "
    "time, so decide open questions yourself rather than waiting: writing "
    "\"blocked\" parks this task until a human happens to look at it, which "
    "may be hours, and pulls someone away from their own work to answer you. "
    "Reserve \"blocked\" for the narrow case where a human, not you, must "
    "decide something (a missing credential, a choice with no way to infer "
    "the right answer) -- an ordinary judgment call is not that. When you do "
    "block, your question does reach a human, and once they answer, this "
    "same session is resumed with their answer. When the task is fully "
    "complete, or provably cannot be completed, write a JSON object to the "
    "path in the CLAUDELOOP_RESULT environment variable with keys \"status\" "
    "(one of \"done\", \"failed\", \"blocked\" -- \"failed\" means you tried "
    "and could not finish, \"blocked\" means a human must decide something "
    "before you can), \"summary\" (one paragraph on what you did), and, when "
    "blocked, \"question\" (the one thing a human must answer). Writing that "
    "file is what ends the task; do not stop without it. One last thing is "
    "ClaudeLoop's own bookkeeping rather than part of the work, and holds "
    "whatever this repository's own instructions say: never git add, stage, "
    "commit, stash or revert ClaudeLoop's task-tracking file if one lives in "
    "this repository. ClaudeLoop rewrites that file itself once you finish, "
    "and a broad `git add -A`, or a branch cleanup like `git checkout -- .` "
    "or `git stash`, can silently make already-finished work look pending "
    "again. Prefer staging files by name."
)
"""The task-file guard lives here, not in the definition of done, because
`compose` drops the built-in definition of done whenever the repository's own
CLAUDE.md says when work is finished -- so the better a repository documented
itself, the fewer of ClaudeLoop's own guards reached the session at all. This
is not a definition of done; it is the one rule ClaudeLoop's own bookkeeping
cannot survive a session breaking."""

WORKING_TREE = """## Your working tree

You are in a git worktree at {tree}, on a branch ClaudeLoop cut for this task
from {default}. Nothing else touches this tree while you have it, so its
branch, its commits and any uncommitted changes are yours alone.

{default} itself is checked out elsewhere, so two things that usually work
do not work here. `git checkout {default}` fails with "already used by worktree".
And `git push origin {default}` from this tree pushes that branch's own ref,
which does not carry your commits: it reports "Everything up-to-date", exits 0,
and ships nothing. Name HEAD explicitly instead:

    git push origin HEAD:{default}   # to land your work on {default}
    git push -u origin HEAD          # to publish this branch, for a pull request

Which of the two is right is this repository's decision, not ClaudeLoop's. If
its own instructions say work lands on {default}, use the first. If they say
nothing about it, or ask for a pull request, use the second."""
"""Fact about the machine, not policy, so it is composed for every task
whatever the repository documents about itself.

The literal command lines are deliberate. "Push HEAD rather than the branch
name" is exactly the kind of instruction a literal-minded session satisfies by
guessing, and the guess it already made -- `git push origin main`, which git
answers with "Everything up-to-date" and a zero exit -- is what made a
finished, committed task report success without shipping anything."""

BUILTIN_DEFINITION_OF_DONE = (
    "Done means: the change is implemented; the repository's own tests and "
    "checks, if it has any, pass; the work is committed on the branch you are "
    "already on; and the work is published -- pushed as this repository's "
    "instructions direct, or, if they do not say, pushed as this branch with "
    "a pull request open. You do not need to create a branch, and you may "
    "rename the one you are on with `git branch -m` if you like. If the "
    "repository has no remote configured, "
    "or a remote is configured but push credentials or a forge CLI (gh, "
    "glab, or similar) to open a pull request with are not available, that "
    "is not blocked: supplying a remote or push credentials is not a "
    "decision anyone can hand you mid-run, so there is nothing to wait on, "
    "and the work itself is finished. Commit, then write status \"done\" "
    "(not \"blocked\") and name in your summary exactly what was missing. "
    "Write that result file and stop there; do not instead end your turn by "
    "asking a human what to do next -- nobody reads your last message, and "
    "the result file is what ends the task."
)
"""Before S6 this told the session to create its own branch off the default
one -- S1's live smoke test measured only ~50% compliance, and S2b's found
the failure mode: a task that skipped it committed straight to the default
branch, and a task parked meanwhile inherited the contamination once it
resumed. ClaudeLoop now cuts the branch itself before the session ever runs,
so the instruction is gone rather than reworded.

S10 took the rest of the branch mechanics out. "Never check out the default
branch" is now stated by WORKING_TREE as the mechanical impossibility it is,
and *where* work is published is deferred to the repository, which is the
layer that knows -- the wording here only covers a repository that says
nothing. The task-file guard moved to PROTOCOL, which is always present."""

CLAUDE_MD_NAMES = ("CLAUDE.md", ".claude/CLAUDE.md", "AGENTS.md")

JIRA_TASK_SOURCE = """## Task source

This task is a Jira issue. The task text begins with the issue key,
followed by a colon and the issue's summary -- in "OPS-42: Fix the widget",
the key is OPS-42.

Read the full ticket, including its comments:
    {python} -m claudeloop.jira show <KEY>
Post a comment (its body is read from stdin):
    {python} -m claudeloop.jira comment <KEY> -

Comment when you find something a human should see.
Do not transition the issue or edit its labels -- ClaudeLoop does that when
the task ends. Commenting is not how a task ends: the result file still is."""
"""Read by a literal-minded agent, so the last sentence is not decoration --
a session told it may talk on the ticket is exactly the session that ends its
turn with a comment instead of the result file."""


def task_source_section(cfg: Config) -> str:
    """Empty for every source that needs no explanation, which today is the
    checklist."""
    if cfg.source != "jira":
        return ""
    # sys.executable, not "python": the box may have several interpreters and
    # only this one is running ClaudeLoop, hence only this one has the
    # package parent on its path via PYTHONPATH.
    return JIRA_TASK_SOURCE.format(python=sys.executable)


def repo_claude_md(repo: Path) -> Path | None:
    """The repository's own instructions file, if it has one."""
    for name in CLAUDE_MD_NAMES:
        candidate = repo / name
        if candidate.exists():
            return candidate
    return None


def _read(path: Path | None) -> str:
    """A missing or unreadable file is 'absent', not an error: these layers
    are optional, and a session must not fail to start over a rename."""
    if path is None:
        return ""
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def working_tree_section(tree: Path | None, default_branch: str | None) -> str:
    """WORKING_TREE filled in, or empty when either fact is unknown.

    Both or neither: a section that guessed the default branch would hand a
    literal-minded session a push command aimed at a branch that may not
    exist, which is a worse failure than the section being absent.
    """
    if tree is None or not default_branch:
        return ""
    return WORKING_TREE.format(tree=tree, default=default_branch)


def precedence(has_operator: bool) -> str:
    """Precedence text naming only the layers actually present.

    Asserting that the operator layer outranks the repository when there is
    no operator instructions file leaves an unattended session reconciling
    a conflict against a document it cannot find.

    S10 reversed S1's ranking. ClaudeLoop's definition of done used to be the
    base, with the repository's own file pointed at from inside it -- so a
    repository whose CLAUDE.md said "push to main" was arguing with a layer
    that outranked it, and the session had no stated way to resolve that. The
    repository decides how work is done here; what is left above it is the
    handful of rules ClaudeLoop itself breaks without, and facts about the
    machine that no instruction can make untrue.
    """
    parts = [
        "These instructions are layered. The ClaudeLoop protocol above is a "
        "small set of invariants that hold because ClaudeLoop itself breaks "
        "without them, and it overrides everything below it. The working tree "
        "section is fact about this machine rather than policy -- nothing "
        "below can make it untrue."
    ]
    if has_operator:
        parts.append(
            "The operator instructions outrank this repository's own "
            "instructions."
        )
    parts.append(
        "Below those, this repository's own instructions come first: they "
        "decide how work is done here, including when it is finished and "
        "where it lands. ClaudeLoop's definition of done is only a fallback "
        "for what they do not say. Where layers conflict, follow the higher "
        "one and say so in your summary."
    )
    return " ".join(parts)


MAX_ARG_BYTES = 128 * 1024
"""Linux's MAX_ARG_STRLEN: the cap on a single argv element, independent of
the much larger total. The composed prompt travels as one
`--append-system-prompt` argument, so an operator instructions file large
enough to push it past this makes execve fail -- with an errno the CLI
reports as something unrelated, on every task, forever."""


def oversized(prompt: str) -> str | None:
    """An error message written for a human, or None.

    Checked at startup against the same composition a session gets, so a
    prompt that cannot be passed says so once, before anything is listening
    and before a single paid task fails on it.
    """
    size = len(prompt.encode())
    if size <= MAX_ARG_BYTES:
        return None
    return (
        f"the composed system prompt is {size} bytes, past the {MAX_ARG_BYTES}"
        " byte limit Linux puts on a single command-line argument. ClaudeLoop"
        " passes it to the CLI as one --append-system-prompt argument, so"
        " every session would fail to start. Shorten your instructions file"
        " or your definition of done -- or point the session at a file in the"
        " repository instead of inlining it."
    )


def compose(
    cfg: Config, tree: Path | None = None, default_branch: str | None = None
) -> str:
    """`tree` is the working directory the session will run in -- its own
    worktree. It differs from cfg.repo, which is only the repository that
    tree was cut from, and it is the copy of CLAUDE.md the session can
    actually edit.

    `default_branch` is the name of the branch that tree was cut from, which
    the session cannot check out and must name explicitly to push to. Passed
    in rather than looked up, so this stays pure."""
    operator = _read(cfg.instructions_file)
    parts = [PROTOCOL, precedence(has_operator=bool(operator))]

    facts = working_tree_section(tree, default_branch)
    if facts:
        parts.append(facts)

    task_source = task_source_section(cfg)
    if task_source:
        parts.append(task_source)

    if operator:
        parts.append(f"## Operator instructions\n\n{operator}")

    claude_md = repo_claude_md(tree or cfg.repo)
    if claude_md is not None:
        # The repository documents itself; point at it rather than imposing
        # a definition of done over the top of one it already has. Most
        # CLAUDE.md files are architecture and style notes that never say
        # when work is finished, so the built-in rides along as a fallback
        # -- costless when the repository's file already covers it.
        parts.append(
            "## Definition of done\n\nThis repository has its own instructions "
            f"at {claude_md}. They come first: follow that file end to end — "
            "it defines what \"done\" means here, including its testing, "
            "verification and publishing requirements. Use what follows only "
            "for what that file does not say:\n\n"
            + (_read(cfg.definition_of_done_file) or BUILTIN_DEFINITION_OF_DONE)
        )
    else:
        parts.append(
            "## Definition of done\n\n"
            + (_read(cfg.definition_of_done_file) or BUILTIN_DEFINITION_OF_DONE)
        )

    return "\n\n".join(parts)
