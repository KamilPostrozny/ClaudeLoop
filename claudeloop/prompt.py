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

from pathlib import Path

from .config import Config

PROTOCOL = (
    "You are running unattended under ClaudeLoop. Nobody is watching, so "
    "decide open questions yourself rather than waiting; reserve \"blocked\" "
    "for the narrow case where a human, not you, must decide something (a "
    "missing credential, a choice with no way to infer the right answer) -- "
    "an ordinary judgment call is not that. When the task is fully complete, "
    "or provably cannot be completed, write a JSON object to the path in the "
    "CLAUDELOOP_RESULT environment variable with keys \"status\" (one of "
    "\"done\", \"failed\", \"blocked\" -- \"failed\" means you tried and "
    "could not finish, \"blocked\" means a human must decide something "
    "before you can), \"summary\" (one paragraph on what you did), and, when "
    "blocked, \"question\" (the one thing a human must answer). Writing that "
    "file is what ends the task; do not stop without it."
)

BUILTIN_DEFINITION_OF_DONE = (
    "Done means: the change is implemented; the repository's own tests and "
    "checks, if it has any, pass; the work is committed on a new branch "
    "created from the repository's default branch (create one for this task "
    "-- do not commit to the default branch itself); and a pull request is "
    "open. If the repository has no remote configured, or a remote is "
    "configured but push credentials or a forge CLI (gh, glab, or similar) "
    "to open a pull request with are not available, stop after committing "
    "and name in your summary exactly what was missing. Never git add, "
    "stage, commit, stash, or revert ClaudeLoop's own task-tracking file if "
    "one lives in this repository -- it is not part of the work, and "
    "ClaudeLoop rewrites it itself once you finish; a broad `git add -A` or "
    "branch-cleanup commands like `git checkout -- .` or `git stash` can "
    "silently make already-finished work look pending again. Prefer staging "
    "files by name over `git add -A`."
)

CLAUDE_MD_NAMES = ("CLAUDE.md", ".claude/CLAUDE.md", "AGENTS.md")


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


def precedence(has_operator: bool) -> str:
    """Precedence text naming only the layers actually present.

    Asserting that the operator layer outranks the repository when there is
    no operator instructions file leaves an unattended session reconciling
    a conflict against a document it cannot find.
    """
    parts = [
        "These instructions are layered. The ClaudeLoop protocol above is "
        "invariant and overrides everything below it."
    ]
    if has_operator:
        parts.append(
            "The operator instructions outrank the definition of done below."
        )
    parts.append(
        "The definition of done is the base. Where layers conflict, follow "
        "the higher one and say so in your summary."
    )
    return " ".join(parts)


def compose(cfg: Config) -> str:
    operator = _read(cfg.instructions_file)
    parts = [PROTOCOL, precedence(has_operator=bool(operator))]

    if operator:
        parts.append(f"## Operator instructions\n\n{operator}")

    claude_md = repo_claude_md(cfg.repo)
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
