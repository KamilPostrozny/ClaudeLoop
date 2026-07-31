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
repo               = "/home/you/Projects/yourrepo"
tasks_file         = "/home/you/Projects/yourrepo/.claudeloop-tasks.md"
model              = "opus"   # optional, default "opus"
max_resumes        = 20       # optional, default 20 -- bounds plain nudges
max_waits          = 200      # optional, default 200 -- bounds quota waits, separately
session_timeout_s  = 14400    # optional, default 14400 (4h) -- kills a wedged session
```

One instance serves one repository. For a second repository, run a second
instance with its own config.

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
