# S2b Question and Answer Channel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A session that hits something only a human can decide parks its task instead of stalling the loop, and resumes in the same session once a human answers through the dashboard or a Jira comment.

**Architecture:** A blocked task is marked in its source and stepped over; the loop keeps working the backlog. An answer arrives either as `runs/<task_id>/answer.json` (written by the web thread — a file, so the web layer never becomes a second writer to `status.py`) or as a Jira comment prefixed `claudeloop:`. At the top of every `main_loop` iteration, before polling for new work, the loop looks for a parked task with an answer, reopens it in its source, and re-enters `run_task` with `resume_with=<answer>`, which resumes the original `session_id` by `--resume`.

**Tech Stack:** Python 3.11+ standard library only. `unittest`, real files on disk, `tests/fake_claude.sh`, `tests/jira_fake.py`. One no-build HTML file for the frontend.

**Spec:** `docs/superpowers/specs/2026-08-01-claudeloop-question-answer-channel-design.md`

## Global Constraints

- **Python 3.11+, standard library only.** No third-party packages — not for the orchestrator, not for the tests, not for the frontend. `pip install` and `npm install` must both remain unnecessary.
- **No build step.** `static/index.html` is one file with inline CSS and an inline module script, making no off-origin requests.
- **`CLAUDELOOP_RESULT` is merged last** into the session environment. Nothing in this slice touches that.
- **No trace of ClaudeLoop lives in a repository it works in.** `answer.json` goes under `~/.claudeloop/runs/<task_id>/`, never in the target repo.
- **Strictly serial.** Parking a task does not mean two sessions run. The parked one is not running at all.
- **Nothing in a `TaskSource` may raise into the loop.** An unreachable Jira looks like "no answer yet", exactly as it already looks like an empty backlog.
- **The prompt strings are the product.** Every wording change in `prompt.py` / `loop.py` needs a test pinning the specific new text, and a live run afterwards.
- **Tests use real files and the fake CLI, not mocks.** A test that spawns `git` in a scratch repository must set `commit.gpgsign false` locally on it.
- Whole suite: `python -m unittest discover -s tests -t .` (~30s). One module: `python -m unittest tests.test_loop -v`.
- Work on a branch created from `main`. Do not push; this repository has no usable remote.

---

### Task 1: `State.blocked()` and `State.last_session()`

The two reads the loop needs to act on a task parked before the current process started. No schema change — `tasks` already stores `source`, `source_ref`, `text` and `question`, and `runs` already stores `session_id`.

**Files:**
- Modify: `claudeloop/state.py` (add two methods after `terminal_ids`, around line 116)
- Test: `tests/test_state.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `State.blocked(self) -> list[sqlite3.Row]` — rows with columns `id`, `source`, `source_ref`, `text`, `question`, oldest first.
  - `State.last_session(self, task_id: str) -> str | None` — `session_id` of the most recent run of that task, or `None` when it has no runs.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_state.py`:

```python
class BlockedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state = State(self.tmp / "state.db")

    def test_blocked_returns_what_a_task_can_be_rebuilt_from(self):
        self.state.start_task("aaaa", "jira", "OPS-1", "OPS-1: do a thing")
        self.state.finish_task("aaaa", "blocked", "stuck", 0.25, "which currency?")

        rows = self.state.blocked()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "aaaa")
        self.assertEqual(rows[0]["source"], "jira")
        self.assertEqual(rows[0]["source_ref"], "OPS-1")
        self.assertEqual(rows[0]["text"], "OPS-1: do a thing")
        self.assertEqual(rows[0]["question"], "which currency?")

    def test_blocked_ignores_every_other_status(self):
        for index, status in enumerate(("done", "failed", "error", "running")):
            self.state.start_task(f"id{index}", "file", "- [ ] x", "x")
            if status != "running":
                self.state.finish_task(f"id{index}", status, "", 0.0)

        self.assertEqual(self.state.blocked(), [])

    def test_blocked_is_oldest_first(self):
        for key in ("first", "second"):
            self.state.start_task(key, "file", f"- [ ] {key}", key)
            self.state.finish_task(key, "blocked", "", 0.0, "?")
            time.sleep(0.01)

        self.assertEqual([row["id"] for row in self.state.blocked()],
                         ["first", "second"])

    def test_last_session_is_the_most_recent_run(self):
        self.state.start_task("aaaa", "file", "- [ ] x", "x")
        self.state.start_run("aaaa", "session-one", 0)
        self.state.start_run("aaaa", "session-two", 1)

        self.assertEqual(self.state.last_session("aaaa"), "session-two")

    def test_last_session_is_none_when_the_task_never_ran(self):
        self.state.start_task("aaaa", "file", "- [ ] x", "x")

        self.assertIsNone(self.state.last_session("aaaa"))
```

Check the imports at the top of `tests/test_state.py` — it needs `time`, `tempfile`, `unittest` and `Path`. Add whichever are missing.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_state -v`
Expected: FAIL with `AttributeError: 'State' object has no attribute 'blocked'`

- [ ] **Step 3: Write the implementation**

In `claudeloop/state.py`, after `terminal_ids`:

```python
    def blocked(self) -> list[sqlite3.Row]:
        """Tasks parked waiting for a human, oldest first.

        Returns enough to rebuild the Task the loop handed to the source, so
        a task parked before this process started can still be resumed.
        """
        return self.db.execute(
            "SELECT id, source, source_ref, text, question FROM tasks"
            " WHERE status='blocked' ORDER BY finished_at"
        ).fetchall()

    def last_session(self, task_id: str) -> str | None:
        """The session id of this task's most recent run.

        An answered task resumes in the session that asked the question: it
        still holds the repository context and the name of the branch it
        created. None means there is no session to resume -- a database from
        before this slice, or a task whose runs were pruned.
        """
        row = self.db.execute(
            "SELECT session_id FROM runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return row["session_id"] if row is not None else None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_state -v`
Expected: PASS, all cases.

- [ ] **Step 5: Commit**

```bash
git add claudeloop/state.py tests/test_state.py
git commit -m "feat: state.db can name the parked tasks and the sessions that asked"
```

---

### Task 2: `TaskSource.reopen`/`answer` and `FileSource`

The protocol grows the two verbs the answer path needs, and `FileSource` implements them. `mark` and `reopen` are the same rewrite with different markers, so the shared body is factored out rather than duplicated.

**Files:**
- Modify: `claudeloop/source.py:24-27` (the protocol) and `:60-78` (`FileSource.mark`)
- Test: `tests/test_source.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `TaskSource.reopen(self, task: Task) -> None` — undo a blocked mark.
  - `TaskSource.answer(self, task: Task) -> str | None` — the human's reply through this source's own channel, or `None`.
  - `FileSource._rewrite(self, match: str, marker: str, text: str) -> None` — internal.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_source.py`:

```python
class ReopenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "tasks.md"

    def source_for(self, body: str) -> FileSource:
        self.path.write_text(body)
        return FileSource(self.path)

    def test_reopen_restores_an_attention_line_to_pending(self):
        source = self.source_for("- [ ] alpha\n- [ ] beta\n")
        task = source.pending()[0]
        source.mark(task, "blocked", "stuck")
        self.assertEqual(self.path.read_text(), "- [!] alpha\n- [ ] beta\n")

        source.reopen(task)

        self.assertEqual(self.path.read_text(), "- [ ] alpha\n- [ ] beta\n")

    def test_reopen_keeps_indentation_and_line_ending(self):
        source = self.source_for("  - [ ] alpha\r\n")
        task = source.pending()[0]
        source.mark(task, "blocked", "stuck")

        source.reopen(task)

        self.assertEqual(self.path.read_text(), "  - [ ] alpha\r\n")

    def test_reopen_leaves_a_line_that_has_since_vanished_alone(self):
        source = self.source_for("- [ ] alpha\n")
        task = source.pending()[0]
        self.path.write_text("- [ ] something else entirely\n")

        source.reopen(task)

        self.assertEqual(self.path.read_text(), "- [ ] something else entirely\n")

    def test_reopen_does_not_touch_a_done_line(self):
        source = self.source_for("- [ ] alpha\n")
        task = source.pending()[0]
        source.mark(task, "done", "finished")

        source.reopen(task)

        self.assertEqual(self.path.read_text(), "- [x] alpha\n")

    def test_a_reopened_task_is_pending_again(self):
        source = self.source_for("- [ ] alpha\n")
        task = source.pending()[0]
        source.mark(task, "blocked", "stuck")
        self.assertEqual(source.pending(), [])

        source.reopen(task)

        self.assertEqual([t.id for t in source.pending()], [task.id])

    def test_a_checklist_has_no_answer_channel(self):
        source = self.source_for("- [ ] alpha\n")

        self.assertIsNone(source.answer(source.pending()[0]))

    def test_mark_survives_a_task_file_that_has_been_deleted(self):
        source = self.source_for("- [ ] alpha\n")
        task = source.pending()[0]
        self.path.unlink()

        source.mark(task, "done", "finished")  # must not raise
        source.reopen(task)
```

Check the imports at the top of `tests/test_source.py` — it needs `tempfile`, `unittest`, `Path`, and `FileSource`. Add whichever are missing.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_source -v`
Expected: FAIL with `AttributeError: 'FileSource' object has no attribute 'reopen'`

- [ ] **Step 3: Write the implementation**

In `claudeloop/source.py`, extend the protocol:

```python
class TaskSource(Protocol):
    def pending(self) -> list[Task]: ...
    def start(self, task: Task) -> None: ...
    def mark(self, task: Task, status: str, summary: str, cost: float = 0.0) -> None: ...
    def reopen(self, task: Task) -> None: ...
    def answer(self, task: Task) -> str | None: ...
```

Replace `FileSource.mark` (currently lines 60-78) with the factored trio:

```python
    def _rewrite(self, match: str, marker: str, text: str) -> None:
        """Replace the line reading exactly `match` with `marker text`,
        keeping its indentation and line ending.

        Matched on exact line text rather than index, so a user editing the
        file while the task ran cannot cause the wrong line to be rewritten.
        A line that has since vanished -- or a whole file that has -- is left
        alone; the database still holds the record.
        """
        try:
            lines = self.path.read_text().splitlines(keepends=True)
        except OSError:
            return
        for index, line in enumerate(lines):
            if line.strip() != match:
                continue
            body = line.rstrip("\r\n")
            indent = line[: len(line) - len(line.lstrip())]
            eol = line[len(body):]
            lines[index] = f"{indent}{marker} {text}{eol}"
            self.path.write_text("".join(lines))
            return

    def mark(self, task: Task, status: str, summary: str, cost: float = 0.0) -> None:
        """Rewrite the task's line to its verdict."""
        self._rewrite(task.source_ref, DONE if status == "done" else ATTENTION, task.text)

    def reopen(self, task: Task) -> None:
        """Undo a blocked mark, so an answered task is offered again.

        Matches the `- [!]` line this source itself wrote, not the original
        `- [ ]` source_ref, which no longer exists by the time a task is
        reopened.
        """
        self._rewrite(f"{ATTENTION} {task.text}", UNCHECKED, task.text)

    def answer(self, task: Task) -> str | None:
        """A markdown checklist has no reply channel. Answers for a file-source
        task arrive through the dashboard instead."""
        return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_source tests.test_loop -v`
Expected: PASS. `test_loop` is included because `MainLoopTest` exercises `mark` end to end and this step refactored it.

- [ ] **Step 5: Commit**

```bash
git add claudeloop/source.py tests/test_source.py
git commit -m "feat: TaskSource gains reopen() and answer(); FileSource implements both"
```

---

### Task 3: Jira — `remove_label`, `question_comment`, `JiraSource.reopen`

The Jira side of the mark that a human sees, and of undoing it. `mark()` posts a question comment instead of a closing comment when the status is `blocked` — "ClaudeLoop finished this task" is false for a task waiting on the reader, and the comment has to teach the reply syntax.

Also quotes the issue key in the three client methods that still interpolate it raw. That closes an open issue tracked in `ROADMAP.md` and stops this task adding a fourth inconsistent method.

**Files:**
- Modify: `claudeloop/jira.py` — `closing_comment` area (~line 99), `JiraClient.add_label`/`transitions`/`transition` (~lines 186-200), `JiraSource.mark` (~line 305)
- Test: `tests/test_jira.py`

**Interfaces:**
- Consumes: `TaskSource.reopen` from Task 2.
- Produces:
  - `jira.QUESTION_MARKER = "claudeloop:"`
  - `jira.QUESTION_HEADING = "ClaudeLoop is blocked on this task and needs an answer."`
  - `jira.question_comment(summary: str, cost: float) -> str`
  - `JiraClient.remove_label(self, key: str, label: str) -> dict`
  - `JiraSource.reopen(self, task: Task) -> None`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_jira.py`:

```python
class QuestionCommentTest(unittest.TestCase):
    def test_it_carries_the_summary_and_teaches_the_reply_syntax(self):
        from claudeloop.jira import QUESTION_HEADING, QUESTION_MARKER, question_comment

        body = question_comment("I stopped.\n\nQuestion: which currency?", 0.25)

        self.assertTrue(body.startswith(QUESTION_HEADING))
        self.assertIn("Question: which currency?", body)
        self.assertIn(QUESTION_MARKER, body)
        self.assertIn("0.2500", body)

    def test_a_blocked_task_gets_the_question_comment_not_the_closing_one(self):
        fake = FakeJira({
            "PUT /issue/OPS-1": (204, {}),
            "POST /issue/OPS-1/comment": (201, {}),
        })
        self.addCleanup(fake.close)
        source = JiraSource(JiraClient(fake.url, "e@x", "t"), "project = OPS")

        source.mark(
            Task("abc", "OPS-1: thing", "jira", "OPS-1"),
            "blocked",
            "I stopped.\n\nQuestion: which currency?",
            0.25,
        )

        comments = [payload for method, path, payload in fake.requests
                    if path == "/issue/OPS-1/comment"]
        self.assertEqual(len(comments), 1)
        from claudeloop.jira import QUESTION_HEADING
        self.assertTrue(comments[0]["body"].startswith(QUESTION_HEADING))
        self.assertNotIn("finished this task", comments[0]["body"])

    def test_a_done_task_still_gets_the_closing_comment(self):
        fake = FakeJira({
            "PUT /issue/OPS-1": (204, {}),
            "POST /issue/OPS-1/comment": (201, {}),
        })
        self.addCleanup(fake.close)
        source = JiraSource(JiraClient(fake.url, "e@x", "t"), "project = OPS")

        source.mark(Task("abc", "OPS-1: thing", "jira", "OPS-1"), "done", "did it", 0.5)

        comments = [payload for method, path, payload in fake.requests
                    if path == "/issue/OPS-1/comment"]
        self.assertIn("finished this task", comments[0]["body"])


class ReopenTest(unittest.TestCase):
    def test_reopen_removes_only_the_blocked_label(self):
        from claudeloop.jira import BLOCKED_LABEL

        fake = FakeJira({"PUT /issue/OPS-1": (204, {})})
        self.addCleanup(fake.close)
        source = JiraSource(JiraClient(fake.url, "e@x", "t"), "project = OPS")

        source.reopen(Task("abc", "OPS-1: thing", "jira", "OPS-1"))

        puts = [payload for method, path, payload in fake.requests if method == "PUT"]
        self.assertEqual(puts, [{"update": {"labels": [{"remove": BLOCKED_LABEL}]}}])

    def test_reopen_survives_a_jira_that_refuses(self):
        fake = FakeJira({"PUT /issue/OPS-1": (403, {"errorMessages": ["nope"]})})
        self.addCleanup(fake.close)
        source = JiraSource(JiraClient(fake.url, "e@x", "t"), "project = OPS")

        with self.assertLogs("claudeloop", level="WARNING"):
            source.reopen(Task("abc", "OPS-1: thing", "jira", "OPS-1"))

    def test_an_issue_key_is_escaped_into_every_client_url(self):
        # add_label / transitions / transition used to interpolate the key
        # raw. Only JiraSource passed keys, always straight from Jira's own
        # search results, so it was safe by accident rather than by design.
        client = JiraClient("http://example.invalid", "e@x", "t")
        seen = []
        client._request = lambda method, path, payload=None: seen.append(path) or {}

        client.add_label("OPS 1/x", "l")
        client.remove_label("OPS 1/x", "l")
        client.transitions("OPS 1/x")
        client.transition("OPS 1/x", "31")

        self.assertTrue(all("OPS%201%2Fx" in path for path in seen), seen)
```

Check the imports at the top of `tests/test_jira.py` — it needs `JiraClient`, `JiraSource`, `Task` and `FakeJira`. Add whichever are missing.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_jira -v`
Expected: FAIL with `ImportError: cannot import name 'QUESTION_HEADING'`

- [ ] **Step 3: Write the implementation**

In `claudeloop/jira.py`, beside `closing_comment`:

```python
QUESTION_MARKER = "claudeloop:"
"""A comment counts as an answer only if it starts with this. The question
comment says so outright, so the human learns the syntax from the message
they are replying to. Without it, a colleague writing "nice catch" would
resume a session and spend real money acting on it -- and identifying our
own account would become necessary, which this avoids entirely: ClaudeLoop's
own comments never carry the prefix."""

QUESTION_HEADING = "ClaudeLoop is blocked on this task and needs an answer."
"""Also the boundary marker for JiraSource.answer: a task can block twice,
and the first answer must not be read again as the answer to the second
question. Locating the newest comment starting with this heading is what
keeps the two rounds straight, with nothing persisted anywhere."""


def question_comment(summary: str, cost: float) -> str:
    """Posted instead of closing_comment when a task parks.

    `summary` already carries the question -- loop.read_result appends it --
    so this adds the heading and the reply instruction rather than repeating
    it.
    """
    return (
        f"{QUESTION_HEADING}\n\n{summary}\n\n"
        f"Reply with a comment starting with `{QUESTION_MARKER}` and ClaudeLoop "
        f"will pick this task back up with your answer.\n\n"
        f"(cost so far ${cost:.4f})"
    )
```

Quote the key in `add_label`, `transitions` and `transition`, and add `remove_label` beside them:

```python
    def add_label(self, key: str, label: str) -> dict:
        # `update` adds one label atomically. Writing fields.labels instead
        # replaces the whole list, deleting the operator's own labels.
        key = urllib.parse.quote(key, safe="")
        return self._request("PUT", f"/issue/{key}", {
            "update": {"labels": [{"add": label}]}
        })

    def remove_label(self, key: str, label: str) -> dict:
        key = urllib.parse.quote(key, safe="")
        return self._request("PUT", f"/issue/{key}", {
            "update": {"labels": [{"remove": label}]}
        })

    def transitions(self, key: str) -> list[dict]:
        key = urllib.parse.quote(key, safe="")
        return self._request("GET", f"/issue/{key}/transitions").get("transitions", [])

    def transition(self, key: str, transition_id: str) -> dict:
        key = urllib.parse.quote(key, safe="")
        return self._request("POST", f"/issue/{key}/transitions", {
            "transition": {"id": transition_id}
        })
```

In `JiraSource.mark`, swap the comment body on the blocked path:

```python
        try:
            self.client.add_comment(key, (
                question_comment(summary, cost) if status == "blocked"
                else closing_comment(status, summary, cost)
            ))
        except JiraError as error:
            log.warning("could not comment on %s (%s)", key, error)
```

And add `reopen` to `JiraSource`, after `mark`:

```python
    def reopen(self, task: Task) -> None:
        """Drop the blocked label so an answered task is offered again.

        A failure here is a warning, never a raise: state.db is what actually
        drives the resume, and the label is for humans.
        """
        try:
            self.client.remove_label(task.source_ref, BLOCKED_LABEL)
        except JiraError as error:
            log.warning(
                "could not remove the %s label from %s (%s) -- the ticket will"
                " look blocked in Jira; the task resumes anyway",
                BLOCKED_LABEL, task.source_ref, error,
            )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_jira -v`
Expected: PASS, all cases.

- [ ] **Step 5: Commit**

```bash
git add claudeloop/jira.py tests/test_jira.py
git commit -m "feat: a parked Jira issue asks its question and can be reopened"
```

---

### Task 4: `JiraSource.answer`

Reads the human's reply off the ticket. Ordering is stateless: find ClaudeLoop's newest question comment, then take the first `claudeloop:` comment after it.

**Files:**
- Modify: `claudeloop/jira.py` — `JiraSource`, after `reopen`
- Test: `tests/test_jira.py`

**Interfaces:**
- Consumes: `QUESTION_MARKER`, `QUESTION_HEADING` from Task 3.
- Produces: `JiraSource.answer(self, task: Task) -> str | None` — the answer text with the marker stripped, or `None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_jira.py`:

```python
class JiraAnswerTest(unittest.TestCase):
    task = Task("abc", "OPS-1: thing", "jira", "OPS-1")

    def source_for(self, *bodies: str) -> JiraSource:
        fake = FakeJira({
            "GET /issue/OPS-1/comment": (200, {
                "comments": [{"body": body} for body in bodies]
            }),
        })
        self.addCleanup(fake.close)
        return JiraSource(JiraClient(fake.url, "e@x", "t"), "project = OPS")

    def test_a_marked_comment_after_the_question_is_the_answer(self):
        from claudeloop.jira import QUESTION_HEADING

        source = self.source_for(
            "some earlier chatter",
            f"{QUESTION_HEADING}\n\nQuestion: which currency?",
            "claudeloop: use EUR",
        )

        self.assertEqual(source.answer(self.task), "use EUR")

    def test_an_unmarked_comment_is_not_an_answer(self):
        from claudeloop.jira import QUESTION_HEADING

        source = self.source_for(
            f"{QUESTION_HEADING}\n\nQuestion: which currency?",
            "nice catch, I'll look into it",
        )

        self.assertIsNone(source.answer(self.task))

    def test_a_marked_comment_before_the_question_is_ignored(self):
        from claudeloop.jira import QUESTION_HEADING

        source = self.source_for(
            "claudeloop: this answers an older question",
            f"{QUESTION_HEADING}\n\nQuestion: which currency?",
        )

        self.assertIsNone(source.answer(self.task))

    def test_a_task_that_blocked_twice_reads_the_second_answer(self):
        from claudeloop.jira import QUESTION_HEADING

        source = self.source_for(
            f"{QUESTION_HEADING}\n\nQuestion: which currency?",
            "claudeloop: use EUR",
            f"{QUESTION_HEADING}\n\nQuestion: which rounding?",
            "claudeloop: round half up",
        )

        self.assertEqual(source.answer(self.task), "round half up")

    def test_no_question_comment_means_no_answer(self):
        source = self.source_for("claudeloop: an answer to nothing")

        self.assertIsNone(source.answer(self.task))

    def test_the_marker_alone_is_not_an_answer(self):
        from claudeloop.jira import QUESTION_HEADING

        source = self.source_for(
            f"{QUESTION_HEADING}\n\nQuestion: which currency?",
            "claudeloop:   ",
        )

        self.assertIsNone(source.answer(self.task))

    def test_an_unreachable_jira_means_no_answer_yet_not_a_raise(self):
        fake = FakeJira({"GET /issue/OPS-1/comment": (500, {"errorMessages": ["boom"]})})
        self.addCleanup(fake.close)
        source = JiraSource(JiraClient(fake.url, "e@x", "t", retries=1), "project = OPS")

        with self.assertLogs("claudeloop", level="WARNING"):
            self.assertIsNone(source.answer(self.task))

    def test_a_comment_list_of_the_wrong_shape_means_no_answer(self):
        fake = FakeJira({"GET /issue/OPS-1/comment": (200, {"comments": "nonsense"})})
        self.addCleanup(fake.close)
        source = JiraSource(JiraClient(fake.url, "e@x", "t"), "project = OPS")

        self.assertIsNone(source.answer(self.task))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_jira.JiraAnswerTest -v`
Expected: FAIL with `AttributeError: 'JiraSource' object has no attribute 'answer'`

- [ ] **Step 3: Write the implementation**

In `claudeloop/jira.py`, in `JiraSource`, after `reopen`:

```python
    def answer(self, task: Task) -> str | None:
        """The human's reply to this task's question, if one has been posted.

        The boundary is found in the comment list itself rather than stored:
        ClaudeLoop's newest question comment, then the first comment after it
        carrying QUESTION_MARKER. A task that blocked twice therefore reads
        the second answer, not the first, across restarts and with nothing
        persisted.
        """
        try:
            comments = self.client.comments(task.source_ref).get("comments")
        except JiraError as error:
            # Indistinguishable from "no answer yet", exactly as pending()
            # treats an unreachable Jira as an empty backlog.
            log.warning(
                "could not read comments on %s (%s); trying again later",
                task.source_ref, error,
            )
            return None
        if not isinstance(comments, list):
            return None
        bodies = [
            str(comment.get("body") or "")
            for comment in comments
            if isinstance(comment, dict)
        ]
        asked = -1
        for index, body in enumerate(bodies):
            if body.lstrip().startswith(QUESTION_HEADING):
                asked = index
        if asked == -1:
            return None
        for body in bodies[asked + 1:]:
            stripped = body.strip()
            if stripped.casefold().startswith(QUESTION_MARKER):
                answer = stripped[len(QUESTION_MARKER):].strip()
                if answer:
                    return answer
        return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_jira -v`
Expected: PASS, all cases.

- [ ] **Step 5: Commit**

```bash
git add claudeloop/jira.py tests/test_jira.py
git commit -m "feat: read a human's answer off the Jira ticket"
```

---

### Task 5: The prompt strings

Three strings, each a claim a literal-minded agent will act on. `PROTOCOL` and `NUDGE_PROMPT` both currently assert that nobody can answer a question, which this slice makes false. `ANSWER_PROMPT` and `FRESH_ANSWER_PROMPT` are new.

`ANSWER_PROMPT` carries a duty the orchestrator cannot discharge itself: other tasks run while a task is parked, and each checks out the default branch on the way in, so the resumed session almost certainly finds the working tree off its branch. Only the session knows the branch name.

**Files:**
- Modify: `claudeloop/prompt.py:22-35` (`PROTOCOL`)
- Modify: `claudeloop/loop.py:44-57` (`NUDGE_PROMPT`), and add two constants beside it
- Test: `tests/test_prompt.py`, `tests/test_loop.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `loop.ANSWER_PROMPT` — a format string with one field, `{answer}`.
  - `loop.FRESH_ANSWER_PROMPT` — a format string with two fields, `{task}` and `{answer}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_prompt.py`:

```python
class BlockedWordingTest(unittest.TestCase):
    """These pin specific sentences on purpose. Every live failure this
    project has had traced back to prompt text that could be read two ways,
    so a reworded claim must break a test and get looked at."""

    def test_the_protocol_no_longer_claims_nobody_can_answer(self):
        from claudeloop.prompt import PROTOCOL

        self.assertNotIn("Nobody is watching, so", PROTOCOL)

    def test_the_protocol_says_blocking_parks_the_task_and_costs_time(self):
        from claudeloop.prompt import PROTOCOL

        self.assertIn("parks this task until a human", PROTOCOL)
        self.assertIn("may be hours", PROTOCOL)

    def test_the_protocol_says_the_answer_comes_back_to_this_session(self):
        from claudeloop.prompt import PROTOCOL

        self.assertIn("this same session is resumed with their answer", PROTOCOL)

    def test_the_protocol_still_reserves_blocked_for_a_human_decision(self):
        from claudeloop.prompt import PROTOCOL

        self.assertIn("an ordinary judgment call is not that", PROTOCOL)
```

Append to `tests/test_loop.py`:

```python
class ResumePromptTest(unittest.TestCase):
    def test_the_nudge_no_longer_claims_nobody_can_answer(self):
        from claudeloop.loop import NUDGE_PROMPT

        self.assertNotIn("Nobody is available to answer", NUDGE_PROMPT)

    def test_the_nudge_points_a_stuck_session_at_the_blocked_status(self):
        from claudeloop.loop import NUDGE_PROMPT

        self.assertIn('status "blocked"', NUDGE_PROMPT)
        self.assertIn('"question"', NUDGE_PROMPT)

    def test_the_nudge_still_refuses_a_question_in_the_last_message(self):
        from claudeloop.loop import NUDGE_PROMPT

        self.assertIn("do not end your turn", NUDGE_PROMPT)

    def test_the_answer_prompt_carries_the_answer(self):
        from claudeloop.loop import ANSWER_PROMPT

        rendered = ANSWER_PROMPT.format(answer="use EUR")

        self.assertIn("use EUR", rendered)

    def test_the_answer_prompt_warns_that_the_branch_may_not_be_checked_out(self):
        # The sharpest consequence of parking: other tasks run meanwhile and
        # each checks out the default branch, so the tree has moved. The
        # orchestrator cannot fix this -- it never learns the branch name.
        rendered = loop.ANSWER_PROMPT.format(answer="use EUR")

        self.assertIn("check out the branch you were working on", rendered)
        self.assertIn("commits on it are intact", rendered)

    def test_the_answer_prompt_still_demands_the_result_file(self):
        rendered = loop.ANSWER_PROMPT.format(answer="use EUR")

        self.assertIn("CLAUDELOOP_RESULT", rendered)
        self.assertIn("not your last message", rendered)

    def test_the_fresh_answer_prompt_carries_the_task_and_the_answer(self):
        rendered = loop.FRESH_ANSWER_PROMPT.format(task="do a thing", answer="use EUR")

        self.assertIn("do a thing", rendered)
        self.assertIn("use EUR", rendered)
        self.assertIn("from the beginning", rendered)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_prompt tests.test_loop -v`
Expected: FAIL — `AssertionError: 'Nobody is watching, so' unexpectedly found`, and `AttributeError: module 'claudeloop.loop' has no attribute 'ANSWER_PROMPT'`.

- [ ] **Step 3: Write the implementation**

In `claudeloop/prompt.py`, replace `PROTOCOL`:

```python
PROTOCOL = (
    "You are running unattended under ClaudeLoop. Nobody is watching in real "
    "time, so decide open questions yourself rather than waiting: writing "
    "\"blocked\" parks this task until a human happens to look at it, which "
    "may be hours, and every other task waits behind nothing but your "
    "patience. Reserve it for the narrow case where a human, not you, must "
    "decide something (a missing credential, a choice with no way to infer "
    "the right answer) -- an ordinary judgment call is not that. When you do "
    "block, a human does answer, and this same session is resumed with their "
    "answer. When the task is fully complete, or provably cannot be "
    "completed, write a JSON object to the path in the CLAUDELOOP_RESULT "
    "environment variable with keys \"status\" (one of \"done\", \"failed\", "
    "\"blocked\" -- \"failed\" means you tried and could not finish, "
    "\"blocked\" means a human must decide something before you can), "
    "\"summary\" (one paragraph on what you did), and, when blocked, "
    "\"question\" (the one thing a human must answer). Writing that file is "
    "what ends the task; do not stop without it."
)
```

In `claudeloop/loop.py`, replace `NUDGE_PROMPT`'s last sentence and add the two new constants:

```python
NUDGE_PROMPT = (
    "You ended your turn without writing the result file. The result file "
    "at the path in the CLAUDELOOP_RESULT environment variable -- not your "
    "last message -- is what ends this task; write it now. If the work is "
    "already complete and committed, do not redo it: write status \"done\" "
    "and say so in the summary. If instead you genuinely need a human to "
    "decide something, that is also the result file's job: write status "
    "\"blocked\" with the one thing you need decided in the \"question\" "
    "field, and a human will answer it. Either way, do not end your turn "
    "with a question in your last message -- nobody reads it."
)
"""Sent after a resume with no result file and no rate limit -- a nudge. Two
live smoke-test sessions read the old \"Continue.\" prompt as confirmation
there was nothing left to do and ended their turn with prose instead of the
result file, burning every resume at $0.10 despite finished, committed work.
This names the actual problem instead. S2b reworded the tail: "nobody is
available to answer a question" stopped being true once a human could
answer one, and a session with a real question now has somewhere to put it."""

ANSWER_PROMPT = (
    "A human has answered the question you were blocked on.\n\n"
    "Their answer: {answer}\n\n"
    "Act on that answer and finish the task. Note that time has passed since "
    "you stopped and other tasks have run in this repository meanwhile, so "
    "the working tree is probably no longer on the branch you created: check "
    "out the branch you were working on before you continue -- your commits "
    "on it are intact. When the work is complete, write the result file at "
    "the path in the CLAUDELOOP_RESULT environment variable exactly as "
    "before; that file, not your last message, is what ends the task."
)
"""Sent when resuming a parked task whose question has been answered.

The branch sentence is load-bearing and cannot be replaced by anything the
orchestrator does itself: every task that ran while this one was parked
called reset_to_default_branch on the way in, so the tree has moved, and
ClaudeLoop never learns what the session named its branch. The session does
-- which is the main reason an answered task resumes its original session
rather than starting fresh."""

FRESH_ANSWER_PROMPT = (
    "{task}\n\n"
    "A human has already answered a question about this task: {answer}\n\n"
    "The session that asked that question is no longer available, so start "
    "this task from the beginning, using that answer."
)
"""For the edge case where a parked task has no session to resume -- a
state.db from before this slice, or a task whose runs were pruned. The work
is not lost, only the context."""
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_prompt tests.test_loop -v`
Expected: PASS. If an older test pinned the removed `PROTOCOL` wording, update it to the new sentence rather than deleting the assertion.

- [ ] **Step 5: Commit**

```bash
git add claudeloop/prompt.py claudeloop/loop.py tests/test_prompt.py tests/test_loop.py
git commit -m "feat: tell the session the truth -- a blocked question now gets answered"
```

---

### Task 6: `run_task(resume_with=...)`

The resume path. Reuses the whole existing attempt loop; only the session id, the first prompt, and two skipped setup steps differ.

**Files:**
- Modify: `claudeloop/loop.py:361-399` (`run_task`'s signature and preamble)
- Test: `tests/test_loop.py`

**Interfaces:**
- Consumes: `State.last_session` (Task 1), `ANSWER_PROMPT` / `FRESH_ANSWER_PROMPT` (Task 5).
- Produces: `run_task(cfg, state, source, task, resume_with: str | None = None) -> dict`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_loop.py`. This reuses `MainLoopTest`'s fake-CLI fixture; `FAKE_ARGS_OUT` in `tests/fake_claude.sh` is what makes the invocation's arguments inspectable.

```python
class ResumeWithAnswerTest(unittest.TestCase):
    """Same fake-CLI fixture as MainLoopTest, deliberately duplicated rather
    than inherited: subclassing a TestCase re-runs every parent test."""

    def setUp(self):
        status.reset()
        self.tmp = Path(tempfile.mkdtemp())
        repo = self.tmp / "repo"
        (repo / ".git").mkdir(parents=True)
        self.tasks = self.tmp / "tasks.md"
        self.tasks.write_text("- [ ] first thing\n")
        self.cfg = Config(
            repo=repo,
            tasks_file=self.tasks,
            home=self.tmp / "home",
            max_resumes=3,
        )
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        shutil.copy(Path(__file__).parent / "fake_claude.sh", bin_dir / "claude")
        (bin_dir / "claude").chmod(0o755)
        self.old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{self.old_path}"
        self.args_out = self.tmp / "args.txt"
        os.environ["FAKE_ARGS_OUT"] = str(self.args_out)
        self.state = State(self.cfg.home / "state.db")
        self.source = FileSource(self.tasks)
        self.task = self.source.pending()[0]

    def tearDown(self):
        os.environ["PATH"] = self.old_path
        os.environ.pop("FAKE_ARGS_OUT", None)

    def park(self) -> str:
        """Leave the task parked with a known session, as a blocked run does."""
        self.state.start_task(self.task.id, self.task.source, self.task.source_ref,
                              self.task.text)
        self.state.start_run(self.task.id, "session-that-asked", 0)
        self.state.finish_task(self.task.id, "blocked", "stuck", 0.1, "which currency?")
        return "session-that-asked"

    def args(self) -> str:
        return self.args_out.read_text()

    def test_a_resume_reuses_the_session_that_asked(self):
        session = self.park()

        asyncio.run(loop.run_task(self.cfg, self.state, self.source, self.task,
                                  resume_with="use EUR"))

        self.assertIn(f"--resume {session}", self.args())
        runs = self.state.db.execute(
            "SELECT session_id FROM runs ORDER BY id").fetchall()
        self.assertEqual([row["session_id"] for row in runs], [session, session])

    def test_a_resume_sends_the_answer_prompt(self):
        self.park()

        asyncio.run(loop.run_task(self.cfg, self.state, self.source, self.task,
                                  resume_with="use EUR"))

        self.assertIn("use EUR", self.args())
        self.assertIn("check out the branch you were working on", self.args())

    def test_a_resume_does_not_reset_the_working_tree(self):
        called = []
        with mock.patch.object(loop, "reset_to_default_branch",
                               side_effect=lambda repo: called.append(repo)):
            self.park()
            asyncio.run(loop.run_task(self.cfg, self.state, self.source, self.task,
                                      resume_with="use EUR"))

        self.assertEqual(called, [], "a resume must not check out the default branch")

    def test_a_resume_does_not_re_fire_the_source_start_hook(self):
        started = []
        self.source.start = lambda task: started.append(task)
        self.park()

        asyncio.run(loop.run_task(self.cfg, self.state, self.source, self.task,
                                  resume_with="use EUR"))

        self.assertEqual(started, [])

    def test_a_normal_task_still_resets_the_tree_and_fires_start(self):
        started = []
        self.source.start = lambda task: started.append(task)
        called = []
        with mock.patch.object(loop, "reset_to_default_branch",
                               side_effect=lambda repo: called.append(repo)):
            asyncio.run(loop.run_task(self.cfg, self.state, self.source, self.task))

        self.assertEqual(called, [self.cfg.repo])
        self.assertEqual(started, [self.task])
        self.assertNotIn("--resume", self.args())

    def test_a_parked_task_with_no_session_starts_over_carrying_the_answer(self):
        # A state.db from before this slice, or a task whose runs were pruned.
        self.state.start_task(self.task.id, self.task.source, self.task.source_ref,
                              self.task.text)
        self.state.finish_task(self.task.id, "blocked", "stuck", 0.1, "which currency?")

        asyncio.run(loop.run_task(self.cfg, self.state, self.source, self.task,
                                  resume_with="use EUR"))

        self.assertNotIn("--resume", self.args())
        self.assertIn("use EUR", self.args())
        self.assertIn("first thing", self.args())

    def test_a_resumed_task_reaches_a_verdict_like_any_other(self):
        self.park()

        result = asyncio.run(loop.run_task(self.cfg, self.state, self.source, self.task,
                                           resume_with="use EUR"))

        self.assertEqual(result["status"], "done")
        row = self.state.db.execute("SELECT * FROM tasks WHERE id=?",
                                    (self.task.id,)).fetchone()
        self.assertEqual(row["status"], "done")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_loop.ResumeWithAnswerTest -v`
Expected: FAIL with `TypeError: run_task() got an unexpected keyword argument 'resume_with'`

- [ ] **Step 3: Write the implementation**

In `claudeloop/loop.py`, change `run_task`'s signature and preamble. Everything from `resume_count = 0` onward is unchanged except the two lines noted.

```python
async def run_task(
    cfg: Config,
    state: State,
    source: TaskSource,
    task: Task,
    resume_with: str | None = None,
) -> dict:
    """Run one task to a terminal status, resuming through rate limits.

    `resume_with` is a human's answer to a question this task parked on. It
    continues the session that asked -- which still holds the repository
    context and the name of the branch it created -- rather than starting the
    task over.
    """
    run_dir = cfg.home / "runs" / task.id
    result_path = run_dir / "result.json"
    run_dir.mkdir(parents=True, exist_ok=True)
    # A previous attempt's verdict would otherwise end this one immediately.
    # For an answered task that previous verdict is the `blocked` result the
    # session wrote before it parked, so this matters more, not less.
    result_path.unlink(missing_ok=True)

    # None when there is no session to resume: a state.db from before S2b, or
    # a task whose runs were pruned. The answer still gets through, only the
    # context is lost.
    resumed = state.last_session(task.id) if resume_with is not None else None
    session_id = resumed or str(uuid.uuid4())
    state.start_task(task.id, task.source, task.source_ref, task.text)
    if resume_with is None:
        # Skipped on a resume. reset_to_default_branch exists to stop task N
        # inheriting task N-1's branch, and a resume is not a new task -- it
        # is the same one, continuing. source.start would likewise re-fire
        # transition_start against an issue already in that status; reopen()
        # covers the source-side state instead.
        #
        # Offloaded to a thread: it shells out to git synchronously, and this
        # coroutine must not block the event loop the heartbeat task and the
        # dashboard share.
        await asyncio.to_thread(reset_to_default_branch, cfg.repo)
        # Offloaded for the same reason: under the Jira source this is a
        # blocking HTTP round trip.
        await asyncio.to_thread(source.start, task)
    log.info(
        "task %s %s: %s",
        task.id,
        "resuming with an answer" if resume_with is not None else "starting",
        task.text,
    )
    status_module.set_status(
        state="running",
        task_id=task.id,
        task_text=task.text,
        run_dir=run_dir,
        session_id=session_id,
        attempt=0,
        started_at=time.time(),
        wait_until=None,
        last_error=None,
    )

    resume_count = 0  # plain nudges: no result, no rate limit
    wait_count = 0  # quota waits: bounded separately, see decide()
    cost = 0.0
    if resume_with is None:
        prompt, resume = task.text, False
    elif resumed:
        prompt, resume = ANSWER_PROMPT.format(answer=resume_with), True
    else:
        prompt = FRESH_ANSWER_PROMPT.format(task=task.text, answer=resume_with)
        resume = False
    while True:
```

The rest of the function is untouched.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_loop -v`
Expected: PASS, including every pre-existing case.

- [ ] **Step 5: Commit**

```bash
git add claudeloop/loop.py tests/test_loop.py
git commit -m "feat: run_task can resume a parked task with a human's answer"
```

---

### Task 7: The answered-task scan in `main_loop`

Where the two channels meet. Answered tasks are checked before new pending ones — an answer a human has already given is worth more than starting fresh work — and one per iteration, because the loop is serial.

**Files:**
- Modify: `claudeloop/loop.py` — new module-level helpers before `main_loop`, and `main_loop`'s body (lines 462-527)
- Test: `tests/test_loop.py`

**Interfaces:**
- Consumes: `State.blocked` (Task 1), `TaskSource.reopen`/`answer` (Tasks 2-4), `run_task(resume_with=...)` (Task 6).
- Produces:
  - `loop.read_answer(run_dir: Path) -> str | None` — consumes `answer.json`.
  - `loop.find_answered(cfg, state, source) -> tuple[Task, str] | None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_loop.py`:

```python
class AnsweredScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.run_dir = self.tmp / "runs" / "abcd"
        self.run_dir.mkdir(parents=True)

    def write(self, payload: str) -> Path:
        path = self.run_dir / "answer.json"
        path.write_text(payload)
        return path

    def test_an_answer_file_is_read(self):
        self.write(json.dumps({"answer": "use EUR", "at": 1.0}))

        self.assertEqual(loop.read_answer(self.run_dir), "use EUR")

    def test_an_answer_file_is_consumed_so_it_cannot_fire_twice(self):
        path = self.write(json.dumps({"answer": "use EUR"}))

        loop.read_answer(self.run_dir)

        self.assertFalse(path.exists())
        self.assertIsNone(loop.read_answer(self.run_dir))

    def test_no_answer_file_is_not_an_answer(self):
        self.assertIsNone(loop.read_answer(self.run_dir))

    def test_an_unreadable_answer_file_is_dropped_with_a_warning(self):
        # Left in place it would re-warn on every poll forever.
        path = self.write("{not json")

        with self.assertLogs("claudeloop", level="WARNING"):
            self.assertIsNone(loop.read_answer(self.run_dir))

        self.assertFalse(path.exists())

    def test_an_empty_answer_is_not_an_answer(self):
        self.write(json.dumps({"answer": "   "}))

        self.assertIsNone(loop.read_answer(self.run_dir))


class FindAnsweredTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = Config(
            repo=self.tmp / "repo",
            tasks_file=self.tmp / "tasks.md",
            home=self.tmp / "home",
        )
        self.cfg.tasks_file.write_text("- [ ] alpha\n")
        (self.cfg.repo / ".git").mkdir(parents=True)
        self.state = State(self.cfg.home / "state.db")
        self.source = FileSource(self.cfg.tasks_file)
        self.task = self.source.pending()[0]
        self.state.start_task(self.task.id, self.task.source, self.task.source_ref,
                              self.task.text)
        self.state.finish_task(self.task.id, "blocked", "stuck", 0.1, "which currency?")

    def answer_file(self, text: str) -> None:
        run_dir = self.cfg.home / "runs" / self.task.id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "answer.json").write_text(json.dumps({"answer": text}))

    def test_no_answer_anywhere_finds_nothing(self):
        self.assertIsNone(loop.find_answered(self.cfg, self.state, self.source))

    def test_the_answer_file_wins(self):
        self.answer_file("use EUR")

        found = loop.find_answered(self.cfg, self.state, self.source)

        self.assertIsNotNone(found)
        task, answer = found
        self.assertEqual(task.id, self.task.id)
        self.assertEqual(task.source_ref, self.task.source_ref)
        self.assertEqual(answer, "use EUR")

    def test_the_source_channel_is_asked_when_there_is_no_answer_file(self):
        self.source.answer = lambda task: "from the ticket"

        found = loop.find_answered(self.cfg, self.state, self.source)

        self.assertEqual(found[1], "from the ticket")

    def test_a_task_that_is_not_blocked_is_not_scanned(self):
        self.state.finish_task(self.task.id, "done", "did it", 0.1)
        self.answer_file("use EUR")

        self.assertIsNone(loop.find_answered(self.cfg, self.state, self.source))


class AnsweredMainLoopTest(unittest.TestCase):
    """The whole path, against the fake CLI."""

    def setUp(self):
        status.reset()
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "repo" / ".git").mkdir(parents=True)
        self.tasks = self.tmp / "tasks.md"
        self.cfg = Config(
            repo=self.tmp / "repo",
            tasks_file=self.tasks,
            home=self.tmp / "home",
            max_resumes=3,
        )
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        self.fake = bin_dir / "claude"
        shutil.copy(Path(__file__).parent / "fake_claude.sh", self.fake)
        self.fake.chmod(0o755)
        self.old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{self.old_path}"

    def tearDown(self):
        os.environ["PATH"] = self.old_path

    def blocking_cli(self) -> None:
        self.fake.write_text(
            "#!/usr/bin/env bash\n"
            'printf \'%s\' \'{"status":"blocked","summary":"stuck",'
            '"question":"which currency?"}\' > "$CLAUDELOOP_RESULT"\n'
            "echo '{\"type\":\"result\",\"total_cost_usd\":0.1}'\n"
        )
        self.fake.chmod(0o755)

    def test_a_blocked_task_parks_and_the_next_task_still_runs(self):
        self.blocking_cli()
        self.tasks.write_text("- [ ] ambiguous thing\n- [ ] second thing\n")

        asyncio.run(loop.main_loop(self.cfg, once=True))

        self.assertEqual(self.tasks.read_text(),
                         "- [!] ambiguous thing\n- [!] second thing\n")
        state = State(self.cfg.home / "state.db")
        rows = state.db.execute("SELECT status FROM tasks").fetchall()
        self.assertEqual([row["status"] for row in rows], ["blocked", "blocked"])

    def test_an_answered_task_is_reopened_and_resumed_before_new_work(self):
        self.blocking_cli()
        self.tasks.write_text("- [ ] ambiguous thing\n")
        asyncio.run(loop.main_loop(self.cfg, once=True))
        self.assertEqual(self.tasks.read_text(), "- [!] ambiguous thing\n")

        # A human answers, exactly as the dashboard's POST route will.
        state = State(self.cfg.home / "state.db")
        parked = state.blocked()[0]
        run_dir = self.cfg.home / "runs" / parked["id"]
        (run_dir / "answer.json").write_text(json.dumps({"answer": "use EUR"}))

        # Back to a CLI that finishes.
        shutil.copy(Path(__file__).parent / "fake_claude.sh", self.fake)
        self.fake.chmod(0o755)

        asyncio.run(loop.main_loop(self.cfg, once=True))

        self.assertEqual(self.tasks.read_text(), "- [x] ambiguous thing\n")
        row = State(self.cfg.home / "state.db").db.execute(
            "SELECT status FROM tasks").fetchone()
        self.assertEqual(row["status"], "done")
        self.assertFalse((run_dir / "answer.json").exists(),
                         "the answer must be consumed, not left to fire again")

    def test_a_source_that_cannot_be_reopened_still_resumes(self):
        self.blocking_cli()
        self.tasks.write_text("- [ ] ambiguous thing\n")
        asyncio.run(loop.main_loop(self.cfg, once=True))
        state = State(self.cfg.home / "state.db")
        parked = state.blocked()[0]
        (self.cfg.home / "runs" / parked["id"] / "answer.json").write_text(
            json.dumps({"answer": "use EUR"}))
        shutil.copy(Path(__file__).parent / "fake_claude.sh", self.fake)
        self.fake.chmod(0o755)

        with mock.patch.object(FileSource, "reopen", side_effect=OSError("disk gone")):
            with self.assertLogs("claudeloop", level="WARNING"):
                asyncio.run(loop.main_loop(self.cfg, once=True))

        row = State(self.cfg.home / "state.db").db.execute(
            "SELECT status FROM tasks").fetchone()
        self.assertEqual(row["status"], "done")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_loop.AnsweredScanTest tests.test_loop.FindAnsweredTest tests.test_loop.AnsweredMainLoopTest -v`
Expected: FAIL with `AttributeError: module 'claudeloop.loop' has no attribute 'read_answer'`

- [ ] **Step 3: Write the implementation**

In `claudeloop/loop.py`, before `main_loop`:

```python
def read_answer(run_dir: Path) -> str | None:
    """The dashboard's answer for a parked task, consumed as it is read.

    Unlinked whatever it contained: a file left in place would resume the
    task a second time on the next poll, or -- if it is malformed -- warn on
    every poll forever.
    """
    path = run_dir / "answer.json"
    try:
        raw = path.read_text()
    except OSError:
        return None
    path.unlink(missing_ok=True)
    try:
        answer = str(json.loads(raw)["answer"]).strip()
    except (json.JSONDecodeError, TypeError, KeyError) as error:
        log.warning("ignoring an unreadable answer file at %s (%s)", path, error)
        return None
    return answer or None


def find_answered(cfg: Config, state: State, source: TaskSource) -> tuple[Task, str] | None:
    """The first parked task with an answer waiting, through either channel.

    Blocking on both counts -- sqlite3 on this connection and, under the Jira
    source, one HTTP round trip per parked task -- so the loop calls this
    through asyncio.to_thread. The Jira reads are only paid while something
    is actually parked.
    """
    for row in state.blocked():
        task = Task(row["id"], row["text"], row["source"], row["source_ref"])
        answer = read_answer(cfg.home / "runs" / task.id) or source.answer(task)
        if answer:
            return task, answer
    return None


def _reopen(source: TaskSource, task: Task) -> None:
    """Undo the source's blocked mark. Never raises: state.db is what drives
    the resume, and the mark is only for humans."""
    try:
        source.reopen(task)
    except Exception as error:
        log.warning("could not reopen task %s in its source (%s)", task.id, error)
```

Then replace the body of `main_loop`'s `while True:` down to the `try:` — everything from the `try:` onward is unchanged:

```python
        while True:
            try:
                answered = await asyncio.to_thread(find_answered, cfg, state, source)
            except Exception:
                # A locked database or a Jira fault must not stop the loop
                # from picking up ordinary pending work.
                log.exception("could not check for answers to parked tasks")
                answered = None
            if answered is not None:
                # Before new pending work on purpose: an answer a human has
                # already given is worth more than starting something fresh.
                # One per iteration, because the loop is serial.
                task, resume_with = answered
                await asyncio.to_thread(_reopen, source, task)
            else:
                pending = await asyncio.to_thread(source.pending)
                if not pending:
                    status_module.set_status(**IDLE_FIELDS)
                    if once:
                        return
                    await asyncio.sleep(POLL_S)
                    continue
                # Re-read after every task: the file may have been edited
                # meanwhile.
                task, resume_with = pending[0], None
                # Published for the dashboard: web reads this off the
                # snapshot rather than re-reading the task source itself,
                # since under the Jira source that would be a network call on
                # the web thread. As a result the list is only as fresh as
                # the start of the current task, not live.
                status_module.set_status(
                    pending=tuple((t.id, t.text) for t in pending)
                )
            try:
                await run_task(cfg, state, source, task, resume_with=resume_with)
            except Exception as error:
```

Update `main_loop`'s docstring to match:

```python
    """Run pending tasks one at a time, forever.

    A task parked on a question is checked for an answer before new work is
    polled for, so an answered task resumes ahead of starting something
    fresh.

    `once` drains the tasks pending right now -- including any that have been
    answered -- and returns, for tests.
    """
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_loop -v`
Expected: PASS, including every pre-existing case.

- [ ] **Step 5: Run the whole suite**

Run: `python -m unittest discover -s tests -t .`
Expected: PASS. The orchestrator half of the slice is now complete.

- [ ] **Step 6: Commit**

```bash
git add claudeloop/loop.py tests/test_loop.py
git commit -m "feat: a parked task resumes when its answer arrives, through either channel"
```

---

### Task 8: `POST /api/tasks/<id>/answer`

The first route in the project that writes anything, deliberately breaking S2a's read-only rule ahead of S5. It writes a file, not `status.py` and not the loop's database, so the web thread never becomes the second writer `status.py`'s docstring warns about.

**Files:**
- Modify: `claudeloop/web.py` — a constant near `TASK_ID_RE` (~line 30), and `do_POST` / `_answer` / `_is_blocked` on `Handler` after `do_GET` (~line 239)
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: nothing from earlier tasks at runtime; the file it writes is read by `loop.read_answer` (Task 7).
- Produces: `POST /api/tasks/<task_id>/answer`, body `{"answer": "..."}`, writing `~/.claudeloop/runs/<task_id>/answer.json` as `{"answer": str, "at": float}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_web.py`:

```python
class AnswerRouteTest(WebTestBase):
    def setUp(self):
        super().setUp()
        self.state = State(self.cfg.home / "state.db")
        self.task_id = task_id("ambiguous thing")
        self.state.start_task(self.task_id, "file", "- [ ] ambiguous thing",
                              "ambiguous thing")
        self.state.finish_task(self.task_id, "blocked", "stuck", 0.1, "which currency?")

    def post(self, path: str, body, content_type="application/json", token=None):
        """Returns (status, decoded body). http.client, not urllib: urllib
        raises on a 4xx and the status code is the thing under test."""
        url = path + (("?token=" + urllib.parse.quote(token)) if token else "")
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            raw = body if isinstance(body, (bytes, str)) else json.dumps(body)
            headers = {"Content-Type": content_type} if content_type else {}
            conn.request("POST", url, raw, headers)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def answer_file(self) -> Path:
        return self.cfg.home / "runs" / self.task_id / "answer.json"

    def test_an_answer_is_written_where_the_loop_looks_for_it(self):
        code, _ = self.post(f"/api/tasks/{self.task_id}/answer", {"answer": "use EUR"})

        self.assertEqual(code, 200)
        payload = json.loads(self.answer_file().read_text())
        self.assertEqual(payload["answer"], "use EUR")
        self.assertIsInstance(payload["at"], float)

    def test_the_answer_is_stripped(self):
        self.post(f"/api/tasks/{self.task_id}/answer", {"answer": "  use EUR \n"})

        self.assertEqual(json.loads(self.answer_file().read_text())["answer"], "use EUR")

    def test_a_task_that_is_not_blocked_is_refused(self):
        self.state.finish_task(self.task_id, "done", "did it", 0.1)

        code, _ = self.post(f"/api/tasks/{self.task_id}/answer", {"answer": "use EUR"})

        self.assertEqual(code, 409)
        self.assertFalse(self.answer_file().exists())

    def test_an_unknown_task_is_refused(self):
        code, _ = self.post("/api/tasks/" + ("0" * 16) + "/answer", {"answer": "x"})

        self.assertEqual(code, 409)

    def test_a_bad_task_id_never_reaches_the_filesystem(self):
        code, _ = self.post("/api/tasks/..%2f..%2fetc/answer", {"answer": "x"})

        self.assertEqual(code, 404)

    def test_a_form_content_type_is_refused(self):
        # A cross-origin fetch sending application/json triggers a CORS
        # preflight this server never answers, and an HTML form cannot set
        # that content type. This is what stops a drive-by submission.
        code, _ = self.post(f"/api/tasks/{self.task_id}/answer",
                            "answer=use+EUR",
                            content_type="application/x-www-form-urlencoded")

        self.assertEqual(code, 415)
        self.assertFalse(self.answer_file().exists())

    def test_an_empty_answer_is_refused(self):
        code, _ = self.post(f"/api/tasks/{self.task_id}/answer", {"answer": "   "})

        self.assertEqual(code, 400)
        self.assertFalse(self.answer_file().exists())

    def test_a_body_that_is_not_an_answer_object_is_refused(self):
        code, _ = self.post(f"/api/tasks/{self.task_id}/answer", {"nope": "x"})

        self.assertEqual(code, 400)

    def test_an_oversized_answer_is_refused(self):
        code, _ = self.post(f"/api/tasks/{self.task_id}/answer",
                            {"answer": "x" * (web.ANSWER_MAX_BYTES + 1)})

        self.assertEqual(code, 413)
        self.assertFalse(self.answer_file().exists())

    def test_an_unknown_post_route_is_a_404(self):
        code, _ = self.post("/api/nonsense", {"answer": "x"})

        self.assertEqual(code, 404)


class AnswerRouteTokenTest(AnswerRouteTest):
    token = "s3cret"

    def test_the_answer_route_needs_the_token(self):
        code, _ = self.post(f"/api/tasks/{self.task_id}/answer", {"answer": "use EUR"})

        self.assertEqual(code, 403)
        self.assertFalse(self.answer_file().exists())

    def test_the_right_token_is_accepted_on_the_answer_route(self):
        code, _ = self.post(f"/api/tasks/{self.task_id}/answer",
                            {"answer": "use EUR"}, token="s3cret")

        self.assertEqual(code, 200)
```

Note: `AnswerRouteTokenTest` subclasses `AnswerRouteTest` deliberately — re-running the parent's cases under a token is the point, and every one of them must be given `token="s3cret"` to keep passing. Rather than thread a token through each, override `post` in the subclass to default the token:

```python
class AnswerRouteTokenTest(AnswerRouteTest):
    token = "s3cret"

    def post(self, path, body, content_type="application/json", token="s3cret"):
        return super().post(path, body, content_type, token)

    def test_the_answer_route_needs_the_token(self):
        code, _ = super().post(f"/api/tasks/{self.task_id}/answer",
                               {"answer": "use EUR"}, token=None)

        self.assertEqual(code, 403)
        self.assertFalse(self.answer_file().exists())
```

Use that version. Check the imports at the top of `tests/test_web.py` — it needs `http.client`, `json`, `urllib.parse`, `Path`, `State`, `task_id` and `web`. Add whichever are missing.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_web.AnswerRouteTest -v`
Expected: FAIL — the server answers 501 "Unsupported method ('POST')".

- [ ] **Step 3: Write the implementation**

In `claudeloop/web.py`, beside `TASK_ID_RE`:

```python
ANSWER_MAX_BYTES = 8 * 1024
"""Cap on a human's answer. It becomes part of an argv element on the resume,
and Linux caps a single argument at 128 KiB -- the composed system prompt is
already in there."""
```

On `Handler`, after `do_GET`:

```python
    def do_POST(self) -> None:
        """The one route in this project that writes anything.

        It writes a file under the run directory -- never status.py, never
        the loop's database -- so the web thread does not become the second
        writer to set_status() that status.py's docstring warns about.
        """
        if not self._host_allowed():
            self._json(403, {"error": "bad host"})
            return
        parsed = urlparse(self.path)
        if not self._authorized(parsed.query):
            self._json(403, {"error": "bad or missing token"})
            return
        route = parsed.path
        if route.startswith("/api/tasks/") and route.endswith("/answer"):
            self._answer(route[len("/api/tasks/") : -len("/answer")])
        else:
            self._json(404, {"error": "not found"})

    def _answer(self, task_id: str) -> None:
        if self.headers.get_content_type() != "application/json":
            # A cross-origin fetch with this content type triggers a CORS
            # preflight this server never answers, so the browser does not
            # send the POST at all; an HTML form cannot set it either. With
            # the Host check above, that is what stops a drive-by submission
            # from another page at the loopback default, where web_token is
            # empty by design.
            self._json(415, {"error": "expected application/json"})
            return
        if not TASK_ID_RE.match(task_id):
            # Interpolated into a filesystem path below: the same traversal
            # guard api_task already applies.
            self._json(404, {"error": "no such task"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._json(400, {"error": "bad content length"})
            return
        if length <= 0 or length > ANSWER_MAX_BYTES:
            self._json(413, {"error": f"the answer must be 1..{ANSWER_MAX_BYTES} bytes"})
            return
        try:
            answer = str(json.loads(self.rfile.read(length))["answer"]).strip()
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError, KeyError):
            self._json(400, {"error": 'expected a JSON object with an "answer"'})
            return
        if not answer:
            self._json(400, {"error": "the answer is empty"})
            return
        if not self._is_blocked(task_id):
            self._json(409, {"error": "that task is not waiting for an answer"})
            return
        run_dir = self.server.cfg.home / "runs" / task_id
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
            # Written then renamed: the loop reads this file on its own
            # thread and must never see half of one.
            tmp = run_dir / "answer.json.tmp"
            tmp.write_text(json.dumps({"answer": answer, "at": time.time()}))
            tmp.replace(run_dir / "answer.json")
        except OSError as error:
            self._json(500, {"error": f"could not record the answer: {error}"})
            return
        self._json(200, {"ok": True})

    def _is_blocked(self, task_id: str) -> bool:
        """Whether that task is actually parked on a question. Keeps stray
        files out of arbitrary run directories."""
        db = _connect(self.server.cfg)
        if db is None:
            return False
        try:
            row = db.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        finally:
            db.close()
        return row is not None and row["status"] == "blocked"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_web -v`
Expected: PASS, all cases.

- [ ] **Step 5: Commit**

```bash
git add claudeloop/web.py tests/test_web.py
git commit -m "feat: the dashboard can answer a parked task's question"
```

---

### Task 9: The answer box on the dashboard

The frontend half. `static/index.html` already renders a blocked task's question in `completedRow`; this adds a form under it. No JS test infrastructure exists in this project and none is being added — verification is by eye here and by the live smoke test at the end.

**Files:**
- Modify: `claudeloop/static/index.html` — CSS beside `.done-item[data-status="blocked"]` (~line 355), and `completedRow` (~line 775)

**Interfaces:**
- Consumes: `POST /api/tasks/<id>/answer` (Task 8), the existing `url()`, `el()` and `$()` helpers.
- Produces: nothing other tasks depend on.

- [ ] **Step 1: Add the styles**

After the `.done-item[data-status="blocked"] .mark` rule:

```css
.answer { display: flex; flex-direction: column; gap: .5rem; margin-top: .6rem; }
.answer textarea {
  width: 100%; box-sizing: border-box; resize: vertical;
  font: inherit; color: inherit; background: var(--bg);
  border: 1px solid var(--line); border-radius: 6px; padding: .5rem;
}
.answer button {
  align-self: flex-start; font: inherit; cursor: pointer;
  color: inherit; background: var(--bg);
  border: 1px solid var(--line); border-radius: 6px; padding: .35rem .8rem;
}
.answer button[disabled] { opacity: .5; cursor: default; }
```

If any of `--bg`, `--line` is not the name this file actually uses, use the neighbouring rules' variables instead — match the file, do not introduce new custom properties.

- [ ] **Step 2: Add the form**

Before `function completedRow(task) {`:

```js
function answerBox(task) {
  const form = el("form", "answer");
  const input = el("textarea");
  input.rows = 3;
  input.placeholder = "Answer this, and the task picks up where it stopped…";
  const button = el("button", null, "Send answer");
  button.type = "submit";
  const note = el("p", "note");
  form.append(input, button, note);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const answer = input.value.trim();
    if (!answer) return;
    button.disabled = true;
    note.textContent = "Sending…";
    try {
      const response = await fetch(
        url("/api/tasks/" + encodeURIComponent(task.id) + "/answer"),
        {
          method: "POST",
          // Not decoration: this content type is what forces a CORS
          // preflight, and the server refuses anything else.
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ answer }),
        });
      if (!response.ok) throw new Error("HTTP " + response.status);
      note.textContent = "Answer recorded. The task resumes when the loop is free.";
      input.disabled = true;
    } catch (e) {
      note.textContent = "Could not send that: " + e.message;
      button.disabled = false;
    }
  });
  return form;
}
```

Inside `completedRow`, after the `if (task.question) ... else if (task.summary) ...` pair and before the `stamp` line:

```js
  if (task.status === "blocked") body.appendChild(answerBox(task));
```

Note on the existing `completedKey`: it is `id:status` per task, so a re-render — which would discard half-typed text — only happens when a task's status actually changes. Do not widen that key.

- [ ] **Step 3: Verify by eye**

```bash
python - <<'PY'
import json, tempfile, time
from pathlib import Path
from claudeloop.config import Config
from claudeloop.state import State
from claudeloop import web

tmp = Path(tempfile.mkdtemp())
(tmp / "repo" / ".git").mkdir(parents=True)
(tmp / "tasks.md").write_text("- [!] ambiguous thing\n")
cfg = Config(repo=tmp / "repo", tasks_file=tmp / "tasks.md", home=tmp / "home",
             web_host="127.0.0.1", web_port=8899)
state = State(cfg.home / "state.db")
state.start_task("a" * 16, "file", "- [ ] ambiguous thing", "ambiguous thing")
state.finish_task("a" * 16, "blocked", "I stopped.", 0.1, "Which currency?")
web.serve(cfg)
print("http://127.0.0.1:8899 -- open the blocked task in Completed")
time.sleep(600)
PY
```

Open it, expand the blocked task, confirm the question and the box render, type an answer and submit. Expect "Answer recorded." and a file at `<tmp>/home/runs/aaaaaaaaaaaaaaaa/answer.json`. Check it in both themes and at a phone width.

- [ ] **Step 4: Run the suite**

Run: `python -m unittest discover -s tests -t .`
Expected: PASS. `tests/test_web.py` asserts the index is served; nothing here should move it.

- [ ] **Step 5: Commit**

```bash
git add claudeloop/static/index.html
git commit -m "feat: answer a parked task's question from the dashboard"
```

---

### Task 10: The read-only rule, and the docs that assert it

Three documents state, as a hard constraint, something this slice deliberately made false. Leaving them is worse than not writing them: the next session reads `CLAUDE.md` as binding.

**Files:**
- Modify: `CLAUDE.md` — the "The dashboard is read-only" hard constraint
- Modify: `claudeloop/status.py:1-25` — the module docstring
- Modify: `README.md` — the dashboard section
- Modify: `ROADMAP.md` — S2b to merged, plus the open issues this slice adds

- [ ] **Step 1: Correct the hard constraint**

In `CLAUDE.md`, replace the read-only bullet:

```markdown
- **The dashboard is read-only, with one exception.** No route mutates the
  loop's state, the task file, or the database. S2b broke the rule
  deliberately and narrowly: `POST /api/tasks/<id>/answer` writes one file,
  `runs/<id>/answer.json`, which the loop reads and consumes. Any further
  write needs the same justification — S5's setup wizard is the next one.
- **The web layer is never a second writer to `status.py`.** `set_status` is
  a read-modify-write and is safe only because exactly one thread calls it.
  S2b's answer route writes a file rather than calling it, which dodges that
  hazard rather than solving it. A future route that needs to call
  `set_status` from the web thread must add a lock first.
```

- [ ] **Step 2: Correct `status.py`'s docstring**

In `claudeloop/status.py`, replace the paragraph beginning "That covers readers":

```python
That covers readers. Writing is safe today for a narrower reason: exactly one
thread -- the loop -- ever calls set_status(). set_status() is a
read-modify-write (read `current`, dataclasses.replace() it, write the
result back), and a read-modify-write is atomic only under a single writer.
The moment a second writer exists, two concurrent calls can each read the
same `current`, compute their own replace(), and the second write silently
clobbers the first's changes. That needs an actual lock; nothing here
provides one.

S2b -- the human answering a parked task's question from the web thread --
was the case this warning was written for, and it did not become the second
writer: the answer route writes a file the loop picks up instead of calling
set_status(). The hazard is dodged, not solved. It is still live for the
next route that wants to write from the web thread.
```

- [ ] **Step 3: Document the channel for operators**

In `README.md`'s dashboard section, add:

```markdown
### Answering a blocked task

A session that hits something only you can decide writes a `blocked` result
with a question instead of guessing. The task parks — it does not stop the
loop, which carries on with the rest of the backlog — and shows up in
**Completed** with a `?` and an answer box.

Answer it there, or, when the task came from Jira, reply on the ticket with a
comment starting with `claudeloop:`:

```
claudeloop: use the staging-eu database, not staging-us
```

Either way the loop picks the task back up before it starts anything new,
resuming the same session, so it still knows what it had done. There is no
deadline: a parked task waits indefinitely.
```

- [ ] **Step 4: Update the roadmap**

In `ROADMAP.md`: move S2b to **merged** in the slices table, move its section from "Next" to "Built" with a description of what was actually built (including anything that turned out differently from the spec), and add to "Open issues carried across slices":

```markdown
- A task parked on a question holds a branch in the target repository while
  other tasks run. The branch and its commits survive, but the next task's
  `reset_to_default_branch` moves the tree off it, so `ANSWER_PROMPT` has to
  tell the resumed session to check its own branch out again. If the parked
  session left uncommitted changes, that checkout fails and the *next* task
  runs on the parked task's branch.
- `JiraSource.answer` reads the full comment list on every poll for every
  parked task, unpaginated — the same limitation `pending()` carries.
- The dashboard's answer box has no draft persistence. A closed tab loses
  typed text.
```

- [ ] **Step 5: Run the whole suite and commit**

Run: `python -m unittest discover -s tests -t .`
Expected: PASS.

```bash
git add CLAUDE.md README.md ROADMAP.md claudeloop/status.py
git commit -m "docs: the dashboard writes exactly one thing now, and it is written down"
```

---

### Task 11: The live smoke test

Not optional. Four of this project's slices have run one and three surfaced defects the passing suite could not catch. This slice is exactly the shape that has burned it before: new prompt text, plus a state machine that only misbehaves on the second task.

**Files:** none — this is a run, followed by fixes to whatever it finds.

- [ ] **Step 1: Set up a scratch target**

```bash
mkdir -p /tmp/claudeloop-smoke/repo && cd /tmp/claudeloop-smoke/repo
git init -q && git config --local commit.gpgsign false
printf '# scratch\n' > README.md && git add README.md
git commit -qm "initial"
```

Write a `config.toml` (mode 0600 — `load_config` refuses anything readable beyond its owner) with `repo` pointing at that directory, `model = "haiku"`, and a `tasks_file` **outside** the repo. Two tasks:

```markdown
- [ ] Add a LICENSE file. Use the MIT licence, with the copyright holder and year taken from whatever this repository already states — do not guess or pick one yourself, and if the repository does not state them, that is a decision only a human can make.
- [ ] Add a .gitignore covering Python build artefacts.
```

The first is written to block: the repository states no copyright holder, and the protocol names exactly that case.

- [ ] **Step 2: Run it and answer through the dashboard**

Start `python -m claudeloop`. Watch for the first task parking with a question rather than guessing a name. Confirm the loop **moves on to the second task** instead of idling. Then answer the first from the dashboard and confirm:

- the task returns to `- [ ]` in the checklist, then to `- [x]` when it finishes
- the resumed run reuses the first run's `session_id` (`sqlite3 ~/.claudeloop/state.db 'SELECT task_id, session_id, exit_reason FROM runs ORDER BY id'`)
- the session checks its branch back out rather than starting the work over — the second task's `reset_to_default_branch` will have moved the tree
- `runs/<id>/answer.json` is gone afterwards

- [ ] **Step 3: Run it again against Jira**

Same scratch repo, `source = "jira"`, two tickets, one written to block the same way. Answer by posting `claudeloop: <answer>` on the ticket. Confirm the question comment appears with the reply instruction, that the `claudeloop-blocked` label is removed on reopen, and that the task finishes and gets `claudeloop-done`.

- [ ] **Step 4: Watch for the failure this design is trying to prevent**

The specific risk in this slice is a session that, now told questions reach a human, starts asking ones it should have decided itself. Read both runs' questions. If either is an ordinary judgment call, `PROTOCOL`'s wording is not strong enough and needs another pass — with its pinning test updated, and this whole smoke test re-run, because prompt fixes are exactly the kind that come back differently broken.

- [ ] **Step 5: Fix what it found, then finish the branch**

Each fix gets a covering test that fails without it. Then run the whole suite, update `ROADMAP.md` with anything the run taught, and use **superpowers:finishing-a-development-branch** to merge.

---

## Self-Review

**Spec coverage.** Park-and-continue → Task 7. Resume the original session → Task 6. Skip the branch reset and `start` → Task 6. Two answer channels → Tasks 4 and 8. Answer crosses as a file → Tasks 7 and 8. `status.py` hazard dodged and documented → Task 10. Source marks blocked, third verb undoes it → Tasks 2, 3, 7. Marker prefix and stateless ordering → Task 4. `question_comment` → Task 3. No timeout → no code; asserted in the README text in Task 10. `State.blocked`/`last_session` → Task 1. Prompt strings → Task 5. No-session fallback → Task 6. Every error-handling case in the spec has a test: `reopen` failure (Tasks 3, 7), malformed `answer.json` (Task 7), `JiraSource.answer` raising (Task 4), missing session (Task 6), blocking twice (Task 4). Live smoke test → Task 11.

**Type consistency.** `reopen(task) -> None` and `answer(task) -> str | None` are used with those signatures in Tasks 2, 3, 4 and 7. `read_answer(run_dir)` takes the run directory, not the file — as called in `find_answered`. `find_answered` returns `tuple[Task, str] | None`, unpacked as `task, resume_with` in Task 7. `question_comment(summary, cost)` — two arguments, no separate `question`, because `read_result` already appends the question to the summary; used that way in Task 3. `ANSWER_PROMPT` has one field, `FRESH_ANSWER_PROMPT` has two; both rendered accordingly in Task 6.

**Known ordering constraint.** Task 7's `find_answered` calls `source.answer`, so Tasks 2-4 must land before it. Task 6 uses `State.last_session` from Task 1. Otherwise the order is the natural one.
