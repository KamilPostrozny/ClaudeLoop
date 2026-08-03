"""Compose the system prompt a session carries.

Four layers with a stated precedence: ClaudeLoop's own protocol, which is
invariant; the operator's instructions, which outrank the repository because
the operator runs the machine; the plugin usage instructions, ClaudeLoop's
own advice about the plugins it installed, which rank below the operator's
own; and the definition of done, which is the repository's own CLAUDE.md
when it has one. Pure, so every combination is testable without spawning
anything.

PROTOCOL and BUILTIN_DEFINITION_OF_DONE are not documentation -- they are
instructions a capable but literal-minded agent executes unattended for
hours with bypassed permissions. Ambiguity here is a defect the same way
a bug in loop.decide() would be.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .config import Config
from . import plugins as plugins_module

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
    "file is what ends the task; do not stop without it."
)

BUILTIN_DEFINITION_OF_DONE = (
    "Done means: the change is implemented; the repository's own tests and "
    "checks, if it has any, pass; the work is committed; and a pull request "
    "is open. You are already on a branch made for this task and cut from "
    "the repository's default branch, so commit there -- you do not need to "
    "create one, and you may rename it to something descriptive with `git "
    "branch -m` if you like. Never check out the default branch and commit "
    "onto it. If the repository has no remote configured, "
    "or a remote is configured but push credentials or a forge CLI (gh, "
    "glab, or similar) to open a pull request with are not available, that "
    "is not blocked: supplying a remote or push credentials is not a "
    "decision anyone can hand you mid-run, so there is nothing to wait on, "
    "and the work itself is finished. Commit, then write status \"done\" "
    "(not \"blocked\") and name in your summary exactly what was missing. "
    "Write that result file and stop there; do not instead end your turn by "
    "asking a human what to do next -- nobody reads your last message, and "
    "the result file is what ends the task. Never git add, stage, commit, "
    "stash, or revert ClaudeLoop's own task-tracking file if one lives in "
    "this repository -- it is not part of the work, and ClaudeLoop rewrites "
    "it itself once you finish; a broad `git add -A` or branch-cleanup "
    "commands like `git checkout -- .` or `git stash` can silently make "
    "already-finished work look pending again. Prefer staging files by name "
    "over `git add -A`."
)
"""Before S6 this told the session to create its own branch off the default
one -- S1's live smoke test measured only ~50% compliance, and S2b's found
the failure mode: a task that skipped it committed straight to the default
branch, and a task parked meanwhile inherited the contamination once it
resumed. ClaudeLoop now cuts the branch itself before the session ever runs,
so the instruction is gone rather than reworded, and the definition of done
only has to forbid checking out the default branch instead of prescribing
how to avoid it."""

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


def precedence(has_operator: bool, has_plugins: bool = False) -> str:
    """Precedence text naming only the layers actually present.

    Asserting that the operator layer outranks the repository when there is
    no operator instructions file leaves an unattended session reconciling
    a conflict against a document it cannot find. The plugin layer follows
    the same rule, including the clause that positions it against the
    operator layer -- which is itself only true when that layer exists.
    """
    parts = [
        "These instructions are layered. The ClaudeLoop protocol above is "
        "invariant and overrides everything below it."
    ]
    if has_operator:
        parts.append(
            "The operator instructions outrank the definition of done below."
        )
    if has_plugins:
        if has_operator:
            clause = (
                "The plugin usage instructions are ClaudeLoop's own advice about "
                "the tools it installed for you. They rank below the operator "
                "instructions and above the definition of done."
            )
        else:
            clause = (
                "The plugin usage instructions are ClaudeLoop's own advice about "
                "the tools it installed for you. They rank above the definition of done."
            )
        parts.append(clause)
    parts.append(
        "The definition of done is the base. Where layers conflict, follow "
        "the higher one and say so in your summary."
    )
    return " ".join(parts)


def compose(cfg: Config, tree: Path | None = None) -> str:
    """`tree` is the working directory the session will run in -- its own
    worktree. It differs from cfg.repo, which is only the repository that
    tree was cut from, and it is the copy of CLAUDE.md the session can
    actually edit."""
    operator = _read(cfg.instructions_file)
    plugin_usage = plugins_module.usage_section(cfg.plugins, cfg.home)
    parts = [
        PROTOCOL,
        precedence(has_operator=bool(operator), has_plugins=bool(plugin_usage)),
    ]

    task_source = task_source_section(cfg)
    if task_source:
        parts.append(task_source)

    if operator:
        parts.append(f"## Operator instructions\n\n{operator}")

    if plugin_usage:
        parts.append(plugin_usage)

    claude_md = repo_claude_md(tree or cfg.repo)
    if claude_md is not None:
        # The repository documents itself; point at it rather than imposing
        # a definition of done over the top of one it already has. Most
        # CLAUDE.md files are architecture and style notes that never say
        # when work is finished, so the built-in rides along as a fallback
        # -- costless when the repository's file already covers it.
        parts.append(
            "## Definition of done\n\nThis repository has its own instructions "
            f"at {claude_md}. Follow that file end to end — it defines what "
            "\"done\" means here, including its testing and verification "
            "requirements. If it does not say when the work is finished, use "
            "this instead:\n\n"
            + (_read(cfg.definition_of_done_file) or BUILTIN_DEFINITION_OF_DONE)
        )
    else:
        parts.append(
            "## Definition of done\n\n"
            + (_read(cfg.definition_of_done_file) or BUILTIN_DEFINITION_OF_DONE)
        )

    return "\n\n".join(parts)
