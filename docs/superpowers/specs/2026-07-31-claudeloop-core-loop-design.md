# ClaudeLoop — S1 Core Loop — Design

Date: 2026-07-31
Status: approved, ready for implementation planning

## Goal

An unattended orchestrator that pulls tasks one at a time from a pluggable
source, runs a headless Claude Code session per task until the task is finished
by the standards of the repository being worked on, survives subscription rate
limits by sleeping until the quota resets and resuming the same session, and
records what happened.

S1 is the CLI orchestrator only. It is the whole product; every later slice
reads the state it writes.

## Project decomposition

ClaudeLoop as described spans five independent subsystems. Each gets its own
spec, plan, and implementation cycle. This document covers S1 only.

| Slice | Scope | Depends on |
|---|---|---|
| **S1** | Core loop: task source interface, file source, headless sessions, rate-limit recovery, state | — |
| **S2** | Web UI: live status, streaming output, pending/completed lists, question display | S1 |
| **S3** | Jira source: JQL-driven task pull, comment Q&A, status transitions | S1 |
| **S4** | Home Assistant OS addon: Dockerfile, `config.yaml`, ingress sidebar, persistent `/data` | S1 + S2 |
| **S5** | Config web UI: model, plugins, credentials via browser | S4 |

S5 is speculative. The addon can mount a prepared config directory; build S5
only if hand-editing that directory proves painful in practice.

## Key insight

The repository defines the work, not the orchestrator.

`~/Projects/assimo/CLAUDE.md` — the first real consumer — already specifies a
complete autonomous workflow: brainstorm to `raw/specs/`, plan to `raw/plans/`,
TDD implementation, live verification against production via both HTTP and a
real browser, an ADR plus wiki refresh, then commit and push to `main`. It is
explicitly handsfree, with no approval gates, since 2026-06-10.

ClaudeLoop therefore contains no workflow logic, no phase engine, no checklist
of its own. The per-task instruction is one paragraph pointing at the repo's
own `CLAUDE.md`. Different repos get different behaviour for free, which is the
extensibility requirement satisfied by doing nothing.

## Decisions

### Runtime: Python, standard library only

`asyncio` for subprocess supervision and streaming, `sqlite3` for state,
`tomllib` for config, `http.server` for S2's UI. Zero dependencies, so the S4
Dockerfile is a base image plus a copy. The orchestrator is a process
supervisor, not application code, so matching the target project's stack buys
nothing.

### Authentication: Max/Pro subscription token

`claude setup-token` produces a long-lived credential for the always-on host.
Consequence: five-hour and weekly session limits are real, and recovery is the
central feature rather than dead code. An API key would remove the limits and
the need for this project's hardest component, at the cost of per-token billing
on an unattended loop.

### Rate-limit detection: the `rate_limit_event` stream message

`--output-format stream-json` emits, continuously and regardless of whether the
request was throttled:

```json
{"type":"rate_limit_event",
 "rate_limit_info":{"status":"allowed","resetsAt":1785516000,
                    "rateLimitType":"five_hour","overageStatus":"rejected",
                    "overageDisabledReason":"out_of_credits"},
 "session_id":"..."}
```

`resetsAt` is a unix timestamp. Recovery is `sleep_until(resetsAt)`, with no
parsing of the English `"You've hit your session limit · resets 1:40pm
(Europe/Warsaw)"` notice and no timezone handling.

Any `status` other than `"allowed"` is treated as blocking. `rateLimitType` is
recorded but not branched on, so a weekly or model-specific limit is handled by
the same path as `five_hour`.

The target account reports `overageStatus: "rejected"` with
`overageDisabledReason: "out_of_credits"`, meaning requests hard-stop at the
limit rather than spilling into paid overage. Recovery is load-bearing.

Because the event arrives even when `status` is `"allowed"`, the most recent
one always carries the current quota headroom. S2 renders it as a live gauge at
no extra cost.

### Session identity: pre-assigned UUID

`claude --session-id <uuid>` accepts a caller-supplied UUID. The orchestrator
mints one per task and resumes with `--resume <uuid>`. Resume never depends on
scraping a session id out of the output stream, and the id is known before the
process starts, so a crash during the first millisecond is still recoverable.

### Completion signal: a result file outside the repository

The session writes JSON to the path in `$CLAUDELOOP_RESULT`:

```json
{"status": "done" | "failed" | "blocked",
 "summary": "one paragraph",
 "question": "only present when status is blocked"}
```

The file lives in the run directory, not the working repository — no
`.gitignore` entry, no stray file in a commit, no interference with the repo's
own tooling.

Rejected alternatives:

- **Exit code.** A rate-limit termination and a success are indistinguishable.
- **`--json-schema` structured output.** Validates the final assistant turn,
  but a session that dies at the limit has no final turn, and after a resume
  the last turn belongs to `"Continue."` rather than to the task. A file
  survives both, including `SIGKILL`.

### Concurrency: strictly serial

The reference workflow pushes to `main`, and CI deploys from `main` to
production. Two concurrent tasks would race on a shared deployment target and
on live verification against it. One task at a time.

Git isolation within a task is not ClaudeLoop's concern: Claude Code has a
native `--worktree` flag and an `EnterWorktree` tool, and the assimo repo
already drives it with a `PreToolUse` hook. The orchestrator sets `cwd` to the
repo root and lets the session decide.

### Blocking questions: not in S1

The reference repo instructs the agent to decide for itself, treating an
unnecessary spec as the cheap failure mode. S1 builds no question channel. The
`blocked` status and `question` field exist in the result schema so that S2 and
S3 can render and route them without a schema change; in S1 a `blocked` task is
recorded and the loop continues to the next one.

### Task-to-repository binding: one repository per configuration

A ClaudeLoop instance serves one repository. A second repository means a second
instance with a second config file. No alias map, no prefix syntax, no
cross-repo ordering semantics.

## Architecture

Single process. Four units, each independently testable.

### `config.py`

Reads `~/.claudeloop/config.toml` via `tomllib`. Never writes it.

```toml
repo              = "/home/kamil/Projects/assimo"
tasks_file        = "/home/kamil/Projects/assimo/.claudeloop-tasks.md"
model             = "opus"
max_resumes       = 20
max_waits         = 200
session_timeout_s = 14400
```

`max_resumes` bounds plain nudges — invocations that exit with no result and
no blocking rate limit, i.e. made no progress on their own. `max_waits` bounds
quota waits separately, with a much larger default: waiting out a real rate
limit is progress, not stalling, so a task that is purely waiting on quota
must not be discarded just because it has been resumed many times.
`session_timeout_s` bounds a single invocation: a wedged `claude` is killed
and its exit treated as a plain nudge rather than parking the orchestrator
forever.

### `source.py`

```python
class TaskSource(Protocol):
    def pending(self) -> list[Task]: ...
    def mark(self, task: Task, status: str, summary: str) -> None: ...
```

`FileSource` reads a markdown checkbox list:

```markdown
- [ ] Fix the cart total rounding on the store grid
- [x] Add Money serialization to the admin SPA
```

`pending()` returns unchecked lines in file order. `mark()` rewrites the single
matching line to `- [x]`, matching on **exact line text rather than index**, so
edits to the file while a task runs cannot check off the wrong line. A task
whose line has vanished by completion time is recorded in the database and the
rewrite is skipped.

State is visible and editable in the file, which is how the human already
thinks about a todo list. The alternative — tracking completions by content
hash in the database and never touching the file — avoids the rewrite but hides
progress from the user and makes an edited task look new.

### `session.py`

Spawns and streams one Claude Code process.

```
claude -p "<task text>"
  --session-id <uuid>
  --append-system-prompt "<protocol>"
  --output-format stream-json --verbose --include-partial-messages
  --permission-mode bypassPermissions
  --model <config.model>
```

with `cwd` set to `config.repo` and `CLAUDELOOP_RESULT` in the environment.
Resume is the identical command with `--resume <uuid>` and the prompt
`"Continue."`.

The protocol paragraph goes in `--append-system-prompt` rather than the prompt
so that it still applies on resume turns, whose prompt is only `"Continue."`:

> Follow this repository's CLAUDE.md end to end — it defines what "done" means
> here, including its testing and verification requirements. When the task is
> fully complete, or provably cannot be completed, write JSON to the path in
> `$CLAUDELOOP_RESULT` with keys `status` (`done`, `failed`, or `blocked`),
> `summary`, and, when blocked, `question`.

Every stdout line is appended verbatim to `runs/<task-id>/events.jsonl` before
being parsed, so a parser bug never loses the record. Malformed lines are
logged and skipped rather than killing the run.

`claude -p` skips the workspace trust dialog, so an unattended first run in a
fresh checkout does not stall.

### `loop.py`

The state machine, expressed as a pure function so it can be tested against
recorded event streams without spawning anything:

```python
def decide(events, result_exists, resume_count, max_resumes, wait_count, max_waits) -> Action
```

`events` is the stream from the invocation that just exited, not the task's
whole history — a rate-limit event from an earlier attempt must not re-trigger
a wait after a later attempt exits for a different reason.

Nudges and quota waits are bounded by separate counters, since a wait is not a
failure to make progress — only a nudge is. A task can be resumed by quota
waits alone far longer than `max_resumes` would otherwise allow.

| Condition | Action |
|---|---|
| result file exists | `Finish(status from file)` |
| last `rate_limit_event` **of the run that just exited** has `status != "allowed"`, and `wait_count < max_waits` | `WaitUntil(resetsAt + 30s)` then resume |
| blocking rate limit, but `wait_count >= max_waits` | `Finish("failed", reason="no_result")` |
| no blocking rate limit, and `resume_count < max_resumes` | `Resume("Continue.")` |
| otherwise | `Finish("failed", reason="no_result")` |

The result file is checked first, so a session that finished its work and then
tripped the limit on a trailing turn is recorded as done rather than parked
until the reset.

The thirty-second pad past `resetsAt` absorbs clock skew between the host and
the API.

Process-level failures — a non-zero exit with no result file and no blocking
rate-limit event — fall through to the nudge path and are bounded by
`max_resumes`. A blocking rate-limit event instead increments `wait_count` and
is bounded by the separate, much larger `max_waits`.

The database records which kind of resume happened — `RateLimited` for a wait,
`Nudge` otherwise — rather than the Python class name, so wall time spent
waiting on quota is a queryable fact rather than something to reverse out of
`Resume` rows after the fact.

A still-running invocation is bounded too: `session.run` kills the process
after `session_timeout_s` (default four hours) and returns whatever partial
events it collected, which `decide` then treats like any other clean-ish exit
with no result and no blocking rate limit — a plain nudge.

### `state.py`

`sqlite3` at `~/.claudeloop/state.db`.

- `tasks(id, source, source_ref, text, status, created_at, started_at, finished_at, summary, cost_usd, question)`
- `runs(id, task_id, session_id, started_at, ended_at, exit_reason, resume_count)`

Completions, summaries, cost, and exit reasons only. The raw event stream stays
in per-run `events.jsonl`; S2's UI tails that file directly rather than
round-tripping megabytes of streaming deltas through the database.

The source file remains the source of truth for what is pending. The database
is the record of what happened.

### Layout

```
~/.claudeloop/
  config.toml
  state.db
  runs/<task-id>/
    events.jsonl
    result.json
```

## Security

Running with `--permission-mode bypassPermissions`, unattended, against the
assimo repository means the session holds production Cloudflare credentials and
Stripe test keys, and is pre-authorized to `git push origin main`, which
triggers a production deploy through CI.

This is an accepted and deliberate consequence of the goal — hands-free
operation is the entire point — and is recorded here as a stated assumption
rather than an oversight. It bounds where ClaudeLoop may be pointed: a
repository whose `CLAUDE.md` authorizes production writes must be one whose
owner accepts an unattended agent making them.

## Deployment constraints discovered (deferred to S4)

Recorded now so S1 does not build anything that forecloses them.

- **Commit signing.** The target repo sets `commit.gpgsign=true` with
  `gpg.format=ssh` and a key held in a 1Password agent. Its own `CLAUDE.md`
  already names a locked signing agent as a known push failure. A headless host
  cannot unlock it, so S4 needs a dedicated unencrypted signing key and deploy
  key, or signing disabled in that container.
- **Browser verification.** Claude-in-Chrome is a browser extension and cannot
  run in a headless container. The repo's mandatory Chrome verification sweep
  must go through Playwright there.
- **First-run trust prompts.** The repo pins a plugin and three hooks in
  `.claude/settings.json`; both prompt interactively on an unfamiliar machine.
  The image must pre-trust them or every session stalls at startup.
- **Secrets.** `CLOUDFLARE_API_TOKEN`, `~/.config/assimo/e2e.env`, and the
  Stripe and BaseLinker test credentials must be present in the container or
  the repo's verification phase cannot pass.

## Verification

One `test_loop.py`, standard-library `unittest`, no framework.

1. `decide()` against recorded `events.jsonl` fixtures, including a real
   blocking `rate_limit_event`, covering each row of the decision table.
2. `FileSource.mark()` checking off the correct line when the file has been
   edited underneath it, and skipping cleanly when the line is gone.
3. One end-to-end run against a fake `claude` shell script that emits a canned
   event stream and writes a result file.

## Out of scope for S1

Web UI, Jira source, Home Assistant packaging, question and answer channel,
parallel task execution, configuration UI. Each reads `state.db` and
`events.jsonl` when its slice arrives.

## Acceptance criteria

- A markdown task file with three unchecked items runs to completion
  unattended, checking off each line and recording three rows in `tasks`.
- A session interrupted by a blocking `rate_limit_event` resumes automatically
  after `resetsAt` and completes the same task under the same session id.
- A session killed mid-run is either resumed or recorded as failed, never left
  hanging.
- A task whose result file reports `blocked` is recorded with its question and
  does not stall the loop.
- The orchestrator runs with no third-party Python packages installed.
