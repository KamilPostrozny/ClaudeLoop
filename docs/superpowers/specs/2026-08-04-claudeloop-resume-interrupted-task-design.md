# S9 — Resume an interrupted task

Design capture, 2026-08-04. Records what was decided at the time.

## The problem

Restarting ClaudeLoop while a task is running throws that task's session away
and starts it over from the original task text.

The trigger was mundane: an operator found `[jira] transition_done` set to
`"In Progress"` against a Jira whose built-in statuses display in Polish, so
`_transition` never matched and tickets never moved. Config is read once, in
`main()` (`loop.py:649`), and `build_source` runs once before the poll loop
(`loop.py:529`), so fixing it meant restarting — and restarting mid-task was
not a thing an operator could do without paying for the running task twice.

The specific Jira defect is not this slice. The restart being expensive is.

## What already exists

Most of the recovery is built, and stops one step short.

- `State.__init__` flips any `running` row for this repository to
  `interrupted` at startup (`state.py:83`), on the correct reasoning that
  nothing but a dead process leaves that status behind.
- `terminal_ids()` excludes `interrupted` on purpose (`state.py:131`), so the
  task source offers the task again.
- `worktree.ensure` reuses `worktrees/<task-id>` when it holds a `.git`, so
  the dead session's commits *and* its uncommitted edits are still on
  `claudeloop/<task-id>` when the task comes back round.
- `state.last_session(task_id)` (`state.py:153`) returns the session id to
  resume, and S2b's answered-task path already resumes through it.

What is missing is the join: `run_task` reaches `resumed = state.last_session(...)`
only `if resume_with is not None`, so an interrupted task takes the
fresh-start branch — new `uuid4()` session id, `prompt = task.text`,
`resume = False` — and `source.start` fires a second time.

The result is a session that starts blind on top of its own half-finished
work, with no idea the branch it is standing on already has commits.

## The decision

An interrupted task resumes its session, the same way an answered one does.

Detection is `state.task(task.id)["status"] == "interrupted"`, read **before**
`start_task`, which is `INSERT OR REPLACE` and resets the row to `running`.

### Scope: `interrupted` only, not `error`

`error` is the status `main_loop`'s crash handler records, and it is also
non-terminal, so an `error` task is re-picked too. It is deliberately left
starting fresh. The causes are environment faults — an `index.lock`, a full
disk, a worktree that cannot be created — and several of them happen *before*
any session exists. Resuming a session id that may never have run buys
nothing, and `--resume` against an unresolvable id fails silently, which is
the exact failure mode the roadmap already records for tasks parked across the
S6 upgrade: no result file, no rate limit, every resume burned on nudges.
`interrupted` is written at one place for one reason and carries no such
ambiguity.

### `source.start` does not re-fire

Already true for an answered resume, for the reason its comment gives: it
would re-fire `transition_start` against an issue already in that status. An
interrupted task fired `start` on its first attempt, so the same holds. The
skip condition widens from "resuming with an answer" to "resuming at all".

### The result file is still unlinked

`run_task` deletes `runs/<id>/result.json` on entry. Left in place for an
interrupted task it could be a genuine verdict the dead process never got to
read — but it is not read until *after* the next `session.run` returns
anyway, so preserving it saves nothing and risks a stale verdict from an
earlier attempt ending the task. It stays deleted, and the resumed session is
told in as many words to write the result file if the work turns out to be
already complete. This is the `NUDGE_PROMPT` lesson applied ahead of time.

### Two prompts, because the session may be gone

`last_session` returns `None` for a database from before S2b or a task whose
runs were pruned. That case mirrors `FRESH_ANSWER_PROMPT` exactly and gets the
same treatment: start over, but say that the branch already carries an earlier
attempt's commits so the session looks before it redoes work. Without it a
fresh start on a reused worktree is silently wrong in the same way the bug
being fixed is.

- `INTERRUPTED_PROMPT` — sent with `--resume`. Says the process was
  restarted, that nothing else touched the tree, to check `git status` and
  `git log` before doing anything, not to start over, and that the result
  file ends the task.
- `FRESH_INTERRUPTED_PROMPT` — the task text plus the same warning about the
  branch. Promises only that the branch exists and may carry commits, which
  stays true whether or not it does.

### `opening_prompt` becomes a pure function

The selection was a flat `if/elif/else` inside `run_task`; four cases make it
nested. It moves out to `opening_prompt(task_text, resume_with, resumed,
interrupted) -> (prompt, resume)`, joining `decide` and `blocking_reset` as
logic that is testable without a subprocess. The prompt strings are the
product, and S7's live failure was a prompt sentence that lost its verb under
a combination seven passing tests never assembled — a pure function is how
each combination gets pinned whole.

## What this does not do

- Config is still read once at startup. Restarting is now cheap; hot-reloading
  is not built, and with a cheap restart it is not obviously worth building.
- Nothing here helps a task interrupted *before* its first `session.run`
  wrote anything; it simply starts over, correctly.
- The cost recorded for a resumed task covers this attempt only. The dead
  attempt's runs keep their own rows, so the money is not lost from the
  database, but `tasks.cost` under-reports. Left alone.
