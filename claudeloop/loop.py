"""The decision state machine and the orchestration around it."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import session
from . import status as status_module
from . import web
from .config import DEFAULT_CONFIG, Config, load_config
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
    "and say so in the summary. Nobody is available to answer a question, "
    "so do not end your turn asking what to do next -- write the result "
    "file instead."
)
"""Sent after a resume with no result file and no rate limit -- a nudge. Two
live smoke-test sessions read the old \"Continue.\" prompt as confirmation
there was nothing left to do and ended their turn with prose instead of the
result file, burning every resume at $0.10 despite finished, committed work.
This names the actual problem instead."""


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


DEFAULT_BRANCH_CANDIDATES = ("main", "master")
"""Checked, in order, when a repository has no remote to ask -- never
assumed outright. Neither name is guaranteed; this only picks one that
actually exists as a local branch."""

GIT_TIMEOUT_S = 10
"""Bounds every git call this module makes. Local git commands finish in
milliseconds; this exists only so a wedged one (a lock held by another
process, anything unforeseen) can't hang an unattended loop forever."""


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run one git command, hardened for an unattended caller: no inherited
    stdin (a prompt for credentials or an editor would otherwise block
    forever reading from the loop's own terminal -- the same class of bug
    session.py's stdin=DEVNULL already guards against for the CLI itself),
    no interactive terminal prompting, and a bounded timeout.
    """
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=GIT_TIMEOUT_S,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def _try_git(repo: Path, *args: str) -> subprocess.CompletedProcess | None:
    """`_git`, or None with a warning already logged if git could not be run
    at all -- a missing binary, a timeout. Distinct from a clean invocation
    that simply exits non-zero, which callers handle themselves."""
    try:
        return _git(repo, *args)
    except (OSError, subprocess.SubprocessError) as error:
        log.warning(
            "could not run `git %s` in %s (%s); running the next task from"
            " the repository's current state",
            " ".join(args),
            repo,
            error,
        )
        return None


def default_branch(repo: Path) -> str | None:
    """The repository's default branch, without guessing.

    `git symbolic-ref refs/remotes/origin/HEAD` is authoritative when there
    is a remote (set by a clone, or `git remote set-head origin -a`). Most
    repositories driven by an unattended loop have none, so this falls back
    to whichever of the two common initial-branch names actually exists
    locally. If neither does, this gives up rather than guess -- the caller
    then leaves the working tree exactly as it found it.
    """
    origin_head = _try_git(repo, "symbolic-ref", "-q", "refs/remotes/origin/HEAD")
    if origin_head is None:
        return None
    prefix = "refs/remotes/origin/"
    ref = origin_head.stdout.strip()
    if origin_head.returncode == 0 and ref.startswith(prefix):
        return ref[len(prefix):]
    for name in DEFAULT_BRANCH_CANDIDATES:
        check = _try_git(repo, "rev-parse", "-q", "--verify", f"refs/heads/{name}")
        if check is None:
            return None
        if check.returncode == 0:
            return name
    return None


def reset_to_default_branch(repo: Path) -> None:
    """Return the working tree to the repository's default branch before a
    task starts.

    Without this, task N's session inherits whatever branch task N-1's
    session left checked out -- confirmed in a smoke run where a
    twenty-task list would have produced one branch carrying every task's
    changes stacked on the last, instead of twenty independent ones each
    branched from the same base. The built-in definition of done already
    tells sessions to branch from the default themselves; a live smoke test
    measured only 50% compliance with that instruction, hence doing it
    structurally here instead.

    Never forces anything -- no checkout -f, no reset --hard, no clean. A
    session may have deliberately left uncommitted work, and destroying a
    user's working tree in an unattended loop is worse than one stacked
    branch. If the default branch can't be determined, or the checkout
    fails for any reason (a dirty tree, a detached HEAD, no such branch, git
    itself missing), this logs and leaves the repository exactly as it is --
    the task then simply runs from wherever the tree currently sits. Never
    raises: called from inside run_task, where a fault here must not look
    any different from any other environment fault the loop already
    survives.
    """
    branch = default_branch(repo)
    if branch is None:
        return
    result = _try_git(repo, "checkout", branch)
    if result is not None and result.returncode != 0:
        log.warning(
            "could not check out default branch %r in %s before starting the"
            " next task -- running from the repository's current state (%s)",
            branch,
            repo,
            result.stderr.strip(),
        )


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


async def run_task(cfg: Config, state: State, source: TaskSource, task: Task) -> dict:
    """Run one task to a terminal status, resuming through rate limits."""
    run_dir = cfg.home / "runs" / task.id
    result_path = run_dir / "result.json"
    run_dir.mkdir(parents=True, exist_ok=True)
    # A previous attempt's verdict would otherwise end this one immediately.
    result_path.unlink(missing_ok=True)

    session_id = str(uuid.uuid4())
    state.start_task(task.id, task.source, task.source_ref, task.text)
    # After start_task, so a fault here (reset_to_default_branch is
    # defensive and shouldn't raise, but this is not load-bearing on that)
    # lands against a real task row like every other crash, rather than
    # main_loop's crash handler finding nothing to update. Offloaded to a
    # thread: it shells out to git synchronously, and this coroutine must
    # not block the event loop the heartbeat task and the dashboard share.
    await asyncio.to_thread(reset_to_default_branch, cfg.repo)
    # Offloaded for the same reason as the git call above: under the Jira
    # source this is a blocking HTTP round trip, and this coroutine shares
    # its thread with the heartbeat and the dashboard.
    await asyncio.to_thread(source.start, task)
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
    prompt = task.text
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
    log.info("task %s %s ($%.4f): %s", task.id, result["status"], cost, result["summary"])
    return result


async def main_loop(cfg: Config, once: bool = False) -> None:
    """Run pending tasks one at a time, forever.

    `once` drains the tasks pending right now and returns, for tests.
    """
    state = State(cfg.home / "state.db")
    source = build_source(cfg, state)
    heartbeat = asyncio.create_task(_heartbeat())
    try:
        while True:
            pending = await asyncio.to_thread(source.pending)
            if not pending:
                if once:
                    status_module.set_status(**IDLE_FIELDS)
                    return
                status_module.set_status(**IDLE_FIELDS)
                await asyncio.sleep(POLL_S)
                continue
            # Re-read after every task: the file may have been edited meanwhile.
            task = pending[0]
            # Published for the dashboard: web reads this off the snapshot
            # rather than re-reading the task source itself, since under the
            # Jira source that would be a network call on the web thread.
            # As a result the list is only as fresh as the start of the
            # current task, not live -- under the file source that's a small
            # step back from the old per-request re-read (an edit to
            # tasks.md mid-task won't show until the next task starts); under
            # the Jira source a per-request re-read would mean a network
            # round trip on the web thread, which is the whole reason this
            # rides on the snapshot instead.
            status_module.set_status(
                pending=tuple((t.id, t.text) for t in pending)
            )
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


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        cfg = load_config()
    except FileNotFoundError:
        raise SystemExit(
            f"no config file at {DEFAULT_CONFIG} -- see README.md to set one up"
        )
    except ValueError as error:
        # load_config's own validation (the permissions guard, a bad
        # settings_file/mcp_config path, strict_mcp without mcp_config, ...)
        # raises ValueError with a message already written for a human. A
        # config.toml at the default umask (0644) is the common case here --
        # every such install must get that message, not a raw traceback.
        raise SystemExit(str(error))
    # After the config validates, so a non-loopback bind with no token fails
    # before anything is listening.
    _serve_dashboard(cfg)
    asyncio.run(main_loop(cfg))
