# ClaudeLoop

An unattended orchestrator for Claude Code. It takes tasks one at a time from a
markdown checklist, runs a headless Claude Code session per task in a target
repository, sleeps through subscription rate limits and resumes the same
session afterwards, and records every outcome.

ClaudeLoop holds no workflow logic of its own. The target repository's
`CLAUDE.md` defines what "done" means there — testing, verification, review,
whatever it says — and ClaudeLoop's per-task instruction just points at it.

## Requirements

Python 3.11 or newer, and the Claude Code CLI on `PATH`, already authenticated
(`claude setup-token` for an unattended host). No Python packages to install.

## Configure

`~/.claudeloop/config.toml`:

```toml
repo                     = "/home/you/Projects/yourrepo"
tasks_file               = "/home/you/Projects/yourrepo/.claudeloop-tasks.md"
model                    = "opus"   # optional, default "opus"
max_resumes              = 20       # optional, default 20 -- bounds plain nudges
max_waits                = 200      # optional, default 200 -- bounds quota waits, separately
session_timeout_s        = 14400    # optional, default 14400 (4h) -- kills a wedged session
web_host                 = "127.0.0.1"  # optional, default "127.0.0.1" -- see Dashboard below
web_port                 = 8765         # optional, default 8765
web_token                = ""           # optional, default "" -- required if web_host isn't loopback
instructions_file        = "~/.claudeloop/instructions.md"        # optional, this is the default
definition_of_done_file  = "~/.claudeloop/definition-of-done.md"  # optional, this is the default
settings_file            = ""     # optional, default unset -- passed to the CLI as --settings
mcp_config               = ""     # optional, default unset -- passed to the CLI as --mcp-config
strict_mcp               = false  # optional, default false -- requires mcp_config; passes --strict-mcp-config

[session_env]              # optional, default empty -- extra environment variables for the session
# GH_TOKEN = "ghp_..."
```

`config.toml` holds secrets (`web_token`, anything in `[session_env]`) and
must be `chmod 600`; ClaudeLoop refuses to load it otherwise.

One instance serves one repository. For a second repository, run a second
instance with its own config.

## The session's instructions

Every session carries a system prompt assembled from three layers, in this
order of precedence:

1. **The ClaudeLoop protocol** — invariant, not configurable. It tells the
   session it's unattended and defines how a task ends (writing the result
   file named in `CLAUDELOOP_RESULT`).
2. **Operator instructions** — `instructions_file`, read from the machine
   running ClaudeLoop. It outranks the repository because the operator, not
   the repository, controls this machine. Optional: absent when the file is.
3. **Definition of done** — the repository's own `CLAUDE.md` (or
   `.claude/CLAUDE.md`), followed end to end when present. A repository with
   neither gets a built-in definition of done instead: implement the change,
   run the tests, commit on a branch, open a pull request — or, with no
   remote configured, stop after committing and say so. `definition_of_done_file`
   overrides the built-in default; the repository's own `CLAUDE.md`, if it
   has one, always wins over both.

Where layers conflict, the higher one wins and the session says so in its
summary.

`settings_file` and `mcp_config` are passed straight through to the `claude`
CLI as `--settings` and `--mcp-config`; `strict_mcp` adds
`--strict-mcp-config` and requires `mcp_config` to be set. `[session_env]`
adds extra environment variables to the session — useful for a `GH_TOKEN` or
similar the repository's workflow expects. It cannot override
`CLAUDELOOP_RESULT`: that variable is always the path ClaudeLoop itself
tracks, no matter what `[session_env]` says, since it's the only thing the
loop uses to decide a task finished.

Every key in this section is optional; an unconfigured ClaudeLoop behaves
exactly as before.

## Dashboard

ClaudeLoop runs a small read-only web dashboard on its own daemon thread:
current task and status, a live stream of the running session's output, the
pending queue, and completed task history with cost and timing. It reads
`state.db` through its own read-only connection and tails `events.jsonl` off
disk — it never touches the loop's own objects, so nothing served here can
affect what the loop does.

By default it binds `127.0.0.1:8765`, reachable only from the machine
ClaudeLoop runs on. To reach it from another device, set `web_host` to a
non-loopback address (or `0.0.0.0`) — at which point `web_token` becomes
required and every request must carry `?token=...`. This isn't optional
because the dashboard watches an agent holding real, unattended credentials
for `repo`: task text, tool output, and file contents it read are all visible
through it, so exposing it beyond this machine has to be a deliberate act
with a token guarding it, not a default.

The dashboard cannot start or stop tasks, edit files, or otherwise change
anything the loop is doing — every route is a read.

## Tasks

A markdown checklist. Unchecked items run in file order.

```markdown
- [ ] Fix the cart total rounding on the store grid
- [x] Add Money serialization to the admin SPA
- [!] Migrate the renderer to Containers
```

`- [x]` succeeded. `- [!]` needs a human — it failed, was blocked on a
question, or exhausted its resume budget. Neither is picked up again. Append
new tasks at any time; the loop re-reads the file after each one.

A task's id is a hash of its line text, so keep task lines distinct — two
identical lines collapse to the same database row while still running twice.

## Run

```bash
python -m claudeloop
```

## Where things go

```
~/.claudeloop/
  config.toml
  state.db                      # what happened: status, summary, cost, timings
  runs/<task-id>/
    events.jsonl                # the raw stream-json stream, appended per attempt
    result.json                 # the session's own verdict
    stderr.log
```

## Warning

Sessions run with `--permission-mode bypassPermissions` and no human present.
Whatever credentials the target repository's workflow uses, an unattended agent
is using them — including, if that repository authorizes it, pushing to `main`
and triggering a production deploy. Point ClaudeLoop only at repositories whose
`CLAUDE.md` you are willing to have executed without review.

## Tests

```bash
python -m unittest discover -s tests -t .
```
