"""Compose the system prompt a session carries.

Three layers with a stated precedence: ClaudeLoop's own protocol, which is
invariant; the operator's instructions, which outrank the repository because
the operator runs the machine; and the definition of done, which is the
repository's own CLAUDE.md when it has one. Pure, so every combination is
testable without spawning anything.
"""

from __future__ import annotations

from pathlib import Path

from .config import Config

PROTOCOL = (
    "You are running unattended under ClaudeLoop. Nobody is watching, so "
    "decide open questions yourself rather than waiting. When the task is "
    "fully complete, or provably cannot be completed, write a JSON object to "
    "the path in the CLAUDELOOP_RESULT environment variable with keys "
    "\"status\" (one of \"done\", \"failed\", \"blocked\"), \"summary\" (one "
    "paragraph on what you did), and, when blocked, \"question\" (the one "
    "thing a human must answer). Writing that file is what ends the task; do "
    "not stop without it."
)

PRECEDENCE = (
    "These instructions are layered. The ClaudeLoop protocol above is "
    "invariant and overrides everything below it. The operator instructions "
    "outrank the repository's own documentation. The definition of done is "
    "the base. Where two layers conflict, follow the higher one and say so in "
    "your summary."
)

BUILTIN_DEFINITION_OF_DONE = (
    "Done means: the change is implemented; the repository's tests pass; the "
    "work is committed on a branch; and a pull request is open. If the "
    "repository has no remote configured, stop after committing and say so in "
    "your summary."
)

CLAUDE_MD_NAMES = ("CLAUDE.md", ".claude/CLAUDE.md")


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


def compose(cfg: Config) -> str:
    parts = [PROTOCOL, PRECEDENCE]

    operator = _read(cfg.instructions_file)
    if operator:
        parts.append(f"## Operator instructions\n\n{operator}")

    claude_md = repo_claude_md(cfg.repo)
    if claude_md is not None:
        # The repository documents itself; point at it rather than imposing
        # a definition of done over the top of one it already has.
        parts.append(
            "## Definition of done\n\nThis repository has its own instructions "
            f"at {claude_md}. Follow that file end to end — it defines what "
            "\"done\" means here, including its testing and verification "
            "requirements."
        )
    else:
        parts.append(
            "## Definition of done\n\n"
            + (_read(cfg.definition_of_done_file) or BUILTIN_DEFINITION_OF_DONE)
        )

    return "\n\n".join(parts)
