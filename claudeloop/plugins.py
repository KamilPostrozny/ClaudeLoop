"""The proposed plugin set: what it is, getting it installed, and the prompt
layer it contributes.

S1.1 decided ClaudeLoop would pass plugins through and never manage them.
S7 reverses half of that deliberately -- the S4 addon operator has no
terminal to run `claude plugin install` in, and a plugin that changes how a
session behaves needs instructions the same way the rest of the prompt does.
`settings_file` passthrough is untouched.

Each plugin's id, marketplace and prompt text live in one record on purpose,
so adding one is a single entry rather than a table here and a constant in
prompt.py. No proposed plugin carries `usage` today -- every one of them
states its own rules -- so the fourth prompt layer comes from an operator's
own file under `plugin-usage/`. A `usage` string is product prompt text:
change one like code, with a covering test pinning the wording and a live
run afterwards.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("claudeloop")


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
    """Resolve a `plugins` entry to a proposed plugin by either spelling:
    its short name, or its full `plugin_id` (`name@marketplace`) -- the form
    `claude plugin list` prints and README.md tells operators to use for
    anything outside the built-in set. Both must resolve, or a fully
    qualified proposed plugin installs correctly but silently composes no
    prompt layer at all."""
    for plugin in PROPOSED:
        if name in (plugin.name, plugin.plugin_id):
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
    reads the same whatever order config.toml lists. A selection entry
    matches a proposed plugin by either its short name or its full
    `plugin_id` -- the same two spellings `by_name` accepts -- so a fully
    qualified `plugin@marketplace` entry still gets its block, under the
    plugin's short name.
    """
    selected = list(names)
    blocks = []
    for plugin in proposed:
        if plugin.name in selected or plugin.plugin_id in selected:
            text = _override(plugin.name, home) or plugin.usage
            if text:
                blocks.append(f"### {plugin.name}\n\n{text}")
    known = {name for plugin in proposed for name in (plugin.name, plugin.plugin_id)}
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
    box cannot be stopped by a marketplace being unreachable.

    The real CLI emits one row per scope a plugin is installed in, id
    repeated -- a plugin installed at both `project` and `user` scope is two
    rows. reconcile only ever cares about `user`, so a user-scope row always
    wins regardless of which row the CLI happens to emit last; a non-user
    row only fills in when no user-scope row has been seen yet.
    """
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
        if not plugin_id:
            continue
        plugin_id = str(plugin_id)
        scope = str(row.get("scope", ""))
        if found.get(plugin_id, ("", False))[0] == "user" and scope != "user":
            continue  # keep the user-scope row already recorded
        found[plugin_id] = (scope, bool(row.get("enabled")))
    return found


def reconcile(names: Sequence[str]) -> str | None:
    """Make this box carry `names`, installed and enabled at user scope.

    A message written for a human, or None. Called once at startup beside
    worktree.probe, and fatal for the same reason: a loop that runs on
    without the plugins the operator chose spends days in a shape they did
    not ask for, one paid session at a time. An empty selection runs no
    subprocess at all; an already-satisfied non-empty selection touches the
    network not at all -- one local read and nothing else.

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
                # against the real CLI, which exits 0 saying so. --scope
                # user is explicit, matching enable and install below: the
                # CLI's default for `marketplace add` is user scope today,
                # but nothing pins it, and _claude never sets cwd, so an
                # auto-detecting default would otherwise write into whatever
                # directory ClaudeLoop was launched from -- plausibly the
                # target repository.
                _claude("plugin", "marketplace", "add", plugin.marketplace,
                        "--scope", "user")
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
