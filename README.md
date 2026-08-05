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
(`claude setup-token` for an unattended host), and a `git` with `git worktree`
— 2.5, from 2015, which is what ClaudeLoop checks for at startup, though
cleanup uses `git worktree remove` from 2.17 and anything older just leaves
finished worktrees on disk. No Python packages to install.

## Setup

`python -m claudeloop` with no config file prints a loopback URL carrying a
one-time token instead of exiting. Open it and a six-screen wizard —
Repository, Task source, Dashboard, Instructions, Advanced, Review and save —
writes `~/.claudeloop/config.toml` for you, at `0600`.

`python -m claudeloop --setup` reopens the wizard against an existing config.
Secret fields (`web_token`, `jira.token`, every `[session_env]` value) come
back blank, marked *set — leave blank to keep*: leave one blank to keep the
stored value, type in it to replace it. It never sends a secret to the
browser to begin with.

The wizard binds `127.0.0.1` regardless of what `web_host` says, and the
one-time token is required on every request, page load included — with no
config yet there is no `web_token` to check requests against, so the network
barrier can't be the only one.

It can check three things live, on demand: that `repo` is a repository `git`
can make worktrees in, that the configured Jira query gets a real answer
(rather than the empty-backlog result a bad token, an unreachable site, and a
rejected JQL all produce identically), and that the `claude` CLI is installed,
signed in, and — under whatever `[session_env]` you've set — still billing to
your subscription rather than a stray API key.

Hand-editing `config.toml` still works and is documented below; the wizard
writes the same file, and the same table of keys that validates it also
supplies the `#` comments above each one.

**With no config file and no `--setup`, `python -m claudeloop` now blocks on
the wizard instead of exiting.** A `systemd` unit with `Restart=on-failure`,
or the S4 addon, whose config goes missing will hang holding the loopback
socket rather than crash-looping visibly — worth knowing before you find out
at 3am that the service is "up" and doing nothing.

## Configure

`~/.claudeloop/config.toml`:

```toml
repo                     = "/home/you/Projects/yourrepo"  # or a URL -- see below
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

`repo` may be a local path or a git URL — `https://…`, `ssh://…`, `file://…`,
or the scp shorthand `git@github.com:owner/repo.git`. A URL is cloned once, at
startup, into `~/.claudeloop/clones/<name>-<hash of the URL>`, and everything
after that works exactly as it does against a local checkout: worktrees are cut
from that clone, and the clone is the repository the dashboard and `state.db`
are keyed on. The hash is there so two projects with the same name never share
one checkout. Nothing prompts for credentials — `GIT_TERMINAL_PROMPT=0` — so a
private repository needs a credential helper, an SSH agent, or a token in the
URL, and fails at startup with git's own message if it has none. **The clone is
never refreshed:** an existing one is left alone on every later start, so tasks
branch from whatever the default branch held when it was made. Delete the clone
directory to start again from the remote.

The `#` comments in a wizard-written file aren't hand-maintained: they come
from the same table of `Field`s that `load_config` validates every key
against, so a key's help text and its validation rule can never drift apart
the way two hand-written copies could.

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
# not an English board? use "indeterminate" / "done" -- see below
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

**If your board is not in English, use a status category key.** Jira
translates its own built-in statuses per account, and the transitions API
reports the translated name — a Polish site offers `Do zrobienia`, `W toku`
and `Gotowe`, so `transition_done = "Done"` matches nothing and the ticket
never moves. (Confusingly, the *pickup* side is unaffected: JQL resolves the
untranslated name, so `status = "To Do"` does find an issue displaying `Do
zrobienia`.) Each transition key accepts four kinds of value, tried in that
order:

| Value | Example | Notes |
|---|---|---|
| Transition id | `31` | Stable across renames and locales |
| Transition name | `Gotowe` | What the API reports, translated |
| Destination status name | `Done` | For workflows like "Finish work" → "Done" |
| Status category key | `done` | `new`, `indeterminate`, `done` — never translated |

The category key is usually what you want on a localised board: it needs no
transcribing and survives a rename. When one matches more than one
transition — a bin often sits in the `done` category beside the real
finished status — ClaudeLoop moves nothing and logs both candidates, rather
than guessing and binning a finished ticket. Configure an id or a name in
that case. The warning for a value that matches nothing lists every offered
transition with the values that would reach it.

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

**The repository's own instructions come first.** Everything ClaudeLoop adds
is one of three things: the handful of rules ClaudeLoop itself breaks without,
facts about the machine the session couldn't otherwise know, and a fallback
for what the repository doesn't say.

Every session carries a system prompt assembled from four layers, in this
order of precedence:

1. **The ClaudeLoop protocol** — invariant, not configurable. It tells the
   session it's unattended, defines how a task ends (writing the result file
   named in `CLAUDELOOP_RESULT`), and carries the one guard ClaudeLoop's own
   bookkeeping can't survive: never stage, commit, stash or revert
   ClaudeLoop's task file if one lives in the repository.
2. **Your working tree** — fact, not policy, so nothing below can override
   it. Names the worktree the session is in and the default branch it was cut
   from, and gives the two publish commands. This matters because the default
   branch is checked out *elsewhere*: `git checkout main` fails from a
   worktree, and `git push origin main` pushes that branch's own ref, reports
   "Everything up-to-date" and ships nothing. The section spells out
   `git push origin HEAD:main` and `git push -u origin HEAD` instead.
3. **Operator instructions** — `instructions_file`, read from the machine
   running ClaudeLoop. It outranks the repository because the operator, not
   the repository, controls this machine. Optional: absent when the file is.
4. **The repository's own instructions**, followed end to end — its
   `CLAUDE.md`, `.claude/CLAUDE.md`, or `AGENTS.md`. They decide how work is
   done here, including when it's finished and **where it lands**: a
   repository whose close-out is a direct push to `main` gets exactly that,
   and one that asks for a pull request gets that. ClaudeLoop's built-in
   definition of done is only a fallback for what that file doesn't say —
   implement the change, run the repository's own tests and checks if it has
   any, commit, publish as the repository directs or open a pull request if
   it doesn't say; or, if there's no remote, or push credentials or a forge
   CLI are missing, stop after committing and say exactly what was missing.
   It doesn't ask the session to create a branch, because ClaudeLoop has
   already made one for it (see below). `definition_of_done_file` overrides
   the built-in; the repository's own file wins over both.

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

## Plugins

ClaudeLoop has no plugin list of its own. The repository being worked on
decides, in its own `.claude/settings.json`, exactly as it would for a human
running `claude` in it:

```json
{
  "extraKnownMarketplaces": {
    "ponytail": {"source": {"source": "github", "repo": "DietrichGebert/ponytail"}}
  },
  "enabledPlugins": {"ponytail@ponytail": true}
}
```

A headless session honours that file — `enabledPlugins` included — and
installs a declared plugin at session start, writing nothing into the
repository. It does that only for a marketplace **this machine already
knows**, and a repository's own `extraKnownMarketplaces` does not make it
known: the registry a session reads is
`~/.claude/plugins/known_marketplaces.json`, which only
`claude plugin marketplace add` fills in.

So ClaudeLoop does that one thing. At startup it reads
`<repo>/.claude/settings.json` and runs, once per marketplace named there:

```
claude plugin marketplace add <source> --scope user
```

User scope, never project or local — those write into the repository being
worked on. The call is idempotent, so a box that already has the marketplace
exits immediately; a repository declaring none runs no subprocess at all. A
marketplace that cannot be added stops startup with a message rather than
running days of sessions without the plugins the repository asked for.

This means a fresh machine needs **no** human running `/plugins`: run the
setup wizard, and the repository's plugins come up on their own — the
registration happens at startup, before the dashboard binds and before the
first session, and every task's worktree gets them.

One condition: **`.claude/settings.json` has to be committed.** Sessions run
in a worktree cut from the default branch, so an uncommitted or ignored
settings file exists for the marketplace registration (which reads your
checkout) but not for the session that would use it — the marketplace lands,
the plugin never does.

Plugins for your own machine rather than for the repository stay yours to
install (`claude plugin install <name> --scope user`); ClaudeLoop never
touches them, and `settings_file` is still passed through as `--settings`.

## Branches and worktrees

Each task runs in its own `git worktree` at
`~/.claudeloop/worktrees/<task-id>`, checked out on a branch ClaudeLoop
creates for it, `claudeloop/<task-id>`, cut from your repository's default
branch. The session commits there, and is told not to rename that branch:
ClaudeLoop finds an interrupted or answered task's earlier commits by looking
the name up. Your own working copy of `repo` is never checked out, reset or
otherwise moved — ClaudeLoop only uses it to create the worktree and to cut
the branch, and the finished commits land in it like any other local branch.

If `repo` has an `origin`, the branch is cut from `origin/<default branch>`
after a fetch, not from your local ref. That matters when the repository's own
instructions tell sessions to land work on the default branch directly: such a
push never moves your local ref, so without the fetch every later task would
branch from the same stale point and silently drop the work in between. A
fetch that fails — no network, no remote, a locked credential agent — falls
back to the local branch rather than failing the task. A worktree that already
exists is reused as it stands: never refetched, never rebased.

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

## Run it as a Home Assistant add-on

A laptop sleeps, reboots into an update, and leaves with its owner. A Home
Assistant OS box does none of those, so `addon/` packages ClaudeLoop as an
add-on: `repository.yaml` at the root makes this repository an add-on
repository, and `addon/config.yaml` describes the add-on itself.
`addon/DOCS.md` is the operator's page — install, first run, what persists.

Three things differ from running it on a workstation:

- **The web UI is reached through ingress**, not a port. `run.sh` sets
  `CLAUDELOOP_INGRESS=1`, which makes both the dashboard and the setup wizard
  bind the ingress port, skip the `Host` check and skip the token — the
  supervisor authenticates a Home Assistant user before anything reaches them,
  and nothing is published to the host. Set it yourself only if something else
  is doing the same job; on a workstation the two checks are the defence.
- **`/data` is `HOME`**, so `~/.claudeloop` and Claude Code's own `~/.claude`
  both live on the add-on's persistent volume.
- **The image is prebuilt** and pulled by tag, because the supervisor builds an
  add-on with the add-on's folder as the docker context and ClaudeLoop's source
  is the repository root. `.github/workflows/addon.yml` builds it from the root;
  by hand it is `docker build -f addon/Dockerfile .`.

A new version is released by bumping `version:` in `addon/config.yaml` and
pushing `main` — nothing else. The workflow runs on every push to `main` but
publishes only when that version is not already in the registry, so a push
that changes code without bumping it ships nothing, and the supervisor offers
an update exactly when the version moves.

## Where things go

```
~/.claudeloop/                  # 0700; state.db is 0600
  config.toml
  state.db                      # what happened: status, summary, cost, timings
  runs/<task-id>/
    events.jsonl                # the raw stream-json stream, appended per attempt
    events.jsonl.1              # the previous generation, once the live one hits 64 MiB
    result.json                 # the session's own verdict
    stderr.log
  worktrees/<task-id>/          # the task's checkout -- see Branches and worktrees
    ...broken-<timestamp>/      # only if a leftover had to be moved aside; see below
```

Event logs rotate at 64 MiB keeping one previous generation, so a run
directory is bounded at roughly 128 MiB per stream however long the task ran.
Nothing deletes `events.jsonl.1`.

If ClaudeLoop finds a directory at `worktrees/<task-id>` that is not a
registered git worktree — it was killed part-way through creating one, or the
box rebooted — it moves that directory to `<task-id>.broken-<timestamp>` and
starts a fresh worktree, reattaching to the task's existing branch if there is
one. Nothing deletes the moved directory: whatever the interrupted attempt had
written is still in it.

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
