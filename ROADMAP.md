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

---

## Next

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

**What S3 leaves it, and the trap in it.** The session can already talk on a
ticket — `python -m claudeloop.jira comment KEY -` — so a Jira question has an
obvious home, and a human's reply has an obvious place to live. But a `blocked`
task today gets the `claudeloop-blocked` label, and `pending()` excludes that
label permanently. Answering a blocked question therefore has to *remove* the
label, or the answered task can never be picked up again — and under the file
source the equivalent is rewriting `- [!]` back to `- [ ]`. Neither source
does that today. Whatever S2b builds, resuming an answered task is a
task-source operation, not just a loop one, so the `TaskSource` protocol
probably grows a third verb.

**Also worth knowing:** the protocol gained `start(task)` and `mark(..., cost)`
in S3, and `JiraSource` shows the shape a non-file source takes — including
that everything it does must be safe to call from `asyncio.to_thread`.

### S5 — Setup wizard and config schema

An in-browser first-run wizard for operators who should not have to hand-edit
TOML. `Config` carries 16 keys today and will be around 25 after S3, with
non-obvious interactions between them.

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
- **`superpowers` is in the proposed set, and its usage file carries a
  question-discipline rule.** Its `brainstorming` skill asks the human one
  question at a time, which is right at a keyboard and wrong under an
  orchestrator where nobody answers. The rule to ship: *if the answer lives in
  the repository, the docs, the roadmap or the git history, go read it and
  never ask; ask only when the answer lives solely in the operator's head —
  priorities, money, who is watching, what "good" means here.* Delegating to a
  subagent is for breadth, not for dodging a question: a fresh agent starts
  cold, costs real money to re-derive context the asking agent already has,
  and cannot answer a preference question anyway. This is written as plugin
  usage instructions in config, not as an edit to the plugin's own files —
  nothing in the addon image may depend on a patched plugin cache. Its worth
  was measured on this repository: brainstorming S2b asked five questions and
  two were answerable from `CLAUDE.md` and `ROADMAP.md` alone.

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
- `JiraClient.add_label`, `transitions` and `transition` still interpolate the
  issue key into the URL unescaped — safe today only because `JiraSource` is
  their only caller and always passes keys straight from Jira's own search
  results.
- A `claude -p` session survives its parent being killed abruptly: it runs
  with `start_new_session=True`, and the loop's kill path only runs on its
  own orderly exit. An operator who kills the orchestrator with SIGKILL must
  also kill the session themselves. Observed during the Jira source's live
  smoke test.

## Working notes

- **`commit.gpgsign` is `false` locally in this repo.** Set during development
  when the 1Password SSH agent locked mid-session. Commits from `551927b` are
  unsigned. Restore with `git config --local --unset commit.gpgsign` once the
  agent is reliably available. Note the same agent makes `git commit` **hang**
  inside scratch repositories created by tests — test fixtures disable signing
  locally for that reason.
- **Nothing has ever been pushed.** `origin` exists but has no `main`.
