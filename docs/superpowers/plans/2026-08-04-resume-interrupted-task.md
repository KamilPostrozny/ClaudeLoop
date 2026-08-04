# S9 — Resume an interrupted task: TDD plan

Spec: `docs/superpowers/specs/2026-08-04-claudeloop-resume-interrupted-task-design.md`

## Step 1 — `opening_prompt`, the pure selector

**Test** (`tests/test_loop.py`, new `OpeningPromptTest`): every combination of
`(resume_with, resumed, interrupted)` returns the right prompt and the right
`resume` flag, asserted against the whole rendered string rather than a
substring.

```python
class OpeningPromptTest(unittest.TestCase):
    def test_a_first_attempt_sends_the_task_text_without_resuming(self):
        self.assertEqual(
            loop.opening_prompt("do the thing", None, None, False),
            ("do the thing", False),
        )

    def test_an_answered_task_with_a_session_resumes_with_the_answer(self):
        self.assertEqual(
            loop.opening_prompt("do the thing", "use EUR", "sess", False),
            (loop.ANSWER_PROMPT.format(answer="use EUR"), True),
        )

    def test_an_answered_task_without_a_session_starts_over_with_the_answer(self):
        self.assertEqual(
            loop.opening_prompt("do the thing", "use EUR", None, False),
            (loop.FRESH_ANSWER_PROMPT.format(task="do the thing", answer="use EUR"), False),
        )

    def test_an_interrupted_task_with_a_session_resumes_it(self):
        self.assertEqual(
            loop.opening_prompt("do the thing", None, "sess", True),
            (loop.INTERRUPTED_PROMPT, True),
        )

    def test_an_interrupted_task_without_a_session_starts_over_warned(self):
        self.assertEqual(
            loop.opening_prompt("do the thing", None, None, True),
            (loop.FRESH_INTERRUPTED_PROMPT.format(task="do the thing"), False),
        )

    def test_an_answer_outranks_an_interruption(self):
        # A task can be both: parked, answered, then the process died before
        # the resumed session got anywhere. The answer is the newer fact.
        self.assertEqual(
            loop.opening_prompt("do the thing", "use EUR", "sess", True),
            (loop.ANSWER_PROMPT.format(answer="use EUR"), True),
        )
```

**Code**: add `INTERRUPTED_PROMPT` and `FRESH_INTERRUPTED_PROMPT` to
`loop.py`, add `opening_prompt`, and replace the inline `if/elif/else` in
`run_task` with a call to it. `run_task` still passes `interrupted=False` at
this step, so behaviour is unchanged and the existing suite must stay green.

## Step 2 — the prompt strings say the load-bearing things

**Test** (extend `PromptSelectionTest`): each new prompt names the result file
and its environment variable, tells the session not to redo finished work, and
is not equal to any of the other prompts. Asserted on whole sentences, per the
S7 failure where substring assertions passed on a sentence with no verb.

**Code**: none beyond step 1 if the wording is already right; this step is
where the wording gets fixed if it is not.

## Step 3 — `run_task` detects an interrupted row

**Test** (new `ResumeInterruptedTest`, same fake-CLI fixture as
`ResumeWithAnswerTest`, with an `interrupt()` helper that leaves the row the
way a dead process does):

```python
    def interrupt(self) -> str:
        """Leave the task exactly as a killed process leaves it: a run row
        with a session id, and a row State.__init__ has since flipped to
        'interrupted'."""
        self.state.start_task(...)
        self.state.start_run(self.task.id, "session-that-died", 0)
        self.state.db.execute(
            "UPDATE tasks SET status='interrupted' WHERE id=?", (self.task.id,))
        return "session-that-died"
```

- `test_an_interrupted_task_resumes_the_session_that_died` — `--resume
  session-that-died` in the args, and both run rows carry that session id.
- `test_an_interrupted_task_sends_the_interrupted_prompt` — the args carry a
  whole sentence from `INTERRUPTED_PROMPT`.
- `test_an_interrupted_task_does_not_re_fire_the_source_start_hook` — mirrors
  the answered-resume test at `test_loop.py:1041`.
- `test_an_interrupted_task_with_no_prior_session_starts_over_warned` — a
  row flipped to `interrupted` with its runs deleted gets a fresh session id
  and `FRESH_INTERRUPTED_PROMPT`.
- `test_a_first_attempt_is_untouched` — a task with no row at all still gets
  a fresh session, `task.text`, and `source.start` fired once. Guards the
  common path against the new lookup.
- `test_an_interrupted_task_returns_to_the_same_worktree` — `ensure` is
  called with the same task id, so the dead session's tree comes back.

**Code** in `run_task`, before `start_task` (which is `INSERT OR REPLACE` and
would erase the status):

```python
    row = state.task(task.id) if resume_with is None else None
    interrupted = row is not None and row["status"] == "interrupted"
    resumed = (
        state.last_session(task.id)
        if resume_with is not None or interrupted
        else None
    )
```

and widen the `source.start` guard from `if resume_with is None:` to
`if resume_with is None and not interrupted:`.

## Step 4 — end to end through `main_loop`

**Test** (new case in `MainLoopTest`'s style): a state.db carrying an
`interrupted` row for the only pending task, a fake CLI that records its
argv, one `main_loop(cfg, once=True)` — the run resumes rather than starting
fresh, and the task reaches a verdict.

**Code**: none expected. This is the test that would have caught the join
being missing.

## Step 5 — full suite, review, roadmap

`python -m unittest discover -s tests -t .` green. Review the whole branch.
`ROADMAP.md`: S9 into the slice table, a `### S9` section under Built, and the
open-issues entry about config being read once at startup.

## Step 6 — live smoke test

Scratch repository, `model = "haiku"`, **two tasks**. Let the first task get
underway, `SIGKILL` the loop, restart it, and watch what the second run does:

- the same session id on the `--resume`,
- the session finding its own earlier commits rather than redoing them,
- the second task running normally afterwards, on its own branch.

Non-negotiable per `CLAUDE.md`: this slice changes prompt text, and prompt
text is exactly what a fixture suite cannot check.
