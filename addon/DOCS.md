# ClaudeLoop

An unattended orchestrator for Claude Code. It takes tasks one at a time from a
source, runs one headless `claude -p` session per task against a target
repository, and moves on when that session writes a result file. It survives
subscription rate limits by sleeping until the quota resets and resuming the
same session, and it is built to run for days with nobody watching — which is
what a Home Assistant box does anyway.

## Before you start

You need a Claude credential the add-on can use without a browser. On a machine
that has one:

```
claude setup-token
```

Paste the result into **Claude code oauth token** in the Configuration tab. It
is the only option that must be set; without it every session fails to
authenticate, and the add-on log says so on every start.

## First run

1. Start the add-on and open **Open Web UI**.
2. The setup wizard appears, because there is no configuration yet. It walks
   through the repository, the task source, the dashboard, your own
   instructions, and the plugin marketplaces.
3. Save. The add-on keeps running and starts on the first task.

Everything the wizard writes goes to `/data/.claudeloop/config.toml` inside the
add-on.

To change it later, turn on **setup** in the Configuration tab and restart the
add-on: it opens the wizard over your existing configuration instead of picking
up a task, and starts the loop as soon as you save. **Turn setup back off
afterwards**, or the next restart opens the wizard again and waits there.

The **repository** is normally a URL — `https://…`, `ssh://…`, or
`git@host:owner/repo.git` — which is cloned once into `/data/.claudeloop/clones/`
and worked in from there. A path also works if you keep a checkout under
`/share`, which is mapped into the add-on — but sessions run unprivileged and
`/share` belongs to root, so you have to make that checkout writable by uid
1000 yourself. The clone is the path with nothing to arrange.

The **tasks file** goes at `/config/tasks.md`, and the add-on creates an empty
one on first start. That folder is the add-on's own: the File editor and Samba
add-ons show it as `addon_configs/local_claudeloop/` (or `xxxxxxxx_claudeloop/`
when installed from this repository), which is how you add tasks without a
shell. Do not put the checklist on `/share`: ClaudeLoop marks each task off as
it finishes, a task it cannot mark is offered and paid for again on every poll,
and the setup wizard will refuse the path rather than let that happen.

## What persists, and what does not

`/data` is the add-on's own volume. Configuration, the run database, event
logs, per-task worktrees, clones, and Claude Code's own credentials all live
there and survive restarts and updates. **Uninstalling the add-on deletes all
of it**, including the branches ClaudeLoop cut in a cloned repository that were
never pushed.

## Authentication and access

The dashboard and the setup wizard are reached through Home Assistant ingress
only. No port is published: the add-on's listener exists on the supervisor's
internal network, and the supervisor authenticates a Home Assistant user before
proxying anything to it. Any user who can log in to Home Assistant can therefore
use the dashboard, answer a session's question, and — before the first save —
run the setup wizard.

An agent running with bypassed permissions and real repository credentials is
what this dashboard watches. Treat access to Home Assistant accordingly.

## Things worth knowing

- **Commits are unsigned.** The add-on sets `commit.gpgsign false` globally,
  because a headless box cannot unlock a signing key. If your repository needs
  signed commits, supply a key through `[session_env]` in `config.toml`; a
  `[session_env]` entry wins over what the add-on sets.
- **Browser-based verification does not work here.** A repository whose
  `CLAUDE.md` mandates a Claude-in-Chrome sweep cannot get one: the extension
  needs a real Chrome. Put an instruction in your operator instructions telling
  sessions to use Playwright, or to skip that phase.
- **Secrets your repository's own checks need** — `GH_TOKEN`, an npm token,
  whatever your test suite reads — go in `[session_env]` in the wizard's last
  screen. They reach the session's environment and nothing else.
- **Nothing prunes what a task leaves behind.** Worktrees for parked or failed
  tasks, one branch per task, and one Claude Code transcript directory per task
  accumulate in `/data`.
