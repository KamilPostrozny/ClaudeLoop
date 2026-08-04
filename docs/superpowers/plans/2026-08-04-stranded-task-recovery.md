# S12 — A stranded task can come back: TDD plan

Spec: `docs/superpowers/specs/2026-08-04-claudeloop-stranded-task-recovery-design.md`

Four steps. Each names the test that must fail first and the code that makes
it pass.

---

## Step 1 — `State.unfinished()`

**Test** (`tests/test_state.py`)

```python
def test_unfinished_returns_interrupted_and_error_oldest_first(self):
    state = State(self.db_path, repo="/repo")
    for task_id, status in (("a", "done"), ("b", "error"), ("c", "blocked")):
        state.start_task(task_id, "jira", f"KAN-{task_id}", f"text {task_id}")
        state.finish_task(task_id, status, "", 0.0)
    state.start_task("d", "jira", "KAN-d", "text d")   # left 'running'
    state.db.close()

    reopened = State(self.db_path, repo="/repo")       # flips 'd' to interrupted
    rows = reopened.unfinished()

    self.assertEqual([row["source_ref"] for row in rows], ["KAN-b", "KAN-d"])
    self.assertEqual(rows[0]["text"], "text b")

def test_unfinished_is_repo_scoped(self):
    ...  # a row written under /other must not appear
```

**Code** (`claudeloop/state.py`), beside `blocked()`

```python
def unfinished(self) -> list[sqlite3.Row]:
    """Tasks that started and reached no verdict, oldest first.

    The complement of terminal_ids(), less 'running' -- which is the task
    this process is working on right now. A source whose backlog is a query
    rather than a file uses this to find its own in-flight work again after
    a restart; see JiraSource._stranded.

    Repo-scoped for the reason was_interrupted() is: tasks.id is not a key
    on its own, so an unscoped read could hand another loop's work over.
    """
    return self.db.execute(
        "SELECT id, source, source_ref, text FROM tasks"
        " WHERE status IN ('interrupted', 'error') AND repo IS ?"
        " ORDER BY started_at",
        (self.repo,),
    ).fetchall()
```

---

## Step 2 — `recovery_jql()`, pure

**Test** (`tests/test_jira.py`)

```python
def test_recovery_jql_names_keys_and_excludes_closed_and_labelled(self):
    self.assertEqual(
        recovery_jql(["KAN-1", "KAN-13"]),
        'key IN (KAN-1, KAN-13) AND statusCategory != Done AND '
        '(labels IS EMPTY OR labels NOT IN ("claudeloop-done", "claudeloop-blocked"))',
    )
```

Pinned whole, not by substring: S7's live failure was a sentence assembled
from fragments where every substring assertion passed.

**Code** (`claudeloop/jira.py`, beside `compose_jql`)

```python
MAX_RECOVERED = 50

_KEY = re.compile(r"^[A-Z][A-Z0-9]*-[0-9]+$")


def recovery_jql(keys: list[str]) -> str:
    """Find these issues whatever the operator's JQL selects on. ..."""
    return (
        f"key IN ({', '.join(keys)}) AND statusCategory != Done AND {GUARD}"
    )
```

---

## Step 3 — `JiraSource._stranded()` and its use in `pending()`

**Tests** (`tests/test_jira.py`), each with `FakeJira` answering
`POST /search/jql` from a two-item queue: backlog first, recovery second.

1. `test_pending_recovers_a_task_the_jql_no_longer_matches` — backlog answers
   empty, recovery answers KAN-13, `pending()` returns it.
2. `test_pending_puts_recovered_work_before_the_backlog` — both answer;
   recovered key is `tasks[0]`.
3. `test_pending_emits_a_key_in_both_answers_once`.
4. `test_pending_asks_only_about_this_repo_s_unfinished_keys` — assert the
   second request's `payload["jql"]` names exactly the expected keys.
5. `test_pending_skips_a_source_ref_that_is_not_an_issue_key` — a row whose
   `source_ref` is `KAN-1 OR project = OTHER` is dropped, warned about, and
   never reaches the query.
6. `test_pending_makes_no_recovery_query_when_nothing_is_unfinished` — one
   request total.
7. `test_pending_survives_an_unreadable_state_db` — `unfinished()` raising
   `sqlite3.Error` leaves the backlog result intact.
8. `test_pending_does_not_recover_another_source_s_rows` — a `file` row is
   ignored.

**Code**

```python
def _stranded(self) -> list:
    """This source's own unfinished work, found by key rather than by the
    operator's query. ..."""
    if self.state is None:
        return []
    try:
        rows = self.state.unfinished()
    except sqlite3.Error as error:
        log.warning(
            "could not read state.db unfinished tasks (%s); not looking for"
            " stranded work this poll", error,
        )
        return []
    keys = []
    for row in rows:
        if row["source"] != "jira":
            continue
        key = (row["source_ref"] or "").strip()
        if not _KEY.match(key):
            log.warning(...)
            continue
        keys.append(key)
    if not keys:
        return []
    return self._search_pages(recovery_jql(keys[:MAX_RECOVERED]))
```

and in `pending()`, the loop body unchanged but fed
`self._stranded() + issues`, with a `seen` set so a key emitted once is not
emitted twice.

---

## Step 4 — the loop still resumes what recovery hands back

**Test** (`tests/test_loop.py`) — end to end over the fake `claude`, no Jira:
a task left `interrupted` by a restart, offered again by a stub source, must
reach `run_task`'s resume branch. This is S9's covered behaviour; the test
exists to pin that S12 did not disturb the join between them.

```python
def test_a_recovered_task_resumes_its_session(self):
    ...  # assert the fake CLI saw --resume <session id>
```

---

## Then

- Whole suite green: `python -m unittest discover -s tests -t .`
- Review the branch.
- Live smoke test per the spec's last section — Jira source, two tickets,
  `SIGKILL` mid-first.
- `ROADMAP.md`: new S12 section under Built, and strike the "Jira live smoke
  test has never run" item in Next down to what is still unverified.
