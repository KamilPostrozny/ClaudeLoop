"""The proposed plugin set: what it is, getting it installed, and the prompt
layer it contributes.

S1.1 decided ClaudeLoop would pass plugins through and never manage them.
S7 reverses half of that deliberately -- the S4 addon operator has no
terminal to run `claude plugin install` in, and a plugin that changes how a
session behaves needs instructions the same way the rest of the prompt does.
`settings_file` passthrough is untouched.

Each plugin's id, marketplace and prompt text live in one record on purpose,
so adding one is a single entry rather than a table here and a constant in
prompt.py. The usage strings are product prompt text: change them like code,
with a covering test pinning the wording and a live run afterwards.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("claudeloop")

SUPERPOWERS_USAGE = (
    "The superpowers plugin is installed here and its skills apply, with two "
    "adjustments for running unattended.\n\n"
    "**Questions.** Its brainstorming skill asks a human one question at a "
    "time. That is right at a keyboard and wrong here. If the answer is in "
    "this repository -- its code, its documentation, its roadmap, its git "
    "history -- go and read it, and never ask. Ask only when the answer "
    "exists solely in the operator's head: priorities, money, who is "
    "watching, what \"good\" means here. Dispatching a subagent is for "
    "breadth, not for dodging a question: a fresh agent starts cold, costs "
    "real money re-deriving context you already have, and cannot answer a "
    "preference question anyway.\n\n"
    "**Approval.** Where a skill gates implementation on a human approving "
    "your design or plan first, that approval has already happened: the "
    "operator approved this work when they queued it as a task. Write the "
    "design document if the skill calls for one, record in your summary that "
    "you approved it yourself, and carry on. This covers only a gate waiting "
    "on sign-off for a plan of your own. It licenses nothing about tests, "
    "verification, or anything the definition of done requires."
)
"""Both rules are live failure modes, not style. Brainstorming S2b in this
repository asked five questions and two were answerable from CLAUDE.md and
ROADMAP.md alone; and a skill that refuses to implement until a human
approves ends an unattended turn with no result file, which costs a nudge,
every remaining resume, and then the task."""


@dataclass(frozen=True)
class Plugin:
    name: str
    """The shorthand written in config.toml."""
    plugin_id: str
    """`name@marketplace`, which is what the CLI takes."""
    marketplace: str
    """Source for `claude plugin marketplace add`."""
    reason: str = ""
    """One line, shown beside the wizard's checkbox."""
    usage: str = ""
    """Prompt text for the fourth layer. Empty is the ordinary case."""


PROPOSED = (
    Plugin(
        "superpowers",
        "superpowers@claude-plugins-official",
        "anthropics/claude-plugins-official",
        reason="Brainstorm, plan, test-drive and review, as explicit workflows.",
        usage=SUPERPOWERS_USAGE,
    ),
    Plugin(
        "caveman",
        "caveman@caveman",
        "JuliusBrussee/caveman",
        reason="Terse output. Code, commits and reports stay written normally.",
    ),
    Plugin(
        "ponytail",
        "ponytail@ponytail",
        "DietrichGebert/ponytail",
        reason="Prefers the smallest solution that works over the general one.",
    ),
)

USAGE_DIR = "plugin-usage"
"""Under `home`. Dropping <name>.md here replaces a plugin's built-in text,
and gives one to a plugin outside the proposed set."""


def by_name(name: str) -> Plugin | None:
    for plugin in PROPOSED:
        if plugin.name == name:
            return plugin
    return None


def _override(name: str, home: Path) -> str:
    """Operator text for `name`, or "". Unreadable counts as absent: a
    permissions mistake must not stop a session starting."""
    try:
        return (home / USAGE_DIR / f"{name}.md").read_text().strip()
    except OSError:
        return ""


def usage_section(
    names: Sequence[str], home: Path, proposed: Sequence[Plugin] = PROPOSED
) -> str:
    """The fourth prompt layer, or "" when nothing selected has anything to
    say.

    Blocks follow `proposed` order rather than the operator's, so the prompt
    reads the same whatever order config.toml lists.
    """
    selected = list(names)
    blocks = []
    for plugin in proposed:
        if plugin.name in selected:
            text = _override(plugin.name, home) or plugin.usage
            if text:
                blocks.append(f"### {plugin.name}\n\n{text}")
    known = {plugin.name for plugin in proposed}
    for name in selected:
        if name not in known:
            text = _override(name, home)
            if text:
                blocks.append(f"### {name}\n\n{text}")
    if not blocks:
        return ""
    return "## Plugin usage\n\n" + "\n\n".join(blocks)


CLAUDE_TIMEOUT_S = 300
"""Bounds every `claude plugin` call. Installing clones a marketplace
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
            "cannot run `claude`: it is not on PATH. ClaudeLoop installs the"
            " plugins it proposes through the Claude Code CLI."
        )
    except OSError as error:
        raise PluginError(f"cannot run `claude {' '.join(args)}`: {error}")
    except subprocess.TimeoutExpired:
        raise PluginError(
            f"`claude {' '.join(args)}` did not finish within"
            f" {CLAUDE_TIMEOUT_S}s"
        )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise PluginError(
            f"`claude {' '.join(args)}` failed: {detail[-1] if detail else 'no output'}"
        )
    return result.stdout


def _installed() -> dict[str, tuple[str, bool]]:
    """Every installed plugin id, mapped to its scope and whether it is
    enabled. A local read: no network, which is why an already-reconciled
    box cannot be stopped by a marketplace being unreachable."""
    raw = _claude("plugin", "list", "--json")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise PluginError(
            "could not read the output of `claude plugin list --json`"
        )
    # A bare list today; the --available variant returns a dict. Accepting
    # both costs one line and survives the CLI changing its mind.
    rows = data.get("installed", []) if isinstance(data, dict) else data
    found: dict[str, tuple[str, bool]] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        plugin_id = row.get("id") or row.get("pluginId")
        if plugin_id:
            found[str(plugin_id)] = (
                str(row.get("scope", "")), bool(row.get("enabled"))
            )
    return found


def reconcile(names: Sequence[str]) -> str | None:
    """Make this box carry `names`, installed and enabled at user scope.

    A message written for a human, or None. Called once at startup beside
    worktree.probe, and fatal for the same reason: a loop that runs on
    without the plugins the operator chose spends days in a shape they did
    not ask for, one paid session at a time. Nothing here runs when the
    selection is empty or already satisfied.

    User scope, never project or local: those write into the target
    repository's .claude/, which nothing ClaudeLoop writes may do, and would
    be per-worktree besides.
    """
    wanted = [by_name(name) or Plugin(name, name, "") for name in names]
    if not wanted:
        return None
    try:
        have = _installed()
        changed = False
        for plugin in wanted:
            scope, enabled = have.get(plugin.plugin_id, ("", False))
            if scope == "user" and enabled:
                continue
            changed = True
            if scope == "user":
                log.info("enabling %s", plugin.plugin_id)
                _claude("plugin", "enable", plugin.plugin_id, "--scope", "user")
                continue
            if plugin.marketplace:
                log.info("adding marketplace %s", plugin.marketplace)
                # Idempotent when it is already configured -- confirmed
                # against the real CLI, which exits 0 saying so.
                _claude("plugin", "marketplace", "add", plugin.marketplace)
            log.info("installing %s", plugin.plugin_id)
            _claude("plugin", "install", plugin.plugin_id, "--scope", "user")
        if not changed:
            return None
        # Re-read rather than trusting the exit codes: a CLI that reports
        # success having done nothing would otherwise leave the session with
        # a prompt describing skills it does not have.
        after = _installed()
        missing = [
            plugin.plugin_id for plugin in wanted
            if after.get(plugin.plugin_id, ("", False)) != ("user", True)
        ]
        if missing:
            return (
                f"these plugins are still not installed and enabled at user"
                f" scope after trying: {', '.join(missing)}. Install them with"
                " `claude plugin install <name> --scope user`, or remove them"
                " from `plugins` in config.toml."
            )
    except PluginError as error:
        return str(error)
    return None
