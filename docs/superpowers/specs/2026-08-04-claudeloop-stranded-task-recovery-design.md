# S12 — A stranded task can come back

**Status:** design settled, 2026-08-04.

## The incident

Observed live on the Home Assistant add-on, Jira project `KAN`.

```
16:41:31  task 98720990de2c5461 starting: KAN-13
16:43:19  task 98720990de2c5461 rate limited, sleeping 5830s
17:12     container killed to upgrade the add-on 0.1.3 -> 0.2.0
20:05:30  loop back up, dashboard on 8765
          <idle, pending: [], nothing again>
```

The row for KAN-13 sat at `interrupted` with `finished_at` null, its run row
carrying `exit_reason: RateLimited` and session
`6987500d-cec7-4dba-9ea0-83ff5c71c387`. Every part of S9's recovery was
present and correct. None of it ran, because nothing ever offered the task
again.

`JiraSource.start` had fired `transition_start` when the task began and moved
the issue to In Progress — confirmed afterwards in Jira's own changelog, which
records `status: "In Progress" -> "Done"` when a human finally closed the
ticket by hand. The operator's JQL selects the backlog status. So from the
moment ClaudeLoop transitioned the issue, its own query could no longer see
it, and `main_loop` takes work from exactly two places:

```python
answered = find_answered(...)      # state.blocked(), rebuilt from state.db
...
pending = source.pending()         # the JQL
```

`interrupted` is in neither. `terminal_ids()` deliberately excludes
`interrupted` so that a source *can* offer the task back, but that backstop
only ever fires on a ticket the query still returns.

The upgrade is incidental. A crash, a reboot, an `ha addons restart`, or a
`SIGKILL` produces the same stranding. KAN-1 was stranded the same way earlier
in the same run.

**The feature working correctly is what set the trap.** `transition_start`
failed against this instance for KAN-9 and KAN-11 — `Jira does not offer a
'In Progress' transition ... leaving the issue where it is` — so those two
stayed in the backlog status, finished, and got labelled. It succeeded for
KAN-12 and KAN-13. A transition that does nothing cannot strand anything.

## What is being fixed

Only the blindness. `transition_start` stays; moving an issue to In Progress
when work starts is the point of it. `run_task`'s resume path stays untouched
— `was_interrupted`, `prior_cost`, `last_session`, `INTERRUPTED_PROMPT`,
`worktree.ensure`'s reuse — all of it already works and is covered.

`JiraSource.pending()` gains a second query that finds the source's own
unfinished work by key, independently of the operator's JQL.

## Design

### Recovery lives in the source, not the loop

`FileSource` does not have this problem: an interrupted task's line is still
`- [ ]`, so `pending()` returns it on the next poll and always has. Only a
source whose backlog is a *query* can lose sight of work it has already
started. Putting the recovery in `main_loop` would mean a second offer path
every source pays for to fix one source's defect.

### The recovery set is every non-terminal, non-running status

```python
State.unfinished() -> list[Row]   # status IN ('interrupted', 'error')
```

The exact complement of `terminal_ids()` minus `running`, which is the live
task. `error` is included because `run_task` already documents that it is
non-terminal and expects the source to offer those back —

> 'error' is non-terminal too, so the source offers those back as well

— which is true of `FileSource` and has never been true of `JiraSource`.
Recovering it does not make `error` resume a session: `run_task` keys the
resume on `was_interrupted` alone, and that stays.

Repo-scoped like `blocked()` and `terminal_ids()`, for the reason S9 records:
`tasks.id` alone is not the key, and an unscoped read could hand another
loop's stranded work to this one.

**This buys head-of-line blocking that the Jira source did not have.** A task
whose crash is task-local and permanent — the leftover-worktree case
`run_task` already documents — now comes back every poll, is re-picked ahead
of the backlog, crashes again, and no later ticket runs. That is precisely
what `FileSource` has always done, and what the open issue in `ROADMAP.md`
already describes; before this slice the Jira source dodged it only by
silently losing the task. Losing work is the worse failure, and the visible
one comes with a repeating log line naming the task. Recorded rather than
mitigated.

### Jira, not state.db, decides whether it is still wanted

The recovered rows carry `text` and `source_ref`, so a `Task` could be rebuilt
from the database with no network call at all. That is rejected, and the
incident is why: by the time anyone looked, a human had finished KAN-13 by
hand and closed it. A recovery that trusted state.db alone would have
resumed a ticket that was already Done and paid for the work twice.

So recovery asks Jira about its own keys:

```
key IN (KAN-1, KAN-13) AND statusCategory != Done AND <GUARD>
```

- `key IN (...)` reaches the issue whatever the operator's JQL selects on.
- `statusCategory != Done` is the one status predicate that is
  workflow-independent and locale-independent. Jira's three categories are
  fixed; status *names* are per-account translations, which is the exact trap
  S9.1 was written for.
- `GUARD` is the same label exclusion `compose_jql` splices in, so a `mark()`
  whose label landed while the row stayed non-terminal still excludes.

An issue that no longer matches is simply absent from the answer, and the row
stays `interrupted` in state.db forever. That is intended: it is a record of
what happened, not a queue entry.

### Recovered work goes first

Ahead of the backlog, and ahead of it in the returned list so `pending[0]`
picks it. Money is already spent on it, a worktree already exists for it, and
a session may still be resumable. A key appearing in both queries is emitted
once.

### Keys are validated before they reach the JQL

`source_ref` arrives from a database column and is spliced into a query
string. JQL has no parameter binding. Keys must match `^[A-Z][A-Z0-9]*-[0-9]+$`
— Jira's own issue-key shape — and anything else is dropped with a warning.
Not defensive padding: it is the only thing between a `tasks` row and an
injected clause.

The list is capped at `MAX_RECOVERED = 50` keys, oldest first, for the same
reason `MAX_PAGES` exists: an unattended loop must not build an unbounded
query out of a table that only grows.

### Failure modes

Both are already the house style and neither is new:

- state.db unreadable → warn, no recovery this poll, the backlog still works.
  Same shape as the `terminal_ids()` guard directly above it.
- Jira unreachable → `_search_pages` already logs once and returns what it
  has. A failed recovery query leaves the backlog query's result intact.

## Rejected

- **Rebuild the Task from state.db, no second query.** Simpler and wrong: it
  would have re-run KAN-13 after a human closed it. See above.
- **Transition the issue back on a quota park.** Fights the operator's
  workflow, and the case that actually strands work is a crash, which never
  reaches the code that would do it.
- **Drop `transition_start`.** The transition is wanted. The blindness is the
  defect.
- **Recover in `main_loop` for every source.** One source's defect, every
  source's cost.
- **Hide `interrupted` from the dashboard's completed panel.** It is what made
  the incident read as "KAN-13 finished", and `web.py`'s
  `WHERE status != 'running'` is why. But after this slice an `interrupted`
  row is transient — it goes back to `running` on the next poll — and in the
  one case where it is not (Jira says the issue is closed), hiding the row
  entirely is worse than filing it under the wrong heading. Left as a known
  wart rather than traded for a worse one.

## Live smoke test

Must use the Jira source — the file source cannot reproduce this at all.
Scratch repository, `model = "haiku"`, `transition_start` configured to a
transition the instance really offers, and a JQL that selects only the backlog
status. Two tickets. `SIGKILL` the loop mid-first-ticket, confirm in Jira that
the issue is out of the JQL's status, restart, and watch it come back and
resume rather than the loop going idle. Then close the second ticket by hand
while the loop is down and confirm it is *not* resumed.
