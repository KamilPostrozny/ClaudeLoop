# CLAUDE.md

Guidance for Claude Code sessions working in this repository.

## Start here

**Read [`ROADMAP.md`](ROADMAP.md) first.** It is the resume point: what is
built, what is next, what has already been decided about unbuilt slices, and
what is still open. Work is organised into numbered slices, each with its own
spec, plan, and merge. Picking up where a previous session left off means
reading the roadmap and starting the next slice.

`README.md` is the operator-facing manual — configuration, running, the
dashboard. This file is for whoever is *changing* the code.

## What this is

An unattended orchestrator for Claude Code. It takes tasks one at a time from a
source, runs one headless `claude -p` session per task against a target
repository, and moves on when that session writes a result file. It survives
subscription rate limits by sleeping until the quota resets and resuming the
same session, and it is designed to run for days with nobody watching.

**The repository being worked on defines the work, not the orchestrator.**
ClaudeLoop has no workflow engine, no phase machine, no checklist of its own.
The per-task instruction points at the target repository's own `CLAUDE.md`
when it has one, and supplies a definition of done only when it does not. So
different repositories get different behaviour for free. Resist every
temptation to add workflow logic here.

## Architecture

| Module | Responsibility |
|---|---|
| `loop.py` | The decision state machine (`decide`), per-task orchestration, the polling loop, `main()` |
| `session.py` | Spawns one `claude -p` invocation, streams and tees its output |
| `worktree.py` | One git worktree per task: create, reuse, release, and the startup probe |
| `prompt.py` | Composes the session's system prompt from three layers. Pure |
| `source.py` | `Task`, the `TaskSource` protocol, `FileSource` over a markdown checklist |
| `state.py` | SQLite record of what happened. Not the source of truth for what is pending |
| `config.py` | Loads and validates `config.toml` |
| `setup.py` | Setup mode: the schema-rendered wizard and the TOML writer. Runs only when the loop does not |
| `status.py` | The one value crossing the loop/web-thread boundary |
| `render.py` | Raw `stream-json` events to compact display entries. Pure |
| `web.py` | The dashboard's HTTP surface, routes, SSE pump |
| `static/index.html` | The entire frontend, one file |

The pure modules — `prompt.py`, `render.py`, and `decide`/`blocking_reset` in
`loop.py` — are pure on purpose: they hold the logic that would otherwise be
untestable behind a subprocess or a socket. Keep new logic in that shape where
you can.

`config.py`'s `SCHEMA` tuple's declaration order is load-bearing: a field's
`required_if` and `check` are only ever called with the coerced values of
fields declared earlier in the tuple, not the whole submission. `tasks_file`'s
check reads `repo`, `web_token`'s condition reads `web_host`, `jira.project`'s
reads `jira.jql` — moving a field earlier than something it depends on breaks
silently, since Python doesn't enforce it.

## Hard constraints

These were enforced through every review and are not negotiable without a
deliberate decision recorded in a spec.

- **Python 3.11+, standard library only.** No third-party packages, ever —
  not for the orchestrator, not for the tests, not for the frontend.
  `pip install` and `npm install` must both remain unnecessary. This is what
  makes the S4 addon image a base image plus a copy.
- **No build step.** The dashboard is one HTML file with inline CSS and an
  inline module script, making no off-origin requests.
- **The dashboard is read-only, with one exception.** No route mutates the
  loop's state, the task file, or the database. S2b broke the rule
  deliberately and narrowly: `POST /api/tasks/<id>/answer` writes one file,
  `runs/<id>/answer.json`, which the loop reads and consumes. Any further
  write needs the same justification. S5's setup wizard is not that further
  exception — it isn't the dashboard at all. Setup mode is a **separate
  server**, running only while the loop is not: `main()` calls
  `setup.run_setup()` and blocks before the dashboard or the loop ever starts,
  so the two never run in the same process at once. It binds `127.0.0.1`
  unconditionally, ignoring whatever `web_host` says, and requires a
  one-time token printed to the console on every request — with no config
  yet there is no `web_token` to authenticate against, so that barrier can't
  be the only one. It writes exactly one file, `config.toml`, and only once
  a full `validate()` pass has approved it.
- **The web layer never touches the loop's objects.** Its SQLite connection is
  its own and opened read-only; event logs are read from disk.
- **The web layer is never a second writer to `status.py`.** `set_status` is a
  read-modify-write and is safe only because exactly one thread calls it. S2b
  was the case that warning was written for, and it writes a file the loop
  picks up rather than calling `set_status` — so the hazard is dodged, not
  solved. A future route that needs to write from the web thread must add a
  lock first.
- **Any route that returns early without draining the request body must
  close its connection.** `do_POST` sets `self.close_connection = True`
  before anything else. Its rejection paths answer without draining the
  request body, and on an HTTP/1.1 keep-alive connection those
  attacker-controlled bytes are otherwise parsed as the next request — which
  made the answer route's content-type guard, the CSRF defence at the
  loopback default, bypassable by request smuggling. `do_GET` returns early
  on its 403 and 404 paths without closing, and genuinely does not need the
  fix: smuggling requires an unread request body, and no browser-issuable GET
  carries one.
- **`CLAUDELOOP_RESULT` is merged last** into the session's environment. The
  loop decides a task is finished by that file appearing; a `session_env` entry
  must never be able to redirect it.
- **Nothing ClaudeLoop writes into a repository may be committable.** The
  result file, event log and database all live under `~/.claudeloop/`, and
  `load_config` refuses a `tasks_file` that resolves inside `repo`. A session
  doing ordinary branch hygiene — `git add -A`, `git checkout -- .`, `git
  stash` — would otherwise revert ClaudeLoop's own mark and make finished work
  look pending. S6's deliberate exception is what `git worktree add` leaves in
  the target repository: `.git/worktrees/<task-id>/`, a `.git` file in the
  worktree, and the `claudeloop/<task-id>` branch ref. None of the three is
  reachable from any working tree's staging area, so none can revert the mark
  — that is the whole test. The first two are usually transient:
  `worktree.release` runs `git worktree remove` on every non-`blocked` result
  `run_task` returns, which takes the administrative entry and the tree
  holding that `.git` file in one go, and declines only when the tree is
  dirty, since it is never forced. A crash out of `run_task` never reaches
  that call at all, so an `error` outcome leaves both behind. The
  `git worktree prune` in `worktree.probe` is a startup backstop for entries
  orphaned from outside git — a wiped `~/.claudeloop` — not the routine
  cleanup. **The branch alone is permanent.**
  Nothing in `worktree.py` or `loop.py` deletes it, on any outcome, so a task
  that finishes cleanly leaves one behind exactly as a parked one does.
  Branches in the target repository are not new — a pre-S6 session that
  complied with "branch before your first commit" left its own, and nothing
  deleted that either — but S6 makes it every task, under a name ClaudeLoop
  chose. Any further exception needs the same justification, recorded in a
  spec.
- **Every stdout line is written to `events.jsonl` verbatim before it is
  parsed.** A parser bug must never lose the record.
- **Strictly serial.** One task at a time, one session at a time.

`session.py`'s subprocess mechanics — the 16 MiB line limit, concurrent
draining of both pipes, `start_new_session=True`, the bounded kill-and-reap,
the invocation timeout, `stdin=DEVNULL` — are each a fix for a real observed
failure. Do not simplify them.

## The prompt strings are the product

`PROTOCOL`, `precedence()` and `BUILTIN_DEFINITION_OF_DONE` in `prompt.py` are
not documentation. They are instructions a capable but **literal-minded** agent
executes unsupervised for hours with bypassed permissions. Ambiguity in them is
a defect exactly the way a bug in `decide()` is. Every live failure this project
has had traced back to a sentence that could be read two ways.

Change them like code: with a covering test that pins the specific new wording,
and a live run afterwards.

## How work is done here

The `superpowers` workflow — the plugin ships in the
`anthropics/claude-plugins-official` marketplace — one slice at a time:

1. **`superpowers:brainstorming`** — settle the design through questions, then
   write a spec to `docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md`.
2. **`superpowers:writing-plans`** — a TDD plan to
   `docs/superpowers/plans/YYYY-MM-DD-<topic>.md`, with real code in every step.
3. **`superpowers:subagent-driven-development`** — a fresh subagent per task, a
   review after each, then one whole-branch review at the end.
4. **The live smoke test** — see below. Not optional.
5. **`superpowers:finishing-a-development-branch`** — merge.
6. **Update `ROADMAP.md`.**

Specs record what was decided *at the time* and are not rewritten as things
change; `ROADMAP.md` records what is true *now*. When a later slice reverses an
earlier decision, note the reversal in the old spec and make the roadmap
correct.

## Always run the live smoke test before merging

Six slices have run one. Four of them surfaced defects the passing suite could
not have caught — eight between them:

- `blocking_reset` treated the live `allowed_warning` status as a quota block,
  parking the loop until the window reset. The fixtures had only ever shown
  `allowed`, because that was all the design capture contained.
- The quota meter was keyed on `rateLimitType` names that do not exist
  (`weekly`, `opus_weekly`); the real value is `seven_day`, and the payload
  carries `utilization` outright.
- The built-in definition of done never said which status to write when a
  session stopped short of a pull request, so completed work landed as `- [!]`.
- The resume nudge said `"Continue."` to a session that believed it had
  finished, which correctly answered that there was nothing to continue.
- Each task branched off the previous task's branch rather than the default.
- The Jira source's `state.db` re-run backstop had never once worked: `State`'s
  SQLite connection is created on the loop's thread, and `pending()` now runs
  through `asyncio.to_thread`, which `sqlite3` refuses. It failed as a warning
  rather than a crash, so only a live run showed it.
- Jira's JQL search index is eventually consistent. A ticket labelled
  `claudeloop-done` still matched the query 0.8 seconds later, so the label
  alone cannot prevent a re-run — with the backstop dead, the loop ran a
  finished ticket a second time and paid for it twice.
- S2b's design said a resumed task must skip `reset_to_default_branch`,
  reasoning that a resume is the same task continuing. Wrong, on both task
  sources: a session usually parks *before* its first commit, so it has no
  branch to preserve, and skipping the reset meant it inherited whatever
  branch the **next** task had left checked out and committed onto it. Eleven
  scoped reviews and 421 passing tests had gone by without noticing, because
  nothing but a real session picks its own branch names.

The two that found nothing wrong were still worth running. S1's is what
confirmed `resetsAt` is in seconds, that `--session-id` is honoured, and that
`--resume` reattaches with the appended system prompt intact — assumptions the
whole recovery path rests on. S6's confirmed that `--resume` still reattaches
when the session's working directory is a git worktree rather than the
repository, which nothing in a fixture suite can tell you, and that no session
tried to check out the default branch — which under a worktree fails outright
with `already checked out at`.

Prompt text, live payload shapes, and what a session does when it thinks it is
done are all invisible to a suite built on fixtures and a fake CLI — and
fixtures written from a design document inherit that document's wrong
assumptions.

Run it with a scratch repository, `model = "haiku"`, and **two tasks, not one**
— several of those defects only appeared on the second task, where state left
by the first one matters. The whole run costs about ten cents. When a fix
changes prompt text, re-run afterwards: text fixes are exactly the kind that
come back differently broken.

## Testing

```bash
python -m unittest discover -s tests -t .        # whole suite, ~75s
python -m unittest tests.test_loop -v            # one module
```

Tests use stdlib `unittest`, real files on disk, and a fake `claude` shell
script rather than mocks. A test that spawns `git` in a scratch repository must
set `commit.gpgsign false` locally on it — see the working notes in
`ROADMAP.md` for why.

## Definition of done in this repository

ClaudeLoop may eventually be pointed at itself, so this section is load-bearing
rather than decorative.

Work is done when: the change is implemented; `python -m unittest discover -s
tests -t .` passes in full; anything non-trivial has a covering test that fails
without the change; `ROADMAP.md` is updated if a slice's state changed; and the
work is committed on a branch created from `main`.

For a whole slice, done additionally means the live smoke test has been run and
its findings fixed.

Do not push or open pull requests without being asked — this repository has no
remote configured yet.
