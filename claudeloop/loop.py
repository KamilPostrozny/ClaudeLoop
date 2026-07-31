"""The decision state machine and the orchestration around it."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import session
from . import status as status_module
from . import web
from .config import DEFAULT_CONFIG, Config, load_config
from .source import FileSource, Task, TaskSource
from .state import State

RESET_PAD_S = 30
"""Slack past resetsAt, to absorb clock skew between this host and the API."""

FALLBACK_WAIT_S = 300
"""Used when a blocking rate-limit event arrives without a resetsAt, or with
one that isn't a number -- both leave us with no real signal for when the
quota resets."""

MAX_WAIT_S = 8 * 24 * 3600
"""Clamp on a single sleep, longer than any real reset window (weekly is the
longest). A malformed resetsAt -- a millisecond timestamp, say -- would
otherwise sleep for centuries silently instead of retrying."""

VALID_STATUSES = ("done", "failed", "blocked")


@dataclass(frozen=True)
class ReadResult:
    """The session wrote a result file; take its verdict."""


@dataclass(frozen=True)
class Resume:
    """Run the session again. wait_until is a unix time, 0 meaning now."""

    wait_until: float = 0.0


@dataclass(frozen=True)
class Fail:
    reason: str


def blocking_reset(events: list[dict]) -> float | None:
    """The resetsAt of the most recent rate_limit_event, if it was blocking.

    These events arrive continuously, including while the quota is fine, so
    only the last one describes the state the run ended in.
    """
    for event in reversed(events):
        if event.get("type") != "rate_limit_event":
            continue
        info = event.get("rate_limit_info") or {}
        if info.get("status") == "allowed":
            return None
        try:
            # resetsAt is documented as a unix timestamp in seconds; this is
            # defence in depth against a malformed or missing value, not a
            # fix for an unknown unit.
            return float(info.get("resetsAt"))
        except (TypeError, ValueError):
            return time.time() + FALLBACK_WAIT_S
    return None


def latest_rate_limit(events: list[dict]) -> dict | None:
    """The most recent quota reading in a run, for the dashboard's gauge."""
    for event in reversed(events):
        if event.get("type") == "rate_limit_event":
            info = event.get("rate_limit_info")
            return info if isinstance(info, dict) else None
    return None


def decide(
    events: list[dict],
    result_exists: bool,
    resume_count: int,
    max_resumes: int,
    wait_count: int,
    max_waits: int,
) -> ReadResult | Resume | Fail:
    """Decide what to do after a claude invocation exits.

    `events` is the stream from the invocation that just exited, not the task's
    whole history: a rate-limit event from an earlier attempt must not
    re-trigger a wait after a later attempt exits for another reason.

    Nudges and quota waits are bounded separately: a wait is not a failure to
    make progress, only a nudge is. A task purely waiting out its quota keeps
    waiting past `max_resumes`, bounded instead by the much larger
    `max_waits`; a task making no progress on its own still fails at
    `max_resumes` regardless of how few waits it has used.
    """
    if result_exists:
        return ReadResult()
    reset_at = blocking_reset(events)
    if reset_at is not None:
        if wait_count >= max_waits:
            return Fail("no_result")
        return Resume(wait_until=reset_at + RESET_PAD_S)
    if resume_count >= max_resumes:
        return Fail("no_result")
    return Resume()


def read_result(path: Path) -> dict:
    """Read the session's result file, tolerating anything it might contain."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        return {"status": "failed", "summary": f"unreadable result file: {error}"}
    if not isinstance(data, dict):
        return {"status": "failed", "summary": f"result file is not an object: {data!r:.200}"}
    status = data.get("status")
    if status not in VALID_STATUSES:
        return {"status": "failed", "summary": f"result file has bad status {status!r}"}
    summary = str(data.get("summary", ""))
    question = data.get("question")
    if status == "blocked" and question:
        summary = f"{summary}\n\nQuestion: {question}"
    return {"status": status, "summary": summary, "question": question}


def total_cost(events: list[dict]) -> float:
    return sum(
        float(event.get("total_cost_usd", 0.0))
        for event in events
        if event.get("type") == "result"
    )


POLL_S = 30
"""How long to idle when the task list is empty, so appended tasks get picked
up without a restart."""

IDLE_FIELDS = {
    "state": "idle",
    "task_id": None,
    "task_text": None,
    "run_dir": None,
    "session_id": None,
    "started_at": None,
    "wait_until": None,
}
"""set_status carries unnamed fields over, so going idle has to clear the
task fields explicitly or the dashboard shows a task that finished an hour
ago. Reasserted on every idle poll, which is also what keeps the heartbeat
fresh while nothing is running."""

log = logging.getLogger("claudeloop")


def sleep_delay(wait_until: float) -> float:
    """Seconds to sleep until `wait_until`, clamped to MAX_WAIT_S.

    A bogus resetsAt (wrong units, corrupted stream) must produce a bounded
    retry, not a multi-year sleep with the task silently stuck.
    """
    return min(max(0.0, wait_until - time.time()), MAX_WAIT_S)


async def run_task(cfg: Config, state: State, source: TaskSource, task: Task) -> dict:
    """Run one task to a terminal status, resuming through rate limits."""
    run_dir = cfg.home / "runs" / task.id
    result_path = run_dir / "result.json"
    run_dir.mkdir(parents=True, exist_ok=True)
    # A previous attempt's verdict would otherwise end this one immediately.
    result_path.unlink(missing_ok=True)

    session_id = str(uuid.uuid4())
    state.start_task(task.id, task.source, task.source_ref, task.text)
    log.info("task %s starting: %s", task.id, task.text)
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
    while True:
        attempt = resume_count + wait_count
        run_id = state.start_run(task.id, session_id, attempt)
        status_module.set_status(state="running", attempt=attempt, wait_until=None)
        events = await session.run(
            cfg,
            run_dir,
            session_id,
            prompt="Continue." if attempt else task.text,
            resume=bool(attempt),
        )
        # session.run returns only this invocation's events, so cost has to
        # accumulate here rather than being read once at the end.
        cost += total_cost(events)
        quota = latest_rate_limit(events)
        if quota is not None:
            status_module.set_status(rate_limit=quota)
        action = decide(
            events,
            result_path.exists(),
            resume_count,
            cfg.max_resumes,
            wait_count,
            cfg.max_waits,
        )
        if isinstance(action, Resume):
            # Distinguished in the database so "how much wall time went to
            # quota" is answerable without decoding a Python class name.
            exit_reason = "RateLimited" if action.wait_until else "Nudge"
        else:
            exit_reason = type(action).__name__
        state.finish_run(run_id, exit_reason)

        if isinstance(action, ReadResult):
            result = read_result(result_path)
            break
        if isinstance(action, Fail):
            result = {"status": "failed", "summary": f"ClaudeLoop gave up: {action.reason}"}
            break
        if action.wait_until:
            delay = sleep_delay(action.wait_until)
            log.info("task %s rate limited, sleeping %.0fs", task.id, delay)
            status_module.set_status(state="waiting", wait_until=action.wait_until)
            await asyncio.sleep(delay)
            status_module.set_status(state="running", wait_until=None)
            wait_count += 1
        else:
            resume_count += 1

    state.finish_task(
        task.id, result["status"], result["summary"], cost, result.get("question")
    )
    source.mark(task, result["status"], result["summary"])
    log.info("task %s %s ($%.4f): %s", task.id, result["status"], cost, result["summary"])
    return result


async def main_loop(cfg: Config, once: bool = False) -> None:
    """Run pending tasks one at a time, forever.

    `once` drains the tasks pending right now and returns, for tests.
    """
    state = State(cfg.home / "state.db")
    source = FileSource(cfg.tasks_file)
    while True:
        pending = source.pending()
        if not pending:
            if once:
                status_module.set_status(**IDLE_FIELDS)
                return
            status_module.set_status(**IDLE_FIELDS)
            await asyncio.sleep(POLL_S)
            continue
        # Re-read after every task: the file may have been edited meanwhile.
        task = pending[0]
        try:
            await run_task(cfg, state, source, task)
        except Exception as error:
            # A crash here (claude missing from PATH, ENOSPC on events.jsonl,
            # a fork failing under memory pressure, ...) is an environment
            # fault, not a task verdict. Deliberately no source.mark: marking
            # it `- [!]` would burn through the whole task list in seconds if
            # the fault is permanent. Recorded as failed so the row doesn't
            # stay stuck at 'running', then retried slowly rather than taking
            # the whole process down.
            log.exception("task %s crashed outside the session state machine", task.id)
            status_module.set_status(state="error", last_error=str(error))
            try:
                # The run row opened before the crash would otherwise sit
                # with ended_at/exit_reason NULL forever.
                state.db.execute(
                    "UPDATE runs SET ended_at=?, exit_reason='Crash'"
                    " WHERE task_id=? AND ended_at IS NULL",
                    (time.time(), task.id),
                )
                state.finish_task(task.id, "failed", f"ClaudeLoop crashed: {error}", 0.0)
            except Exception:
                # Recording a crash must never itself be able to crash the
                # loop -- an unattended run has to survive even a state.db
                # write that fails (e.g. a schema this process's migration
                # didn't handle).
                log.exception("task %s: failed to record crash in state.db", task.id)
            if once:
                return
            await asyncio.sleep(POLL_S)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        cfg = load_config()
    except FileNotFoundError:
        raise SystemExit(
            f"no config file at {DEFAULT_CONFIG} -- see README.md to set one up"
        )
    # After the config validates, so a non-loopback bind with no token fails
    # before anything is listening.
    web.serve(cfg)
    asyncio.run(main_loop(cfg))
