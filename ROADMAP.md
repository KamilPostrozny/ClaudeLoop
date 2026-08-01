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
| **S3** | Jira task source | designed, no spec written |
| **S2b** | Question and answer channel | not started |
| **S5** | Setup wizard and config schema | not started |
| **S4** | Home Assistant OS addon | not started |

Two orderings are deliberate. **S3 precedes S2b** so the answer channel is
designed against two task sources at once, rather than built for the web and
retrofitted to Jira. **S5 follows S3** so its config schema is written once
against the final key set, absorbing the cost of folding S1.1's and S3's
hand-written validation into it. **S4 is free-floating** — nothing blocks it,
and it is the slice that stops a laptop being the thing that dies mid-task.

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

---

## Next

### S3 — Jira task source

A second `TaskSource` implementation. The loop, session, database and dashboard
need no changes — they already talk to `pending()` and `mark()`.

**Decided:**

- **Jira Cloud, API token, Basic auth, `urllib`.** API tokens are free on any
  tier. OAuth 3LO would need a registered app and a browser consent flow, which
  a headless box cannot complete.
- **REST v2, not v3.** v2 returns `description` as a plain string and accepts a
  plain-string comment body; v3 returns and demands ADF JSON. If v2 is ever
  retired the fallback is a small ADF flattener.
- **Task identity hashes the issue key, not the text.** `task_id(issue_key)`
  keeps the id 16 hex characters, which `web.py`'s `TASK_ID_RE` requires as a
  path-traversal guard, and means editing a ticket does not mint a new task.
  The issue key is the `source_ref`.
- **ClaudeLoop composes the blocked-label exclusion into the operator's JQL**
  rather than trusting them to remember it, splitting on `ORDER BY` so their
  ordering survives. The guard cannot be accidentally disabled.
- **Failure adds a label ClaudeLoop owns** (`claudeloop-blocked`) rather than
  transitioning to a status the workflow may not permit from where the issue
  sits. The direct analogue of `- [!]` in the file source.
- **Transitions on start and on done**, target names in config, matched per
  issue against the transitions Jira actually offers. A missing name logs and
  continues rather than failing the task.
- **The orchestrator posts the closing comment** with status, summary and cost.
  It exists even when the session died mid-run and never got to say anything —
  which is exactly when a record on the ticket matters most.
- **The session talks to Jira through `python -m claudeloop.jira`**, a small
  CLI over the same HTTP code, not through the Atlassian MCP plugin. One
  credential instead of two, no OAuth browser flow on the S4 box, and the
  session's Jira access is code that can be tested. The plugin stays available
  for interactive use; nothing depends on it.
- **Network failures never look like an empty backlog.** `pending()` swallows
  HTTP errors, logs, and returns `[]` so the loop idles and retries. `mark()`
  retries a few times then logs loudly — the task really is finished and
  `state.db` records it.

**Open — one non-obvious JQL trap.** `labels != "x"` **excludes issues that
have no labels at all.** The correct idiom is
`(labels IS EMPTY OR labels NOT IN ("x"))`.

**Unverified.** The current search endpoint path — Atlassian moved it recently.
Confirm against the live instance before writing the code, read-only first.

### S2b — Question and answer channel

The one part of the original UI wish-list that is not built. Needs the session
protocol to permit asking, the loop to pause on a `blocked` result, and a
resume carrying the human's answer.

**Already in place for it:** the `blocked` status and the `question` field
exist in the result schema, and `tasks.question` exists as a column,
specifically so S2b needs no schema change.

**Known hazard:** `status.py`'s lock-free design holds only because exactly one
thread — the loop — calls `set_status`. A human answering from the web thread
is a second writer, and `set_status` is a read-modify-write. Its docstring says
so; S2b has to solve it rather than rediscover it.

### S5 — Setup wizard and config schema

An in-browser first-run wizard for operators who should not have to hand-edit
TOML. The config surface is roughly 25 keys with non-obvious interactions.

**Decided:**

- **One schema that both validates and renders the wizard**, replacing
  `config.py`'s hand-written validation. A wizard needs a human explanation per
  key; a schema is where those live.
- **It is the first slice to write anything from the browser**, deliberately
  breaking S2a's global read-only rule, and it writes a file containing tokens.
- **Setup mode binds loopback unconditionally and additionally requires a
  one-time token printed to the console.** Two independent barriers: with no
  config there is no `web_token` to authenticate with, and an unauthenticated
  setup endpoint would let anyone reaching it configure an agent that runs with
  bypassed permissions against real credentials.
- **A curated "proposed plugin set"**, each plugin carrying optional usage
  instructions in **its own file, separate from `instructions.md`** — different
  authors, different lifetimes. That adds a fourth prompt layer, slotting below
  the operator layer and above the definition of done, since it is ClaudeLoop's
  advice about its own tooling and the operator must be able to override it.

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

## Working notes

- **`commit.gpgsign` is `false` locally in this repo.** Set during development
  when the 1Password SSH agent locked mid-session. Commits from `551927b` are
  unsigned. Restore with `git config --local --unset commit.gpgsign` once the
  agent is reliably available. Note the same agent makes `git commit` **hang**
  inside scratch repositories created by tests — test fixtures disable signing
  locally for that reason.
- **Nothing has ever been pushed.** `origin` exists but has no `main`.
