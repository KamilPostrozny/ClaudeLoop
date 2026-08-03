# S7 — Proposed plugin set

**Date:** 2026-08-03
**Status:** built, then reversed by S8 (2026-08-04)

> **Reversed by S8.** This design assumed a session could not pick up the
> target repository's own plugin settings, so ClaudeLoop had to choose and
> install a curated set and describe it in the prompt. Measured against the
> real CLI, a headless session *does* honour a repository's
> `.claude/settings.json` (`enabledPlugins` included) and installs from it
> itself — it only cannot register the marketplace. S8 deleted the curated
> set, the reconciliation and the prompt layer, keeping just that one call.
> See `ROADMAP.md` § S8. Everything below is the record of what was decided
> at the time.

## The problem

A session runs with whatever plugins the box happens to have. `settings_file`
passes an operator-written `settings.json` through to `--settings`, which can
name `enabledPlugins`, but nothing installs them, nothing checks they are
there, and nothing tells the session how to use them. S1.1 decided that
deliberately — "pass through, do not manage".

Two things have made that decision wrong.

The first is S4. The Home Assistant addon image is a base image plus a copy,
built for an operator with no terminal. Telling that operator to run
`claude plugin install` by hand is telling them to do the one thing the whole
packaging slice exists to avoid.

The second is that a plugin changes how the session behaves, and this
repository's own experience is that behaviour needs instructions. `superpowers`
is the case in point: its `brainstorming` skill asks the human one question at
a time and refuses to implement until a human approves the design. Both are
correct at a keyboard. Under an orchestrator, one wastes a paid session asking
what a `git log` would have answered, and the other ends the turn waiting for
an approval that will never come — no result file, then a nudge, then burned
resumes, then `- [!]`.

## Scope

In: a curated set of three plugins, installed and enabled by ClaudeLoop at
startup; one config key selecting them; and a fourth prompt layer carrying
per-plugin usage instructions.

Out: any plugin outside the set beyond the escape hatch below, per-task plugin
selection, MCP server management (`mcp_config` already covers it), and hook
trust — the S4 landmine about first-run trust prompts stays with S4.

**This slice reverses half of S1.1's "pass through, do not manage".**
ClaudeLoop now installs and enables the plugins it proposes. `settings_file`
passthrough is untouched and stays the way an operator loads anything else.
S1.1's spec is not rewritten; this paragraph and `ROADMAP.md` record the
reversal.

## Design

### 1. `claudeloop/plugins.py`

One new module, one responsibility: what the proposed set is, getting it
installed, and the prompt text it contributes.

```python
@dataclass(frozen=True)
class Plugin:
    name: str          # the shorthand written in config.toml
    plugin_id: str     # name@marketplace, what the CLI takes
    marketplace: str   # source for `claude plugin marketplace add`
    usage: str = ""    # fourth-layer text; "" means the plugin contributes none

PROPOSED = (
    Plugin("superpowers", "superpowers@claude-plugins-official",
           "anthropics/claude-plugins-official", SUPERPOWERS_USAGE),
    Plugin("caveman", "caveman@caveman", "JuliusBrussee/caveman"),
    Plugin("ponytail", "ponytail@ponytail", "DietrichGebert/ponytail"),
)
```

Everything about a plugin sits in this one record — id, marketplace, and its
prompt text. `SUPERPOWERS_USAGE` is product prompt text living in `plugins.py`
rather than `prompt.py`, so that adding a plugin is one entry in one table
rather than an entry here and a constant over there. `CLAUDE.md`'s module
table and its "the prompt strings are the product" section both gain a line
saying so, because the rule that those strings change only with a covering
test and a live run has to follow the text.

**caveman and ponytail ship no usage text.** They were selected for the set
but carry nothing in the fourth layer: both already state their own rules
about what they do not compress and do not simplify, and ClaudeLoop's second
copy of those rules would drift out of step with the plugin's own. `usage=""`
is the ordinary case, not a placeholder.

### 2. Installing: `reconcile(names)`

Called from `main()` next to `worktree.probe`, before the dashboard binds and
before the first task runs.

1. Read the installed set once: `claude plugin list --json`. That returns a
   bare JSON list; the `--available` variant returns a dict keyed
   `installed`/`available`. Accept either shape (`d["installed"] if
   isinstance(d, dict) else d`) — one line against CLI version drift.
2. For each requested plugin, by `plugin_id`:
   - present and `enabled` → nothing;
   - present and disabled → `claude plugin enable <id> --scope user`;
   - absent → `claude plugin marketplace add <source>` (idempotent when the
     marketplace is already configured), then
     `claude plugin install <id> --scope user`.
3. Re-read the installed set and confirm every requested plugin is now present
   and enabled. A CLI that exits 0 having done nothing must not read as
   success.

**`--scope user`, not project or local.** Both of those write
`.claude/settings.json` or `.claude/settings.local.json` into the target
repository, which the constraint that nothing ClaudeLoop writes into a
repository may be committable forbids outright. They would also be
per-worktree, so every task would reinstall.

**Failure is fatal, like `worktree.probe`.** `reconcile` raises, `main()`
prints the message and exits. The alternative — warn and run on — means
sessions run for days in a shape the operator did not choose, and if the
fourth layer were still composed from config the prompt would describe skills
the session does not have, which is exactly the literal-minded-agent failure
this repository keeps paying for.

**A transient outage cannot stop a steady-state run.** Step 1 is a local read,
and no network call happens at all when everything requested is already
installed and enabled. Only a genuinely missing plugin reaches
`marketplace add` — that is, a fresh box or an operator's first run after
adding one to the list, which is the case where stopping is the right answer.

### 3. Config: one key

```toml
plugins = ["superpowers", "caveman", "ponytail"]
```

A new `Field("plugins", "list", step="plugins", default=())`. `_coerce` gains
a `list` branch: TOML hands over a real list, the wizard posts a JSON array,
and a bare string is split on commas so a hand-edited `plugins = "superpowers"`
is not silently a three-character list. Every entry is stripped, and empty
entries are dropped.

The check: an entry naming a `PROPOSED` plugin is fine; any other entry must
be in `plugin@marketplace` form, and is installed as written with no usage
text and no marketplace source — the marketplace must already be configured on
the box. A bare unknown name is a `validate()` error naming the proposed set,
because the alternative is a startup exit hours later on a typo the wizard
could have caught while the operator was still looking at it.

Omitting the key entirely selects nothing, `reconcile` runs no commands, and
the fourth layer is absent. Existing configs keep working untouched.

**Overriding the usage text** is a conventional file, not a config key:
`~/.claudeloop/plugin-usage/<name>.md`, read at compose time, replacing the
built-in text for that plugin when it exists. Same convention as
`instructions.md` and `definition-of-done.md`, and it costs nothing in
`SCHEMA` or the wizard. It also gives an operator a way to supply usage text
for a plugin outside the proposed set.

### 4. The fourth prompt layer

`compose` inserts, after the operator instructions and above the definition of
done:

```
## Plugin usage

### superpowers

<text>
```

One `###` block per selected plugin that has text, in `PROPOSED` order.
Selected plugins with no text contribute nothing, and when no selected plugin
has text the whole section is absent — no empty header.

`precedence()` gains a clause naming the layer, and only when it is present,
exactly as the operator clause already works: telling a session that plugin
usage instructions outrank the definition of done when there are none leaves
it reconciling against a document that does not exist. Order stated: protocol
invariant on top, then operator, then plugin usage, then the definition of
done. Plugin usage sits below the operator because it is ClaudeLoop's advice
about its own tooling and the operator must be able to overrule it.

`SUPERPOWERS_USAGE`, verbatim:

> The superpowers plugin is installed here and its skills apply, with two
> adjustments for running unattended.
>
> **Questions.** Its brainstorming skill asks a human one question at a time.
> That is right at a keyboard and wrong here. If the answer is in this
> repository — its code, its documentation, its roadmap, its git history — go
> and read it, and never ask. Ask only when the answer exists solely in the
> operator's head: priorities, money, who is watching, what "good" means here.
> Dispatching a subagent is for breadth, not for dodging a question: a fresh
> agent starts cold, costs real money re-deriving context you already have,
> and cannot answer a preference question anyway.
>
> **Approval.** Where a skill gates implementation on a human approving your
> design or plan first, that approval has already happened: the operator
> approved this work when they queued it as a task. Write the design document
> if the skill calls for one, record in your summary that you approved it
> yourself, and carry on. This covers only a gate waiting on sign-off for a
> plan of your own. It licenses nothing about tests, verification, or anything
> the definition of done requires.

The question rule's worth was measured on this repository: brainstorming S2b
asked five questions and two were answerable from `CLAUDE.md` and
`ROADMAP.md` alone.

### 5. The wizard

A new step, `{"id": "plugins", "title": "Plugins"}`, placed after
`instructions` and before `advanced`. It renders the `PROPOSED` set as three
checkboxes with each plugin's one-line reason, plus a free-text row for
`plugin@marketplace` entries outside the set. The page posts `plugins` as a
JSON array, which `validate()` coerces like any other field.

`dump_toml` gains an array branch in `_scalar`'s caller: `plugins =
["superpowers", "caveman"]`, each element through the existing `_scalar` so
the surrogate-pair and `U+007F` fixes apply to elements too. `_blank` treats
an empty list as blank, so an unselected set leaves the key out rather than
writing `plugins = []`.

The wizard does **not** install. `reconcile` runs at loop startup, which is
the same process moments later on the first-run path, and on every subsequent
start for a config that arrived any other way. One code path, one place
failures are reported.

### 6. Testing

Unit, against the existing fake-`claude`-script pattern rather than mocks:

- `reconcile` over each case — all present and enabled; present but disabled;
  absent, needing `marketplace add` then `install`; a failing `marketplace
  add`; and a CLI that exits 0 without installing anything, which step 3 must
  still catch.
- `reconcile` with an empty selection runs no subprocess at all.
- `plugins` coercion: TOML list, JSON array, comma string, blank entries; the
  unknown-bare-name error; a `plugin@marketplace` entry accepted.
- `dump_toml` writing an array that `tomllib` reads back identically, and
  omitting an empty one.
- `compose` pinning the new wording, the `###` block per plugin, `PROPOSED`
  order, the absent-section case, and `precedence()` with and without the
  layer.

### 7. The live smoke test

Two tasks, `haiku`, a scratch repository, against a box where at least one of
the three is genuinely not installed — the point is to watch
`marketplace add` and `install` run for real, which no fake CLI proves.

What it has to answer:

- all three install and enable at startup, and the loop then starts normally;
- the composed prompt actually carries the fourth layer, in the right place;
- a task phrased to trigger `brainstorming` implements the work instead of
  ending its turn waiting for approval — the defect this slice exists to
  prevent, and the one no fixture can show;
- the session does not ask a question answerable from the repository;
- an already-reconciled second start makes no network call.

## Open questions

None. The escape hatch for plugins outside the set is deliberately thin — a
`plugin@marketplace` string and an optional usage file — and if operators
reach for it often, `[[plugins]]` records carrying their own marketplace
source are the upgrade.
