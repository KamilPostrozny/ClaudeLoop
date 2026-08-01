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
The per-task instruction points at the target repository's own `CLAUDE.md`, so
different repositories get different behaviour for free. Resist every
temptation to add workflow logic here.

## Architecture

| Module | Responsibility |
|---|---|
| `loop.py` | The decision state machine (`decide`), per-task orchestration, the polling loop, `main()` |
| `session.py` | Spawns one `claude -p` invocation, streams and tees its output |
| `prompt.py` | Composes the session's system prompt from three layers. Pure |
| `source.py` | `Task`, the `TaskSource` protocol, `FileSource` over a markdown checklist |
| `state.py` | SQLite record of what happened. Not the source of truth for what is pending |
| `config.py` | Loads and validates `config.toml` |
| `status.py` | The one value crossing the loop/web-thread boundary |
| `render.py` | Raw `stream-json` events to compact display entries. Pure |
| `web.py` | The dashboard's HTTP surface, routes, SSE pump |
| `static/index.html` | The entire frontend, one file |

The pure modules — `prompt.py`, `render.py`, and `decide`/`blocking_reset` in
`loop.py` — are pure on purpose: they hold the logic that would otherwise be
untestable behind a subprocess or a socket. Keep new logic in that shape where
you can.

## Hard constraints

These were enforced through every review and are not negotiable without a
deliberate decision recorded in a spec.

- **Python 3.11+, standard library only.** No third-party packages, ever —
  not for the orchestrator, not for the tests, not for the frontend.
  `pip install` and `npm install` must both remain unnecessary. This is what
  makes the S4 addon image a base image plus a copy.
- **No build step.** The dashboard is one HTML file with inline CSS and an
  inline module script, making no off-origin requests.
- **The dashboard is read-only.** No route mutates state, the task file, or the
  database. S5 will break this deliberately, for setup only.
- **The web layer never touches the loop's objects.** Its SQLite connection is
  its own and opened read-only; event logs are read from disk.
- **`CLAUDELOOP_RESULT` is merged last** into the session's environment. The
  loop decides a task is finished by that file appearing; a `session_env` entry
  must never be able to redirect it.
- **No trace of ClaudeLoop lives in a repository it works in.** The result
  file, event log and database all live under `~/.claudeloop/`, and
  `load_config` refuses a `tasks_file` that resolves inside `repo`. A session
  doing ordinary branch hygiene would otherwise revert ClaudeLoop's own mark and
  make finished work look pending.
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

The [superpowers](https://github.com/obra/superpowers) workflow, one slice at a
time:

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

Four slices, four real defects that the passing suite could not have caught:

- `blocking_reset` treated the live `allowed_warning` status as a quota block,
  parking the loop for hours. The fixtures had only ever shown `allowed`.
- The quota meter was keyed on `rateLimitType` names that do not exist.
- The built-in definition of done never said which status to write when a
  session stopped short of a pull request, so completed work landed as `- [!]`.
- The resume nudge said `"Continue."` to a session that believed it had
  finished, which correctly answered that there was nothing to continue.

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
python -m unittest discover -s tests -t .        # whole suite, ~30s
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
