"""Make the target repository's own plugin settings work on this box.

S1.1 decided ClaudeLoop would pass plugins through and never manage them.
S7 reversed half of that with a curated set ClaudeLoop installed and wrote
prompt text for; S8 drops the curation and keeps only the part the box
actually cannot do for itself.

Claude Code honours a repository's `.claude/settings.json` in a headless
`claude -p` run -- `enabledPlugins` included -- and auto-installs a
project-declared plugin at session start, writing nothing into the
repository. It does that only for a marketplace this machine already knows:
project-declared `extraKnownMarketplaces` is ignored, and hand-writing the
same table into user settings is not enough either, because the registry a
session reads is `~/.claude/plugins/known_marketplaces.json`, which only
`claude plugin marketplace add` fills in. So that one call, once per
marketplace the repository names, is the whole feature.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

log = logging.getLogger("claudeloop")

PROJECT_SETTINGS = ".claude/settings.json"

CLAUDE_TIMEOUT_S = 300
"""Bounds every `claude plugin` call. Adding a marketplace clones a
repository, which is slow on a cold box and not slow enough to justify an
unattended loop hanging on it forever."""


class PluginError(Exception):
    """Carries a message already written for a human."""


def _claude(*args: str) -> str:
    """One `claude` invocation, hardened for an unattended caller: no
    inherited stdin (a prompt would otherwise block forever reading from the
    loop's own terminal) and a bounded timeout."""
    try:
        result = subprocess.run(
            ["claude", *args],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=CLAUDE_TIMEOUT_S,
        )
    except FileNotFoundError:
        raise PluginError(
            "cannot run `claude`: it is not on PATH. ClaudeLoop registers the"
            " plugin marketplaces the repository declares through the Claude"
            " Code CLI."
        )
    except OSError as error:
        raise PluginError(f"cannot run `claude {' '.join(args)}`: {error}")
    except subprocess.TimeoutExpired:
        raise PluginError(
            f"`claude {' '.join(args)}` did not finish within {CLAUDE_TIMEOUT_S}s"
        )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise PluginError(
            f"`claude {' '.join(args)}` failed: {detail[-1] if detail else 'no output'}"
        )
    return result.stdout


def marketplace_sources(repo: Path) -> dict[str, str]:
    """Every marketplace the repository declares, name to the argument
    `claude plugin marketplace add` takes.

    A missing, unreadable or malformed settings file is 'declares nothing':
    it is the repository's file, not ClaudeLoop's, and a syntax error in it
    must not stop the loop starting -- the CLI ignores it too.
    """
    try:
        data = json.loads((repo / PROJECT_SETTINGS).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    table = data.get("extraKnownMarketplaces") if isinstance(data, dict) else None
    sources: dict[str, str] = {}
    for name, entry in (table or {}).items() if isinstance(table, dict) else ():
        source = entry.get("source", entry) if isinstance(entry, dict) else entry
        if isinstance(source, str):
            sources[str(name)] = source
            continue
        if not isinstance(source, dict):
            continue
        # github -> owner/repo, directory -> path, git/url -> url. The CLI
        # takes all three as one positional argument and works out which.
        value = source.get("repo") or source.get("path") or source.get("url")
        if value:
            sources[str(name)] = str(value)
        else:
            log.warning("ignoring marketplace %s: no repo, path or url in it", name)
    return sources


def register_marketplaces(repo: Path) -> str | None:
    """Register what the repository declares. A message for a human, or None.

    Called once at startup beside worktree.probe, and fatal for the same
    reason: a repository whose plugins never load runs every task in a shape
    nobody chose, one paid session at a time. A repository declaring no
    marketplace runs no subprocess at all; `marketplace add` is idempotent,
    so a box that already has one is a fast exit-0.

    User scope, never project or local: those write into the target
    repository's .claude/, which nothing ClaudeLoop writes may do.
    """
    try:
        for name, source in marketplace_sources(repo).items():
            log.info("registering marketplace %s (%s)", name, source)
            _claude("plugin", "marketplace", "add", source, "--scope", "user")
    except PluginError as error:
        return (
            f"{error}. Remove the marketplace from {repo / PROJECT_SETTINGS},"
            " or register it by hand with"
            " `claude plugin marketplace add <source> --scope user`."
        )
    return None
