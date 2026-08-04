# ClaudeLoop Roadmap

**This file is the resume point.** It records what is built, what is next, what
has already been decided about the slices that are not built yet, and what is
still open. Read it before starting work; update it when a slice lands.

The specs under `docs/superpowers/specs/` record what was decided *at the time*
and are not rewritten as things change. This file records what is true *now*.

## Slices

| | Slice | State |
|---|---|---|
| **S1** | Core loop | merged |
| **S1.1** | Session environment | merged |
| **S2a** | Read-only web dashboard | merged |
| **S3** | Jira task source | merged |
| **S2b** | Question and answer channel | merged |
| **S6** | A git worktree per task | merged |
| **S5** | Setup wizard and config schema | merged |
| **S7** | Proposed plugin set | merged, reversed by S8 |
| **S4** | Home Assistant OS addon | merged |
| **S8** | Repository-owned plugins | merged |
| **S9** | Resume an interrupted task | merged |
| **S9.1** | Locale-proof Jira transitions | merged, live check outstanding |
| **S10** | The repository's instructions come first | merged |
| **S11** | Backlog defects | merged, Jira live check outstanding |
| **S12** | A stranded task can come back | implemented, **live smoke test not run** |

Two orderings were deliberate. **S3 preceded S2b** so the answer channel was
designed against two task sources at once, rather than built for the web and
retrofitted to Jira — and it paid: the Jira channel shaped the protocol's
third and fourth verbs, not the web one. **S5 follows S3** so its config
schema is written once against the final key set, absorbing the cost of
folding S1.1's and S3's hand-written validation into it. **S4 is
free-floating** — nothing blocks it, and it is the slice that stops a laptop
being the thing that dies mid-task.

---

## Built

### S1 — Core loop

`python -m claudeloop` takes tasks one at a time from a markdown checklist,
runs one headless `claude -p` session per task against a target repository,
and decides the task is finished when the session writes a JSON result file to
`$CLAUDELOOP_RESULT`. Survives subscription rate limits by reading `resetsAt`
out of the CLI's `rate_limit_event` stream message and resuming the same
session by pre-assigned UUID. Outcomes go to SQLite; the raw stream is teed to
`events.jsonl` per run.

Spec: `docs/superpowers/specs/2026-07-31-claudeloop-core-loop-design.md`

### S1.1 — Session environment

Three instruction layers with an explicit precedence — ClaudeLoop's invariant
protocol, the operator's `instructions.md`, and a definition of done which is
the repository's own `CLAUDE.md` when it has one and a built-in otherwise.
Plugin and MCP flag passthrough. A `[session_env]` table so credentials reach
the session on a box with no ambient git or forge auth. Refuses a `config.toml`
readable beyond its owner, and refuses a `tasks_file` inside the target
repository.

Spec: `docs/superpowers/specs/2026-08-01-claudeloop-session-environment-design.md`

### S2a — Read-only web dashboard

`ThreadingHTTPServer` on a daemon thread inside the orchestrator's process,
reading `state.db` read-only and tailing `events.jsonl` off disk. Live session
output over Server-Sent Events, status beacon with a real heartbeat, quota
meter, pending and completed lists, task drill-down. One no-build HTML file.
Localhost by default; a token is required to bind wider.

Spec: `docs/superpowers/specs/2026-07-31-claudeloop-web-dashboard-design.md`

### S3 — Jira task source

A second `TaskSource` implementation. The protocol carried most of it — the
loop still talks only to `pending()`, `start()` and `mark()` — but the design's
claim that nothing else would change did not survive contact: `loop.py` now
runs every source call through `asyncio.to_thread` (they are blocking HTTP
under Jira), the dashboard's pending list moved onto the status snapshot
because `cfg.tasks_file` is `None` here, and `state.db` gained
`terminal_ids()`. Jira Cloud, REST v2, API-token Basic auth over `urllib`. `config.py`
composes the operator's `jql` from a `project`/`status` shorthand when they
don't give one outright, and `pending()` splices a label-exclusion guard onto
that query so a finished ticket cannot be picked up again, then turns each
matching issue into one task carrying its key, summary and description. `mark()` labels the issue `claudeloop-done` or
`claudeloop-blocked`, posts a closing comment with status, summary and cost,
then fires `transition_done` if the workflow offers it from the issue's
current status; `start()` fires `transition_start` the same way. A terminal row
in `state.db` is what actually stops a second run, and the live smoke test is
what proved it load-bearing: Jira's search index is eventually consistent, so a
ticket labelled `claudeloop-done` still matched the query 0.8 seconds later.
The label closes the window; the database covers it. The session reaches Jira
through `python -m claudeloop.jira show`/`comment`; it cannot transition
issues or touch labels, so a confused session can't park a ticket somewhere
the operator didn't expect. An unreachable Jira, a 401, or a JQL Jira rejects
all read as an empty backlog, so the loop idles and retries instead of
failing tasks.

Spec: `docs/superpowers/specs/2026-08-01-claudeloop-jira-task-source-design.md`

### S2b — Question and answer channel

A session that hits something only a human can decide writes `blocked` with a
question. The task **parks** rather than stalling the loop: it is marked in
its source, recorded, and stepped over, so a question asked at 2am does not
waste the night. A human answers on the dashboard or with a `claudeloop:`
comment on the Jira ticket, and the loop picks that task up ahead of new work,
resuming the *same session* by `--resume` — which still holds the repository
context nothing else can reconstruct.

The `TaskSource` protocol grew two verbs, not the one the design predicted:
`reopen(task)` undoes the blocked mark, and `answer(task)` is the source's own
reply channel — `None` for a checklist, a comment scan for Jira. The Jira scan
is ordered against ClaudeLoop's own newest question comment rather than
against stored state, so a task that blocks twice reads the second answer
across restarts with nothing persisted.

The answer crosses the loop/web boundary as a **file**, `runs/<id>/answer.json`,
consumed as it is read. That was chosen precisely to dodge the hazard
`status.py` documents: a human answering from the web thread would have been
the second writer to a read-modify-write. Writing a file means the web thread
never calls `set_status` at all — the hazard is dodged, not solved, and the
docstring now says so.

`run_task` gained `resume_with`, which reuses the recorded `session_id` and
skips `source.start` — re-firing `transition_start` against an issue already
in that status is only noise. There is a real fallback for a task with no
session left to resume — a pre-S2b database, or pruned runs — which starts the
task over with the answer in the prompt.

The design also had a resume skip `reset_to_default_branch`, on the reasoning
that a resume is the same task continuing rather than a new one. **The live
smoke test proved that wrong** and it was removed before merge: a session
usually parks *early*, before its first commit, so there is no branch of its
own to preserve — and skipping the reset meant the resumed session inherited
whatever branch the previous task left checked out, then committed onto it.
Seen on both task sources. That finding is what S6 was written from, and
`reset_to_default_branch` no longer exists.

Three prompt strings changed, because two of them had become lies: `PROTOCOL`
opened "Nobody is watching", and `NUDGE_PROMPT` said "Nobody is available to
answer a question". The rewrite keeps the bar for asking high while telling
the truth, and a first draft of it asserted that other tasks queue behind a
blocked one — the opposite of what this slice built, and caught in review
before it shipped.

The dashboard gained the project's **first write route**, deliberately
breaking S2a's read-only rule ahead of S5. Its guards are load-bearing: at the
loopback default `web_token` is empty, so the `Host` check and the
`Content-Type: application/json` requirement are what stand between an
arbitrary web page and an agent running with bypassed permissions. See the
working note below on why `do_POST` closes its connection.

Spec: `docs/superpowers/specs/2026-08-01-claudeloop-question-answer-channel-design.md`

### S6 — A git worktree per task

Every task runs in `~/.claudeloop/worktrees/<task-id>`, on a branch
ClaudeLoop cuts itself: `git worktree add -b claudeloop/<task-id> <path>
<default-branch>`. There is no shared working tree left, so there is nothing
to inherit and nothing to reset — `reset_to_default_branch` is deleted, and
`session.run` and `prompt.compose` take the worktree path where they used to
take `cfg.repo`, which is now only the repository to branch *from*.

Creating the branch is the point. The built-in definition of done had told
sessions to branch before their first commit since S1, at about 50%
compliance; a session cannot fail to comply with an instruction it is not
given. Three prompt strings changed with it: the definition of done now
states the session is already on its branch, may rename it, and must never
check out the default branch and commit there; `ANSWER_PROMPT`'s
branch-checkout clause is gone, replaced with the opposite assurance; and
`FRESH_ANSWER_PROMPT` stops claiming commits that a pre-S6 parked task never
put on that branch.

`ensure(repo, root, task_id)` returns the same path every time for a task,
and reuses the tree when one is already there — the test is `.git` existing
inside it, a proxy for registration rather than registration itself. That
reuse is what makes a parked task survive intact, branch, commits and
uncommitted changes, until its answer arrives. If the tree is gone but `claudeloop/<task-id>` still
exists, `add` is retried against the branch so an answered task lands back on
its own work. `release(repo, path)` runs after any non-`blocked` result and
is never forced: git refuses to remove a dirty tree, and that refusal is the
feature. `probe(repo)` runs `git worktree prune` and resolves the default
branch at startup; `main()` exits with its message rather than failing every
task in turn, one paid session at a time.

Two things changed under implementation. A worktree that cannot be created is
an **environment fault, not a verdict**: it propagates out of `run_task`, and
`main_loop`'s crash handler records `error` with no `source.mark`. The design
first said `failed` and marked — but `failed` is terminal, and a held
`index.lock` or a full disk stops every task equally, so that would have
burned the whole list to `- [!]` in seconds. And `FRESH_ANSWER_PROMPT` lost
the clause asserting an earlier attempt's commits are on the branch: true for
a task pruned since S6, false for one parked before it.

**The live smoke test passed, and is the first one that found nothing wrong
with the slice it tested.** Two tasks on `haiku` against a scratch
repository, $0.13: task 1 parked on a question it could not decide, task 2
ran and finished while it was parked, the question was answered on the
dashboard, and task 1 resumed. Every claim held. Each task committed on its
own `claudeloop/<task-id>` branch carrying exactly its own commit — task 1's
branch has the LICENSE and not task 2's function, which is the S2b defect
this slice exists to remove. The parked tree sat untouched in
`~/.claudeloop/worktrees/` for the whole of task 2. `--resume` reattached
from a worktree cwd on the same session id, and the resumed session went
straight to the work for $0.016 rather than rediscovering the repository.
The scratch repository never left `main` and its tree was never dirtied.
Both worktrees were released on completion, leaving the directory empty and
both branches in place.

Two things it confirmed rather than caught: no session tried to check out the
default branch (which under a worktree fails with `already checked out at`),
and none renamed its branch, though the prompt now allows it. It also
surfaced one pre-existing accounting bug, recorded in the open issues below:
a task that parks and is answered reports only the cost of its resume.

Spec: `docs/superpowers/specs/2026-08-02-claudeloop-worktree-per-task-design.md`

### S5 — Setup wizard and config schema

`config.py` gained a frozen `Field` dataclass and a `SCHEMA` tuple describing
all 23 keys — 15 top-level, 8 under `[jira]` — each carrying its type,
default, wizard screen (`step`), label, help text, and the `required_if`/
`check` callables that used to be hand-written `if` statements. `validate(data)
-> (values, errors)` walks the table once and returns coerced values plus
**every** error found, not just the first; `load_config` calls the same
function and raises on `errors[0]`, appending a clause naming how many more
problems there are, so its behaviour from the command line is unchanged. The
hand-written `_jql`, `_jira`, `_optional_path`, `REQUIRED_KEYS` and `JIRA_KEYS`
are gone. **`SCHEMA`'s declaration order is load-bearing**: `tasks_file`'s
check reads `repo`, `web_token`'s condition reads `web_host`, `jira.project`'s
reads `jira.jql` — a field's `required_if` and `check` see only the coerced
values of fields declared earlier in the tuple.

`setup.py` (new) is the wizard: a schema-driven TOML emitter (`dump_toml`), a
loopback-only HTTP server subclassing `web.Handler` — inheriting the `Host`
check, the token check, and the request-smuggling fix `do_POST`'s
`close_connection = True` gives it for free rather than a hand-rolled second
handler losing it — and the three live checks (`repo`, Jira, `claude auth
status`). `python -m claudeloop` with no config, or `--setup` with one, calls
`setup.run_setup()` and blocks until a valid file is written, then falls
through into the ordinary `load_config` → `worktree.probe` → loop startup
path unchanged: the config that runs is the one the ordinary loader reads back
off disk, never the wizard's own in-memory parse, so a file the wizard could
write but the loader would reject is impossible to actually run on.

**Setup mode's two barriers are independent on purpose.** It binds `127.0.0.1`
unconditionally, ignoring whatever an existing config's `web_host` says, and
requires a `secrets.token_urlsafe(32)` printed to the console on every
request, page load included. With no config there is no `web_token` to
authenticate against, so the network barrier can't be the only one.

Five things surfaced across the build and a first real-browser pass that the
design had not anticipated:

- `json.dumps`'s default `ensure_ascii=True` encodes a non-BMP character as a
  UTF-16 surrogate pair, which TOML rejects outright as not a Unicode scalar
  value — one emoji in a `[session_env]` value made `tomllib` fail on the
  *whole file* the wizard had just written. `ensure_ascii=False` alone then
  opens the other end: it emits `U+007F` raw, which a TOML basic string
  forbids. The fix needed both, plus quoting for a `[session_env]` name that
  isn't a bare TOML key — a name containing a dot silently parses as a nested
  table otherwise.
- `write_config` narrows the open fd's mode with `os.fchmod` before writing,
  because `O_CREAT`'s mode only applies when the call creates the file —
  rewriting an existing `0644 config.toml`, which is exactly the `--setup`
  path, would otherwise put a Jira token on disk world-readable for the length
  of the write.
- The file is written from `validate()`'s coerced values, not the browser's
  own strings (`_typed()` exists for exactly this): a form posts every field
  as a string, so the submission verbatim would have written
  `web_port = "9999"` and `strict_mcp = "false"` as quoted TOML — survivable by
  `load_config`'s lenient coercion, but then handed back to the browser on the
  next `--setup` as the JS-truthy string `"false"` for what is really `False`.
- `merge_secrets` treats an absent section as unchanged, not cleared, so an
  operator on `source = "file"` doesn't silently lose a stored Jira token by
  saving.
- Driving the actual page in a real browser — not just asserting on its text,
  which is all three earlier review rounds had done — found the worst of the
  five: the page's `get()` did not fall back to a field's default, so on a
  genuine first run it believed `source` was `""` while the server defaulted
  it to `"file"`. The `<select>` displayed "file" anyway, because its own
  render branch falls back separately, and the `Next` gate — itself a correct
  fix for an earlier defect, filtering errors to only the fields actually
  rendered on screen — therefore had nothing on this screen to block on. The
  result: **a first run could not be completed at all.** Save reported
  `tasks_file` was required, on a screen where that field had never rendered,
  with no way out short of knowing to toggle the source dropdown to `jira` and
  back. No fixture and no headless DOM shim caught it; only an actual Chrome
  session did.

**The live smoke test found two more, and both were the same shape as the
worst of the five above.** It ran against no `~/.claudeloop` at all, a scratch
repository, and two tasks on `haiku`, for $0.136.

- Typing `haiku` into the Model field produced **`opushaiku`**. The `get()`
  fix immediately above was correct for the visibility logic it was written
  for, and wrong where a text input bound its `value` to it: the default
  landed in the box as real text rather than a greyed placeholder, so the
  operator types over the top of it instead of replacing it. `validate()`
  accepts it, because `model` is a free string — the run would have started
  on a model that does not exist. Text inputs now read `drafted`, which is
  the operator's own value and nothing else. That the fix for one live
  finding created the next one is the argument for running this thing rather
  than reasoning about it.
- `run_setup` announced `ClaudeLoop is not configured yet` on the `--setup`
  path, over a config that plainly existed.

Everything else held. **First run:** the six screens complete end to end; all
three live checks answer from reality, `claude auth status --json` reporting
`signed in via claude.ai (pro)` on its first run against the real CLI; the
saved `config.toml` is `0600`, annotated with the schema's help text as `#`
comments, and carries only the keys actually supplied with no pinned
defaults; `load_config` reads it back with correct types; and the **same
process** then bound the dashboard and started task 1, so the in-process
handoff works. Both tasks ran and were marked `- [x]`, the target repository
never left `main` and was never dirtied, and each task got its own
`claudeloop/<task-id>` branch.

**The `--setup` path, which the whole-branch review flagged as never having
been driven live, was driven:** the schema payload comes back with
`editing: true`, `secrets_set: ["web_token"]` and `session_env` carrying names
with empty values — and **neither secret value anywhere in it**; the token
field renders blank with `set — leave blank to keep` and no `*`; a
deliberately-`0644` config comes back `0600` after saving, which is the
narrowing no unit test can distinguish; and `model` changed while both the
`web_token` and the `[session_env]` entry survived being left blank.

One observation that is **not** an S5 defect: task 2 wrote `status: "done"`
with a summary claiming it had created `BETA.md`, but never committed — its
branch still points at the base commit and the file sits untracked. That is
session compliance with `BUILTIN_DEFINITION_OF_DONE` under `haiku`, the same
~50% rate S1's smoke test measured against a similar instruction. S6's safety
net did its job: `worktree.release` refused to remove the dirty tree, so the
uncommitted work survives on disk rather than being destroyed.

Spec: `docs/superpowers/specs/2026-08-03-claudeloop-setup-wizard-design.md`

### S7 — Proposed plugin set

**Reversed by S8 below.** The curated set, the install/enable
reconciliation and the fourth prompt layer are all gone; what survives is
the `--scope user` rule and the `claude` subprocess hardening. The rest of
this section is kept as the record of what was decided at the time.

`plugins.py` (new) is a single table plus the logic to install it. `Plugin`
is a frozen dataclass — `name`, `plugin_id` (`name@marketplace`),
`marketplace`, `reason` (the wizard's one-line checkbox caption) and `usage`
(the fourth prompt layer's text, empty on the ordinary plugin). `PROPOSED` is
caveman and ponytail, in that order; `by_name` looks one up by its
`config.toml` shorthand. Neither carries `usage`, by design: both already
state their own rules, so the fourth layer is the operator's to fill through
`~/.claudeloop/plugin-usage/<name>.md`. The set shipped with a third entry
whose `usage` text worked around its human-in-the-loop habits; it was dropped
afterwards, along with that text.

`config.py` gained `plugins: tuple[str, ...]` — `Field("plugins", "list",
step="plugins", default=())`, a `"list"` branch in `_coerce`, and a
`_known_plugins` check: a bare name must be one of the proposed set, and
anything else must carry its own `@marketplace`, because `reconcile` has
nowhere else to learn one.

`plugins.reconcile(names)` is what `main()` calls, once, between
`worktree.probe` and `_serve_dashboard` — fatal for the same reason as the
worktree probe: a box that cannot get the plugins the operator chose would
otherwise run every task with a system prompt describing tools the session
does not have. It reads `claude plugin list --json` once (`_installed()`), a
local call with no network round trip, then for each wanted plugin: enables
it if installed but disabled, or adds its marketplace and installs it if
missing. An empty `plugins` runs no subprocess at all; an already-satisfied
non-empty selection makes exactly that one local call and touches the
network not at all, so a marketplace outage cannot stop a loop that is
already reconciled. It re-reads the installed set to confirm, but only when
something actually changed — trusting a `0` exit code that lied would
otherwise leave a session with a prompt describing skills it does not have.
Every install and enable happens at **`--scope user`**, never project or
local: those write `.claude/settings.json` or `.claude/settings.local.json`
into the target repository, which nothing ClaudeLoop writes may do, and
would be per-worktree besides — this is the half of S1.1's "pass through, do
not manage" that S7 reverses, because the S4 addon operator has no terminal
to run `claude plugin install` in. `settings_file` passthrough is untouched.
A caught `PluginError` — `claude` missing from `PATH`, a non-zero exit, or a
timeout past `CLAUDE_TIMEOUT_S` (300s) — becomes the message `main()` exits
with, the same shape as a bad worktree probe.

One defect found in review rather than by the fixtures: the real CLI's
`plugin list --json` emits **one row per scope** a plugin is installed in,
id repeated, not one row per plugin. `_installed()` now prefers the
user-scope row whenever the same id appears more than once, so a plugin also
visible at project or local scope cannot mask the user-scope row `reconcile`
actually cares about.

The fourth prompt layer: `plugins.usage_section(names, home, proposed=
PROPOSED)` builds one `### <name>` block per selected plugin that has
something to say, in `PROPOSED` order rather than the operator's config
order, so the prompt reads the same regardless of how `plugins` is written.
`~/.claudeloop/plugin-usage/<name>.md`, if present, replaces a built-in
plugin's text and gives a plugin outside the proposed set a block of its
own; unreadable counts as absent, so a permissions mistake can't stop a
session starting. `prompt.precedence()` gained `has_plugins`, stating the
layer only when it is non-empty, and positions it below the operator layer
and above the definition of done — ClaudeLoop's own advice about its own
tooling, which the operator must be able to override. `prompt.compose()`
slots the block in between the operator instructions and the definition of
done.

`setup.py` and `static/setup.html` gained a Plugins wizard step: the
proposed set renders as checkboxes with their `reason` as the caption
(never their `usage` text, which stays server-side), plus a free-text
`plugin@marketplace, comma separated` row for anything else. `dump_toml`
writes `plugins` as a TOML array through the same `SCHEMA`-driven path as
every other key, and an empty selection writes no key at all, exactly like
every other optional field.

**The live smoke test ran twice**, on `haiku`, against a fresh scratch
repository each time, with all three plugins the set then proposed, two
tasks per run, $0.30 total ($0.148 + $0.154).

**Run 1** deliberately started with `ponytail@ponytail` uninstalled at user
scope, so the install path ran for real rather than being skipped. Confirmed:

- Startup logged `adding marketplace DietrichGebert/ponytail` then
  `installing ponytail@ponytail`, both **before** `dashboard on
  http://127.0.0.1:8765` — the reconcile gate really does sit ahead of
  anything listening.
- `claude plugin list --json` afterwards showed all three at user scope,
  enabled.
- A second start of the same config logged only the dashboard line: no
  `marketplace add`, no `install`. An already-satisfied selection touching
  the network not at all held.
- The fourth prompt layer reached the session verbatim — read out of the
  running session's own `--append-system-prompt` argv in `/proc`, not a
  fixture and not the stored transcript, since Claude Code's transcript does
  not record the appended system prompt.
- Task 1 was phrased as a feature to design and build, the shape that trips
  a workflow plugin's approval gate. It implemented the work and wrote a
  result file rather than ending its turn waiting for a human to approve a
  design — the defect that plugin's `usage` text existed to prevent.
- Neither task blocked, so neither asked a question. Both were marked
  `- [x]`, each committed on its own `claudeloop/<task-id>` branch, and the
  scratch repository never left `main` and was never dirtied.

**The one defect it found.** With a plugin layer present but **no** operator
instructions file, `precedence()` emitted:

> "The plugin usage instructions are ClaudeLoop's own advice about the tools
> it installed for you and above the definition of done."

The ranking clause (", and rank below the operator instructions") was only
appended when an operator layer existed, while " and above the definition of
done." was appended unconditionally — so the two only read correctly
together. Without an operator layer the sentence lost its verb and stated no
precedence at all. Seven tests covered `precedence()` and all passed, because
they asserted on substrings rather than the whole sentence. The fix (`ae69b95`)
splits it into two sentences — "...installed for you. They rank above the
definition of done." and, with an operator layer, "...They rank below the
operator instructions and above the definition of done." — and the tests now
pin both sentences whole.

**Run 2**, after the fix, uninstalled `caveman@caveman` at user scope instead,
so the install path ran again against a different marketplace. The corrected
sentence was confirmed in a live session's argv. Both tasks completed, were
marked, and landed on their own branches; the repository stayed clean on
`main`. No further findings.

One observation that is **not** an S7 defect: run 1's second task left its
worktree behind, because the session's own `__pycache__/` made the tree dirty
and `worktree.release` is never forced. That is S6 behaving as designed.

Spec: `docs/superpowers/specs/2026-08-03-claudeloop-plugin-set-design.md`

### S4 — Home Assistant OS addon

`addon/` — `config.yaml`, `Dockerfile`, `run.sh`, `DOCS.md` — plus
`repository.yaml` at the root, so this repository *is* an addon repository, and
`.github/workflows/addon.yml`, which builds the image the supervisor pulls.

**The image is prebuilt, and that is forced rather than chosen.** The
supervisor builds an addon with the addon's own folder as the docker context
(`AddonBuild.get_docker_args` passes `path=addon.path_location`), and
ClaudeLoop's source is the repository root. The alternatives were duplicating
`claudeloop/` into `addon/`, or having the Dockerfile clone this repository at
build time and build whatever is on the remote rather than what the operator
installed. So `config.yaml` names
`image: ghcr.io/kamilpostrozny/claudeloop-{arch}` and the workflow builds it
from the root with `docker/build-push-action` — not `home-assistant/builder`,
which has the same addon-folder context the supervisor does. **Nothing installs
until this repository is pushed and that workflow has run once.**

**Ingress is the part the roadmap did not have.** All three of the web layer's
assumptions — bind `web_host`, compare `Host`, require `?token=` — are false
behind the supervisor's proxy. `CLAUDELOOP_INGRESS=1`, set by `run.sh` and read
by `config.ingress()` at request time, moves the bind to `0.0.0.0:8765`
(`bind_address`, shared by both servers) and drops the other two. Each is
replaced, not removed: an ingress addon publishes **no port**, so there is no
address for DNS rebinding to reach, and the supervisor authenticates a real
Home Assistant user before proxying — a login rather than a secret in a query
string, which also keeps the token out of the ingress access log. It is an
environment variable rather than a config key because the wizard has to be
reachable on a box with no `config.toml` to read a key out of. **This is a
deliberate reversal of half of S5's second barrier**, recorded in the spec:
outside ingress both barriers are exactly as S5 left them.

`index.html` and `setup.html` each derive a base path from `location.pathname`,
so their absolute routes resolve under `/api/hassio_ingress/<session>/`. The
server needs nothing: the supervisor strips the prefix before proxying.

**Configuration stays in S5's wizard**, not in addon options — a second
description of 23 keys next to `SCHEMA` is the one thing S5 exists to prevent,
and the S4 operator is exactly the operator with no terminal. The addon
declares four options, all of them things the wizard structurally cannot carry:
`claude_code_oauth_token`, `git_user_name`, `git_user_email`, and `setup`,
which is the `--setup` flag an addon has no command line to pass.

Landmines, and what became of them:

| Landmine | Outcome |
|---|---|
| Commit signing | Closed. `run.sh` sets `commit.gpgsign false` in the container's global git config; `[session_env]`'s `GIT_CONFIG_COUNT` trio still wins for an operator with a real key. |
| Claude-in-Chrome cannot run headless | Not closeable here — it is a statement about what an operator writes in `instructions.md`. Documented in `DOCS.md`. |
| First-run trust prompts | Closed. `run.sh` seeds `~/.claude.json` with `hasCompletedOnboarding` and `bypassPermissionsModeAccepted` when it is absent. |
| Secrets the target repo needs | Already `[session_env]`, and it reaches the wizard. Nothing to build. |
| Claude authentication | Closed, by the `claude_code_oauth_token` option. |

**The live smoke test ran in the container itself**, which is the only place
that means anything for this slice: the image built with podman, a scratch
repository reached as `file:///share/…` and cloned into `/data`, two tasks on
`haiku`, the wizard driven over HTTP with Home Assistant's own `Host` header
and no token. **It found two defects, one of them expensive.**

- **`claude --permission-mode bypassPermissions` refuses to run as root** —
  "--dangerously-skip-permissions cannot be used with root/sudo privileges for
  security reasons" — and an addon container is root. Every task failed in
  about three seconds with no result file and `$0.0000`. The image now creates
  `claudeloop` (uid 1000) with `/data` as its home, `run.sh` hands `/data` over
  and starts the loop through `setpriv`. Invisible to the test suite, whose
  fake `claude` has no opinion about who runs it.
- **An unwritable tasks file is a silent, unbounded, paid loop.**
  `FileSource._rewrite` suppresses the `OSError` from its write on purpose —
  the checklist is the operator's file and may vanish mid-run — but an
  unwritten mark leaves the line `- [ ]`, so the task is offered again on the
  next poll and paid for again. **37 runs of one task in fifteen minutes,
  $1.10**, stopped only because a human was watching a terminal, which is the
  one thing this project assumes nobody is doing. Not an addon defect in
  origin; the addon is what makes it ordinary, since the loop now runs as uid
  1000 and a checklist on `/share` belongs to root. Fixed at both ends:
  `tasks_file` gains a writability check in `SCHEMA` (a file that does not
  exist yet still passes — `pending()` reads a missing checklist as an empty
  backlog), and a failed mark is now logged as an error naming the task.

Everything else held. The wizard was served and saved through a foreign `Host`
with no token; the new writability check refused `/share/smoke/tasks.md` live,
with its own message, before anything was paid for; the same process then bound
the dashboard on `0.0.0.0:8765` and started task 1; both tasks completed, were
marked `- [x]`, and landed on their own `claudeloop/<task-id>` branch carrying
exactly their own commit; the clone stayed on `main` and was never dirtied;
both worktrees were released; and `/api/state` and `/logo.png` answered through
the same ingress-shaped requests afterwards. $0.073 for the successful pair.

Spec: `docs/superpowers/specs/2026-08-04-claudeloop-home-assistant-addon-design.md`

### S8 — Repository-owned plugins

S7's premise was that ClaudeLoop had to choose plugins because the S4 addon
operator has no terminal. Measured against the real CLI (2.1.220), most of
that premise was false: a headless `claude -p` **does** honour the target
repository's own `.claude/settings.json`, `enabledPlugins` included, and
auto-installs a plugin declared there at session start, writing nothing into
the repository. Exactly one link is missing, and it is the one thing the
session cannot do for itself.

Measured, on a scratch repository with a scratch `CLAUDE_CONFIG_DIR` and a
throwaway local marketplace:

- A repository declaring `extraKnownMarketplaces` + `enabledPlugins` on a box
  that has never seen either: the plugin does **not** load, `claude plugin
  list` is empty, and there is no prompt, no warning, and nothing in
  `--debug`. Project-declared marketplaces are ignored outright — `claude
  plugin marketplace list` says "No marketplaces configured", and setting
  `hasTrustDialogAccepted` for the directory changes nothing.
- Hand-writing the same `extraKnownMarketplaces` table into **user**
  settings.json is not enough either. The registry that counts is
  `~/.claude/plugins/known_marketplaces.json`, which only `claude plugin
  marketplace add` fills in (it also clones the marketplace to disk).
- After one `claude plugin marketplace add <source> --scope user`, the very
  next headless session installs and enables the repository's declared
  plugin by itself, and the repository's `.claude/settings.json` is
  byte-identical afterwards. A second `marketplace add` exits 0 with
  "already on disk".
- Project `enabledPlugins: true` beats a user-scope `false` once the plugin
  exists on the box, so a repository's choice really is the one that wins.
- **Gotcha worth knowing:** one malformed value inside
  `extraKnownMarketplaces` (`"source": "local"` where the CLI writes
  `"directory"`) silently invalidated the *whole* project settings file —
  hooks and `env` in it stopped applying too, with no error anywhere. Two
  hours of the investigation above were spent on results that were really
  this.

So `plugins.py` is now `marketplace_sources(repo)` (pure: read
`<repo>/.claude/settings.json`, map each `extraKnownMarketplaces` entry to
the one argument the CLI takes — `repo` for github, `path` for directory,
`url` for git) plus `register_marketplaces(repo)`, which runs `claude plugin
marketplace add <source> --scope user` once per entry. `main()` calls it
where `plugins.reconcile` used to sit, between `worktree.probe` and
`_serve_dashboard`, and it is fatal for the same reason. A repository
declaring nothing runs no subprocess at all. A missing, unreadable or
malformed settings file counts as "declares nothing": it is the repository's
file, and the CLI ignores a broken one too.

Deleted with the curated set: `Plugin`/`PROPOSED`/`by_name`, `reconcile` and
`_installed`, the `plugins` config key (with `_known_plugins` and `_coerce`'s
`"list"` branch, which nothing else used), the Plugins wizard step and the
checkbox rendering in `static/setup.html`, `usage_section` and the
`~/.claudeloop/plugin-usage/` layer, and `precedence()`'s `has_plugins`
clause. The system prompt is three layers again. `settings_file` passthrough
is untouched, and so is the `--scope user` constraint.

No spec of its own: the finding above *is* the design, and it is recorded
here rather than in a document that would only repeat it.

### S9 — Resume an interrupted task

Restarting ClaudeLoop mid-task used to throw that task's session away and
start it over from the original task text — on a worktree that already held
the dead session's commits and uncommitted edits, which the fresh session was
told nothing about.

Almost all of the recovery already existed and stopped one step short.
`State.__init__` flips a `running` row to `interrupted`, `terminal_ids()`
excludes `interrupted` so the source offers the task back, `worktree.ensure`
reuses the tree, and `last_session()` holds the id to resume. What was missing
was the join: `run_task` only reached `last_session` when it had an answer to
deliver, so an interrupted task took the fresh-start branch.

Now `run_task` asks `State.was_interrupted(task.id)` **before** `start_task`
(which is `INSERT OR REPLACE` and would erase the status), and on a yes
resumes that session with `INTERRUPTED_PROMPT` — which says the process was
restarted, names `git status` and `git log` rather than saying "check what you
did", forbids starting over, and still demands the result file.
`FRESH_INTERRUPTED_PROMPT` covers the case where the session is gone, exactly
as `FRESH_ANSWER_PROMPT` does for a parked task. `source.start` no longer
re-fires: its skip condition widened from "resuming with an answer" to
"resuming at all", since an interrupted task already fired
`transition_start` on the attempt that died.

The selection moved out of `run_task` into `opening_prompt(task_text,
resume_with, resumed, interrupted)`, pure, joining `decide` and
`blocking_reset`. Four prompts chosen by three inputs is exactly the shape
that produced S7's live failure, and every combination is now pinned against
the whole rendered string. An answer outranks an interruption, for a task
that is both.

Two deliberate limits. **`error` does not resume**: it is non-terminal too,
but its causes are environment faults that can happen before any session
exists, and `--resume` against an id that never ran fails silently — the same
failure this file already records for tasks parked across the S6 upgrade.
**`was_interrupted` is repo-scoped**, like `terminal_ids()` and `blocked()`,
and here that is load-bearing rather than tidy: `tasks.id` is the primary key
on its own, so an unscoped read could answer yes on another loop's
interruption and resume a transcript belonging to a different repository's
worktree.

Not built: config is still read once in `main()`. Restarting is now cheap,
which is what an operator needed; hot-reload is in the open issues below.

**Live smoke test** — two scratch repositories, `model = "haiku"`, two tasks
each, `SIGKILL` on the loop mid-first-task, then a restart. Both cases the
slice has to handle came up, the second only because the first attempt to
provoke the first one missed:

- *Killed after the session wrote `result.json`, before the loop read it.*
  Row left `running`, run row with no `exit_reason` — the narrow window, hit
  by accident because haiku finished a two-commit task faster than the poll
  that was watching for it. The resumed session ran `git status && git log
  --oneline -5`, said "Work already done. Both commits present.", and wrote
  the result file. No commits redone; the branch kept the two SHAs the dead
  session's own summary had named.
- *Killed genuinely mid-work*, two of five steps committed. The resumed
  session opened with `git log --oneline -10 && git status` and carried on at
  step 3. The branch ended with five commits in order, steps 1 and 2 still
  the pre-kill SHAs.

Both runs reused the dead session's id across the restart, so `--resume`
reattaches to a session whose process is gone, not merely one this process
started. The second task ran normally afterwards, on its own branch cut from
the default rather than from the resumed task's.

No defects found — the third such run out of nine. What it did confirm is the
sentence the slice turns on: naming `git status` and `git log` produced that
exact command as the resumed session's first action in both runs.

### S9.1 — Locale-proof Jira transitions

Observed live on a real instance: `transition_done = "In Progress"` never
fired, warning once per task —

> `KAN-9: Jira does not offer a 'In Progress' transition from its current status (offered: Idea, Do zrobienia, W toku, Testing, Gotowe, Kosz) -- leaving the issue where it is`

Jira translates its **built-in** statuses per account, and
`/issue/{key}/transitions` reports the translated display name.
`Do zrobienia` / `W toku` / `Gotowe` are To Do / In Progress / Done in
Polish; `Idea` and `Testing`, added by hand, came back in the English they
were typed in — which is what identifies this as translation rather than
someone renaming things.

The half that kept working is the confusing part, and worth remembering: the
*pickup* side of the same config was fine. `_compose_jql` emits `status = "To
Do"`, and JQL resolves the untranslated canonical name, so it matched issues
displaying `Do zrobienia`. Jira does the resolving in JQL; `_transition` was
doing its own string compare and had nothing to resolve against.

So `match_transitions(offered, wanted)` — pure — compares a configured value
against four tiers, most specific first: transition id, transition name,
destination status name, destination status category key. It returns a
**list**, from the first tier that matches anything at all, because the
caller has to tell "nothing" from "several".

- The category keys (`new`, `indeterminate`, `done`) are Jira's own
  vocabulary and are never translated, so they are what a localised board can
  configure and keep.
- First-tier-wins means a transition literally named `done` beats every
  transition whose *category* is done, rather than colliding with them.
- Ambiguity moves nothing and logs every candidate. This is not caution for
  its own sake: `Kosz` is the board's bin and sits in the `done` category
  beside `Gotowe`, so an arbitrary pick is the one that bins a finished
  ticket.
- The unmatched warning now lists each offered transition with the values
  that would reach it, since bare names were exactly what made the live
  failure hard to act on.

`README.md` gains the four-value table and the "not an English board" note;
the wizard's help text for both keys says the same.

**No live check yet.** It needs a real Jira, and the fixtures assert a
`to.statusCategory.key` shape that only an instance can confirm is really in
the offered payload — the precise kind of assumption this file's smoke-test
rule exists for. Run one before trusting `done` in anger.

**S11 tried and could not.** The configured instance has no projects left at
all: `/project/search` comes back empty and `project = "KAN"` matches nothing,
so the very tickets this finding came from are gone. It needs a Jira with a
real board again — the same run would then also cover S11's pagination and
bounded comment read, which are in the same position for the same reason.

---

### S10 — The repository's instructions come first

A live task against a repository whose `CLAUDE.md` closes out with `git push
origin main` committed its work, reported `done`, and shipped nothing.
Reproduced in three lines:

```
$ git push origin main     # from a worktree checked out on claudeloop/<id>
Everything up-to-date
$ echo $?
0
```

`git push origin main` pushes the *ref named main* — the repository's own
untouched default branch — not `HEAD`. Git says success. A literal-minded
session reads that as shipped.

Three defects in the layering, all fixed here, plus one the fix would
otherwise have created:

- **Nothing told the session where it was.** No layer named the worktree, the
  branch, the default branch, or how to publish from a worktree. `WORKING_TREE`
  now states all of it as fact — composed for every task, before the task
  source — with both push commands spelled out literally, because "name HEAD
  rather than the branch" is exactly what a session satisfies by guessing.
- **ClaudeLoop's guards were inside a layer that gets dropped.** `compose`
  drops `BUILTIN_DEFINITION_OF_DONE` whenever the repository's own file says
  when work is finished, and that block held the task-file guard and the
  branch rules. The better a repository documented itself, the fewer of
  ClaudeLoop's guards reached the session. The task-file guard moved to
  `PROTOCOL`; the branch rules became facts in `WORKING_TREE`.
- **`precedence()` ranked the wrong thing.** ClaudeLoop's definition of done
  was the base, with the repository's file pointed at from inside it, so a
  repository asking for a push to `main` was arguing with a layer above it.
  It now says the repository's own instructions come first and the built-in
  is only a fallback — a deliberate reversal of S1's framing, recorded in the
  S10 spec rather than rewritten into S1's.
- **Stale bases.** A session landing work on the remote's default branch never
  moves the local ref, so `worktree.ensure` fetches and cuts new branches from
  `origin/<default>`, degrading to the local branch on any failure (no remote,
  no network, a locked credential agent). Reused trees are never refetched or
  rebased: they hold uncommitted work, and moving that under an unattended
  session is worse than a stale base.

Where a session publishes is now the target repository's decision. A
repository whose ship flow is direct-to-`main` gets exactly that, unattended,
which is a real authorisation and was made deliberately — the alternative
considered was ClaudeLoop always substituting a branch and a pull request,
which is ClaudeLoop overriding the repository, the thing this slice exists to
stop. A `publish = "branch" | "default-branch"` config key was rejected for
the same reason: the repository is the layer that knows.

**Live smoke test, two tasks, $0.12.** Scratch repository with a local bare
remote and a `CLAUDE.md` demanding a push to `main`. Both sessions ran exactly
`git push origin HEAD:main`; neither attempted `git checkout main`; no
"Everything up-to-date" appeared in either event log; both wrote `done`; and
task two's commit landed on top of task one's on the remote, which is the
fetch working. Nothing new was found, which is the first time a prompt slice
here has run clean.

### S11 — Backlog defects

No slice was outstanding, so this one took the open-issues list below: the
entries that are **genuine defects** — a wrong number, an unbounded growth, a
permanent block, a leak — leaving the ones that are deliberate decisions with
recorded reasons, since reversing one of those needs its own spec.

Thirteen fixed, and the two most valuable were the two the fixtures could not
have shown:

- **A parked task reported only the cost of its resume.** `run_task` starts
  its accumulator at zero on every call and `finish_task` writes `cost_usd=?`
  rather than adding, so the money spent before the question was overwritten
  by the money spent after it. `State.prior_cost` seeds the accumulator, read
  *before* `start_task` — which is `INSERT OR REPLACE` and puts the column
  back to NULL, the same ordering `was_interrupted` needs. Confirmed live:
  parked at $0.0316, resume cost $0.0183, recorded $0.0499.
- **`tasks.id` was the primary key on its own**, and `id` is a hash of the
  task text, so two repositories whose file sources hold identical text shared
  a row and `INSERT OR REPLACE` overwrote the other's. `tasks` is rebuilt on
  `(id, repo)`; `runs` gains `repo` so `last_session` cannot hand back a
  session id belonging to another repository's worktree. `repo` stays
  **nullable** — SQLite does not enforce uniqueness across a NULL key part, so
  pre-column rows keep belonging to no repository exactly as documented. The
  `runs.repo` backfill runs *before* the rebuild, while `id` is still unique,
  or a task parked across the upgrade loses the session it resumes.

The rest: `~/.claudeloop`, `state.db` and `runs/` are narrowed to 0700/0600
(`config.narrow`, which only ever takes permissions away and never raises);
`events.jsonl` rotates at 64 MiB keeping one generation, with the matching
`size < offset` guard in the dashboard's SSE pump; `JiraSource.pending` follows
`nextPageToken` up to `MAX_PAGES`; `JiraSource.answer` reads a bounded
newest-first page and backs off to 600s, cutting a parked ticket from ~2,900
Jira requests a day to ~150, while the dashboard's answer file stays checked
every poll because it is free; a leftover `worktrees/<id>` with no `.git` is
moved aside rather than blocking the head of the queue forever; an `error`
outcome releases its worktree; `State` closes its own connection; `prompt.
oversized` refuses a system prompt past Linux's 128 KiB argv cap at startup;
the dashboard keeps answer drafts across rebuilds and re-reads the checklist
per request under the file source.

**Live smoke test, two tasks on haiku, $0.09.** Task one finished; task two
parked on a question, was answered on the dashboard, and resumed. The cost
figure above is the finding it was run for. Everything else held: composite
key and `runs.repo` live in the database, the resume reused the dead session's
id, both branches carried exactly their own commit, the repository never left
`main` and was never dirtied, both worktrees were released, and every path
under the scratch home came out 0700/0600.

Two checks beyond the two tasks. **The migration was run against a copy of a
real `~/.claudeloop/state.db`** — old single-column key, no `runs.repo`, 7
tasks and 8 runs — which came through with every row and every cost intact,
was a no-op on the second open, and was narrowed to 0600. And the
head-of-line worktree fault was reproduced by hand: the leftover was moved to
`<id>.broken-<timestamp>` with its contents intact, and `ensure` reattached to
the **existing branch**, so the earlier attempt's commit came back with it.

**What could not be checked, and it is S9.1's outstanding item, not this
slice's.** The Jira half — pagination, the bounded comment read, `orderBy`
ordering — has unit tests against a fake and a real captured fixture, but no
live run: the configured instance now has **no projects at all**
(`/project/search` returns empty, `project = "KAN"` matches nothing), so the
tickets behind S9.1's report are gone and there is nothing to run against.
S9.1's `transition_done` check is still outstanding for the same reason. One
thing the probe did confirm live: `/search/jql` really does answer with
`isLast` and no token on a final page, which is the shape the pagination reads.

Spec: `docs/superpowers/specs/2026-08-04-claudeloop-backlog-defects-design.md`

### S12 — A stranded task can come back

**Implemented and committed. The live smoke test has not been run** — see
Next, which is where the blocker lives. Treat this slice as unfinished by
this file's own rule until it has.

Found by an operator, not by the suite. On the Home Assistant add-on, Jira
project `KAN`:

```
16:41:31  task 98720990de2c5461 starting: KAN-13
16:43:19  task 98720990de2c5461 rate limited, sleeping 5830s
17:12     container killed to upgrade the add-on 0.1.3 -> 0.2.0
20:05:30  loop back up, dashboard on 8765
          <idle, pending: [], nothing again>
```

The row sat at `interrupted`, its run row carrying `exit_reason:
RateLimited` and a session id. Every part of S9's recovery was present and
correct. **None of it ran, because nothing ever offered the task again.**

`start()` had fired `transition_start` and moved the issue to In Progress —
confirmed afterwards in Jira's changelog, which records `status: "In
Progress" -> "Done"` when a human finally closed the ticket by hand. The
operator's JQL selects the backlog status, so from that moment the backlog
query could not see it, and `main_loop` takes work from `pending()` and
`state.blocked()` only. KAN-1 had been stranded the same way earlier in the
same run.

The upgrade is incidental — a crash, a reboot or a `SIGKILL` does the same.
And **the feature working correctly is what set the trap**: the same
`transition_start` failed against this instance for KAN-9 and KAN-11
(`Jira does not offer a 'In Progress' transition`), so those two stayed in the
backlog status, finished, and got labelled. A transition that does nothing
cannot strand anything.

`State.unfinished()` returns the non-terminal, non-running rows for this
repository — `interrupted` and `error`, the exact complement of
`terminal_ids()`. `JiraSource._stranded` asks Jira about their keys with a
second query per poll:

```
key IN (KAN-1, KAN-13) AND statusCategory != Done AND <GUARD>
```

**Jira, not `state.db`, decides whether the work is still wanted.** The rows
carry `text` and `source_ref`, so a Task could be rebuilt with no network call
at all — rejected, and the incident is why: by the time anyone looked, a human
had finished KAN-13 and closed it, and a recovery trusting `state.db` alone
would have paid for that work a second time. The *category*, never a status
name: Jira translates those per account (this instance renders Done as
"Gotowe"), which is the defect S9.1 already fixed once in the transition
matcher.

Recovered work is offered ahead of the backlog and first in the list
`main_loop` takes `[0]` from: money is already spent on it, a worktree
already exists, and its session may still be resumable. A key in both answers
is emitted once. Keys are validated against Jira's own issue-key shape before
being spliced into a query string — `source_ref` comes out of a database
column and JQL has no parameter binding — and the list is capped at
`MAX_RECOVERED = 50`, oldest first. `_search_pages` now names which of the two
queries a warning is about.

`FileSource` needs none of it: an interrupted task's line is still `- [ ]`.
Only a source whose backlog is a *query* can lose sight of what it started.

**The price, accepted deliberately.** Recovering `error` gives the Jira source
the head-of-line blocking `run_task` already documents for the file source —
a task whose fault is task-local and permanent comes back every poll, is
re-picked ahead of the backlog, fails again, and nothing later runs. S11
removed the one known cause (a leftover `worktrees/<id>` is now moved aside),
so this is a shape rather than a live fault; before this slice the Jira source
dodged it only by silently losing the task. Losing work is the worse failure,
and the visible one names itself in the log every poll.

Not built: nothing hides an `interrupted` row from the dashboard's completed
panel, which is what made this incident read as "KAN-13 finished"
(`web.py`'s `WHERE status != 'running'`). After this slice such a row is
transient, and in the one case where it is not — Jira says the issue is
closed — hiding it entirely is worse than filing it under the wrong heading.

Spec: `docs/superpowers/specs/2026-08-04-claudeloop-stranded-task-recovery-design.md`
Plan: `docs/superpowers/plans/2026-08-04-stranded-task-recovery.md`

---

## Next

**S12 is the scheduled slice and it is not finished.** One Jira live smoke
test now covers S9.1, S11 and S12 at once — the same run, the same board.

1. **The Jira live smoke test has never run against S9.1, S11 or S12.** S9.1
   made `transition_done` match on transition id, status name and status
   category as well as name, and its fixtures assert a `to.statusCategory.key`
   shape that only a real instance can confirm is actually in the offered
   payload. S11 added `nextPageToken` pagination to `pending()` and a bounded,
   `orderBy=-created` comment read to `answer()`. S12 added a second query per
   poll whose `key IN (...) AND statusCategory != Done` has never been sent to
   a real Jira — and S12 is the one slice here that *cannot* be smoke-tested
   on the file source at all, because the defect it fixes does not exist
   there.

   **The instance came back.** The note that used to sit here — no projects
   left, `project = "KAN"` matching nothing — is out of date:
   `assimo.atlassian.net` has project `KAN` with eleven issues, and the
   add-on has been running real tasks against it. So the blocker is no longer
   "there is no board".

   What is needed now is a **scratch board**, not this one. Every KAN issue is
   Done, and the run has to create tickets, transition them out of the backlog
   status, kill the loop mid-task and close one by hand — none of which
   belongs in a project someone is working from. It also needs a Jira API
   token; the only copy lives in the add-on's `/data/.claudeloop/config.toml`,
   which is not reachable from the SSH add-on under protection mode.

   Meanwhile the live run behind S12 confirmed two things a fixture cannot,
   both from the add-on's own log and Jira's changelog: **`transition_start`
   really does fire against a real instance** (KAN-12 and KAN-13 moved to In
   Progress with no warning logged, where KAN-9 and KAN-11 warned that no
   matching transition was offered), and the *whole* JQL round trip works
   against a real board. `transition_done`, pagination past 50 issues, the
   comment ordering and S12's recovery query all remain unverified against
   reality — which is exactly the state this file's smoke-test rule exists to
   make visible.

   What an earlier probe confirmed live: `/search/jql` answers a final page
   with `isLast` and no token, which is the shape the pagination reads, and
   Jira still refuses an unbounded JQL outright.

2. **`main` is ahead of `origin/main`.** The remote sits at `73ca68e`
   (`chore: addon 0.1.3`); S10, S11 and S12 are local only. Pushing is a
   deliberate act nobody has asked for yet — and note that publishing the S4
   add-on image is a tag, not a branch push, so a `main` push alone changes
   nothing an operator installs.


The open issues below are the rest of the backlog. None is scheduled.

---

## Open issues carried across slices

Real, deliberately deferred, tracked here so they are not lost.

- **A repository whose definition of done requires a live environment cannot
  fully be met.** S10 made the deploy half reachable — work can land on the
  default branch, so a CI that deploys from it runs — but a `CLAUDE.md` that
  also demands a browser sweep needs an MCP server the session only has if
  `mcp_config` supplies one. Left as the target repository's decision to
  record in its own file; ClaudeLoop must not paper over it in a prompt.
- **`state.db` is one database per machine, scoped by a repository path
  string.** `home` is `~/.claudeloop` for every config, so every read that
  means "this loop's work" filters on `repo`, and S11 made `(id, repo)` the
  primary key so two repositories can no longer overwrite each other's rows.
  One consequence is left standing: moving or renaming a repository orphans
  its history, since the scope is the configured path verbatim. Nothing
  migrates a renamed path, and guessing which old scope a new path used to be
  is not something ClaudeLoop can do safely — an operator who moves a
  repository can `UPDATE tasks SET repo=...` themselves.
- The dashboard token travels in the query string, because `EventSource` cannot
  set headers. It therefore reaches browser history and screenshots. S4 no
  longer adds the ingress access log to that list — under ingress the token is
  not checked at all — so this is back to affecting only an operator who
  deliberately exposes `web_host`. The complete fix is a `Set-Cookie` plus
  `history.replaceState` pair, worth doing when the token path is actually used.
- **The add-on's `setup` option is a flag, not a button.** It stays on until
  the operator turns it off, so a restart with it still set reopens the wizard
  and waits there instead of working. `DOCS.md` says so; the supervisor has no
  way to offer a one-shot action. **Hit live on 2026-08-04**: both restarts
  around the 0.1.3 → 0.2.0 upgrade logged `setup is on: opening the wizard
  instead of starting a task`, with the option still set hours later. The
  documentation being correct and bold did not prevent it. If it recurs, the
  fix is `run.sh` clearing the option through the supervisor API once the
  wizard saves.
- **A repository checked out under `/share` is not usable by the add-on
  without a `chown`.** Sessions run as uid 1000 and `/share` belongs to root,
  so `git worktree add` fails with "Permission denied" — observed. The
  supported path is a URL cloned into `/data`, which the loop's own user owns;
  the `share:rw` mapping is for an operator who has already dealt with the
  ownership.
- **`config.toml` is read once, in `main()`.** `build_source` runs once before
  the poll loop, so every key — the Jira credentials, the JQL, the transition
  names, `model`, `max_resumes` — is fixed for the life of the process. An
  operator who gets one wrong has to restart. S9 made that cheap rather than
  free: the running task now resumes its session instead of starting over.
  Hot-reload is not obviously worth building on top of that, and would need a
  per-key allowlist regardless — `repo`, `home` and the worktree root are not
  safely swappable under a task that is mid-flight.
- `Config` has a `dict` field, so it is unhashable. Nothing hashes it.
- Nothing prunes what a task leaves behind, and there are three kinds of it.
  **Worktree directories** accumulate conditionally: a parked task's persists
  by design, and a failed or errored task's persists when it is dirty, since
  `git worktree remove` is never forced — bounded in practice by how many
  questions go unanswered. S11 closed the unconditional half: `main_loop`'s
  crash handler now releases an `error` task's tree, which a crash out of
  `run_task` used to skip entirely. **Branches** accumulate unconditionally:
  nothing deletes `claudeloop/<task-id>`, on any outcome, so a long run leaves
  one branch per task ever run, done ones included. **Claude Code's own
  transcripts** now do too: it keys stored sessions on the slugified working
  directory, so `~/.claude/projects/` gains one directory per task id holding
  that session's full transcript, where before S6 every task shared one
  directory because they all ran in `cfg.repo`. `release` takes the worktree;
  the transcript directory outlives it, and it lives outside `~/.claudeloop`
  where nothing ClaudeLoop documents will look. No age or count policy for any
  of the three; the branches and the transcripts are the unbounded halves, and
  S11 deliberately left both. Deleting a finished task's branch would destroy
  the only copy of the work that task was paid to produce, and the transcript
  directories belong to Claude Code rather than to ClaudeLoop.
- **A task parked across the S6 upgrade cannot be resumed.** Same cause:
  Claude Code keys its stored sessions on the working directory, every pre-S6
  session ran with `cwd=cfg.repo`, and every S6 session runs in a worktree, so
  `--resume` cannot find the session that asked the question. The failure is
  silent — no result file and no rate limit, so the loop nudges, burns every
  resume against an unresolvable session id, and marks the task `- [!]`. It is
  a one-time cost affecting only tasks parked at the moment of the upgrade;
  the remedy is to answer parked tasks before upgrading.
- `ANSWER_PROMPT` tells a resumed session its uncommitted changes are still
  there, unconditionally. False only if an operator wipes
  `~/.claudeloop/worktrees` while a task is parked, in which case `ensure`
  recreates the tree from the task's branch and the session is told about work
  that is gone. Left as written: qualifying it would cost every honest resume
  clarity to cover an operator action.
- The answered path does not publish `set_status(pending=...)`, so the
  dashboard's backlog list can be stale while a resumed task runs. Deliberate:
  publishing it would cost a `source.pending()` network round trip on every
  resume.
- A `claude -p` session survives its parent being killed abruptly: it runs
  with `start_new_session=True`, and the loop's kill path only runs on its
  own orderly exit. An operator who kills the orchestrator with SIGKILL must
  also kill the session themselves. Observed during the Jira source's live
  smoke test.
- **With no config file and no `--setup`, `main()` now blocks on the setup
  wizard instead of exiting.** Deliberate — S5's whole point is that a
  first-run operator has something to open instead of a README to read — but
  it means a `systemd` unit with `Restart=on-failure`, or the S4 addon, whose
  `config.toml` goes missing hangs holding the loopback socket rather than
  crash-looping visibly where a supervisor would notice.
- Nothing ever *removes* a marketplace. Dropping an entry from the target
  repository's `extraKnownMarketplaces` leaves it registered at user scope,
  where it stays available to every other Claude Code run on the box.
  Deliberate: removing something an operator may also use by hand is a worse
  failure than leaving it, and `claude plugin marketplace remove` is one
  command.
- Nothing pins a version. A plugin updated in its marketplace changes what
  sessions do with no change to the repository and nothing in the run log to
  say so. Since S8 the repository owns the choice, so this is the
  repository's problem rather than ClaudeLoop's.
- No automated test executes `static/index.html`'s or `static/setup.html`'s
  JavaScript. `tests/test_setup.py`'s `WizardPageTest` only asserts on the
  file's text. Every real client-side defect S5 found — a `[session_env]` row
  corrupting its siblings on removal, a failed save's errors never reaching
  the screen, an error on a hidden field wedging `Next` with no visible fix,
  and the `get()`/default gap that made a first run uncompletable outright —
  was caught only by a hand-built Node DOM shim or an actual browser session,
  neither of which runs as part of the test suite. No third-party package may
  be added to close this; the stdlib has no DOM.

## Working notes

- **`commit.gpgsign` is `false` locally in this repo.** Set during development
  when the 1Password SSH agent locked mid-session. Commits from `551927b` are
  unsigned. Restore with `git config --local --unset commit.gpgsign` once the
  agent is reliably available. Note the same agent makes `git commit` **hang**
  inside scratch repositories created by tests — test fixtures disable signing
  locally for that reason.
- **`origin/main` exists but trails.** It sits at `73ca68e`; every slice from
  S10 on is local only. The older note here said nothing had ever been
  pushed, which stopped being true.
- **`home` is not a config key.** It is an argument to `load_config(path,
  home)`, defaulting to `~/.claudeloop`, and `validate()` silently ignores a
  `home =` line in a TOML file. S10's smoke test put one in a scratch config
  and the run wrote its state, runs and worktrees into the *real*
  `~/.claudeloop` — harmless, since `state.db` is scoped by repository path,
  but the rows and directories had to be deleted by hand afterwards. A smoke
  test that wants its own home has to pass one, not configure one.
- **Any route on the web server that returns early must close its
  connection.** `do_POST` sets `self.close_connection = True` as its first
  statement, and that line is a security fix rather than tidiness. Every
  rejection path in the answer route answers *without* draining the request
  body, and `protocol_version` is `HTTP/1.1` — so on a keep-alive connection
  those unread, attacker-controlled bytes were parsed as the next request. A
  cross-origin page could send one CORS-safelisted `text/plain` POST (no
  preflight) whose body was a well-formed `application/json` POST: the first
  got its 415, the smuggled second cleared every guard and wrote the answer
  file, which the loop then splices into a prompt for a session running with
  bypassed permissions. It bypassed the `Host` check too, since the smuggled
  request carries its own. Found in review, reproduced live against a running
  server, and covered by a same-socket regression test. Any future route that
  returns before reading the body has the same exposure.
