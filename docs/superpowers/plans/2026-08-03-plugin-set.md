# S7 — Proposed plugin set: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ClaudeLoop installs and enables a small curated set of Claude Code
plugins at startup, and carries per-plugin usage instructions as a fourth
layer of the session's system prompt.

**Architecture:** One new module, `claudeloop/plugins.py`, owning three things:
the `PROPOSED` table (each plugin's shorthand name, `plugin@marketplace` id,
marketplace source, wizard blurb, and prompt text), `reconcile()` which drives
the `claude plugin` CLI to make the box match the selection, and
`usage_section()` which renders the prompt layer. One new config key,
`plugins`, selects from the table. `loop.main()` calls `reconcile` next to
`worktree.probe`, and refuses to start when it reports a problem.
`prompt.compose` inserts the layer between the operator instructions and the
definition of done.

**Tech Stack:** Python 3.11+, standard library only. `unittest`, real files on
disk, a fake `claude` shell script on `PATH` — no mocks, no third-party
packages.

**Spec:** `docs/superpowers/specs/2026-08-03-claudeloop-plugin-set-design.md`

## Global Constraints

- **Python 3.11+, standard library only.** No third-party packages, in the
  orchestrator, the tests, or the frontend.
- **No build step.** `static/setup.html` stays one file with inline CSS and an
  inline module script, making no off-origin requests.
- **Prompt strings are the product.** Every change to `SUPERPOWERS_USAGE`,
  `precedence()`, or any composed layer needs a covering test pinning the
  specific wording, and a live run afterwards.
- **Plugins install at `--scope user` only.** Project and local scope write
  `.claude/settings.json` or `.claude/settings.local.json` into the target
  repository, which the "nothing ClaudeLoop writes into a repository may be
  committable" constraint forbids.
- **`SCHEMA`'s declaration order is load-bearing.** A field's `required_if`
  and `check` see only the coerced values of fields declared *earlier* in the
  tuple.
- **Every subprocess this project spawns is hardened**: `capture_output=True`,
  `text=True`, `stdin=subprocess.DEVNULL`, and a bounded `timeout`. Copy
  `worktree._git`'s shape.
- **Existing configs keep working untouched.** Omitting `plugins` selects
  nothing, runs no `claude plugin` command, and composes no fourth layer.
- Run the whole suite with `python -m unittest discover -s tests -t .`
  (~75s). One module: `python -m unittest tests.test_plugins -v`.
- Work happens on branch `feat/plugin-set`, already created from `main`, which
  already carries the spec commit.

---

### Task 1: The `PROPOSED` table and the prompt layer it renders

**Files:**
- Create: `claudeloop/plugins.py`
- Test: `tests/test_plugins.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Plugin(name: str, plugin_id: str, marketplace: str, reason: str = "", usage: str = "")`, frozen dataclass.
  - `PROPOSED: tuple[Plugin, ...]` — superpowers, caveman, ponytail, in that order.
  - `SUPERPOWERS_USAGE: str`
  - `by_name(name: str) -> Plugin | None`
  - `usage_section(names: Sequence[str], home: Path) -> str` — `""` or a
    `## Plugin usage` block.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_plugins.py`:

```python
import tempfile
import unittest
from pathlib import Path

from claudeloop.plugins import (
    PROPOSED,
    SUPERPOWERS_USAGE,
    Plugin,
    by_name,
    usage_section,
)


class TableTest(unittest.TestCase):
    def test_the_proposed_set_is_three_plugins_in_a_fixed_order(self):
        self.assertEqual(
            [plugin.name for plugin in PROPOSED],
            ["superpowers", "caveman", "ponytail"],
        )

    def test_every_proposed_plugin_carries_an_id_marketplace_and_reason(self):
        for plugin in PROPOSED:
            self.assertIn("@", plugin.plugin_id, plugin.name)
            self.assertTrue(plugin.marketplace, plugin.name)
            # The wizard renders this next to the checkbox; a blank one is a
            # checkbox with no argument for ticking it.
            self.assertTrue(plugin.reason, plugin.name)

    def test_only_superpowers_carries_usage_text(self):
        # caveman and ponytail were selected for the set but contribute
        # nothing to the prompt: both already state their own rules, and a
        # second copy here would drift out of step with the plugin.
        self.assertEqual(
            [plugin.name for plugin in PROPOSED if plugin.usage],
            ["superpowers"],
        )

    def test_by_name_finds_a_proposed_plugin_and_nothing_else(self):
        self.assertEqual(by_name("caveman").plugin_id, "caveman@caveman")
        self.assertIsNone(by_name("nonesuch"))
        self.assertIsNone(by_name(""))


class SuperpowersUsageTest(unittest.TestCase):
    """Prompt text is the product here: these pin the two rules the text
    exists to carry, so a rewrite that drops one fails rather than passing
    quietly."""

    def test_it_states_the_question_discipline(self):
        self.assertIn("go and read it, and never ask", SUPERPOWERS_USAGE)
        self.assertIn("solely in the operator's head", SUPERPOWERS_USAGE)

    def test_it_refuses_a_subagent_as_a_way_to_dodge_a_question(self):
        self.assertIn("for breadth, not for dodging a question", SUPERPOWERS_USAGE)

    def test_it_resolves_the_approval_gate(self):
        self.assertIn("approved this work when they queued it", SUPERPOWERS_USAGE)

    def test_the_approval_licence_is_bounded(self):
        # A literal-minded agent told "approval already happened" will
        # generalise it to every gate it meets unless this sentence is here.
        self.assertIn("licenses nothing about tests, verification", SUPERPOWERS_USAGE)


class UsageSectionTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())

    def test_no_selection_renders_nothing(self):
        self.assertEqual(usage_section((), self.home), "")

    def test_a_selection_with_no_usage_text_renders_nothing(self):
        # No empty "## Plugin usage" header over two plugins that say nothing.
        self.assertEqual(usage_section(("caveman", "ponytail"), self.home), "")

    def test_a_plugin_with_text_renders_a_header_and_its_block(self):
        text = usage_section(("superpowers",), self.home)
        self.assertTrue(text.startswith("## Plugin usage\n\n### superpowers\n\n"))
        self.assertIn(SUPERPOWERS_USAGE, text)

    def test_blocks_follow_proposed_order_not_selection_order(self):
        one = Plugin("aaa", "aaa@m", "m", reason="r", usage="A text")
        two = Plugin("zzz", "zzz@m", "m", reason="r", usage="Z text")
        text = usage_section(("zzz", "aaa"), self.home, proposed=(one, two))
        self.assertLess(text.index("### aaa"), text.index("### zzz"))

    def test_a_plugin_usage_file_replaces_the_built_in_text(self):
        directory = self.home / "plugin-usage"
        directory.mkdir()
        (directory / "superpowers.md").write_text("operator's own wording\n")
        text = usage_section(("superpowers",), self.home)
        self.assertIn("operator's own wording", text)
        self.assertNotIn(SUPERPOWERS_USAGE, text)

    def test_a_usage_file_gives_a_plugin_outside_the_set_a_block(self):
        directory = self.home / "plugin-usage"
        directory.mkdir()
        (directory / "mine@market.md").write_text("how to use mine\n")
        text = usage_section(("mine@market",), self.home)
        self.assertIn("### mine@market\n\nhow to use mine", text)

    def test_an_unreadable_usage_file_falls_back_to_the_built_in(self):
        # Same rule prompt._read already follows: a layer is optional, and a
        # session must not fail to start over a permissions mistake.
        directory = self.home / "plugin-usage"
        directory.mkdir()
        path = directory / "superpowers.md"
        path.write_text("unreadable")
        path.chmod(0o000)
        self.addCleanup(path.chmod, 0o600)
        self.assertIn(SUPERPOWERS_USAGE, usage_section(("superpowers",), self.home))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_plugins -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'claudeloop.plugins'`

- [ ] **Step 3: Write the module**

Create `claudeloop/plugins.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_plugins -v`
Expected: PASS, 15 tests.

- [ ] **Step 5: Commit**

```bash
git add claudeloop/plugins.py tests/test_plugins.py
git commit -m "feat: the proposed plugin set and the prompt layer it renders"
```

---

### Task 2: The `plugins` config key

**Files:**
- Modify: `claudeloop/config.py` — `_coerce`, `SCHEMA`, `Config`, `load_config`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `plugins.PROPOSED`, `plugins.by_name` (Task 1).
- Produces: `Config.plugins: tuple[str, ...]`, defaulting to `()`; a
  `Field("plugins", "list", step="plugins", default=())` in `SCHEMA`; a
  `"list"` branch in `_coerce` returning `tuple[str, ...]`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py`, at the end of `ConfigTest`:

```python
    def test_plugins_defaults_to_nothing_selected(self):
        path = self.write(
            f'repo = "{self.repo}"\n'
            f'tasks_file = "{self.tmp}/tasks.md"\n'
        )
        cfg = load_config(path, home=self.tmp / "home")
        self.assertEqual(cfg.plugins, ())

    def test_plugins_reads_a_list(self):
        path = self.write(
            f'repo = "{self.repo}"\n'
            f'tasks_file = "{self.tmp}/tasks.md"\n'
            'plugins = ["superpowers", "caveman"]\n'
        )
        cfg = load_config(path, home=self.tmp / "home")
        self.assertEqual(cfg.plugins, ("superpowers", "caveman"))

    def test_plugins_accepts_a_comma_separated_string(self):
        # A hand-edited `plugins = "superpowers"` must not become a list of
        # eleven characters.
        values, errors = validate({"repo": str(self.repo),
                                   "tasks_file": str(self.tmp / "tasks.md"),
                                   "plugins": "superpowers, caveman"})
        self.assertEqual(errors, [])
        self.assertEqual(values["plugins"], ("superpowers", "caveman"))

    def test_plugins_drops_blank_entries(self):
        values, _ = validate({"repo": str(self.repo),
                              "tasks_file": str(self.tmp / "tasks.md"),
                              "plugins": ["superpowers", "", "  "]})
        self.assertEqual(values["plugins"], ("superpowers",))

    def test_plugins_rejects_a_name_outside_the_proposed_set(self):
        # Caught here rather than at startup hours later: the wizard can show
        # this while the operator is still looking at the screen.
        _, errors = validate({"repo": str(self.repo),
                              "tasks_file": str(self.tmp / "tasks.md"),
                              "plugins": ["superpowers", "nonesuch"]})
        self.assertEqual([key for key, _ in errors], ["plugins"])
        self.assertIn("nonesuch", errors[0][1])
        self.assertIn("plugin@marketplace", errors[0][1])

    def test_plugins_accepts_an_explicit_plugin_at_marketplace(self):
        values, errors = validate({"repo": str(self.repo),
                                   "tasks_file": str(self.tmp / "tasks.md"),
                                   "plugins": ["mine@market"]})
        self.assertEqual(errors, [])
        self.assertEqual(values["plugins"], ("mine@market",))

    def test_plugins_rejects_a_table(self):
        _, errors = validate({"repo": str(self.repo),
                              "tasks_file": str(self.tmp / "tasks.md"),
                              "plugins": {"superpowers": True}})
        self.assertEqual([key for key, _ in errors], ["plugins"])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_config -v`
Expected: FAIL — `AttributeError: 'Config' object has no attribute 'plugins'`
on the first two, and `KeyError: 'plugins'` on the rest.

- [ ] **Step 3: Add the type, the field, and the check**

In `claudeloop/config.py`, add the import at the top of the module, after the
existing imports:

```python
from . import plugins as plugins_module
```

In `_coerce`, before the final `return str(value)`:

```python
    if field.type == "list":
        if isinstance(value, str):
            items: list = value.split(",")
        elif isinstance(value, (list, tuple)):
            items = list(value)
        else:
            raise ValueError(
                f"{field.key} must be a list of names, not {value!r}"
            )
        return tuple(str(item).strip() for item in items if str(item).strip())
```

Add the validator next to the other module-level `_`-prefixed validators:

```python
def _known_plugins(value, values) -> str | None:
    """A bare name must be one ClaudeLoop knows how to install.

    Anything else has to say which marketplace it comes from, because
    reconcile() has nowhere else to learn it -- and that marketplace must
    already be configured on the box.
    """
    unknown = [
        name for name in value
        if "@" not in name and plugins_module.by_name(name) is None
    ]
    if not unknown:
        return None
    proposed = ", ".join(plugin.name for plugin in plugins_module.PROPOSED)
    return (
        f"plugins: {', '.join(unknown)} is not in the proposed set"
        f" ({proposed}). Name a plugin outside it as plugin@marketplace,"
        " with its marketplace already configured on this machine."
    )
```

In `SCHEMA`, immediately after the `definition_of_done_file` field (the
`instructions` step) and before the `settings_file` field:

```python
    Field("plugins", "list", step="plugins", default=(),
          check=_known_plugins, label="Plugins",
          help="Claude Code plugins ClaudeLoop installs and enables for every"
               " session, at user scope. Names from the proposed set, or"
               " plugin@marketplace for one outside it."),
```

In `Config`, after `strict_mcp`:

```python
    plugins: tuple[str, ...] = ()
```

In `load_config`'s `return Config(...)`, after `strict_mcp=values["strict_mcp"],`:

```python
        plugins=values["plugins"],
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_config -v`
Expected: PASS. Then `python -m unittest discover -s tests -t .` — the whole
suite must still pass; `tests/test_setup.py` walks `SCHEMA` and will fail if
the new field breaks a payload assumption.

- [ ] **Step 5: Commit**

```bash
git add claudeloop/config.py tests/test_config.py
git commit -m "feat: a plugins key selecting from the proposed set"
```

---

### Task 3: The fourth prompt layer

**Files:**
- Modify: `claudeloop/prompt.py` — `precedence`, `compose`
- Test: `tests/test_prompt.py`

**Interfaces:**
- Consumes: `plugins.usage_section` (Task 1), `Config.plugins` (Task 2).
- Produces: `precedence(has_operator: bool, has_plugins: bool = False) -> str`
  — the second parameter is new and keyword-safe for existing callers.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_prompt.py`. Extend the existing import from
`claudeloop.prompt` to include nothing new, and add at the top:

```python
from claudeloop.plugins import SUPERPOWERS_USAGE
```

Then add a class at the end of the file:

```python
class PluginLayerTest(unittest.TestCase):
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

    def test_no_plugins_means_no_layer_and_no_precedence_clause(self):
        text = compose(self.cfg())
        self.assertNotIn("## Plugin usage", text)
        self.assertNotIn("plugin usage instructions", text.lower())

    def test_a_selected_plugin_with_text_adds_the_layer(self):
        text = compose(self.cfg(plugins=("superpowers",)))
        self.assertIn("## Plugin usage", text)
        self.assertIn(SUPERPOWERS_USAGE, text)

    def test_a_selected_plugin_without_text_adds_nothing(self):
        text = compose(self.cfg(plugins=("caveman",)))
        self.assertNotIn("## Plugin usage", text)

    def test_the_layer_sits_below_the_operator_and_above_done(self):
        instructions = self.tmp / "instructions.md"
        instructions.write_text("operator says hello")
        text = compose(self.cfg(plugins=("superpowers",),
                                instructions_file=instructions))
        self.assertLess(text.index("## Operator instructions"),
                        text.index("## Plugin usage"))
        self.assertLess(text.index("## Plugin usage"),
                        text.index("## Definition of done"))

    def test_precedence_names_the_layer_only_when_it_is_present(self):
        self.assertNotIn("plugin usage", precedence(has_operator=True).lower())
        with_layer = precedence(has_operator=True, has_plugins=True)
        self.assertIn("plugin usage instructions", with_layer)
        self.assertIn("below the operator instructions", with_layer)

    def test_precedence_does_not_name_an_absent_operator_layer(self):
        # Same rule the operator clause already follows: never send a session
        # to reconcile against a layer that is not there.
        text = precedence(has_operator=False, has_plugins=True)
        self.assertIn("plugin usage instructions", text)
        self.assertNotIn("below the operator instructions", text)

    def test_the_layer_is_composed_from_the_operators_override_file(self):
        home = self.tmp / "home"
        (home / "plugin-usage").mkdir(parents=True)
        (home / "plugin-usage" / "superpowers.md").write_text("my rules")
        text = compose(self.cfg(plugins=("superpowers",)))
        self.assertIn("my rules", text)
        self.assertNotIn(SUPERPOWERS_USAGE, text)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_prompt -v`
Expected: FAIL — `TypeError: precedence() got an unexpected keyword argument
'has_plugins'`, and the layer assertions fail.

- [ ] **Step 3: Wire the layer into `compose`**

In `claudeloop/prompt.py`, add to the imports:

```python
from . import plugins as plugins_module
```

Replace `precedence`:

```python
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
        clause = (
            "The plugin usage instructions are ClaudeLoop's own advice about "
            "the tools it installed for you"
        )
        if has_operator:
            clause += ", and rank below the operator instructions"
        parts.append(clause + " and above the definition of done.")
    parts.append(
        "The definition of done is the base. Where layers conflict, follow "
        "the higher one and say so in your summary."
    )
    return " ".join(parts)
```

In `compose`, replace the first three statements:

```python
    operator = _read(cfg.instructions_file)
    plugin_usage = plugins_module.usage_section(cfg.plugins, cfg.home)
    parts = [
        PROTOCOL,
        precedence(has_operator=bool(operator), has_plugins=bool(plugin_usage)),
    ]
```

and insert, immediately after the `if operator:` block that appends the
operator section:

```python
    if plugin_usage:
        parts.append(plugin_usage)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_prompt -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add claudeloop/prompt.py tests/test_prompt.py
git commit -m "feat: compose plugin usage instructions as a fourth prompt layer"
```

---

### Task 4: `reconcile` — installing the selection

**Files:**
- Modify: `claudeloop/plugins.py`
- Create: `tests/fake_claude_plugin.sh`
- Test: `tests/test_plugins.py`

**Interfaces:**
- Consumes: `PROPOSED`, `by_name` (Task 1).
- Produces: `reconcile(names: Sequence[str]) -> str | None` — a message
  written for a human, or `None` when the box now matches. Same contract as
  `worktree.probe`, so `main()` treats them identically.

The fake CLI keeps its installed set as one plugin id per line in the file
named by `FAKE_PLUGIN_STATE`, a leading `!` meaning installed-but-disabled.
It renders that file as the JSON `claude plugin list --json` returns, appends
every invocation to `FAKE_PLUGIN_CALLS`, and honours two failure switches.

- [ ] **Step 1: Write the fake CLI**

Create `tests/fake_claude_plugin.sh`:

```bash
#!/usr/bin/env bash
# Stand-in for `claude plugin ...`, driven by files rather than mocks --
# the same choice tests/ already makes for the CLI and for Jira.
#
# FAKE_PLUGIN_STATE: one plugin id per line, "!" prefix meaning installed
#   but disabled. Read by `plugin list --json`, written by install/enable.
# FAKE_PLUGIN_CALLS: every invocation's arguments, appended one per line.
# FAKE_PLUGIN_FAIL: a substring; any invocation containing it exits 1.
# FAKE_PLUGIN_NOOP_INSTALL: if set, `plugin install` reports success and
#   changes nothing, which is the case reconcile's re-check exists for.
set -u

if [ -n "${FAKE_PLUGIN_CALLS:-}" ]; then
  printf '%s\n' "$*" >> "$FAKE_PLUGIN_CALLS"
fi

if [ -n "${FAKE_PLUGIN_FAIL:-}" ] && [[ "$*" == *"$FAKE_PLUGIN_FAIL"* ]]; then
  echo "fake failure for: $*" >&2
  exit 1
fi

touch "$FAKE_PLUGIN_STATE"

case "$1 $2" in
  "plugin list")
    printf '['
    first=1
    while IFS= read -r line; do
      [ -n "$line" ] || continue
      case "$line" in
        !*) enabled=false; id=${line#!} ;;
        *)  enabled=true;  id=$line ;;
      esac
      [ $first -eq 1 ] || printf ','
      first=0
      printf '{"id":"%s","scope":"user","enabled":%s}' "$id" "$enabled"
    done < "$FAKE_PLUGIN_STATE"
    printf ']\n'
    ;;
  "plugin install")
    if [ -z "${FAKE_PLUGIN_NOOP_INSTALL:-}" ]; then
      printf '%s\n' "$3" >> "$FAKE_PLUGIN_STATE"
    fi
    echo "installed $3"
    ;;
  "plugin enable")
    tmp=$(mktemp)
    sed "s|^!$3$|$3|" "$FAKE_PLUGIN_STATE" > "$tmp"
    mv "$tmp" "$FAKE_PLUGIN_STATE"
    echo "enabled $3"
    ;;
  "plugin marketplace")
    echo "marketplace $3 $4"
    ;;
  *)
    echo "fake claude: unexpected invocation: $*" >&2
    exit 2
    ;;
esac
```

Make it executable: `chmod +x tests/fake_claude_plugin.sh`

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_plugins.py`:

```python
import os
import shutil
import subprocess
import unittest.mock

from claudeloop.plugins import reconcile


class ReconcileTest(unittest.TestCase):
    """Against a fake `claude` on PATH, the same harness tests/test_loop.py
    uses for the real CLI."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        shutil.copy(Path(__file__).parent / "fake_claude_plugin.sh",
                    bin_dir / "claude")
        (bin_dir / "claude").chmod(0o755)
        self.state = self.tmp / "state.txt"
        self.calls = self.tmp / "calls.txt"
        self.state.write_text("")
        patch = unittest.mock.patch.dict(os.environ, {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_PLUGIN_STATE": str(self.state),
            "FAKE_PLUGIN_CALLS": str(self.calls),
        })
        patch.start()
        self.addCleanup(patch.stop)

    def calls_made(self) -> list[str]:
        if not self.calls.exists():
            return []
        return [line for line in self.calls.read_text().splitlines() if line]

    def test_an_empty_selection_runs_no_command_at_all(self):
        self.assertIsNone(reconcile(()))
        self.assertEqual(self.calls_made(), [])

    def test_an_already_installed_and_enabled_plugin_touches_nothing(self):
        self.state.write_text("superpowers@claude-plugins-official\n")
        self.assertIsNone(reconcile(("superpowers",)))
        # One read, and no network: this is the steady state on every start,
        # so a marketplace outage must not be able to stop the loop.
        self.assertEqual(self.calls_made(), ["plugin list --json"])

    def test_a_disabled_plugin_is_enabled_not_reinstalled(self):
        self.state.write_text("!caveman@caveman\n")
        self.assertIsNone(reconcile(("caveman",)))
        self.assertIn("plugin enable caveman@caveman --scope user",
                      self.calls_made())
        self.assertNotIn("install", " ".join(self.calls_made()))

    def test_a_missing_plugin_adds_its_marketplace_then_installs_it(self):
        self.assertIsNone(reconcile(("ponytail",)))
        calls = self.calls_made()
        self.assertIn("plugin marketplace add DietrichGebert/ponytail", calls)
        self.assertIn("plugin install ponytail@ponytail --scope user", calls)
        self.assertLess(calls.index("plugin marketplace add DietrichGebert/ponytail"),
                        calls.index("plugin install ponytail@ponytail --scope user"))

    def test_a_plugin_outside_the_set_installs_without_a_marketplace_add(self):
        self.assertIsNone(reconcile(("mine@market",)))
        calls = self.calls_made()
        self.assertIn("plugin install mine@market --scope user", calls)
        self.assertNotIn("marketplace", " ".join(calls))

    def test_a_failing_marketplace_add_is_reported_and_stops_the_loop(self):
        with unittest.mock.patch.dict(os.environ, {"FAKE_PLUGIN_FAIL": "marketplace"}):
            problem = reconcile(("ponytail",))
        self.assertIsNotNone(problem)
        self.assertIn("DietrichGebert/ponytail", problem)
        self.assertIn("fake failure", problem)

    def test_an_install_that_reports_success_and_does_nothing_is_caught(self):
        # The re-check exists for exactly this: a CLI exiting 0 having
        # installed nothing must not read as success, or the session runs
        # with a prompt describing skills it does not have.
        with unittest.mock.patch.dict(os.environ, {"FAKE_PLUGIN_NOOP_INSTALL": "1"}):
            problem = reconcile(("caveman",))
        self.assertIsNotNone(problem)
        self.assertIn("caveman@caveman", problem)

    def test_a_claude_that_is_not_on_path_is_reported_not_raised(self):
        with unittest.mock.patch.dict(os.environ, {"PATH": str(self.tmp / "empty")}):
            problem = reconcile(("caveman",))
        self.assertIsNotNone(problem)
        self.assertIn("claude", problem)

    def test_unparseable_output_is_reported_not_raised(self):
        bad = self.tmp / "bin" / "claude"
        bad.write_text("#!/usr/bin/env bash\necho not json\n")
        bad.chmod(0o755)
        problem = reconcile(("caveman",))
        self.assertIsNotNone(problem)
        self.assertIn("could not read", problem)

    def test_the_dict_shaped_list_output_is_accepted_too(self):
        # `claude plugin list --json` returns a bare list; the --available
        # variant returns {"installed": [...], "available": [...]}. Accepting
        # both is one line against CLI version drift.
        bad = self.tmp / "bin" / "claude"
        bad.write_text(
            "#!/usr/bin/env bash\n"
            "echo '{\"installed\":[{\"id\":\"caveman@caveman\",\"scope\":\"user\","
            "\"enabled\":true}]}'\n"
        )
        bad.chmod(0o755)
        self.assertIsNone(reconcile(("caveman",)))

    def test_a_plugin_installed_only_in_another_scope_is_installed_at_user(self):
        # Project and local scope are per-repository and cannot be used here,
        # so a project-scope row is not evidence this box has it.
        bad = self.tmp / "bin" / "claude"
        shutil.copy(Path(__file__).parent / "fake_claude_plugin.sh", bad)
        self.state.write_text("caveman@caveman\n")
        with unittest.mock.patch("claudeloop.plugins._installed",
                                 side_effect=[{"caveman@caveman": ("project", True)},
                                              {"caveman@caveman": ("user", True)}]):
            self.assertIsNone(reconcile(("caveman",)))
        self.assertIn("plugin install caveman@caveman --scope user",
                      self.calls_made())
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m unittest tests.test_plugins -v`
Expected: FAIL — `ImportError: cannot import name 'reconcile'`.

- [ ] **Step 4: Implement `reconcile`**

Append to `claudeloop/plugins.py`:

```python
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
        for plugin in wanted:
            scope, enabled = have.get(plugin.plugin_id, ("", False))
            if scope == "user" and enabled:
                continue
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
```

Note the marketplace name appears in the failure message via `_claude`'s
`' '.join(args)`, which is what `test_a_failing_marketplace_add_is_reported_and_stops_the_loop`
asserts on.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m unittest tests.test_plugins -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add claudeloop/plugins.py tests/fake_claude_plugin.sh tests/test_plugins.py
git commit -m "feat: install and enable the selected plugins at user scope"
```

---

### Task 5: `main()` reconciles before anything starts

**Files:**
- Modify: `claudeloop/loop.py:658-667` — the startup block in `main()`
- Test: `tests/test_loop.py`

**Interfaces:**
- Consumes: `plugins.reconcile` (Task 4), `Config.plugins` (Task 2).
- Produces: nothing new; `main()` raises `SystemExit` with `reconcile`'s
  message.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_loop.py`, at the end of the file:

```python
class MainReconcilesPluginsTest(unittest.TestCase):
    """main() must refuse to start on a box that could not get the plugins
    the operator chose -- the same treatment worktree.probe gets, and for
    the same reason: otherwise every task runs in a shape nobody asked for."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_a_plugin_problem_stops_startup_before_the_dashboard_binds(self):
        with unittest.mock.patch("claudeloop.loop.load_config") as load, \
             unittest.mock.patch("claudeloop.worktree.probe", return_value=None), \
             unittest.mock.patch("claudeloop.plugins.reconcile",
                                 return_value="marketplace unreachable") as reconcile, \
             unittest.mock.patch("claudeloop.loop._serve_dashboard") as serve, \
             unittest.mock.patch("claudeloop.loop.DEFAULT_CONFIG") as config_path:
            config_path.exists.return_value = True
            load.return_value = Config(repo=self.tmp, tasks_file=self.tmp / "t.md",
                                       home=self.tmp, plugins=("caveman",))
            with self.assertRaises(SystemExit) as raised:
                main([])
        self.assertIn("marketplace unreachable", str(raised.exception))
        reconcile.assert_called_once_with(("caveman",))
        serve.assert_not_called()

    def test_a_clean_reconcile_lets_startup_continue(self):
        with unittest.mock.patch("claudeloop.loop.load_config") as load, \
             unittest.mock.patch("claudeloop.worktree.probe", return_value=None), \
             unittest.mock.patch("claudeloop.plugins.reconcile", return_value=None), \
             unittest.mock.patch("claudeloop.loop._serve_dashboard") as serve, \
             unittest.mock.patch("claudeloop.loop.asyncio.run") as run, \
             unittest.mock.patch("claudeloop.loop.DEFAULT_CONFIG") as config_path:
            config_path.exists.return_value = True
            load.return_value = Config(repo=self.tmp, tasks_file=self.tmp / "t.md",
                                       home=self.tmp, plugins=("caveman",))
            main([])
        serve.assert_called_once()
        run.assert_called_once()
```

Check the file's existing imports first: it already imports `tempfile`,
`unittest`, `Path`, `Config` and `main`. Add `import unittest.mock` only if
it is not already there.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m unittest tests.test_loop.MainReconcilesPluginsTest -v`
Expected: FAIL — `test_a_plugin_problem_stops_startup_before_the_dashboard_binds`
with `AssertionError: SystemExit not raised`, because startup runs straight
past a problem nobody asked about.
`test_a_clean_reconcile_lets_startup_continue` passes already; it is the
regression guard that the new exit does not fire when `reconcile` is happy.

- [ ] **Step 3: Wire it into `main()`**

In `claudeloop/loop.py`, add `plugins` to the existing package-relative
import of `worktree` (currently `from . import ... worktree`), then after the
`worktree.probe` block:

```python
    # Same treatment, same reason: a box that could not get the plugins the
    # operator selected would otherwise run every task with a system prompt
    # describing tools the session does not have. Nothing runs here when
    # `plugins` is empty, and nothing touches the network when the box
    # already matches.
    problem = plugins.reconcile(cfg.plugins)
    if problem:
        raise SystemExit(problem)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_loop -v`
Expected: PASS. Then the whole suite:
`python -m unittest discover -s tests -t .`

- [ ] **Step 5: Commit**

```bash
git add claudeloop/loop.py tests/test_loop.py
git commit -m "feat: reconcile the plugin set at startup, beside the worktree probe"
```

---

### Task 6: The wizard's Plugins screen

**Files:**
- Modify: `claudeloop/setup.py` — `STEPS`, `_scalar`, `_blank`, `dump_toml`,
  `schema_payload`
- Modify: `claudeloop/static/setup.html` — `renderField`
- Test: `tests/test_setup.py`

**Interfaces:**
- Consumes: `plugins.PROPOSED` (Task 1), the `plugins` field (Task 2).
- Produces: `schema_payload` gains a `"proposed"` key —
  `[{"name": str, "reason": str}, ...]` — which the page renders as
  checkboxes; `dump_toml` emits `plugins = ["a", "b"]`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_setup.py`:

```python
class PluginsStepTest(unittest.TestCase):
    def test_the_wizard_has_a_plugins_step_before_advanced(self):
        ids = [step["id"] for step in STEPS]
        self.assertIn("plugins", ids)
        self.assertLess(ids.index("plugins"), ids.index("advanced"))
        self.assertLess(ids.index("instructions"), ids.index("plugins"))

    def test_the_schema_payload_carries_the_proposed_set(self):
        payload = schema_payload({})
        names = [entry["name"] for entry in payload["proposed"]]
        self.assertEqual(names, ["superpowers", "caveman", "ponytail"])
        for entry in payload["proposed"]:
            self.assertTrue(entry["reason"])

    def test_dump_toml_writes_a_plugins_array_that_reads_back(self):
        text = dump_toml({"repo": "/tmp/r", "plugins": ["superpowers", "caveman"]})
        self.assertIn('plugins = ["superpowers", "caveman"]', text)
        self.assertEqual(tomllib.loads(text)["plugins"], ["superpowers", "caveman"])

    def test_dump_toml_omits_an_empty_selection(self):
        # `plugins = []` would be a key that says nothing, and every other
        # unset key is left out too.
        text = dump_toml({"repo": "/tmp/r", "plugins": []})
        self.assertNotIn("plugins", text)

    def test_dump_toml_escapes_a_plugin_name_like_any_other_string(self):
        text = dump_toml({"repo": "/tmp/r", "plugins": ['odd"name@m']})
        self.assertEqual(tomllib.loads(text)["plugins"], ['odd"name@m'])
```

Add to the existing `WizardPageTest` (which asserts on the text of
`static/setup.html`):

```python
    def test_the_page_renders_a_list_field_as_checkboxes(self):
        page = (Path(__file__).parent.parent / "claudeloop" / "static"
                / "setup.html").read_text()
        self.assertIn('field.type === "list"', page)
        self.assertIn("schema.proposed", page)
        # The escape hatch for a plugin outside the set. Without it the only
        # way to use one is hand-editing the file the wizard exists to avoid.
        self.assertIn("plugin@marketplace", page)
```

Check `tests/test_setup.py`'s imports and add `STEPS`, `schema_payload`,
`dump_toml` and `tomllib` to them if they are not already imported.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_setup -v`
Expected: FAIL — `plugins` not in `STEPS`, `KeyError: 'proposed'`.

- [ ] **Step 3: Server side**

In `claudeloop/setup.py`, import the module:

```python
from . import plugins as plugins_module
```

Add the step to `STEPS`, between `instructions` and `advanced`:

```python
    {"id": "plugins", "title": "Plugins"},
```

In `_scalar`, before the `bool` branch (a `list` is not a `bool`, but keeping
the container check first reads better):

```python
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_scalar(item) for item in value) + "]"
```

In `_blank`, so an empty selection leaves the key out:

```python
def _blank(value: object) -> bool:
    """Whether to leave the key out entirely.

    An emitted `settings_file = ""` reads back as a path that does not exist,
    and load_config would then refuse the file this module just wrote. False
    and 0 are real values, not blanks; an empty list is not.
    """
    if isinstance(value, (list, tuple)):
        return not value
    return value is None or (isinstance(value, str) and not value.strip())
```

In `schema_payload`'s returned dict, add:

```python
        # The checkbox list the plugins screen renders. Names and one-line
        # reasons only -- the prompt text itself never goes to the browser.
        "proposed": [
            {"name": plugin.name, "reason": plugin.reason}
            for plugin in plugins_module.PROPOSED
        ],
```

- [ ] **Step 4: Page side**

In `claudeloop/static/setup.html`, inside `renderField`, add a branch before
the final `else` (the text-input branch):

```javascript
  } else if (field.type === "list") {
    input = document.createElement("div");
    // `drafted`, not `get`: an untouched list is nothing selected, and the
    // default is the empty tuple anyway.
    const chosen = new Set(drafted(field.key) ?? []);
    const proposed = new Set(schema.proposed.map((p) => p.name));
    const commit = () => put(field.key, [...chosen]);
    for (const plugin of schema.proposed) {
      const row = document.createElement("label");
      row.className = "check-row";
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = chosen.has(plugin.name);
      box.onchange = () => {
        box.checked ? chosen.add(plugin.name) : chosen.delete(plugin.name);
        commit();
      };
      const text = document.createElement("span");
      text.textContent = plugin.name + " — " + plugin.reason;
      row.append(box, text);
      input.append(row);
    }
    // Anything already selected that is not in the proposed set: an
    // operator's own plugin@marketplace entries, kept editable as one line
    // so saving the form cannot silently drop them.
    const extras = document.createElement("input");
    extras.type = "text";
    extras.placeholder = "plugin@marketplace, comma separated";
    extras.value = [...chosen].filter((name) => !proposed.has(name)).join(", ");
    extras.oninput = () => {
      for (const name of [...chosen]) if (!proposed.has(name)) chosen.delete(name);
      for (const name of extras.value.split(",").map((s) => s.trim())) {
        if (name) chosen.add(name);
      }
      commit();
    };
    input.append(extras);
```

The generic `input.oninput = ...` assignment that follows the branch chain
would overwrite the handlers above, so guard it:

```javascript
  if (field.type !== "list") {
    input.oninput = () => put(field.key, field.type === "bool" ? input.checked : input.value);
    input.onchange = input.oninput;
  }
```

Add the row style to the existing inline `<style>` block:

```css
.check-row { display: flex; gap: .5rem; align-items: baseline; margin: .25rem 0; }
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m unittest tests.test_setup -v`
Expected: PASS.

- [ ] **Step 6: Drive the page in a real browser**

Not optional, and not replaceable by the text assertions above: **every**
real client-side defect S5 found was invisible to them, including one that
made a first run impossible to complete. Start the wizard against a scratch
home:

```bash
python - <<'PY'
from pathlib import Path
from claudeloop import setup
home = Path("/tmp/claudeloop-wizard-check")
home.mkdir(exist_ok=True)
setup.run_setup(home / "config.toml", home)
PY
```

Open the printed URL, walk to the Plugins screen, and confirm by hand:

1. Three checkboxes render, each with its reason.
2. Ticking two and walking forward and back keeps both ticked.
3. Typing `mine@market` in the extras box, then ticking a checkbox, keeps
   both — the two controls must not clobber each other.
4. Saving writes `plugins = [...]` into `config.toml` with the entries in it.
5. Re-running with `--setup` comes back with those boxes already ticked.

Fix anything this finds before committing.

- [ ] **Step 7: Commit**

```bash
git add claudeloop/setup.py claudeloop/static/setup.html tests/test_setup.py
git commit -m "feat: a Plugins screen in the setup wizard"
```

---

### Task 7: Documentation

**Files:**
- Modify: `CLAUDE.md` — the module table, the prompt-strings section
- Modify: `README.md` — the config block, the instructions section, a new
  Plugins section
- Modify: `ROADMAP.md` — S7's row and entry

**Interfaces:**
- Consumes: everything above. Produces: no code.

- [ ] **Step 1: `CLAUDE.md`**

Add a row to the module table, after `setup.py`:

```markdown
| `plugins.py` | The proposed plugin set: its table, installing it, and the prompt layer it renders |
```

In "The prompt strings are the product", extend the first sentence so the
rule covers the new text:

```markdown
`PROTOCOL`, `precedence()` and `BUILTIN_DEFINITION_OF_DONE` in `prompt.py`,
and the `usage` text on every `Plugin` in `plugins.py`, are
not documentation.
```

Add to "Hard constraints", after the `CLAUDELOOP_RESULT` bullet:

```markdown
- **Plugins install at `--scope user`, never project or local.** Both of
  those write `.claude/settings.json` or `.claude/settings.local.json` into
  the target repository, which the constraint above forbids, and both would
  be per-worktree so every task would reinstall. S7 reversed half of S1.1's
  "pass through, do not manage" deliberately: ClaudeLoop installs the plugins
  it proposes, because the S4 addon operator has no terminal to do it in.
  `settings_file` passthrough is untouched.
```

- [ ] **Step 2: `README.md`**

Add to the `config.toml` block, after `strict_mcp`:

```toml
plugins                  = []     # optional, default [] -- see Plugins below
```

Renumber the layers in "The session's instructions": the list becomes four
entries, with the new one third:

```markdown
3. **Plugin usage instructions** — ClaudeLoop's own advice about the plugins
   it installed, one block per selected plugin that has any. It ranks below
   the operator instructions, because it is ClaudeLoop's advice and the
   operator runs the machine. Optional: absent when nothing selected has
   anything to say.
4. **Definition of done** — ...
```

Add a section after "The session's instructions":

```markdown
## Plugins

`plugins` names Claude Code plugins ClaudeLoop installs and enables for every
session:

```toml
plugins = ["superpowers", "caveman", "ponytail"]
```

Three names are built in, and each carries its marketplace so you don't have
to:

| Name | What it is |
|---|---|
| `superpowers` | Brainstorm, plan, test-drive and review, as explicit workflows |
| `caveman` | Terse output; code, commits and reports stay written normally |
| `ponytail` | Prefers the smallest solution that works over the general one |

Anything else is named as `plugin@marketplace` and its marketplace must
already be configured on the machine (`claude plugin marketplace add ...`).

Installation happens once at startup, at **user scope** — never project or
local scope, which would write into the repository being worked on.
ClaudeLoop reads the installed set first and touches the network only when
something is genuinely missing, so a marketplace outage cannot stop a loop
that is already reconciled. A plugin it cannot install stops startup with a
message rather than running days of sessions without it.

`superpowers` ships usage instructions, because two of its habits are wrong
under an orchestrator: its brainstorming skill asks a human one question at a
time, and it refuses to implement until a human approves the design. The
shipped text tells the session to read the repository instead of asking, and
that queuing the task *was* the approval. `caveman` and `ponytail` ship none —
both already state their own rules.

To replace what a plugin's block says, drop your own
`~/.claudeloop/plugin-usage/<name>.md`. The same file gives a plugin outside
the built-in three a block of its own.

Omitting `plugins` entirely installs nothing and adds no prompt layer.
```

- [ ] **Step 3: `ROADMAP.md`**

Move S7's row to `merged` in the slices table, move its section from "Next" to
"Built", and rewrite the entry as what is true now rather than what was
decided: the three plugins, the one config key, the fourth layer, `reconcile`
at startup and why it is fatal, the `plugin-usage/<name>.md` override, and the
reversal of S1.1's "pass through, do not manage". Record what the live smoke
test found — leave that paragraph until the smoke test has actually run.

Also add to "Open issues carried across slices":

```markdown
- Nothing ever *removes* a plugin. Dropping a name from `plugins` leaves it
  installed and enabled at user scope, so it keeps affecting every session
  and every other Claude Code run on the box. Deliberate: uninstalling
  something an operator may also use by hand is a worse failure than leaving
  it, and `claude plugin uninstall` is one command.
- `reconcile` pins no version. A plugin updated in its marketplace changes
  what sessions do with no config change and nothing in the run log to say
  so. `claude plugin install` takes no version, so pinning would mean
  managing the plugin cache directly.
```

- [ ] **Step 4: Run the whole suite one more time**

Run: `python -m unittest discover -s tests -t .`
Expected: PASS, ~75s.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md README.md ROADMAP.md
git commit -m "docs: the proposed plugin set"
```

---

### Task 8: The live smoke test

**Files:** none — this is a run, and then fixes for whatever it finds.

Not optional. Six of this repository's seven slices have run one; four found
defects a passing suite could not, eight between them, and two of those were
created *by the fix for* an earlier live finding.

- [ ] **Step 1: Prepare a box where at least one plugin is genuinely missing**

The point is to watch `marketplace add` and `install` run for real. Check
what is already there:

```bash
claude plugin list --json | python3 -c "import json,sys; print([p['id'] for p in json.load(sys.stdin) if p.get('scope')=='user'])"
```

If all three are already installed at user scope, uninstall one first:

```bash
claude plugin uninstall ponytail@ponytail --scope user
```

- [ ] **Step 2: Set up the scratch run**

A scratch git repository, `model = "haiku"`, **two tasks, not one** — several
past findings only appeared on the second task, where state left by the first
one matters. A tasks file *outside* the repository. `plugins = ["superpowers",
"caveman", "ponytail"]`. Set `commit.gpgsign false` locally in the scratch
repository, or `git commit` hangs on the 1Password agent.

Write the two tasks so the first would ordinarily trip the approval gate —
something phrased as a feature to design and build, not a one-line edit:

```markdown
- [ ] Add a `--verbose` flag to hello.py that prints the Python version too, and document it in the README
- [ ] Add a test file for hello.py covering both the plain and verbose output
```

- [ ] **Step 3: Run it and watch**

```bash
python -m claudeloop
```

- [ ] **Step 4: Confirm every claim, and write down what actually happened**

- The missing plugin's marketplace is added and the plugin installed, before
  the dashboard binds — check the log order.
- `claude plugin list --json` afterwards shows all three at user scope,
  enabled.
- A second start makes **no** `marketplace add` or `install` call at all.
- The composed prompt carries `## Plugin usage` with the superpowers block,
  positioned below the operator layer and above the definition of done —
  read it out of `~/.claudeloop/runs/<id>/events.jsonl`, not out of a test.
- Task 1 **implements the work** rather than ending its turn waiting for a
  human to approve a design. This is the defect the slice exists to prevent
  and the only way to see it.
- Neither task asks a question answerable from the repository.
- Both tasks end with a result file, are marked `- [x]`, and each commits on
  its own `claudeloop/<task-id>` branch.
- Cost: expect roughly ten to fifteen cents.

- [ ] **Step 5: Fix what it found, then run it again**

Any fix that changes prompt text gets a re-run: text fixes are exactly the
kind that come back differently broken — S5's live findings included one
defect introduced by the fix for the previous one.

- [ ] **Step 6: Record the findings in `ROADMAP.md` and commit**

```bash
git add -A
git commit -m "docs: what S7's live smoke test found"
```

---

### Task 9: Whole-branch review and merge

- [ ] **Step 1: Request a whole-branch code review**

Use `superpowers:requesting-code-review` against `main..feat/plugin-set`.

- [ ] **Step 2: Work through the findings**

Use `superpowers:receiving-code-review` — verify each finding technically
before implementing it, and push back on the ones that are wrong.

- [ ] **Step 3: Merge**

Use `superpowers:finishing-a-development-branch`. Do not push and do not open
a pull request: this repository has no usable remote.

- [ ] **Step 4: Update `ROADMAP.md`**

S7 `merged` in the table, and the working notes current.
