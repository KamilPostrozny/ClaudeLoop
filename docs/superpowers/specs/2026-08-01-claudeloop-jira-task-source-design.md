# S3 — Jira task source

**Status:** designed, not built.
**Date:** 2026-08-01.

A second `TaskSource` implementation, so a ClaudeLoop instance can take its
backlog from a Jira Cloud project instead of a markdown checklist. The loop,
session, database and dashboard need no behavioural change — they already talk
to `pending()` and `mark()`.

This spec records what was decided at the time. `ROADMAP.md` records what is
true now.

## What already decided this

The roadmap carried S3's design before this spec existed. Those decisions stand
and are not re-argued here:

- Jira Cloud, API token, Basic auth, `urllib`. API tokens are free on any tier;
  OAuth 3LO needs a browser consent flow a headless box cannot complete.
- REST **v2**, not v3. v2 returns `description` as a plain string and accepts a
  plain-string comment body; v3 returns and demands ADF JSON. If v2 is retired
  the fallback is a small ADF flattener.
- Task identity hashes the **issue key**, not the text. `task_id(issue_key)`
  stays 16 hex characters, which `web.py`'s `TASK_ID_RE` requires as a
  path-traversal guard, and editing a ticket does not mint a new task. The
  issue key is the `source_ref`.
- The orchestrator embeds the ticket in the task text; the session additionally
  talks to Jira live.
- Still one repository per configuration.
- ClaudeLoop composes its exclusion into the operator's JQL rather than
  trusting them to remember it, splitting on `ORDER BY` so their ordering
  survives.
- Failure adds a label ClaudeLoop owns rather than transitioning to a status
  the workflow may not permit.
- Transitions on start and on done, names in config, matched per issue against
  what Jira actually offers. A missing name logs and continues.
- The orchestrator posts the closing comment, because it exists even when the
  session died mid-run and never got to say anything.
- The session talks to Jira through `python -m claudeloop.jira`, not the
  Atlassian MCP plugin: one credential, no OAuth browser flow on the S4 box,
  and code that can be tested.
- Network failures never look like an empty backlog.

## Architecture

One new file. No other module gains Jira knowledge.

### `claudeloop/jira.py`

Three things, in one file because they are one subject and share the client:

**`JiraClient`** — `urllib.request`, Basic auth from `email:token`, REST v2,
JSON in and out. Methods: `search(jql)`, `issue(key)`, `comments(key)`,
`add_comment(key, body)`, `add_label(key, label)`, `transitions(key)`,
`transition(key, name)`.

Retries: 3 attempts with backoff on 5xx and on network errors (`URLError`,
timeouts). A 4xx raises immediately — retrying a 403 or a malformed JQL never
helps, and an unattended loop must not spend a minute discovering that on every
poll. Every request carries a timeout; none may hang the loop.

**`JiraSource`** — the `TaskSource` implementation. Here rather than in
`source.py` so `source.py` keeps `Task`, the protocol and `FileSource`, and
nothing imports across.

**`main()`** — the session-facing CLI, reached as `python -m claudeloop.jira`.
Two subcommands, no more:

```
python -m claudeloop.jira show OPS-42          # summary, description, status,
                                               # labels, comments
python -m claudeloop.jira comment OPS-42 -     # body read from stdin
```

Transitions and labels are deliberately absent: the orchestrator owns those, and
a confused session must not be able to park a ticket in a status the operator
did not expect. Search is absent because nothing in this slice needs it.

The CLI reads the same `config.toml` the orchestrator does — same file, same
owner, already `chmod 600`. No credential is copied into `[session_env]`.

### Changes to existing modules

Small and enumerable:

| Module | Change |
|---|---|
| `config.py` | `source` key; `[jira]` table; conditional requirement of `tasks_file` vs `[jira]` |
| `loop.py` | `main_loop` picks the source from `cfg.source` instead of always `FileSource`; `run_task` calls `source.start(task)` |
| `source.py` | `TaskSource.mark` gains `cost: float = 0.0`; the protocol gains `start(task)`; `FileSource` ignores both |
| `session.py` | `child_env` prepends ClaudeLoop's package parent to `PYTHONPATH` |
| `prompt.py` | a `## Task source` section, emitted only when `source = "jira"` |
| `state.py` | one query: task ids with a terminal status |

## Configuration

```toml
source = "jira"                     # "file" (default) | "jira"

[jira]
site  = "https://acme.atlassian.net"
email = "me@acme.com"
token = "ATATT..."
jql   = "project = OPS AND status = 'To Do' ORDER BY priority DESC"
transition_start = "In Progress"    # optional; skipped when unset
transition_done  = "Done"           # optional; skipped when unset
```

An explicit discriminator, not an implicit one: a half-written `[jira]` table
must not silently change which backlog runs, and S5's schema gets one obvious
key to render a branch on.

`tasks_file` is required only when `source = "file"`, `[jira]` only when
`source = "jira"`. `source` must be one of the two known values. `site`,
`email`, `token` and `jql` are each required within `[jira]`; a missing one
fails at load, loudly, the way a bad `repo` path already does — not four hours
later inside a subprocess.

The permissions guard in `config.py` already covers the Jira token: it exists
precisely because this file holds secrets.

## Data flow

### `pending()`

1. Compose the guard into the operator's JQL, preserving their ordering:

   ```
   (<operator jql without its ORDER BY>)
   AND (labels IS EMPTY OR labels NOT IN ("claudeloop-done", "claudeloop-blocked"))
   ORDER BY <theirs, if any>
   ```

   `labels != "x"` **excludes issues that have no labels at all** — the single
   non-obvious trap in this slice, and the reason for the `IS EMPTY` disjunct.
   The exclusion is composed, never operator-supplied, so it cannot be
   accidentally disabled.

2. `search(jql)` with `maxResults = 50`, fields `summary,description`. One page:
   the loop consumes one task at a time and re-polls constantly, so pagination
   would buy nothing.

3. Build one `Task` per issue:

   ```python
   Task(
       id=task_id(key),                       # hashes the key, not the text
       text=f"{key}: {summary}\n\n{description}",
       source="jira",
       source_ref=key,
   )
   ```

   A `null` description yields key and summary alone.

4. Drop any task whose id already holds a terminal row in `state.db` — the
   backstop described below.

Any HTTP or network failure: log, return `[]`. The loop then idles and retries
in `POLL_S`. An empty backlog and an unreachable Jira must look the same to the
loop, and neither may burn tasks.

### `mark(task, status, summary, cost=0.0)`

In this order, because the order encodes what is load-bearing:

1. **Add the label** — `claudeloop-done` for `done`, `claudeloop-blocked` for
   `failed` and `blocked`. Retried. This is the write the loop trusts.
2. **Post the closing comment** — status, summary, cost.
3. **Transition** — `transition_done` when the task ended, matched by name
   against `transitions(key)` for that issue. Missing name, or a refusal: warn
   and continue.

`mark` grows an optional `cost` keyword on the protocol so the closing comment
can carry it. `FileSource.mark` accepts and ignores it.

### `start(task)`

The protocol gains a second method, called by `run_task` just after
`state.start_task`. `JiraSource.start` fires `transition_start` by the same
name-matching rule, and warns rather than raising if Jira refuses it —
identical handling to the closing transition, for identical reasons.
`FileSource.start` is a no-op: a checklist has nothing to say when work begins.

Never raises. A fault here must not look different from any other environment
fault the loop already survives, the same contract
`reset_to_default_branch` holds.

## Why the label, not the transition

The file source marks `- [x]`. The obvious Jira analogue is a transition — but
Jira, not ClaudeLoop, decides whether a transition is allowed, and the roadmap
already decided a refused transition logs and continues rather than failing the
task. Every one of these is an orchestrator-side failure, none of them a bug:

- the configured name is not offered from the issue's **current** status — a
  human moved it to "In Review" mid-run and the workflow only allows
  To Do → In Progress;
- the transition's screen has a required field (resolution, fix version) and
  Jira answers 400;
- the token's account lacks Transition Issues permission on that project;
- a condition or validator guards it (needs an assignee, needs subtasks closed);
- 5xx or a network fault, retries exhausted.

If the transition were the thing that removed an issue from the JQL result set,
each of those would put the loop into an infinite re-run of one ticket, paying
for it every time.

A label add is a plain field edit: no workflow, no validators, no screen. It is
far likelier to land, so it is what the guard keys on. The transition stays —
its job is telling humans where the work is — but it is best-effort on top.

### The backstop

If even the label write fails through all its retries, the issue reappears on
the next poll. So `pending()` also drops ids that already hold a terminal row in
`state.db`:

```sql
SELECT id FROM tasks WHERE status IN ('done', 'failed', 'blocked')
```

`interrupted` is deliberately absent from that list. `State.__init__` rewrites
`running` rows to `interrupted` when a previous process died mid-task, and such
a task never finished — it must remain eligible.

The loop moves to the next ticket and logs loudly that Jira never took the mark.
The dashboard shows the finished row against a ticket that looks untouched,
which is exactly the discrepancy a human needs to see.

`JiraSource` needs the database for this, so `main_loop` constructs it with the
already-open `State`. That is the loop's own connection, on the loop's own
thread — not the web layer's read-only one, and no new connection.

## The session's side

### The import trap

The session runs with `cwd = repo`, so a bare `python -m claudeloop.jira` there
raises `ImportError` — the package is not on its path. Running `jira.py` by
absolute path instead breaks its relative imports.

`child_env` therefore prepends ClaudeLoop's package parent to `PYTHONPATH`, and
the prompt names `sys.executable` rather than bare `python`, since the box may
have several interpreters and only one of them is the one running ClaudeLoop.
Merged before `CLAUDELOOP_RESULT`, which stays last and unoverridable.

### The prompt layer

`compose` emits a `## Task source` section only when `source = "jira"`. Static
text: no issue key is threaded through `compose`/`build_command`/`run`
signatures, because the task text already begins with the key.

```
## Task source

This task is a Jira issue. Its key is the first token of the task text.
Read the full ticket, including comments:
    <sys.executable> -m claudeloop.jira show <KEY>
Post a comment (body on stdin):
    <sys.executable> -m claudeloop.jira comment <KEY> -
Comment when you find something a human should see, or before a long
step. Do not transition the issue or edit its labels — ClaudeLoop does
that when the task ends. Commenting is not how a task ends: the result
file still is.
```

The final sentence is not decoration. This layer is read by a literal-minded
agent, and a session told it may talk on the ticket is exactly the session that
ends its turn with a comment instead of `result.json`. Every live failure this
project has had traced back to a sentence that could be read two ways.

The section sits inside the existing precedence chain, below the protocol.

## Failure handling, in full

| Failure | Behaviour |
|---|---|
| `pending()` network error, 401, bad JQL | log, return `[]`; loop idles, retries in `POLL_S` |
| `mark()` label write fails 3× | log loudly; `state.db` backstop prevents the re-run |
| transition name absent or refused | warn; the task's status is unaffected |
| closing comment fails | warn; the label already carries the outcome |
| `description` is `null` | task text is key and summary alone |
| CLI subcommand fails in-session | non-zero exit and a message on stderr; the session decides what to do, and the task is unaffected |

## Testing

Stdlib only, real files, no mocks — matching how the suite already fakes the
`claude` CLI with a shell script.

- **`JiraClient`** against an `http.server` fixture on a scratch port, replaying
  payloads recorded from the live instance. Covers retry-on-5xx,
  no-retry-on-4xx, and timeouts.
- **`JiraSource.pending`/`mark`** against the same fixture: task construction,
  the null description, the `state.db` backstop, the label-then-comment-then-
  transition order, a refused transition leaving the status alone.
- **Pure functions tested directly** — JQL composition (the `ORDER BY` split and
  the `labels IS EMPTY` idiom each get a pinning test) and task-text building.
- **`prompt.compose`** — the `## Task source` section present under
  `source = "jira"`, absent under `source = "file"`, with the specific wording
  pinned.
- **`config.load_config`** — each new validation path.

## Verification

**Step 0, before any code is written:** a read-only probe of the live instance,
to settle what fixtures would otherwise inherit as assumptions:

- the search endpoint path — Atlassian moved it; `/rest/api/2/search` versus
  `/rest/api/2/search/jql`;
- the response and pagination shape;
- the `transitions` payload;
- that `description` really does come back as a plain string on v2.

Recorded payloads from that probe become the fixtures. A fixture written from a
design document inherits that document's wrong assumptions — this project has
lost five defects to exactly that.

**At the end:** the live smoke test, non-negotiable. Scratch repository,
`model = "haiku"`, **two tickets not one**, against a real Jira project. What it
has to confirm, none of which a fixture can:

- the composed JQL returns what the operator expected;
- the label lands and the second poll does not re-offer the first ticket;
- the session actually finds and runs `python -m claudeloop.jira`, which is the
  whole point of the `PYTHONPATH` change;
- the session comments without mistaking commenting for finishing;
- the transitions fire, or fail the way this spec says they fail.

## Out of scope

- Jira Server or Data Center. Cloud only.
- Attachments, subtasks, issue links, worklogs.
- More than one repository per configuration.
- The answer channel: S2b builds on the session's ability to comment, and this
  slice deliberately stops at that ability existing.
