"""The decision state machine and the orchestration around it."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import plugins
from . import session
from . import setup
from . import status as status_module
from . import web
from . import worktree
from .config import DEFAULT_CONFIG, HOME, Config, load_config
from .jira import JiraClient, JiraSource
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

CONTINUE_PROMPT = "Continue."
"""Sent after a resume that interrupted genuine work in progress -- a quota
wait. The session was cut off mid-task, so telling it to carry on is
correct."""

NUDGE_PROMPT = (
    "You ended your turn without writing the result file. The result file "
    "at the path in the CLAUDELOOP_RESULT environment variable -- not your "
    "last message -- is what ends this task; write it now. If the work is "
    "already complete and committed, do not redo it: write status \"done\" "
    "and say so in the summary. If instead you genuinely need a human to "
    "decide something, that is also the result file's job: write status "
    "\"blocked\" with the one thing you need decided in the \"question\" "
    "field, and a human will answer it when they next look. Either way, do "
    "not end your turn with a question in your last message -- nobody reads "
    "it; write the result file instead."
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
    "Act on that answer and finish the task. Your working tree is exactly as "
    "you left it -- still on your branch, with any uncommitted changes still "
    "there -- so carry on from where you stopped. When the work is complete, "
    "write the result file at the path in the CLAUDELOOP_RESULT environment "
    "variable exactly as before; that file, not your last message, is what "
    "ends the task."
)
"""Sent when resuming a parked task whose question has been answered.

Before S6 this had to talk the session back onto its own branch: every task
that ran while this one was parked reset the single shared working tree. Each
task now has its own worktree, which nothing else touches while it is parked
-- so the honest thing to say is the opposite, and saying it stops a resumed
session guessing at a branch name it may have renamed."""

FRESH_ANSWER_PROMPT = (
    "{task}\n\n"
    "A human has already answered a question about this task: {answer}\n\n"
    "The session that asked that question is no longer available, so start "
    "this task from the beginning, using that answer. You are on this task's "
    "branch, and any commits an earlier attempt made are on it; look before "
    "you redo work that is already done."
)
"""For the edge case where a parked task has no session to resume -- a
state.db from before this slice, or a task whose runs were pruned. `ensure`
reuses `claudeloop/<task.id>` when it already exists, so a pruned task from
this slice lands back on its own commits. A pre-slice task never had that
branch -- its session named its own -- so `ensure` cuts a fresh one from the
default instead, and there is nothing of the earlier attempt on it. The
wording promises only the branch, not what is on it, so it stays true either
way."""


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
        status = info.get("status")
        # The vocabulary here is thin -- "allowed" and "allowed_warning"
        # (a live smoke test surfaced the latter: 80% of the seven-day
        # window used, still allowed) plus "rejected" from this repo's own
        # fake CLI -- so this keys off the "allowed" prefix rather than an
        # exact-match list. Every headroom report seen so far is shaped
        # "allowed*"; a blocking status uses an unrelated word. `utilization`
        # and `surpassedThreshold` are informational and never looked at
        # here -- they describe headroom, not whether a request went
        # through.
        #
        # A status that is neither "allowed" nor "allowed*" -- including one
        # this code has never seen -- falls on the blocking side. Of the two
        # ways to be wrong: a false wait costs hours but is bounded
        # (MAX_WAIT_S) and visible on the dashboard as "waiting", still
        # making progress once it wakes up. A false non-wait would instead
        # hammer the CLI through every remaining resume against a real
        # block, burning max_resumes in seconds and failing the task
        # outright -- the exact failure mode this whole recovery path
        # exists to prevent. Blocking-by-default is the safer guess.
        if isinstance(status, str) and status.startswith("allowed"):
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
    "pending": (),
}
"""set_status carries unnamed fields over, so going idle has to clear the
task fields explicitly or the dashboard shows a task that finished an hour
ago. Reasserted on every idle poll, which is also what keeps the heartbeat
fresh while nothing is running."""

log = logging.getLogger("claudeloop")

HEARTBEAT_S = 10
"""How often the background heartbeat task refreshes status.heartbeat.

set_status() only fires on a state *transition* -- task start, once per
attempt, a quota transition, the idle poll -- and there is none while
`await session.run(...)` runs (up to session_timeout_s, 4h by default) or
while `await asyncio.sleep(delay)` waits out a quota (up to MAX_WAIT_S, 8
days). Both are long real-time awaits with no transition in between, so
without a task that fires independently of them, web.STALE_AFTER_S (90s)
trips during every normal run and the dashboard shows a dead loop that is
working fine.
"""


async def _heartbeat() -> None:
    """Prove the event loop is still spinning, independent of any task
    state. This is what web.STALE_AFTER_S actually measures."""
    while True:
        await asyncio.sleep(HEARTBEAT_S)
        status_module.set_status()


def sleep_delay(wait_until: float) -> float:
    """Seconds to sleep until `wait_until`, clamped to MAX_WAIT_S.

    A bogus resetsAt (wrong units, corrupted stream) must produce a bounded
    retry, not a multi-year sleep with the task silently stuck.
    """
    return min(max(0.0, wait_until - time.time()), MAX_WAIT_S)


def build_source(cfg: Config, state: State) -> TaskSource:
    """The one place that knows which task source a config selects."""
    if cfg.source == "jira" and cfg.jira is not None:
        return JiraSource(
            JiraClient(cfg.jira.site, cfg.jira.email, cfg.jira.token),
            cfg.jira.jql,
            state,
            cfg.jira.transition_start,
            cfg.jira.transition_done,
        )
    return FileSource(cfg.tasks_file)


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
    context -- rather than starting the task over.
    """
    run_dir = cfg.home / "runs" / task.id
    result_path = run_dir / "result.json"
    run_dir.mkdir(parents=True, exist_ok=True)
    # A previous attempt's verdict would otherwise end this one immediately.
    # For an answered task that previous verdict is the `blocked` result the
    # session wrote before it parked, so this matters more, not less.
    result_path.unlink(missing_ok=True)
    # Same reasoning as the result file above: an answer written into the
    # window between find_answered consuming the previous one and this row
    # going back to 'running' would otherwise survive, and be read as the
    # answer to a different question the next time this task parks.
    (run_dir / "answer.json").unlink(missing_ok=True)

    # None when there is no session to resume: a state.db from before S2b, or
    # a task whose runs were pruned. The answer still gets through, only the
    # context is lost.
    resumed = state.last_session(task.id) if resume_with is not None else None
    session_id = resumed or str(uuid.uuid4())
    state.start_task(task.id, task.source, task.source_ref, task.text)
    # One worktree per task, so nothing is shared between tasks and there is
    # nothing to reset. reset_to_default_branch lived here until S6: it
    # compensated for a single shared tree by mutating it between tasks, and
    # the S2b live smoke test showed the cost -- a task that parked before
    # its first commit resumed onto the *next* task's branch. A parked task
    # now keeps its own tree, uncommitted work included, until its answer
    # arrives.
    #
    # Offloaded to a thread for the same reason the reset was: it shells out
    # to git synchronously, and this coroutine must not block the event loop
    # the heartbeat and the dashboard share.
    #
    # Deliberately not caught: a worktree that cannot be created is an
    # environment fault, not a verdict on the task, and main_loop's crash
    # handler is where this file already says what to do about those. The
    # causes worth designing for are box-wide -- an index.lock a stray process
    # is holding, a full disk -- and failing the task here would mark the
    # whole list `- [!]` in seconds, since 'failed' is terminal and
    # State.terminal_ids() would then keep a task source from ever offering
    # any of them again. Recorded as 'error' by the handler instead, with no
    # source.mark, so the work survives the box being fixed.
    #
    # The price is head-of-line blocking when the cause is task-local and
    # permanent instead. A non-empty leftover directory at
    # worktrees/<task.id> with no `.git` in it -- ClaudeLoop killed mid-`add`,
    # a reboot, an operator deleting `.git` while tidying -- falls past
    # `ensure`'s reuse check into `add`, which fails with "already exists",
    # and the branch retry fails identically. 'error' is non-terminal by
    # design, so source.pending() keeps offering this task, the loop keeps
    # re-picking it every POLL_S, and no later task ever runs. Left as an
    # environment fault an operator clears (delete the directory); recorded
    # in ROADMAP.md's open issues so it is not a surprise.
    tree = await asyncio.to_thread(
        worktree.ensure, cfg.repo, cfg.home / "worktrees", task.id
    )
    if resume_with is None:
        # source.start would re-fire transition_start against an issue
        # already in that status; reopen() covers the source-side state
        # instead, so this stays conditional even though the worktree above
        # is not.
        #
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
        attempt = resume_count + wait_count
        run_id = state.start_run(task.id, session_id, attempt)
        status_module.set_status(state="running", attempt=attempt, wait_until=None)
        events = await session.run(
            cfg,
            run_dir,
            session_id,
            prompt=prompt,
            resume=resume,
            cwd=tree,
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
            prompt = CONTINUE_PROMPT
        else:
            resume_count += 1
            prompt = NUDGE_PROMPT
        resume = True

    state.finish_task(
        task.id, result["status"], result["summary"], cost, result.get("question")
    )
    await asyncio.to_thread(
        source.mark, task, result["status"], result["summary"], cost
    )
    if result["status"] != "blocked":
        # A parked task keeps its tree -- that is what its resumed session
        # comes back to. Everything else is released, which never forces
        # anything: git refuses to remove a tree with uncommitted changes and
        # that refusal is kept.
        await asyncio.to_thread(worktree.release, cfg.repo, tree)
    log.info("task %s %s ($%.4f): %s", task.id, result["status"], cost, result["summary"])
    return result


def read_answer(run_dir: Path) -> str | None:
    """The dashboard's answer for a parked task, consumed as it is read.

    Unlinked whatever it contained: a file left in place would resume the
    task a second time on the next poll, or -- if it is malformed -- warn on
    every poll forever.
    """
    path = run_dir / "answer.json"
    try:
        raw = path.read_text(errors="replace")
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
    is actually parked, and only for a task with no answer file waiting.
    """
    for row in state.blocked():
        task = Task(row["id"], row["text"], row["source"], row["source_ref"])
        try:
            answer = read_answer(cfg.home / "runs" / task.id) or source.answer(task)
        except Exception as error:
            # Confined to this row on purpose: one faulty task must not hide
            # every later parked task's answer from the same scan.
            log.warning("could not check task %s for an answer (%s)", task.id, error)
            continue
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


async def main_loop(cfg: Config, once: bool = False) -> None:
    """Run pending tasks one at a time, forever.

    A task parked on a question is checked for an answer before new work is
    polled for, so an answered task resumes ahead of starting something
    fresh.

    `once` drains the tasks pending right now -- including any that have been
    answered -- and returns, for tests.
    """
    state = State(cfg.home / "state.db", str(cfg.repo))
    source = build_source(cfg, state)
    heartbeat = asyncio.create_task(_heartbeat())
    try:
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
                # the start of the current task, not live -- under the file
                # source that's a small step back from the old per-request
                # re-read (an edit to tasks.md mid-task won't show until the
                # next task starts); under the Jira source a per-request
                # re-read would mean a network round trip on the web thread,
                # which is the whole reason this rides on the snapshot
                # instead.
                status_module.set_status(
                    pending=tuple((t.id, t.text) for t in pending)
                )
            try:
                await run_task(cfg, state, source, task, resume_with=resume_with)
            except Exception as error:
                # A crash here (claude missing from PATH, ENOSPC on events.jsonl,
                # a fork failing under memory pressure, ...) is an environment
                # fault, not a task verdict. Deliberately no source.mark: marking
                # it `- [!]` would burn through the whole task list in seconds if
                # the fault is permanent. Recorded as 'error', not 'failed': a
                # crash outside the session state machine is not a verdict on
                # the task, and State.terminal_ids() -- the backstop a task
                # source uses to avoid re-offering work it already finished --
                # is keyed on terminal statuses, which must be able to offer
                # this task again rather than treat it as permanently done.
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
                    state.finish_task(task.id, "error", f"ClaudeLoop crashed: {error}", 0.0)
                except Exception:
                    # Recording a crash must never itself be able to crash the
                    # loop -- an unattended run has to survive even a state.db
                    # write that fails (e.g. a schema this process's migration
                    # didn't handle).
                    log.exception("task %s: failed to record crash in state.db", task.id)
                if once:
                    return
                await asyncio.sleep(POLL_S)
    finally:
        # Never leak the heartbeat task past this function -- once=True is
        # used from tests, and a task still scheduled after the event loop
        # that owns it stops would print an "unhandled exception" warning at
        # best and pin the fixture's asyncio loop open at worst.
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat


def _serve_dashboard(cfg: Config) -> None:
    """Start the dashboard, but never let it stop the loop from starting.

    A stale process, a leftover container, or anything else already holding
    web_port raises OSError from bind(). The observer must not be able to
    prevent the observed thing from running at all -- so this is caught here
    and only logged, and the loop starts either way.
    """
    try:
        web.serve(cfg)
    except OSError as error:
        log.warning(
            "dashboard could not bind %s:%s (%s) -- probably something else is"
            " already using that port; continuing without the web UI",
            cfg.web_host,
            cfg.web_port,
            error,
        )


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser(prog="python -m claudeloop")
    parser.add_argument(
        "--setup", action="store_true",
        help="open the setup wizard against the existing config",
    )
    args = parser.parse_args(argv)
    # No config is not an error any more -- it is a first run. The wizard
    # blocks until it has written one, then this falls through into the
    # ordinary startup path, so the config the loop runs is the one the
    # ordinary loader reads back off disk rather than the wizard's own parse.
    if args.setup or not DEFAULT_CONFIG.exists():
        setup.run_setup(DEFAULT_CONFIG, HOME)
    try:
        cfg = load_config(DEFAULT_CONFIG, HOME)
    except FileNotFoundError:
        raise SystemExit(f"no config file at {DEFAULT_CONFIG}")
    except ValueError as error:
        # load_config's own validation (the permissions guard, a bad
        # settings_file/mcp_config path, strict_mcp without mcp_config, ...)
        # raises ValueError with a message already written for a human. A
        # config.toml at the default umask (0644) is the common case here --
        # every such install must get that message, not a raw traceback.
        raise SystemExit(str(error))
    # Before anything starts listening or runs: a box whose git cannot make
    # worktrees would otherwise fail every task in turn, one paid session at
    # a time, instead of saying so once.
    problem = worktree.probe(cfg.repo)
    if problem:
        raise SystemExit(problem)
    # Same treatment, same reason: a box that could not get the plugins the
    # operator selected would otherwise run every task with a system prompt
    # describing tools the session does not have. Nothing runs here when
    # `plugins` is empty, and nothing touches the network when the box
    # already matches.
    problem = plugins.reconcile(cfg.plugins)
    if problem:
        raise SystemExit(problem)
    # After the config validates, so a non-loopback bind with no token fails
    # before anything is listening.
    _serve_dashboard(cfg)
    asyncio.run(main_loop(cfg))
