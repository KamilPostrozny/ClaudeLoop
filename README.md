# ClaudeLoop

An unattended orchestrator for Claude Code. It takes tasks one at a time from a
markdown checklist, runs a headless Claude Code session per task in a target
repository, sleeps through subscription rate limits and resumes the same
session afterwards, and records every outcome.

ClaudeLoop holds no workflow logic of its own. The target repository's
`CLAUDE.md` defines what "done" means there — testing, verification, review,
whatever it says — and ClaudeLoop's per-task instruction just points at it.

## Requirements

Python 3.11 or newer, the Claude Code CLI on `PATH`, already authenticated
(`claude setup-token` for an unattended host), and a `git` with `git worktree
remove` — 2.17, from 2018. No Python packages to install.

## Configure

`~/.claudeloop/config.toml`:

```toml
repo                     = "/home/you/Projects/yourrepo"
tasks_file               = "/home/you/claudeloop-tasks/yourrepo.md"  # outside repo -- see Tasks below
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

## Taking tasks from Jira

Instead of a checklist, ClaudeLoop can take its backlog from a Jira Cloud
project. `source` selects the task source, `"file"` or `"jira"`, and defaults
to `"file"`. `tasks_file` is required only under `source = "file"`; `[jira]`
is required only under `source = "jira"`.

```toml
source = "jira"

[jira]
site    = "https://yourcompany.atlassian.net"   # no /jira suffix
email   = "you@yourcompany.com"
token   = "ATATT..."          # id.atlassian.com -> Security -> API tokens
project = "OPS"               # which project to take work from
status  = "To Do"             # optional; the exact status name on your board
transition_start = "In Progress"   # optional; skipped if unset or unavailable
transition_done  = "Done"          # optional; same
```

That composes `project = "OPS" AND status = "To Do" ORDER BY created ASC`. If
you want something the two keys cannot say — an assignee, a label, a priority
ordering — give `jql` instead and it wins outright:

```toml
[jira]
site  = "https://yourcompany.atlassian.net"
email = "you@yourcompany.com"
token = "ATATT..."
jql   = "project = OPS AND assignee = currentUser() ORDER BY priority DESC"
```

ClaudeLoop always ANDs its label guard onto your query, and that guard is
itself enough of a restriction for Jira to accept — so a `jql` that is only
an `ORDER BY` still works. It still only fetches one page of 50 issues per
poll, though, so an ordering that puts the work you want past the 50th row
never reaches it.

Each matching issue becomes one task, whose text is the issue key, its
summary and its description. When a task starts, ClaudeLoop moves the issue
to `transition_start` if the workflow offers that transition from where it
currently sits. When a task ends, in order: ClaudeLoop labels the issue
`claudeloop-done` or `claudeloop-blocked`, posts a closing comment carrying
the status, the summary and the cost, then moves the issue to
`transition_done` the same way `transition_start` was tried. A transition
Jira doesn't offer from the issue's current status just logs a warning and
is not a failure — Jira, not ClaudeLoop, decides whether a transition is
permitted.

**The label is how ClaudeLoop knows a ticket is finished, not the status:**
it composes `(labels IS EMPTY OR labels NOT IN ("claudeloop-done",
"claudeloop-blocked"))` into your JQL, keeping your `ORDER BY`. You cannot
turn that off — without it a workflow that refuses the done transition would
run the same ticket forever. To re-run a ticket, remove the label. A task
that ends `failed` gets `claudeloop-blocked` too — the label only suppresses
re-runs, it does not distinguish a blocked task from a failed one. A second
backstop covers the label write itself failing: a task whose id already has
a terminal row in `state.db` is skipped even if Jira never took the label.

The session can read and comment on the ticket while it works:

```bash
python -m claudeloop.jira show OPS-42
python -m claudeloop.jira comment OPS-42 -   # body on stdin
```

`--config` points either subcommand at a config file other than the default.
The session cannot transition issues or change labels — ClaudeLoop does that
itself.

An unreachable Jira, a 401, or a JQL Jira rejects all look like an empty
backlog: ClaudeLoop logs it, idles, and tries again on the next poll. It
never burns through tasks.

## The session's instructions

Every session carries a system prompt assembled from three layers, in this
order of precedence:

1. **The ClaudeLoop protocol** — invariant, not configurable. It tells the
   session it's unattended and defines how a task ends (writing the result
   file named in `CLAUDELOOP_RESULT`).
2. **Operator instructions** — `instructions_file`, read from the machine
   running ClaudeLoop. It outranks the repository because the operator, not
   the repository, controls this machine. Optional: absent when the file is.
3. **Definition of done** — the repository's own `CLAUDE.md`, `.claude/CLAUDE.md`,
   or `AGENTS.md`, followed end to end when present (with the built-in as a
   fallback, in case that file doesn't say when the work is finished).
   A repository with none of those gets the built-in definition of done on
   its own instead: implement the change, run the repository's own tests and
   checks if it has any, commit, open a pull request — or, if there's no
   remote, or push credentials or a forge CLI are missing, stop after
   committing and say exactly what was missing. It doesn't ask the session to
   create a branch, because ClaudeLoop has already made one for it (see
   below); it only forbids checking out the default branch and committing
   there. `definition_of_done_file` overrides the built-in default; the
   repository's own file, if it has one, always wins over both.

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

Three names in `[session_env]` deserve care before you set them. Only
`CLAUDELOOP_RESULT` is actually protected (see above) -- these three aren't,
and each breaks something different:

- `PATH` — the child resolves `claude` through it; overriding it crash-loops
  every task.
- `HOME` — relocates `~/.claude`, so every session fails to authenticate.
- `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_USE_BEDROCK` — the
  quiet one. These switch the session off your Claude subscription onto
  per-token billing. ClaudeLoop's whole premise is sleeping through
  *subscription* rate limits, so with one of these set the `rate_limit_event`s
  the loop is built around simply stop arriving, and cost accrues silently
  with nothing on the dashboard to say so.

## Branches and worktrees

Each task runs in its own `git worktree` at
`~/.claudeloop/worktrees/<task-id>`, checked out on a branch ClaudeLoop
creates for it, `claudeloop/<task-id>`, cut from your repository's default
branch. The session commits there; it may rename that branch to something
descriptive. Your own working copy of `repo` is never checked out, reset or
otherwise moved — ClaudeLoop only uses it to create the worktree and to cut
the branch, and the finished commits land in it like any other local branch.

A task's worktree is removed when the task finishes. It is kept when the task
parks on a question, which is what its resumed session comes back to, and
kept when removal would destroy work: `git worktree remove` is never forced,
so a tree with uncommitted changes in it simply stays on disk and is logged.

The branch is never removed — not when the task finishes, not when it fails.
That is deliberate, since the branch is where the work is, but it means every
task ClaudeLoop has ever run leaves a `claudeloop/<task-id>` branch in `repo`,
and nothing cleans them up by age or count. Tidying is yours to do:

```bash
git worktree list                    # directories still registered
git worktree remove <path>           # clear one you're done with
git branch --list 'claudeloop/*'     # every task branch ClaudeLoop has made
git branch -d claudeloop/<task-id>   # delete one -- refuses if unmerged
```

Prefer `git branch -d` over `-D`: it refuses to delete a branch whose commits
aren't merged anywhere, which is what stops a finished task's work going
quietly when you sweep up.

Because of this, ClaudeLoop refuses to start if `repo` isn't a git repository,
if its git is too old for `git worktree`, or if it has no default branch it
can find — a local `main` or `master`, or an origin with its HEAD set
(`git remote set-head origin -a`). It says so once at startup rather than
failing every task in turn.

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
anything the loop is doing. Exactly one route writes anything: answering a
blocked task's question, below. It writes a single file that the loop reads
and consumes, and nothing else on the page can change what the loop does.

### Answering a blocked task

A session that hits something only you can decide writes a `blocked` result
with a question instead of guessing. The task **parks** — it does not stop
the loop, which carries on with the rest of the backlog — and appears in
**Completed** marked `?`, with its question and a box to answer it.

Answer it there, or, when the task came from Jira, reply on the ticket with a
comment starting with `claudeloop:`:

```
claudeloop: use the staging-eu database, not staging-us
```

Either way the loop picks that task back up before it starts anything new,
resuming the **same session** — so it still knows what it had already done
and which branch it was working on. There is no deadline: a parked task waits
indefinitely, and nothing is lost if nobody answers today.

While a task is parked, other tasks run — but each of those has its own
worktree, so the parked task's is left untouched. The resumed session finds
its branch, its commits and its uncommitted changes exactly as it left them,
and doesn't have to check anything back out.

## Tasks

`tasks_file` must live **outside** the target repository -- `load_config`
refuses to start if it resolves inside `repo`. The built-in definition of
done has each session commit its work on a branch, and a session working on
a branch reasonably runs `git add -A`, or cleans up with `git checkout --
.` or `git stash`, as ordinary branch hygiene. If `tasks_file` lived inside
the repository, any of those could sweep ClaudeLoop's own `- [x]` mark into
a commit or wipe it out entirely -- and since `main_loop` re-reads the file
on every iteration, the next task would see the same line pending again and
repeat finished work, unattended, with no bound on how many times. If you
followed an older version of this README that placed `tasks_file` inside
the repository, move it out before upgrading -- ClaudeLoop will not start
otherwise.

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
  worktrees/<task-id>/          # the task's checkout -- see Branches and worktrees
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
