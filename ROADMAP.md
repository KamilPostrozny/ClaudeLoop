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
| **S7** | Proposed plugin set | merged |
| **S4** | Home Assistant OS addon | not started |

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

`plugins.py` (new) is a single table plus the logic to install it. `Plugin`
is a frozen dataclass — `name`, `plugin_id` (`name@marketplace`),
`marketplace`, `reason` (the wizard's one-line checkbox caption) and `usage`
(the fourth prompt layer's text, empty on the ordinary plugin). `PROPOSED` is
superpowers, caveman and ponytail, in that order; `by_name` looks one up by
its `config.toml` shorthand. Only `superpowers` carries `usage` —
its brainstorming skill asks a human one question at a time and refuses to
implement without approval, both wrong under an orchestrator, so
`SUPERPOWERS_USAGE` tells the session to read the repository instead of
asking, and that queuing the task *was* the approval. `caveman` and
`ponytail` ship none, by design: both already state their own rules.

`config.py` gained `plugins: tuple[str, ...]` — `Field("plugins", "list",
step="plugins", default=())`, a `"list"` branch in `_coerce`, and a
`_known_plugins` check: a bare name must be one of the three proposed, and
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
plugin's text and gives a plugin outside the proposed three a block of its
own; unreadable counts as absent, so a permissions mistake can't stop a
session starting. `prompt.precedence()` gained `has_plugins`, stating the
layer only when it is non-empty, and positions it below the operator layer
and above the definition of done — ClaudeLoop's own advice about its own
tooling, which the operator must be able to override. `prompt.compose()`
slots the block in between the operator instructions and the definition of
done.

`setup.py` and `static/setup.html` gained a Plugins wizard step: the
proposed three render as checkboxes with their `reason` as the caption
(never their `usage` text, which stays server-side), plus a free-text
`plugin@marketplace, comma separated` row for anything else. `dump_toml`
writes `plugins` as a TOML array through the same `SCHEMA`-driven path as
every other key, and an empty selection writes no key at all, exactly like
every other optional field.

**The live smoke test ran twice**, on `haiku`, against a fresh scratch
repository each time, `plugins = ["superpowers", "caveman", "ponytail"]`, two
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
- Task 1 was phrased as a feature to design and build, exactly the shape that
  trips `superpowers`' brainstorming approval gate. It implemented the work
  and wrote a result file rather than ending its turn waiting for a human to
  approve a design — the defect `SUPERPOWERS_USAGE` exists to prevent.
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

---

## Next

### S4 — Home Assistant OS addon

Dockerfile, `config.yaml`, ingress sidebar, persistent `/data`. Nothing blocks
it. The work is mostly in the landmines below rather than in the packaging.

**Landmines, all discovered by reading the first real target repository:**

- **Commit signing.** The reference repo signs with an SSH key held in a
  1Password agent. A headless box cannot unlock it. S1.1's `[session_env]`
  addresses this — the `GIT_CONFIG_COUNT`/`KEY_0`/`VALUE_0` trio forces
  `commit.gpgsign=false` for the session's git alone — but it has not been
  verified live.
- **Claude-in-Chrome is a browser extension** and cannot run headless. A repo
  mandating a Chrome verification sweep needs Playwright instead, which belongs
  in the operator instruction layer.
- **Plugin and hook first-run trust prompts** stall a headless session. The
  image has to pre-trust them.
- **Secrets** the target repo's own verification phase needs must exist in the
  container.
- **Claude authentication.** `claude setup-token` produces the long-lived
  credential; subscription session limits are what the whole recovery path
  exists for.

---

## Open issues carried across slices

Real, deliberately deferred, tracked here so they are not lost.

- `state.db` is created at the default umask and holds task text, summaries and
  blocked questions. Run directories are 0700 and event logs 0600, but
  `~/.claudeloop` and `runs/` are not.
- `events.jsonl` grows without bound. No rotation, no size cap. Reads are
  bounded, so this is disk usage rather than a hang.
- `--append-system-prompt` carries the composed prompt as one argv element.
  Linux caps a single argument at 128 KiB; a very large operator instructions
  file would fail `execve` with an opaque error.
- The dashboard token travels in the query string, because `EventSource` cannot
  set headers. It therefore reaches browser history and screenshots, and in S4
  it will reach the Home Assistant ingress access log. The complete fix is a
  `Set-Cookie` plus `history.replaceState` pair, worth doing when the token path
  is actually used.
- `Config` has a `dict` field, so it is unhashable. Nothing hashes it.
- `JiraSource.pending` fetches one page of 50 issues and never paginates, so an
  ordering that puts wanted work past the 50th row never reaches it.
- The dashboard's pending list is now published on the loop's status snapshot
  rather than re-read on the web thread, so it reflects the backlog as of the
  current task's start rather than live — under the file source, that's a
  step back from re-reading the tasks file on every request.
- Nothing prunes what a task leaves behind, and there are three kinds of it.
  **Worktree directories** accumulate conditionally: a parked task's persists
  by design, a failed task's persists when it is dirty, since `git worktree
  remove` is never forced, and an `error` task's persists always, because a
  crash out of `run_task` never reaches `release` — bounded in practice by how
  many questions go unanswered. **Branches** accumulate unconditionally:
  nothing deletes `claudeloop/<task-id>`, on any outcome, so a long run leaves
  one branch per task ever run, done ones included. **Claude Code's own
  transcripts** now do too: it keys stored sessions on the slugified working
  directory, so `~/.claude/projects/` gains one directory per task id holding
  that session's full transcript, where before S6 every task shared one
  directory because they all ran in `cfg.repo`. `release` takes the worktree;
  the transcript directory outlives it, and it lives outside `~/.claudeloop`
  where nothing ClaudeLoop documents will look. No age or count policy for any
  of the three; the branches and the transcripts are the unbounded halves.
- **A task parked across the S6 upgrade cannot be resumed.** Same cause:
  Claude Code keys its stored sessions on the working directory, every pre-S6
  session ran with `cwd=cfg.repo`, and every S6 session runs in a worktree, so
  `--resume` cannot find the session that asked the question. The failure is
  silent — no result file and no rate limit, so the loop nudges, burns every
  resume against an unresolvable session id, and marks the task `- [!]`. It is
  a one-time cost affecting only tasks parked at the moment of the upgrade;
  the remedy is to answer parked tasks before upgrading.
- **A per-task-permanent worktree fault blocks the head of the queue
  indefinitely.** `ensure` reuses `worktrees/<task-id>` only when `.git`
  exists inside it, so a non-empty directory left there without one —
  ClaudeLoop killed mid-`add`, a reboot, an operator deleting `.git` while
  tidying — makes `git worktree add` fail with "already exists" every time.
  That is an environment fault by design, recorded as `error`, which is
  deliberately non-terminal, so the task keeps being offered and re-picked
  every `POLL_S` forever and no later task runs. Not new in kind — any
  per-task-permanent crash in `run_task` did this before S6 — but S6 adds a
  way to reach it. Clearing the directory unblocks it.
- `ANSWER_PROMPT` tells a resumed session its uncommitted changes are still
  there, unconditionally. False only if an operator wipes
  `~/.claudeloop/worktrees` while a task is parked, in which case `ensure`
  recreates the tree from the task's branch and the session is told about work
  that is gone. Left as written: qualifying it would cost every honest resume
  clarity to cover an operator action.
- **A task that parks and is later answered reports only the cost of its
  resume.** `run_task` starts its `cost` accumulator at zero on every call and
  `State.finish_task` writes `cost_usd=?` rather than adding to it, so the
  money spent before the question was asked is overwritten. Measured in S6's
  live smoke test: a task that spent $0.0395 parking and $0.0162 finishing is
  recorded at $0.0162, and the dashboard and the source's closing comment both
  report that. Pre-existing, from S2b — parking is what made a task able to
  span two `run_task` calls.
- The answered path does not publish `set_status(pending=...)`, so the
  dashboard's backlog list can be stale while a resumed task runs. Deliberate:
  publishing it would cost a `source.pending()` network round trip on every
  resume.
- `JiraSource.answer` reads the full comment list on every poll for every
  parked task — one `GET /comment` every `POLL_S` (30s), indefinitely, since
  a parked task never expires: roughly 2,900 Jira requests per parked ticket
  per day, forever. Also unpaginated, the same limitation `pending()` already
  carries.
- The dashboard's answer box has no draft persistence. `renderCompleted` keys
  on `id:status` across every task, so any unrelated task changing status
  mid-typing rebuilds the list and wipes the draft — not only a closed tab.
- The test suite emits roughly 46 `ResourceWarning`s about unclosed SQLite
  connections. Pre-existing on `main` and unrelated to any slice so far, so
  not yet triaged.
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
- `write_config`'s fix for `O_CREAT`'s mode only applying to a file it
  creates — `os.fchmod` on the open fd before writing — has no test that
  fails without it. The existing test only reads the file's mode after the
  write finishes, which lands on `0600` either way; a test that would actually
  fail needs to catch the mode mid-write, e.g. mocking `os.fdopen` to assert
  `os.fstat(fd)` before any byte is written. Not added: this repo's
  convention is real files on disk, not mocks. Flagged during S5's review for
  triage rather than fixed, so it isn't rediscovered as a silent gap later.
- Nothing ever *removes* a plugin. Dropping a name from `plugins` leaves it
  installed and enabled at user scope, so it keeps affecting every session
  and every other Claude Code run on the box. Deliberate: uninstalling
  something an operator may also use by hand is a worse failure than leaving
  it, and `claude plugin uninstall` is one command.
- `reconcile` pins no version. A plugin updated in its marketplace changes
  what sessions do with no config change and nothing in the run log to say
  so. `claude plugin install` takes no version, so pinning would mean
  managing the plugin cache directly.
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
- **Nothing has ever been pushed.** `origin` exists but has no `main`.
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
