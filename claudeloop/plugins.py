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
