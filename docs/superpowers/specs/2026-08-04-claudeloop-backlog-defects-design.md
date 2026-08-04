# S11 — Backlog defects

Every slice through S10 is merged, and `ROADMAP.md`'s "Next" section says
nothing is scheduled. What is left is the open-issues list: 28 items carried
across slices, deliberately deferred and tracked so they were not lost.

This slice takes the ones that are **genuine defects** — a wrong number, an
unbounded growth, a permanent block, a leak — and leaves the ones that are
deliberate decisions with recorded reasons. Reversing one of those needs its
own spec, which is exactly what this one is not.

## Not in scope, and why

These stay in `ROADMAP.md`'s open issues, unchanged:

| Issue | Why it stays |
|---|---|
| Hot-reload of `config.toml` | The roadmap already argues it is not obviously worth building on top of S9's cheap restart, and it needs a per-key allowlist regardless. |
| Removing a marketplace | Removing something an operator may also use by hand is a worse failure than leaving it. |
| Version pinning for plugins | Since S8 the repository owns the choice, so it is the repository's problem. |
| The add-on's `setup` flag | The supervisor has no way to offer a one-shot action. |
| A repository under `/share` needing a `chown` | An environment fact, documented in `DOCS.md`. |
| A session surviving SIGKILL of its parent | `start_new_session=True` is what makes it survive, and that is a fix for a real failure. |
| The token in the dashboard's query string | `EventSource` cannot set headers; the complete fix is a cookie pair, worth doing when the token path is actually used. |
| No test executes the frontend's JavaScript | The stdlib has no DOM, and no third-party package may be added. |
| A task parked across the S6 upgrade | One-time, already past. |
| `ANSWER_PROMPT`'s unconditional claim about uncommitted changes | Qualifying it costs every honest resume clarity to cover an operator action. |
| `Config` being unhashable | Nothing hashes it. |
| Moving a repository orphaning its history | The scope is the configured path verbatim; a rename is a new repository as far as this is concerned. |

**Branch and transcript accumulation is also not fixed, and that is the one
worth stating outright.** The roadmap lists three kinds of leftover. The
worktree half is fixed below. The other two are not, because the fix would
destroy the deliverable: `claudeloop/<task-id>` *is* the task's work, the only
place its commits live, and a loop that deleted finished tasks' branches would
be throwing away everything it had been paid to produce. Claude Code's
transcript directories belong to Claude Code, under `~/.claude/projects/`,
and deleting another tool's state on its behalf is not ClaudeLoop's call. Both
remain open issues with their reasoning now recorded here rather than only as
a complaint.

## What is fixed

### 1. A parked task reports only the cost of its resume

`run_task` starts `cost` at zero on every call, and `State.finish_task` writes
`cost_usd=?` rather than adding to it, so a task that parks and is later
answered has the money spent before the question overwritten by the money
spent after it. Measured in S6's live smoke test: $0.0395 + $0.0162 recorded
as $0.0162, on the dashboard and in the source's closing comment both.

`State.prior_cost(task_id)` reads what the row already carries, and `run_task`
seeds its accumulator with it when resuming. Read **before** `start_task`,
which is `INSERT OR REPLACE` and puts `cost_usd` back to NULL — the same
ordering constraint `was_interrupted` already documents. Only when resuming: a
task starting fresh must not inherit anything.

The alternative — making `finish_task` add rather than assign — does not work,
because `start_task` erases the column first.

### 2. `tasks.id` is the primary key on its own

`id` is a hash of the task text, so two repositories whose file sources hold
identical text produce the same id, and `start_task`'s `INSERT OR REPLACE`
silently overwrote the other repository's row.

`tasks` is rebuilt on `PRIMARY KEY (id, repo)`. SQLite cannot alter a key in
place, so `_rekey()` does the copy-drop-rename once per database and is a
no-op on every subsequent start.

`repo` stays **nullable**. Rows written before the column existed carry NULL,
SQLite does not enforce uniqueness across a NULL key part, and the documented
behaviour is that those rows belong to no repository — converting them to `''`
would have changed that.

`runs` gains `repo` too, and `last_session` scopes on it: resuming a session
id belonging to another repository's worktree is exactly the hazard
`was_interrupted`'s docstring already describes. The column is **backfilled
from `tasks` before the rebuild**, while `id` is still unique there, or a task
parked across this upgrade would lose the session it resumes.

`runs` also loses its `REFERENCES tasks(id)` clause, which named a key that no
longer exists on its own. Foreign keys were never enforced (the pragma is off
and nothing turns it on), so this removes a declaration that had stopped being
true rather than a constraint.

The dashboard's `api_task` and `_is_blocked` are scoped the same way — they
read by `id` alone, which under a composite key is ambiguous.

### 3. `~/.claudeloop`, `state.db` and `runs/` at the default umask

`state.db` holds task text, session summaries and the questions a session
parked on; `runs/`'s names alone say which tasks this box has worked on. The
config file's own guard already refuses to *load* a world-readable
`config.toml` one step earlier.

`config.narrow(path, mode)` takes permissions away and never grants them,
never raises, and is applied unconditionally rather than on creation —
`mkdir(exist_ok=True)` leaves an existing directory's mode alone, and the
common case is a home directory made by a version without this. Never raising
matters on the add-on, where `/data` may not be this process's to chmod.

### 4. `events.jsonl` grows without bound

A session under `bypassPermissions` streams every tool result through it, and
every resume of a task reopens the same file. `session._Log` rotates at
`MAX_LOG_BYTES` (64 MiB) keeping one generation, so a run directory is bounded
at twice that rather than by how long the task ran.

Size is tracked as bytes go by rather than `stat()`ed per line, and **includes
what was already in the file at open** — otherwise a task that nudges twenty
times grows it twenty caps deep. Rotation happens before a write, never
mid-line: the constraint that every stdout line reaches disk verbatim before
it is parsed is untouched, only which file the older ones are in changes.

`web.Handler._drain` gains the matching guard: `size < offset` means the file
rotated under the SSE pump, so it restarts from zero rather than resuming into
an offset that no longer means anything.

### 5. `JiraSource.pending` never paginates

One page of 50, so an ordering that put wanted work past the 50th row never
reached the loop — it simply never appeared, with nothing saying why.

`/search/jql` pages on an opaque `nextPageToken`, not `startAt`. The captured
`tests/fixtures/jira/search.json` — a real response — carries both
`nextPageToken` and `isLast: false`, which is what pins the shape. `search()`
sends the token back only when the previous response offered one, so an
instance that answers without one sees exactly the request it always did.

Bounded at `MAX_PAGES` (10, so 500 issues): a token that never stops coming
must not spin an unattended loop through Jira forever. A page that fails keeps
the pages already read — the loop can start on what arrived.

### 6. `JiraSource.answer` polls unboundedly, forever

One `GET /comment` per parked task per `POLL_S`, and a parked task never
expires: roughly 2,900 Jira requests per parked ticket per day, unpaginated,
for a question nobody may ever answer.

Two halves:

- **Payload.** The read is bounded to `ANSWER_COMMENTS` (50) and asks for
  `orderBy=-created`, so the newest comments are the ones that fit — and the
  boundary this looks for, ClaudeLoop's own newest question, is by
  construction near the end.
- **Frequency.** `AnswerSchedule` backs the *source's* channel off from
  `POLL_S`, doubling to `ANSWER_POLL_MAX_S` (600s). A ticket parked for a day
  costs ~150 requests instead of ~2,900. The **dashboard's channel is not on
  this schedule**: `answer.json` is a local file read, it costs nothing, and a
  human who has just typed an answer must not wait out a backoff meant for the
  network.

`_chronological()` turns the page back around, because the boundary rule needs
chronological order. It sorts on the comment **id** rather than reversing on
trust: Jira allocates those from one increasing sequence, so it is right
whatever the instance did with `orderBy`, including ignoring it.

### 7. A leftover worktree directory blocks the head of the queue forever

A non-empty `worktrees/<task-id>` with no `.git` in it — killed mid-`add`, a
reboot, an operator deleting `.git` while tidying — fails `worktree add` with
"already exists" every time, and the branch retry fails identically. `error`
is deliberately non-terminal, so the task was re-offered every `POLL_S`
forever and no later task ever ran.

`_move_aside` renames it to `<path>.broken-<timestamp>` and lets `add` proceed.
**Renamed, not removed**: whatever the dead attempt had written is the
operator's. An empty directory is left alone, since `worktree add` accepts one.

### 8. An `error` outcome always leaks its worktree

A crash out of `run_task` never reaches its own `release` call, so this was the
one leftover that accumulated on every occurrence rather than only on a dirty
tree. `main_loop`'s crash handler now releases it — still never forced, so a
tree holding uncommitted work survives exactly as on any other outcome.

### 9. ~80 `ResourceWarning`s about unclosed SQLite connections

Every one of those call sites is a test, so the fix belongs on `State` rather
than on eighty edits: `State.close()`, plus a `__del__` that calls it. `State`
owns its connection outright, and closing it is what that ownership means.
`main_loop` closes explicitly.

One real leak surfaced alongside: `JiraClient._request` read an `HTTPError`
without closing it, and `HTTPError` is itself a file object over the response
socket. An unattended loop polling every 30s makes that a slow fd leak.

### 10. The composed prompt can exceed a single argv element

`--append-system-prompt` carries it as one argument, and Linux caps that at
128 KiB. Past it, `execve` fails on every task with an errno the CLI reports
as something unrelated.

`prompt.oversized()` is checked at startup, beside the worktree probe and the
marketplace registration, so it is named once before anything is listening.
Measured in **bytes**, not characters: a prompt of multi-byte characters that
fits as a `str` would otherwise pass and still fail to start.

### 11. The dashboard's answer box loses a draft on any unrelated change

`renderCompleted` keys on every task's `id:status`, so any other task
finishing rebuilds the list and wiped whatever was half-typed. On an
unattended loop that is not rare — the point of the dashboard is that
something else is running while you read it.

Drafts are kept by task id across rebuilds, collected before
`replaceChildren` destroys the textareas, dropped once sent, and dropped for
tasks no longer listed.

### 12. `write_config`'s `fchmod` had no test that fails without it

The existing test reads the mode after the write finishes, which lands on
`0600` whether the narrowing happened before the first byte or after the last
— and the after version leaves a Jira API token world-readable for the length
of the write, which is the whole point of `fchmod` on the open fd.

Catching it needs the mode observed mid-write, which is the one place this
suite's real-files-not-mocks convention cannot reach. The mock is confined to
`os.fdopen` and only observes. Verified: the new test fails against a
chmod-afterwards implementation while the old one passes.

### 13. The dashboard's pending list is stale under the file source

Published on the status snapshot since S3, so an edit to `tasks.md` made
mid-task did not show until the next one began — a step back from the
per-request re-read this had before.

`pending_now()` re-reads the checklist per request, which is a local file
read. **The Jira source stays on the snapshot**: re-reading there is an HTTP
round trip on the web thread on every poll of every open dashboard, which is
what moved this onto the snapshot in the first place.

## Constraints this does not touch

Standard library only; no build step; the dashboard stays read-only but for
the answer route; the web layer keeps its own read-only connection and never
writes `status.py`; `CLAUDELOOP_RESULT` is still merged last; nothing new is
written into the target repository; every stdout line still reaches disk
verbatim before it is parsed; strictly serial.

`_Log` is the one worth naming against that last constraint: it changes which
file an older line lives in, never whether it was written.
