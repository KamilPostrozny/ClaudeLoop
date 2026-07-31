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
from .config import Config, load_config
from .source import FileSource, Task, TaskSource
from .state import State

RESET_PAD_S = 30
"""Slack past resetsAt, to absorb clock skew between this host and the API."""

FALLBACK_WAIT_S = 300
"""Used when a blocking rate-limit event arrives without a resetsAt."""

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
        return float(info.get("resetsAt") or time.time() + FALLBACK_WAIT_S)
    return None


def decide(
    events: list[dict], result_exists: bool, resume_count: int, max_resumes: int
) -> ReadResult | Resume | Fail:
    """Decide what to do after a claude invocation exits.

    `events` is the stream from the invocation that just exited, not the task's
    whole history: a rate-limit event from an earlier attempt must not
    re-trigger a wait after a later attempt exits for another reason.
    """
    if result_exists:
        return ReadResult()
    if resume_count >= max_resumes:
        return Fail("no_result")
    reset_at = blocking_reset(events)
    if reset_at is not None:
        return Resume(wait_until=reset_at + RESET_PAD_S)
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
    return {"status": status, "summary": summary}


def total_cost(events: list[dict]) -> float:
    return sum(
        float(event.get("total_cost_usd", 0.0))
        for event in events
        if event.get("type") == "result"
    )


POLL_S = 30
"""How long to idle when the task list is empty, so appended tasks get picked
up without a restart."""

log = logging.getLogger("claudeloop")


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

    resume_count = 0
    cost = 0.0
    while True:
        run_id = state.start_run(task.id, session_id, resume_count)
        events = await session.run(
            cfg,
            run_dir,
            session_id,
            prompt="Continue." if resume_count else task.text,
            resume=bool(resume_count),
        )
        # session.run returns only this invocation's events, so cost has to
        # accumulate here rather than being read once at the end.
        cost += total_cost(events)
        action = decide(events, result_path.exists(), resume_count, cfg.max_resumes)
        state.finish_run(run_id, type(action).__name__)

        if isinstance(action, ReadResult):
            result = read_result(result_path)
            break
        if isinstance(action, Fail):
            result = {"status": "failed", "summary": f"ClaudeLoop gave up: {action.reason}"}
            break
        if action.wait_until:
            delay = max(0.0, action.wait_until - time.time())
            log.info("task %s rate limited, sleeping %.0fs", task.id, delay)
            await asyncio.sleep(delay)
        resume_count += 1

    state.finish_task(task.id, result["status"], result["summary"], cost)
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
                return
            await asyncio.sleep(POLL_S)
            continue
        # Re-read after every task: the file may have been edited meanwhile.
        await run_task(cfg, state, source, pending[0])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    asyncio.run(main_loop(load_config()))
