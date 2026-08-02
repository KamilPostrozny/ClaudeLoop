# S2b — Question and answer channel

**Status:** design agreed, not built.
**Date:** 2026-08-01.

A session that hits something only a human can decide has, today, exactly one
move: write `status: "blocked"` with a `question`, and stop. The question then
sits in `state.db` and on the dashboard where nobody can act on it. This slice
closes the circuit — a human answers, and the same session picks up where it
left off.

The parts already in place for it, put there deliberately by earlier slices:
`blocked` is a valid result status, `question` is a field in the result schema
and a column on `tasks`, and `runs.session_id` records the session that asked.

## What is being built

1. A blocked task **parks** instead of ending the loop's work. The orchestrator
   marks it in the task source, records it, and starts the next pending task.
2. A human answers through **either** channel — a box on the dashboard, or a
   comment on the Jira issue.
3. The loop notices the answer at the top of its next iteration, **reopens** the
   task in its source, and **resumes the original session** with the answer as
   its prompt.

## Decisions

### Park and continue, not stall

A blocked task must not freeze the backlog. The loop marks it, moves on, and
comes back when an answer exists. The alternative — idling on the blocked task
until answered — is a smaller state machine, but a question asked at 2am wastes
the entire night, and unattended multi-day operation is the whole point of the
project.

The loop stays **strictly serial**. Parking does not mean two sessions running;
it means the parked one is not running at all.

### Resume the original session, do not re-run the task

The answered task resumes by `--resume` against the `session_id` of its last
run, with the human's answer as the prompt. The session already holds hours of
context about the repository, the branch it created, and what it had done — a
fresh session would rediscover all of it and pay for the privilege. This is
also what the recovery path already does after a quota wait; the mechanism is
proven, only the prompt differs.

> **Reversed by the live smoke test.** The rule below — that a resume skips
> `reset_to_default_branch` — was wrong, and was removed before this slice
> merged. It holds only when the parked session already has a branch of its
> own. Sessions usually park *early*, before their first commit, because the
> thing they cannot decide blocks them at the start; skipping the reset then
> means the resumed session inherits whatever branch the previous task left
> checked out. Observed on both task sources: a resumed task committed its
> work onto the *next* task's branch. The reset now runs on a resume too.
> Skipping `source.start` was correct and still stands.

Two things must **not** happen on a resume:

- `reset_to_default_branch` must be skipped. It exists to stop task N inheriting
  task N-1's branch, and a resume is not a new task — it is the same one,
  continuing.
- `source.start(task)` must be skipped. Under Jira it would re-fire
  `transition_start` against an issue already in that status, producing a
  warning every time. `reopen()` covers the source-side state instead.

**Skipping the reset does not mean the session's branch is still checked out**,
and this is the sharpest consequence of parking. Other tasks run while a task
waits for its answer, and each of them checks out the default branch on the way
in. By the time the answer arrives, the target repository is almost certainly
sitting on the default branch, not on the branch the parked session created.
Nothing is lost — the branch and its commits are intact — but the resumed
session will find a working tree that has moved under it.

The orchestrator cannot fix this itself: it does not know what the session
named its branch. `ANSWER_PROMPT` therefore carries the duty. It must tell the
resumed session that the working tree may have moved while it waited, and that
it should check out the branch it was working on before continuing. The session
knows the name from its own context, which is precisely why resuming the
original session rather than starting a fresh one is what makes this
recoverable at all.

### Two answer channels

**The dashboard**, because it is the only channel the file source can have — a
markdown checklist has no reply mechanism — and because it works identically
under both sources.

**A Jira comment**, because under the Jira source the human is already looking
at the ticket, and the question was posted there. Making them switch to a
different tool to answer is the kind of friction that leaves tasks parked.

### The answer crosses the boundary as a file

The dashboard's POST writes `~/.claudeloop/runs/<task_id>/answer.json`. The
loop reads it on its next iteration and unlinks it.

```
~/.claudeloop/runs/<task_id>/
    events.jsonl
    result.json      session writes, loop reads     (existing)
    answer.json      web writes, loop reads         (new)
```

This mirrors the mechanic the loop already runs on — a file appearing is how a
session says it finished — and it sidesteps the hazard `status.py` documents at
length. That module's lock-free design holds because exactly one thread calls
`set_status`, and `set_status` is a read-modify-write. A human answering from
the web thread was going to be the second writer. Writing a file instead means
the web thread never calls `set_status` at all, so no lock is needed. The
hazard is **dodged, not solved**; `status.py`'s docstring must be updated to
say so, because the next slice to consider writing from the web thread will
read that docstring and needs the truth.

Unlinking after reading is what stops one answer resuming a task twice.

Rejected: a read-write SQLite connection on the web thread (makes the web layer
a writer to the loop's database, and the loop must poll it anyway), and a
lock-guarded in-memory queue (most machinery, and the answer is lost on
restart — a file survives one for free).

### The source marks a blocked task, and a third verb undoes it

A blocked task gets `- [!]` in the checklist, or the `claudeloop-blocked` label
plus a question comment on the issue. A human must be able to see that the work
is waiting on them in the place they already look, not only on a dashboard
nobody has open.

That mark is also what stops the source re-offering the task, so answering has
to remove it. `TaskSource` therefore grows:

```python
def reopen(self, task: Task) -> None: ...
def answer(self, task: Task) -> str | None: ...
```

- `FileSource.reopen` rewrites the `- [!]` line back to `- [ ]`.
  `FileSource.answer` returns `None` — a checklist has no reply channel.
- `JiraSource.reopen` removes the `claudeloop-blocked` label.
  `JiraSource.answer` reads the issue's comments.

`state.db` also has to stop excluding the task: `terminal_ids()` includes
`blocked`. Nothing special is needed — `run_task` calls `start_task`, which is
an `INSERT OR REPLACE` setting status back to `running`, so the row leaves both
`terminal_ids()` and `blocked()` the moment the resume begins.

### The Jira answer is recognised by a marker prefix, and ordered statelessly

A comment counts as an answer only if its body starts with `claudeloop:`. The
question comment ClaudeLoop posts says so outright, so the human learns the
syntax from the message they are replying to.

```
ClaudeLoop — blocked
  Question: which staging database should the migration target?
  Reply with a comment starting with `claudeloop:` and I will continue.

Sam
  claudeloop: use staging-eu, not -us
```

The alternative — treat any comment by anyone other than ClaudeLoop's account
as the answer — needs no syntax from the human, but a colleague writing "nice
catch" would resume a session and spend real money acting on it. It also needs
the bot's own account identity to filter on; the marker removes that need
entirely, since ClaudeLoop's own comments never carry the prefix.

**Ordering without stored state.** A task can block twice, and the first
answer must not be read again as the answer to the second question. The
boundary is found in the comment list itself: locate ClaudeLoop's *newest*
question comment, then take the first `claudeloop:` comment after it. Both
markers are already in the list, so nothing has to be persisted to make this
correct across restarts.

`question_comment(question)` is a new function beside `closing_comment`. It is
posted instead of the closing comment when status is `blocked` — "ClaudeLoop
finished this task" is false for a task that is waiting on the reader.

### No answer timeout

A parked task waits indefinitely. Parking costs nothing — no session running,
no quota consumed, one row in a table — while auto-failing after some interval
throws away the human's chance to answer, which is the entire point of the
slice. Unanswered tasks are visible on the dashboard and marked in their source.

## Components

| Module | Change |
|---|---|
| `source.py` | `TaskSource` gains `reopen` and `answer`; `FileSource` implements both |
| `jira.py` | `JiraClient.remove_label`; `question_comment`; `JiraSource.reopen`/`answer` |
| `state.py` | `blocked()` and `last_session(task_id)`. No schema change |
| `loop.py` | Answered-task scan at the top of `main_loop`; `run_task(resume_with=...)` |
| `prompt.py` | `PROTOCOL` and `NUDGE_PROMPT` reworded; new `ANSWER_PROMPT` |
| `web.py` | `do_POST` and `POST /api/tasks/<id>/answer` |
| `status.py` | Docstring only: the second-writer hazard is dodged, not solved |
| `static/index.html` | Question and answer box on a blocked task |

`state.blocked()` returns enough to rebuild a `Task` — `id`, `text`, `source`,
`source_ref` are all already stored — so the loop can act on a task parked
before the process restarted.

## Flow

```
session writes result {status: "blocked", question: "..."}
    finish_task(status='blocked', question)          [unchanged]
    source.mark(task, 'blocked', ...)  ->  label + question comment / - [!]
    loop moves on to the next pending task

human answers
    dashboard POST   ->  runs/<id>/answer.json
    Jira comment     ->  "claudeloop: use staging-eu"

loop, top of each main_loop iteration, before source.pending():
    for each state.blocked() row:
        answer = read answer.json  or  source.answer(task)
    first answer found wins:
        source.reopen(task)
        run_task(cfg, state, source, task, resume_with=answer)
            session_id = state.last_session(task.id)
            no reset_to_default_branch, no source.start
            prompt = ANSWER_PROMPT.format(answer=answer)
            resume = True
```

Answered tasks are checked before new pending ones: an answer a human has
already given is more valuable than starting fresh work. One per iteration,
because the loop is serial.

The Jira channel costs one `GET /issue/{key}/comment` per parked task per poll.
With `POLL_S` at 30 seconds and a realistic number of parked tasks that is
negligible, and it is only paid while something is actually parked.

## The prompt layer

Three strings change. `CLAUDE.md` treats these as code: each needs a test
pinning the specific new wording, and a live run afterwards.

**`PROTOCOL`** today asserts *"Nobody is watching, so decide open questions
yourself"*. That becomes false with this slice, and a literal-minded agent
executing a false premise is exactly how this project's live failures have
happened. The new wording must keep the bar for asking high — a question parks
the task until a human happens to look, which can be hours — while stating
truthfully that `blocked` now reaches someone and the answer comes back to this
same session.

**`NUDGE_PROMPT`** today says *"Nobody is available to answer a question, so do
not end your turn asking what to do next"*. The second half stays right: the
nudge fires when no result file was written, and prose is never the answer. The
first half becomes a lie. New wording: if a human genuinely must decide, that
is what the result file's `blocked` status and `question` field are for — write
the file, do not end the turn with a question in your last message.

**`ANSWER_PROMPT`** is new. The bare answer text is not enough for a session
resumed hours later. It must say four things: this is the reply to the question
you asked; act on it; **the working tree may have moved to another branch while
you waited, so check out the branch you were working on before continuing**;
and the result file, not your last message, still ends the task.

## Error handling

Every failure keeps the loop running, in line with the rest of the project.

- **`reopen()` fails** (Jira unreachable, a 403): log a warning and resume
  anyway. `state.db` is what actually drives the resume; the label is for
  humans. Same reasoning as `mark()`'s existing behaviour.
- **`answer.json` is unreadable or malformed**: log a warning and unlink it.
  Leaving it in place would re-warn on every poll forever.
- **`JiraSource.answer` raises**: caught, logged, treated as "no answer yet",
  exactly as `pending()` treats an unreachable Jira as an empty backlog.
- **No `session_id` for a blocked task** (a database from before this slice, a
  row whose runs were pruned): fall back to a fresh session, with the original
  task text and the answer both in the prompt. The work is not lost, only the
  context.
- **The resume itself blocks again**: entirely legitimate. It parks again with
  a new question comment, and the stateless comment ordering above is what
  keeps the second round straight.

## The web write

This is the first route in the project that writes anything, deliberately
breaking S2a's read-only rule ahead of S5.

`POST /api/tasks/<task_id>/answer`, body `{"answer": "..."}`.

Existing guards apply unchanged: the `Host` header check (the DNS-rebinding
defence, and the only thing standing between an arbitrary website and this
route at the loopback default where `web_token` is empty) and the token check.

Added for this route:

- **`Content-Type: application/json` required.** A cross-origin `fetch` with
  that content type triggers a CORS preflight, which this server does not
  answer, so the browser never sends the POST. An HTML form cannot set it. This
  is what stops a drive-by cross-origin submission.
- **Task id validated** against the existing `TASK_ID_RE` before it touches a
  filesystem path — the same traversal guard `api_task` already applies.
- **The task must actually be `blocked`**, checked through the existing
  read-only connection. Prevents stray files under arbitrary run directories.
- **Non-empty, 8 KiB cap.** The answer becomes part of an argv element; a
  single argument is capped at 128 KiB on Linux, and the composed system prompt
  is already in there.
- **Written tmp-then-rename**, so the loop can never read a half-written file.

The dashboard renders a blocked task's question with an answer box and a submit
button. On success it shows that the answer was recorded; the task returns to
running on its own once the loop reaches it.

## Testing

Following the project's existing shape — stdlib `unittest`, real files, the
fake `claude` script, and `tests/jira_fake.py` rather than mocks.

- `state.blocked()` and `state.last_session()`, including a blocked task with
  no runs.
- `FileSource.reopen` restores `- [ ]`, preserving indentation and line ending
  the way `mark` does; a line that has since vanished is left alone.
- `JiraSource.answer`: the marker is required; a comment before the newest
  question comment is ignored; a task that blocked twice reads the second
  answer, not the first; a `JiraError` is swallowed as "no answer".
- `JiraSource.reopen` removes only the blocked label.
- Loop: a blocked task parks and the next pending task runs; an answer file
  resumes the parked task with the original `session_id`; `resume_with` skips
  the branch reset and `source.start`; a consumed `answer.json` does not fire
  twice.
- Web: the POST writes the file; a task that is not blocked is rejected; a
  missing or wrong content type is rejected; a bad task id is rejected; the
  token guard covers the new route.
- Prompt: the specific new wording of all three strings, pinned.

## The live smoke test

Not optional, per `CLAUDE.md`, and this slice is exactly the shape that has
burned this project before — new prompt text plus a state machine that only
misbehaves on the second task.

Scratch repository, `model = "haiku"`, two tasks, one of them written so the
session genuinely cannot proceed without a human decision. Answer it through
the dashboard, confirm the resumed session continues on its own branch rather
than starting over. Then a second run under the Jira source, answered by
comment, to exercise the marker and the reopen path.

What this run is looking for specifically: whether a real session, told that
questions now reach a human, starts asking questions it should have decided
itself. That is the failure this design's prompt wording is trying to prevent,
and no fixture can show it. Re-run after any prompt-text fix.

## Open, deliberately

- **Uncommitted work in a parked session's tree is not protected.** The branch
  and its commits survive — the next task's `reset_to_default_branch` never
  forces anything, by design — but a checkout with uncommitted changes in the
  way simply fails and logs, leaving the *next* task running on the parked
  task's branch. The existing behaviour, now reachable more often. Sessions are
  told to commit; this slice does not enforce it, and `ANSWER_PROMPT` telling
  the resumed session to check its branch out again is the mitigation.
- The dashboard answer box has no draft persistence. A closed tab loses typed
  text.
- `JiraSource.answer` reads the full comment list each poll, unpaginated —
  the same limitation `pending()` already carries.
