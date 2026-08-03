import asyncio
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from claudeloop import loop, status
from claudeloop.config import Config, JiraConfig
from claudeloop.jira import JiraSource
from claudeloop.loop import (
    FALLBACK_WAIT_S,
    MAX_WAIT_S,
    RESET_PAD_S,
    Fail,
    ReadResult,
    Resume,
    blocking_reset,
    decide,
    main,
    read_result,
    sleep_delay,
    total_cost,
)
from claudeloop.source import FileSource, Task, task_id
from claudeloop.state import State

from .gitrepo import make_repo

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> list[dict]:
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines() if line]


def git_only_path(tmp: Path) -> str:
    """A PATH holding nothing but git, for the tests that reproduce a host
    with no `claude` on it.

    An outright empty PATH used to be enough. Since run_task creates the
    task's worktree before it spawns anything, a PATH without git fails the
    task on the worktree instead -- a verdict, not the environment crash
    those tests are about. A symlink rather than git's own directory: that
    directory may hold a real `claude`, and these tests must not run it.
    """
    git = shutil.which("git")
    if git is None:  # the make_repo fixtures would have died long before this
        raise unittest.SkipTest("git is not on PATH")
    only = tmp / "git-only-bin"
    only.mkdir()
    (only / "git").symlink_to(git)
    return str(only)


class BlockingResetTest(unittest.TestCase):
    def test_returns_reset_time_when_the_latest_report_is_blocking(self):
        self.assertEqual(blocking_reset(load("rate_limited.jsonl")), 1785516000.0)

    def test_returns_none_when_the_latest_report_is_allowed(self):
        self.assertIsNone(blocking_reset(load("completed.jsonl")))

    def test_returns_none_when_there_is_no_report(self):
        self.assertIsNone(blocking_reset([{"type": "assistant"}]))

    def test_an_earlier_block_does_not_outvote_a_later_allow(self):
        events = [
            {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 1}},
            {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "resetsAt": 2}},
        ]
        self.assertIsNone(blocking_reset(events))

    def test_blocked_without_a_reset_time_falls_back_to_a_short_wait(self):
        events = [{"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}}]
        self.assertAlmostEqual(
            blocking_reset(events), time.time() + FALLBACK_WAIT_S, delta=1
        )

    def test_blocked_with_a_non_numeric_reset_time_falls_back_to_a_short_wait(self):
        info = {"status": "rejected", "resetsAt": "soon"}
        events = [{"type": "rate_limit_event", "rate_limit_info": info}]
        self.assertAlmostEqual(
            blocking_reset(events), time.time() + FALLBACK_WAIT_S, delta=1
        )

    def test_allowed_warning_does_not_block(self):
        # The real payload a live smoke test produced: 80% of the seven-day
        # window used, status "allowed_warning" -- still allowed. Before the
        # fix, anything other than an exact "allowed" was read as blocking,
        # so this parked the loop until resetsAt for no reason.
        self.assertIsNone(blocking_reset(load("allowed_warning.jsonl")))

    def test_utilization_and_surpassed_threshold_do_not_affect_blocking(self):
        # Informational fields, present or absent, must never flip the
        # decision either way.
        warning = {
            "status": "allowed_warning",
            "rateLimitType": "seven_day",
            "resetsAt": 1785600000,
            "utilization": 0.8,
            "isUsingOverage": False,
            "surpassedThreshold": 0.75,
        }
        bare_warning = {"status": "allowed_warning", "resetsAt": 1785600000}
        self.assertIsNone(
            blocking_reset([{"type": "rate_limit_event", "rate_limit_info": warning}])
        )
        self.assertIsNone(
            blocking_reset([{"type": "rate_limit_event", "rate_limit_info": bare_warning}])
        )

        blocked = {
            "status": "rejected",
            "resetsAt": 1785600000,
            "utilization": 1.0,
            "surpassedThreshold": 0.75,
        }
        bare_blocked = {"status": "rejected", "resetsAt": 1785600000}
        self.assertEqual(
            blocking_reset([{"type": "rate_limit_event", "rate_limit_info": blocked}]),
            1785600000.0,
        )
        self.assertEqual(
            blocking_reset([{"type": "rate_limit_event", "rate_limit_info": bare_blocked}]),
            1785600000.0,
        )

    def test_unrecognised_status_is_treated_as_blocking(self):
        # Thin known vocabulary ("allowed", "allowed_warning", "rejected");
        # anything else -- including a status nobody's seen -- must not be
        # read as headroom. See the comment in blocking_reset for why this
        # is the safer of the two ways to guess wrong.
        info = {"status": "throttled", "resetsAt": 1785600000}
        events = [{"type": "rate_limit_event", "rate_limit_info": info}]
        self.assertEqual(blocking_reset(events), 1785600000.0)


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

    def test_the_answer_prompt_says_the_tree_is_as_it_was_left(self):
        # Under one worktree per task the tree does not move while a task is
        # parked, so the old "check out the branch you were working on"
        # instruction became false -- and telling a session to check out a
        # branch it is already on invites it to guess at a name it may have
        # renamed.
        text = loop.ANSWER_PROMPT.format(answer="use EUR")
        self.assertIn("exactly as you left it", text)
        self.assertIn("still on your branch", text)
        self.assertNotIn("check out the branch you were working on", text)

    def test_the_fresh_answer_prompt_says_the_earlier_attempts_commits_are_here(self):
        text = loop.FRESH_ANSWER_PROMPT.format(task="do a thing", answer="use EUR")
        self.assertIn("any commits an earlier attempt made", text)
        self.assertNotIn("may have left a branch", text)
        self.assertNotIn("on the branch that attempt used", text)

    def test_the_answer_prompt_still_demands_the_result_file(self):
        rendered = loop.ANSWER_PROMPT.format(answer="use EUR")

        self.assertIn("CLAUDELOOP_RESULT", rendered)
        self.assertIn("not your last message", rendered)

    def test_the_fresh_answer_prompt_carries_the_task_and_the_answer(self):
        rendered = loop.FRESH_ANSWER_PROMPT.format(task="do a thing", answer="use EUR")

        self.assertIn("do a thing", rendered)
        self.assertIn("use EUR", rendered)
        self.assertIn("from the beginning", rendered)

    def test_the_nudge_keeps_the_qualifier_that_holds_the_bar(self):
        # Without "genuinely", the nudge reads as an open invitation to ask
        # -- and it is sent to a session that has already shown it wants to
        # stop. This is the word doing that work; pin it.
        from claudeloop.loop import NUDGE_PROMPT

        self.assertIn("genuinely need a human to decide", NUDGE_PROMPT)


class SleepDelayTest(unittest.TestCase):
    def test_normal_wait_is_unclamped(self):
        self.assertAlmostEqual(sleep_delay(time.time() + 100), 100, delta=1)

    def test_clamps_an_absurd_wait_until(self):
        # E.g. a resetsAt that was actually milliseconds: without the clamp
        # this sleeps for tens of thousands of years instead of retrying.
        self.assertEqual(sleep_delay(time.time() + 999_999_999_999), MAX_WAIT_S)

    def test_never_negative(self):
        self.assertEqual(sleep_delay(time.time() - 100), 0.0)


class DecideTest(unittest.TestCase):
    def test_result_file_wins_even_over_a_rate_limit(self):
        action = decide(load("rate_limited.jsonl"), True, 0, 20, 0, 200)
        self.assertIsInstance(action, ReadResult)

    def test_rate_limit_waits_until_the_reset(self):
        action = decide(load("rate_limited.jsonl"), False, 0, 20, 0, 200)
        self.assertEqual(action, Resume(wait_until=1785516000.0 + RESET_PAD_S))

    def test_clean_exit_without_a_result_is_nudged(self):
        action = decide(load("completed.jsonl"), False, 0, 20, 0, 200)
        self.assertEqual(action, Resume(wait_until=0.0))

    def test_exhausted_resumes_fails(self):
        action = decide(load("completed.jsonl"), False, 20, 20, 0, 200)
        self.assertEqual(action, Fail("no_result"))

    def test_a_task_keeps_waiting_past_max_resumes_worth_of_quota_events(self):
        # Rewrite of the old test_exhausted_resumes_fails_even_when_rate_limited,
        # which asserted the shared-budget behaviour this change replaces: a
        # wait is not a failure to make progress, only a nudge is, so
        # resume_count alone must not cut off a run that is purely waiting on
        # quota.
        action = decide(load("rate_limited.jsonl"), False, 20, 20, 5, 200)
        self.assertEqual(action, Resume(wait_until=1785516000.0 + RESET_PAD_S))

    def test_exhausted_waits_fails(self):
        action = decide(load("rate_limited.jsonl"), False, 0, 20, 200, 200)
        self.assertEqual(action, Fail("no_result"))

    def test_empty_stream_is_nudged(self):
        self.assertEqual(decide([], False, 0, 20, 0, 200), Resume(wait_until=0.0))


class ReadResultTest(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "result.json"

    def test_reads_a_good_file(self):
        self.path.write_text('{"status": "done", "summary": "all green"}')
        self.assertEqual(
            read_result(self.path),
            {"status": "done", "summary": "all green", "question": None},
        )

    def test_blocked_folds_the_question_into_the_summary(self):
        self.path.write_text(
            '{"status": "blocked", "summary": "stuck", "question": "which currency?"}'
        )
        result = read_result(self.path)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("which currency?", result["summary"])
        # Kept as its own field too, not just folded into the summary text,
        # so the caller can store it in its own database column.
        self.assertEqual(result["question"], "which currency?")

    def test_malformed_json_becomes_a_failure(self):
        self.path.write_text("{not json")
        self.assertEqual(read_result(self.path)["status"], "failed")

    def test_missing_file_becomes_a_failure(self):
        self.assertEqual(read_result(self.path / "nope")["status"], "failed")

    def test_unknown_status_becomes_a_failure(self):
        self.path.write_text('{"status": "vibes", "summary": "hm"}')
        result = read_result(self.path)
        self.assertEqual(result["status"], "failed")
        self.assertIn("vibes", result["summary"])

    def test_non_object_json_becomes_a_failure(self):
        self.path.write_text("[1, 2, 3]")
        self.assertEqual(read_result(self.path)["status"], "failed")


class TotalCostTest(unittest.TestCase):
    def test_sums_result_events_only(self):
        events = load("completed.jsonl") + [{"type": "assistant", "total_cost_usd": 99.0}]
        self.assertAlmostEqual(total_cost(events), 0.0248249)

    def test_no_result_event_is_zero(self):
        self.assertEqual(total_cost([]), 0.0)


class MainLoopTest(unittest.TestCase):
    """End to end against the fake CLI, including one rate-limit recovery."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        make_repo(self.tmp / "repo")
        self.tasks = self.tmp / "tasks.md"
        self.tasks.write_text("- [ ] first thing\n- [ ] second thing\n")
        self.cfg = Config(
            repo=self.tmp / "repo",
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

    def tearDown(self):
        os.environ["PATH"] = self.old_path
        os.environ.pop("FAKE_LIMIT_FLAG", None)

    def test_runs_every_task_and_checks_it_off(self):
        asyncio.run(loop.main_loop(self.cfg, once=True))
        self.assertEqual(self.tasks.read_text(), "- [x] first thing\n- [x] second thing\n")

    def test_records_status_and_cost(self):
        asyncio.run(loop.main_loop(self.cfg, once=True))
        state = State(self.cfg.home / "state.db", str(self.cfg.repo))
        rows = state.db.execute("SELECT * FROM tasks ORDER BY started_at").fetchall()
        self.assertEqual([row["status"] for row in rows], ["done", "done"])
        self.assertEqual([row["summary"] for row in rows], ["fake work", "fake work"])
        self.assertAlmostEqual(rows[0]["cost_usd"], 0.5)

    def test_recovers_from_a_rate_limit_and_finishes_the_task(self):
        flag = self.tmp / "limit.flag"
        flag.write_text("")
        os.environ["FAKE_LIMIT_FLAG"] = str(flag)
        self.tasks.write_text("- [ ] first thing\n")

        asyncio.run(loop.main_loop(self.cfg, once=True))

        self.assertEqual(self.tasks.read_text(), "- [x] first thing\n")
        state = State(self.cfg.home / "state.db", str(self.cfg.repo))
        runs = state.db.execute("SELECT * FROM runs ORDER BY id").fetchall()
        self.assertEqual(len(runs), 2, "expected one limited run and one resume")
        self.assertEqual(runs[0]["exit_reason"], "RateLimited")
        self.assertEqual(runs[1]["exit_reason"], "ReadResult")
        # The resume must reuse the session, not start a fresh one.
        self.assertEqual(runs[0]["session_id"], runs[1]["session_id"])

    def test_gives_up_after_max_resumes_and_marks_for_attention(self):
        # A CLI that never writes a result: every invocation is a nudge.
        fake = self.tmp / "bin" / "claude"
        fake.write_text('#!/usr/bin/env bash\necho \'{"type":"result"}\'\n')
        fake.chmod(0o755)
        self.tasks.write_text("- [ ] doomed thing\n")

        asyncio.run(loop.main_loop(self.cfg, once=True))

        self.assertEqual(self.tasks.read_text(), "- [!] doomed thing\n")
        state = State(self.cfg.home / "state.db", str(self.cfg.repo))
        row = state.db.execute("SELECT * FROM tasks").fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn("no_result", row["summary"])
        runs = state.db.execute("SELECT * FROM runs ORDER BY id").fetchall()
        self.assertEqual(len(runs), self.cfg.max_resumes + 1)
        nudges = ["Nudge"] * self.cfg.max_resumes
        self.assertEqual([row["exit_reason"] for row in runs[:-1]], nudges)
        self.assertEqual(runs[-1]["exit_reason"], "Fail")

    def test_stale_result_from_a_previous_attempt_is_discarded(self):
        stale = self.cfg.home / "runs" / task_id("first thing")
        stale.mkdir(parents=True)
        (stale / "result.json").write_text('{"status": "failed", "summary": "old news"}')
        self.tasks.write_text("- [ ] first thing\n")

        asyncio.run(loop.main_loop(self.cfg, once=True))

        state = State(self.cfg.home / "state.db", str(self.cfg.repo))
        row = state.db.execute("SELECT * FROM tasks").fetchone()
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["summary"], "fake work")

    def test_blocked_result_stores_its_question(self):
        fake = self.tmp / "bin" / "claude"
        fake.write_text(
            "#!/usr/bin/env bash\n"
            'printf \'%s\' \'{"status":"blocked","summary":"stuck",'
            '"question":"which currency?"}\' > "$CLAUDELOOP_RESULT"\n'
            "echo '{\"type\":\"result\",\"total_cost_usd\":0.1}'\n"
        )
        fake.chmod(0o755)
        self.tasks.write_text("- [ ] ambiguous thing\n")

        asyncio.run(loop.main_loop(self.cfg, once=True))

        state = State(self.cfg.home / "state.db", str(self.cfg.repo))
        row = state.db.execute("SELECT * FROM tasks").fetchone()
        self.assertEqual(row["status"], "blocked")
        self.assertEqual(row["question"], "which currency?")
        self.assertIn("which currency?", row["summary"])

    def test_a_crash_outside_the_session_state_machine_is_recorded_not_raised(self):
        # Reproduces `claude` missing from PATH: create_subprocess_exec raises
        # FileNotFoundError. Before the fix this killed the whole process,
        # leaving the task row at 'running' forever and the task file
        # untouched but never retried. once=True must still terminate rather
        # than spin on the same crashing task forever.
        os.environ["PATH"] = git_only_path(self.tmp)

        asyncio.run(loop.main_loop(self.cfg, once=True))

        # Deliberately not marked `- [!]`: an environment fault is not a task
        # verdict, and marking it would burn through the whole list.
        self.assertEqual(self.tasks.read_text(), "- [ ] first thing\n- [ ] second thing\n")
        state = State(self.cfg.home / "state.db", str(self.cfg.repo))
        row = state.db.execute("SELECT * FROM tasks").fetchone()
        # 'error', not 'failed': a source with a re-run backstop keyed on
        # terminal statuses (JiraSource, via State.terminal_ids()) must be
        # able to offer this task again after an environment crash.
        self.assertEqual(row["status"], "error")
        self.assertNotIn(row["id"], State(self.cfg.home / "state.db", str(self.cfg.repo)).terminal_ids())
        self.assertIn("ClaudeLoop crashed", row["summary"])


class StatusWiringTest(unittest.TestCase):
    """Same fixture as MainLoopTest, deliberately duplicated rather than
    inherited: subclassing a TestCase re-runs every one of the parent's tests,
    which here means re-running seven subprocess-driven cases for nothing."""

    def setUp(self):
        from claudeloop import status

        status.reset()
        self.status = status
        self.tmp = Path(tempfile.mkdtemp())
        make_repo(self.tmp / "repo")
        self.tasks = self.tmp / "tasks.md"
        self.tasks.write_text("- [ ] first thing\n- [ ] second thing\n")
        self.cfg = Config(
            repo=self.tmp / "repo",
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

    def tearDown(self):
        os.environ["PATH"] = self.old_path
        os.environ.pop("FAKE_LIMIT_FLAG", None)

    def test_the_loop_ends_idle_with_the_task_fields_cleared(self):
        asyncio.run(loop.main_loop(self.cfg, once=True))
        self.assertEqual(self.status.current.state, "idle")
        self.assertIsNone(self.status.current.task_id)
        self.assertIsNone(self.status.current.run_dir)
        self.assertIsNone(self.status.current.session_id)

    def test_the_heartbeat_is_fresh_after_a_run(self):
        asyncio.run(loop.main_loop(self.cfg, once=True))
        self.assertAlmostEqual(self.status.current.heartbeat, time.time(), delta=5)

    def test_the_quota_reading_is_captured_from_the_stream(self):
        flag = self.tmp / "limit.flag"
        flag.write_text("")
        os.environ["FAKE_LIMIT_FLAG"] = str(flag)
        self.tasks.write_text("- [ ] first thing\n")
        asyncio.run(loop.main_loop(self.cfg, once=True))
        self.assertIsNotNone(self.status.current.rate_limit)
        self.assertEqual(self.status.current.rate_limit["rateLimitType"], "five_hour")

    def test_a_crash_is_recorded_as_the_error_state(self):
        os.environ["PATH"] = git_only_path(self.tmp)
        asyncio.run(loop.main_loop(self.cfg, once=True))
        self.assertEqual(self.status.current.state, "error")
        self.assertIn("claude", self.status.current.last_error or "")

    def test_pending_is_published_on_the_snapshot_and_cleared_on_idle(self):
        # web reads pending off the status snapshot instead of re-reading the
        # task source, so main_loop must publish the whole source-order list
        # -- including the task about to run -- at some point during the run,
        # and clear it once the backlog is drained.
        seen: list[tuple[tuple[str, str], ...]] = []

        real_set_status = self.status.set_status

        def recording_set_status(**changes):
            result = real_set_status(**changes)
            seen.append(result.pending)
            return result

        with mock.patch("claudeloop.status.set_status", side_effect=recording_set_status):
            asyncio.run(loop.main_loop(self.cfg, once=True))

        # Both tasks, in source order, on the same snapshot -- the first
        # poll's pending list, before either task has run.
        texts_by_snapshot = [tuple(text for _id, text in pending) for pending in seen]
        self.assertIn(("first thing", "second thing"), texts_by_snapshot)
        self.assertEqual(self.status.current.pending, ())


class LatestRateLimitTest(unittest.TestCase):
    def test_returns_the_last_one(self):
        events = [
            {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}},
            {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}},
        ]
        self.assertEqual(loop.latest_rate_limit(events)["status"], "rejected")

    def test_none_when_absent_or_malformed(self):
        self.assertIsNone(loop.latest_rate_limit([]))
        self.assertIsNone(loop.latest_rate_limit([{"type": "result"}]))
        self.assertIsNone(
            loop.latest_rate_limit([{"type": "rate_limit_event", "rate_limit_info": "nope"}])
        )


class HeartbeatTest(unittest.TestCase):
    """set_status() only moves on a state transition, and there is none for
    the whole span of `await session.run(...)` (up to session_timeout_s, 4h
    by default) or `await asyncio.sleep(delay)` (up to MAX_WAIT_S, 8 days).
    The background heartbeat task exists to keep status.heartbeat fresh
    through exactly those spans. Reproduced with HEARTBEAT_S monkeypatched
    small and a fake CLI slow enough to outlast it, rather than waiting out
    the real multi-hour/multi-day windows.
    """

    def setUp(self):
        status.reset()
        self.tmp = Path(tempfile.mkdtemp())
        make_repo(self.tmp / "repo")
        self.tasks = self.tmp / "tasks.md"
        self.tasks.write_text("- [ ] slow thing\n")
        self.cfg = Config(repo=self.tmp / "repo", tasks_file=self.tasks, home=self.tmp / "home")
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        slow = bin_dir / "claude"
        slow.write_text(
            "#!/usr/bin/env bash\n"
            "sleep 0.6\n"
            "printf '%s' '{\"status\":\"done\",\"summary\":\"ok\"}' > \"$CLAUDELOOP_RESULT\"\n"
            "echo '{\"type\":\"result\",\"total_cost_usd\":0.1}'\n"
        )
        slow.chmod(0o755)
        self.old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{self.old_path}"
        self.old_heartbeat_s = loop.HEARTBEAT_S
        loop.HEARTBEAT_S = 0.1

    def tearDown(self):
        os.environ["PATH"] = self.old_path
        loop.HEARTBEAT_S = self.old_heartbeat_s

    def test_the_heartbeat_does_not_go_stale_while_a_session_runs(self):
        stale_after = 0.3  # shorter than the fake session's 0.6s sleep

        async def watch():
            task = asyncio.create_task(loop.main_loop(self.cfg, once=True))
            seen_running = False
            went_stale = False
            while not task.done():
                snap = status.current
                if snap.state == "running":
                    seen_running = True
                    if time.time() - snap.heartbeat > stale_after:
                        went_stale = True
                await asyncio.sleep(0.03)
            await task
            return seen_running, went_stale

        seen_running, went_stale = asyncio.run(watch())
        self.assertTrue(seen_running, "never observed the running state")
        self.assertFalse(went_stale, "heartbeat went stale while a session was running")

    def test_the_heartbeat_task_does_not_leak_past_once(self):
        async def scenario():
            await loop.main_loop(self.cfg, once=True)
            return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

        pending = asyncio.run(scenario())
        self.assertEqual(pending, [])


class MainConfigErrorTest(unittest.TestCase):
    """A pre-existing config.toml at the default umask (0644) makes
    load_config raise ValueError from the permissions guard. Before the fix,
    main() only caught FileNotFoundError, so every current operator's next
    start died with a raw traceback instead of the guard's own message."""

    def setUp(self):
        # main() now checks DEFAULT_CONFIG.exists() before load_config runs,
        # to decide whether to enter the setup wizard. These tests are about
        # load_config's own error paths, not the wizard, so DEFAULT_CONFIG
        # must point at a path that exists -- otherwise, on a machine with no
        # real ~/.claudeloop/config.toml, main() would launch the real
        # wizard server and block waiting for a browser.
        real_default = loop.DEFAULT_CONFIG
        self.addCleanup(lambda: setattr(loop, "DEFAULT_CONFIG", real_default))
        loop.DEFAULT_CONFIG = Path(__file__)

    def test_a_value_error_from_load_config_exits_cleanly_with_its_message(self):
        with mock.patch(
            "claudeloop.loop.load_config",
            side_effect=ValueError("config.toml: ... Run: chmod 600 ..."),
        ):
            with self.assertRaises(SystemExit) as caught:
                loop.main([])
        self.assertIn("chmod 600", str(caught.exception))

    def test_a_repo_that_cannot_do_worktrees_exits_with_the_probe_message(self):
        cfg = Config(repo=Path("/nope"), tasks_file=Path("/tmp/tasks.md"),
                     home=Path("/tmp/home"))
        with mock.patch.object(loop, "load_config", return_value=cfg), \
             mock.patch.object(loop.worktree, "probe",
                               return_value="cannot use git worktrees in /nope"), \
             mock.patch.object(loop, "_serve_dashboard") as serve:
            with self.assertRaises(SystemExit) as raised:
                loop.main([])

        self.assertIn("cannot use git worktrees", str(raised.exception))
        serve.assert_not_called()


class ServeDashboardTest(unittest.TestCase):
    def test_a_bind_failure_does_not_prevent_the_loop_from_running(self):
        # A real bound-and-listening socket, not a mock: SO_REUSEADDR (which
        # _Server sets) only helps rebind a port stuck in TIME_WAIT, not one
        # actively held by a live listener, so this reliably reproduces
        # OSError: Address already in use.
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        self.addCleanup(blocker.close)
        port = blocker.getsockname()[1]

        tmp = Path(tempfile.mkdtemp())
        (tmp / "repo" / ".git").mkdir(parents=True)
        tasks = tmp / "tasks.md"
        tasks.write_text("")  # nothing pending: main_loop(once=True) returns at once
        cfg = Config(
            repo=tmp / "repo",
            tasks_file=tasks,
            home=tmp / "home",
            web_host="127.0.0.1",
            web_port=port,
        )

        loop._serve_dashboard(cfg)  # must swallow the OSError, not raise

        status.reset()
        asyncio.run(loop.main_loop(cfg, once=True))
        self.assertEqual(status.current.state, "idle")


class PromptSelectionTest(unittest.TestCase):
    """A nudge (no result, no rate limit) and a quota resume must send
    different prompts: the old shared "Continue." read as confirmation that
    nothing was left to do, which is exactly what burned two live
    smoke-test sessions' resumes on finished, committed work."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        make_repo(self.tmp / "repo")
        self.tasks = self.tmp / "tasks.md"
        self.tasks.write_text("- [ ] do the thing\n")
        self.cfg = Config(
            repo=self.tmp / "repo",
            tasks_file=self.tasks,
            home=self.tmp / "home",
            max_resumes=5,
        )

    def test_nudge_prompt_names_the_result_file(self):
        self.assertIn("CLAUDELOOP_RESULT", loop.NUDGE_PROMPT)
        self.assertNotEqual(loop.NUDGE_PROMPT, loop.CONTINUE_PROMPT)

    def test_first_attempt_gets_the_task_then_a_wait_says_continue_then_a_nudge_names_the_problem(
        self,
    ):
        state = State(self.cfg.home / "state.db", str(self.cfg.repo))
        source = FileSource(self.tasks)
        task = source.pending()[0]

        past = time.time() - 120
        rate_limited = [
            {
                "type": "rate_limit_event",
                "rate_limit_info": {"status": "rejected", "resetsAt": past},
            }
        ]
        clean_no_result = [{"type": "result", "total_cost_usd": 0.0}]
        prompts = []

        async def fake_run(cfg, run_dir, session_id, prompt, resume, cwd=None):
            prompts.append(prompt)
            if len(prompts) == 1:
                return rate_limited  # quota wait: next prompt must be "Continue."
            if len(prompts) == 2:
                return clean_no_result  # nudge: next prompt must name the problem
            (run_dir / "result.json").write_text('{"status": "done", "summary": "ok"}')
            return [{"type": "result", "total_cost_usd": 0.0}]

        with mock.patch("claudeloop.loop.session.run", side_effect=fake_run):
            asyncio.run(loop.run_task(self.cfg, state, source, task))

        self.assertEqual(prompts, [task.text, loop.CONTINUE_PROMPT, loop.NUDGE_PROMPT])


class WorktreePerTaskTest(unittest.TestCase):
    """End to end against a real git repo and a fake `claude` that commits
    wherever it is run. Each task must land on its own branch, in its own
    tree, carrying only its own commit."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = make_repo(self.tmp / "repo")
        self.tasks = self.tmp / "tasks.md"
        self.tasks.write_text("- [ ] first thing\n- [ ] second thing\n")
        self.cfg = Config(
            repo=self.repo, tasks_file=self.tasks, home=self.tmp / "home", max_resumes=3
        )
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "claude"
        # The file it commits is named after the run directory (named after
        # the task id), so each invocation's commit is unique without relying
        # on timing -- and the branch it lands on is whatever ClaudeLoop gave
        # it as a working tree, which is the whole point of the test.
        fake.write_text(
            "#!/usr/bin/env bash\n"
            "set -e\n"
            'name="$(basename "$(dirname "$CLAUDELOOP_RESULT")")"\n'
            'echo work > "$name.txt"\n'
            'git add "$name.txt"\n'
            'git commit -q -m "$name"\n'
            "git rev-parse --abbrev-ref HEAD >> "
            f'"{self.tmp}/branches.txt"\n'
            'printf \'%s\' \'{"status":"done","summary":"ok"}\' > "$CLAUDELOOP_RESULT"\n'
            "echo '{\"type\":\"result\",\"total_cost_usd\":0.01}'\n"
        )
        fake.chmod(0o755)
        self.old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{self.old_path}"

    def tearDown(self):
        os.environ["PATH"] = self.old_path

    def test_each_task_commits_on_its_own_branch_carrying_only_its_own_commit(self):
        asyncio.run(loop.main_loop(self.cfg, once=True))

        branches = [
            line.strip()
            for line in (self.tmp / "branches.txt").read_text().splitlines()
            if line.strip()
        ]
        self.assertEqual(len(branches), 2)
        self.assertEqual(len(set(branches)), 2, "two tasks must not share a branch")
        for branch in branches:
            self.assertTrue(branch.startswith("claudeloop/"))
            ahead = subprocess.run(
                ["git", "rev-list", "--count", f"main..{branch}"],
                cwd=self.repo, capture_output=True, text=True, check=True,
                stdin=subprocess.DEVNULL,
            ).stdout.strip()
            self.assertEqual(ahead, "1", f"{branch} should carry only its own commit")

    def test_the_repository_itself_is_never_moved_off_its_branch(self):
        asyncio.run(loop.main_loop(self.cfg, once=True))

        head = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=self.repo,
            capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL,
        ).stdout.strip()
        self.assertEqual(head, "main")

    def test_a_finished_tasks_worktree_is_released(self):
        asyncio.run(loop.main_loop(self.cfg, once=True))

        trees = self.cfg.home / "worktrees"
        # Both halves matter: an empty check that tolerates a missing
        # directory passes against code that never made a worktree at all.
        self.assertTrue(trees.exists(), "the run must have created worktrees here")
        self.assertEqual([p for p in trees.iterdir() if p.is_dir()], [])


class BuildSourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.state = State(self.tmp / "state.db")

    def test_file_config_builds_a_file_source(self):
        cfg = Config(repo=self.repo, tasks_file=self.tmp / "tasks.md", home=self.tmp)
        source = loop.build_source(cfg, self.state)
        self.assertIsInstance(source, FileSource)

    def test_jira_config_builds_a_jira_source_wired_to_the_database(self):
        cfg = Config(
            repo=self.repo,
            home=self.tmp,
            source="jira",
            jira=JiraConfig("https://example.atlassian.net", "me@example.com",
                            "secret", "project = OPS", "In Progress", "Done"),
        )
        source = loop.build_source(cfg, self.state)
        self.assertIsInstance(source, JiraSource)
        self.assertEqual(source.jql, "project = OPS")
        self.assertEqual(source.transition_start, "In Progress")
        self.assertEqual(source.transition_done, "Done")
        self.assertIs(source.state, self.state)


class RecordingSource:
    """A TaskSource that records the lifecycle calls run_task makes on it."""

    def __init__(self):
        self.calls = []

    def pending(self):
        return []

    def start(self, task):
        self.calls.append(("start", task.id))

    def mark(self, task, status, summary, cost=0.0):
        self.calls.append(("mark", status, cost))


class SourceLifecycleTest(unittest.TestCase):
    """run_task must tell the source when work starts, and what it cost.

    Same fake-CLI harness as MainLoopTest above: tests/fake_claude.sh writes
    a done result with summary "fake work" and a total_cost_usd of 0.5.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        make_repo(self.tmp / "repo")
        self.cfg = Config(
            repo=self.tmp / "repo",
            tasks_file=self.tmp / "tasks.md",
            home=self.tmp / "home",
            max_resumes=3,
        )
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        shutil.copy(Path(__file__).parent / "fake_claude.sh", bin_dir / "claude")
        (bin_dir / "claude").chmod(0o755)
        self.old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{self.old_path}"

    def tearDown(self):
        os.environ["PATH"] = self.old_path

    def test_start_comes_first_and_mark_carries_the_cost(self):
        state = State(self.cfg.home / "state.db", str(self.cfg.repo))
        source = RecordingSource()
        task = Task("abcd1234abcd1234", "OPS-1: do it", "jira", "OPS-1")
        asyncio.run(loop.run_task(self.cfg, state, source, task))
        self.assertEqual(source.calls[0], ("start", task.id))
        self.assertEqual(source.calls[-1][:2], ("mark", "done"))
        self.assertAlmostEqual(source.calls[-1][2], 0.5)


class ResumeWithAnswerTest(unittest.TestCase):
    """Same fake-CLI fixture as MainLoopTest, deliberately duplicated rather
    than inherited: subclassing a TestCase re-runs every parent test."""

    def setUp(self):
        status.reset()
        self.tmp = Path(tempfile.mkdtemp())
        repo = self.tmp / "repo"
        make_repo(repo)
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
        self.state = State(self.cfg.home / "state.db", str(self.cfg.repo))
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

    def fake_blocked(self) -> None:
        """Swap the CLI on PATH for one that parks, so the same fixture can
        produce both a terminal verdict and a blocked one."""
        fake = Path(os.environ["PATH"].split(os.pathsep)[0]) / "claude"
        fake.write_text(
            "#!/usr/bin/env bash\n"
            'printf \'%s\' \'{"status":"blocked","summary":"stuck",'
            '"question":"which currency?"}\' > "$CLAUDELOOP_RESULT"\n'
            "echo '{\"type\":\"result\",\"total_cost_usd\":0.1}'\n"
        )
        fake.chmod(0o755)

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
        self.assertIn("exactly as you left it", self.args())

    def test_a_resume_returns_to_the_same_worktree(self):
        # The point of the slice: the parked session finds its own tree,
        # including work it never committed.
        self.park()
        tree = self.cfg.home / "worktrees" / self.task.id
        tree.mkdir(parents=True, exist_ok=True)

        calls = []
        with mock.patch.object(loop.worktree, "ensure",
                               side_effect=lambda repo, root, task_id: (
                                   calls.append((repo, root, task_id)) or tree)), \
             mock.patch.object(loop.worktree, "release"):
            asyncio.run(loop.run_task(self.cfg, self.state, self.source, self.task,
                                      resume_with="use EUR"))

        self.assertEqual(calls, [(self.cfg.repo, self.cfg.home / "worktrees",
                                  self.task.id)])

    def test_a_parked_task_keeps_its_worktree_and_a_finished_one_does_not(self):
        released = []
        tree = self.cfg.home / "worktrees" / self.task.id
        tree.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(loop.worktree, "ensure",
                               side_effect=lambda repo, root, task_id: tree), \
             mock.patch.object(loop.worktree, "release",
                               side_effect=lambda repo, path: released.append(path)):
            # fake_claude.sh writes a done result.
            asyncio.run(loop.run_task(self.cfg, self.state, self.source, self.task))
            self.assertEqual(released, [tree])

            released.clear()
            self.fake_blocked()
            asyncio.run(loop.run_task(self.cfg, self.state, self.source, self.task))

        self.assertEqual(released, [], "a parked task must keep its tree")

    def test_a_parked_tasks_tree_really_survives(self):
        """The same claim as the test above, with no mocks in the way.

        The mocked version asserts that `release` was not *called*, which is
        a statement about run_task's control flow, not about the disk. Real
        `ensure` and real `release` run here against the fixture's real
        repository, so this fails if the tree is gone however it went --
        including for reasons a recorded call list cannot see. S2b's
        equivalent defect survived eleven scoped reviews and 421 passing
        tests precisely because nothing looked at what was actually left
        behind.
        """
        self.fake_blocked()

        asyncio.run(loop.run_task(self.cfg, self.state, self.source, self.task))

        tree = self.cfg.home / "worktrees" / self.task.id
        # A worktree's .git is a file pointing back at the repository; its
        # presence is what makes this a live checkout rather than a leftover
        # directory.
        self.assertTrue((tree / ".git").exists(), "a parked task must keep its tree")
        registered = subprocess.run(
            ["git", "worktree", "list"], cwd=self.cfg.repo,
            capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL,
        ).stdout
        self.assertIn(f"claudeloop/{self.task.id}", registered)

    def test_a_task_whose_worktree_cannot_be_created_is_an_environment_fault(self):
        # Not a verdict on the task. Whatever stops `git worktree add` -- an
        # index.lock a stray process holds, a full disk -- stops it for every
        # task, so failing this one would burn the whole list `- [!]` in
        # seconds and 'failed' is terminal, which would keep a source from
        # ever offering any of them again. run_task lets it out, and
        # main_loop's crash handler records 'error' without marking.
        with mock.patch.object(loop.worktree, "ensure",
                               side_effect=RuntimeError("no disk")):
            with self.assertRaises(RuntimeError):
                asyncio.run(loop.run_task(self.cfg, self.state, self.source, self.task))

            with self.assertLogs("claudeloop", level="ERROR"):
                asyncio.run(loop.main_loop(self.cfg, once=True))

        row = State(self.cfg.home / "state.db", str(self.cfg.repo)).db.execute(
            "SELECT * FROM tasks WHERE id=?", (self.task.id,)).fetchone()
        self.assertEqual(row["status"], "error")
        self.assertNotIn(self.task.id, State(self.cfg.home / "state.db", str(self.cfg.repo)).terminal_ids())
        self.assertEqual(self.tasks.read_text(), "- [ ] first thing\n",
                         "an environment fault must not mark the task in its source")
        # The dashboard has to see it too: the failure path this replaced
        # returned before run_task published any status at all.
        self.assertEqual(status.current.state, "error")
        self.assertIn("no disk", status.current.last_error or "")

    def test_a_resume_does_not_re_fire_the_source_start_hook(self):
        started = []
        self.source.start = lambda task: started.append(task)
        self.park()

        asyncio.run(loop.run_task(self.cfg, self.state, self.source, self.task,
                                  resume_with="use EUR"))

        self.assertEqual(started, [])

    def test_a_normal_task_still_fires_start(self):
        started = []
        self.source.start = lambda task: started.append(task)

        asyncio.run(loop.run_task(self.cfg, self.state, self.source, self.task))

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
        self.state = State(self.cfg.home / "state.db", str(self.cfg.repo))
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
        make_repo(self.tmp / "repo")
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
        state = State(self.cfg.home / "state.db", str(self.cfg.repo))
        rows = state.db.execute("SELECT status FROM tasks").fetchall()
        self.assertEqual([row["status"] for row in rows], ["blocked", "blocked"])

    def test_an_answered_task_is_reopened_and_resumed_before_new_work(self):
        self.blocking_cli()
        self.tasks.write_text("- [ ] ambiguous thing\n")
        asyncio.run(loop.main_loop(self.cfg, once=True))
        self.assertEqual(self.tasks.read_text(), "- [!] ambiguous thing\n")

        # A human answers, exactly as the dashboard's POST route will.
        state = State(self.cfg.home / "state.db", str(self.cfg.repo))
        parked = state.blocked()[0]
        run_dir = self.cfg.home / "runs" / parked["id"]
        (run_dir / "answer.json").write_text(json.dumps({"answer": "use EUR"}))

        # Back to a CLI that finishes.
        shutil.copy(Path(__file__).parent / "fake_claude.sh", self.fake)
        self.fake.chmod(0o755)

        asyncio.run(loop.main_loop(self.cfg, once=True))

        self.assertEqual(self.tasks.read_text(), "- [x] ambiguous thing\n")
        row = State(self.cfg.home / "state.db", str(self.cfg.repo)).db.execute(
            "SELECT status FROM tasks").fetchone()
        self.assertEqual(row["status"], "done")
        self.assertFalse((run_dir / "answer.json").exists(),
                         "the answer must be consumed, not left to fire again")

    def test_a_source_that_cannot_be_reopened_still_resumes(self):
        self.blocking_cli()
        self.tasks.write_text("- [ ] ambiguous thing\n")
        asyncio.run(loop.main_loop(self.cfg, once=True))
        state = State(self.cfg.home / "state.db", str(self.cfg.repo))
        parked = state.blocked()[0]
        (self.cfg.home / "runs" / parked["id"] / "answer.json").write_text(
            json.dumps({"answer": "use EUR"}))
        shutil.copy(Path(__file__).parent / "fake_claude.sh", self.fake)
        self.fake.chmod(0o755)

        with mock.patch.object(FileSource, "reopen", side_effect=OSError("disk gone")):
            with self.assertLogs("claudeloop", level="WARNING") as logs:
                asyncio.run(loop.main_loop(self.cfg, once=True))

        self.assertTrue(
            any("could not reopen" in line for line in logs.output), logs.output
        )
        row = State(self.cfg.home / "state.db", str(self.cfg.repo)).db.execute(
            "SELECT status FROM tasks").fetchone()
        self.assertEqual(row["status"], "done")

    def test_a_fault_in_the_answered_scan_does_not_stop_ordinary_work(self):
        # A locked database or a Jira fault must cost this poll its answer
        # check, not the loop's ability to do ordinary pending work.
        self.tasks.write_text("- [ ] first thing\n")

        with mock.patch.object(loop, "find_answered", side_effect=RuntimeError("boom")):
            with self.assertLogs("claudeloop", level="ERROR"):
                asyncio.run(loop.main_loop(self.cfg, once=True))

        self.assertEqual(self.tasks.read_text(), "- [x] first thing\n")

    def test_a_resumed_task_commits_only_its_own_work_not_the_intervening_tasks(self):
        # Regression for the S2b live smoke test: task 1 parked before its
        # first commit (the usual case -- the question that blocks it blocks
        # it early), task 2 then ran and left its own branch checked out, and
        # the resumed task 1 committed onto *that* branch -- observed for real
        # as "File committed to add-gitignore branch". Under one worktree per
        # task there is no shared checkout to inherit. This pins that.
        count_file = self.tmp / "invocations"
        self.fake.write_text(
            "#!/usr/bin/env bash\n"
            "set -e\n"
            f'n=$(( $(cat "{count_file}" 2>/dev/null || echo 0) + 1 ))\n'
            f'echo "$n" > "{count_file}"\n'
            'name="$(basename "$(dirname "$CLAUDELOOP_RESULT")")"\n'
            'if [ "$n" -eq 1 ]; then\n'
            '  printf \'%s\' \'{"status":"blocked","summary":"stuck",'
            '"question":"which currency?"}\' > "$CLAUDELOOP_RESULT"\n'
            "else\n"
            '  echo work > "$name.txt"\n'
            '  git add "$name.txt"\n'
            '  git commit -q -m "$name"\n'
            '  printf \'%s\' \'{"status":"done","summary":"ok"}\' > "$CLAUDELOOP_RESULT"\n'
            "fi\n"
            "echo '{\"type\":\"result\",\"total_cost_usd\":0.1}'\n"
        )
        self.fake.chmod(0o755)
        self.tasks.write_text("- [ ] ambiguous thing\n- [ ] second thing\n")

        asyncio.run(loop.main_loop(self.cfg, once=True))

        self.assertEqual(self.tasks.read_text(),
                         "- [!] ambiguous thing\n- [x] second thing\n")
        state = State(self.cfg.home / "state.db", str(self.cfg.repo))
        parked = state.blocked()[0]
        others = [row["id"] for row in
                  state.db.execute("SELECT id FROM tasks WHERE status='done'").fetchall()]
        self.assertEqual(len(others), 1)
        self.assertTrue((self.cfg.home / "worktrees" / parked["id"]).exists(),
                        "a parked task must keep its worktree while other tasks run")

        # A human answers the parked task.
        (self.cfg.home / "runs" / parked["id"] / "answer.json").write_text(
            json.dumps({"answer": "use EUR"}))

        asyncio.run(loop.main_loop(self.cfg, once=True))

        # The resumed session committed in its own tree, on its own branch.
        files = subprocess.run(
            ["git", "ls-tree", "--name-only", f"claudeloop/{parked['id']}"],
            cwd=self.cfg.repo, check=True, capture_output=True, text=True,
            stdin=subprocess.DEVNULL,
        ).stdout.split()
        self.assertIn(f"{parked['id']}.txt", files)
        self.assertNotIn(f"{others[0]}.txt", files,
                         "the resumed task must not carry the intervening task's work")


class MainSetupTest(unittest.TestCase):
    """main() enters setup mode when there is no config, and on --setup.

    Stubbed rather than mocked: run_setup is replaced with a function that
    records the call and raises, which is enough to pin the ordering without
    standing up a server or running the loop.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.calls = []
        self.real_run_setup = loop.setup.run_setup
        self.real_default = loop.DEFAULT_CONFIG
        self.addCleanup(lambda: setattr(loop.setup, "run_setup", self.real_run_setup))
        self.addCleanup(lambda: setattr(loop, "DEFAULT_CONFIG", self.real_default))

        def stub(path, home, port=8765):
            self.calls.append(path)
            raise SystemExit("stub ran")

        loop.setup.run_setup = stub
        loop.DEFAULT_CONFIG = self.tmp / "config.toml"

    def test_no_config_file_enters_setup(self):
        with self.assertRaises(SystemExit) as caught:
            loop.main([])
        self.assertEqual(str(caught.exception), "stub ran")
        self.assertEqual(self.calls, [self.tmp / "config.toml"])

    def test_setup_flag_enters_setup_even_with_a_config(self):
        loop.DEFAULT_CONFIG.write_text("repo = \"/nope\"\n")
        loop.DEFAULT_CONFIG.chmod(0o600)
        with self.assertRaises(SystemExit):
            loop.main(["--setup"])
        self.assertEqual(len(self.calls), 1)

    def test_an_existing_config_does_not_enter_setup(self):
        loop.DEFAULT_CONFIG.write_text("nonsense = [\n")
        loop.DEFAULT_CONFIG.chmod(0o600)
        # SystemExit, not Exception: it does not subclass Exception, and
        # main()'s existing ValueError guard deliberately turns a bad
        # config.toml into a friendly SystemExit message rather than a raw
        # traceback -- see the comment on that clause in loop.py.
        with self.assertRaises(SystemExit):
            loop.main([])
        self.assertEqual(self.calls, [])


class MainReconcilesPluginsTest(unittest.TestCase):
    """main() must refuse to start on a box that could not get the plugins
    the operator chose -- the same treatment worktree.probe gets, and for
    the same reason: otherwise every task runs in a shape nobody asked for."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_a_plugin_problem_stops_startup_before_the_dashboard_binds(self):
        # asyncio.run is patched too, not just asserted unreached below: if
        # the SystemExit gate this test pins were ever deleted, main() would
        # fall through into the real main_loop and poll forever against this
        # temp-dir config, hanging the whole suite instead of failing it.
        with unittest.mock.patch("claudeloop.loop.load_config") as load, \
             unittest.mock.patch("claudeloop.worktree.probe", return_value=None), \
             unittest.mock.patch("claudeloop.plugins.reconcile",
                                 return_value="marketplace unreachable") as reconcile, \
             unittest.mock.patch("claudeloop.loop._serve_dashboard") as serve, \
             unittest.mock.patch("claudeloop.loop.asyncio.run") as run, \
             unittest.mock.patch("claudeloop.loop.DEFAULT_CONFIG") as config_path:
            config_path.exists.return_value = True
            load.return_value = Config(repo=self.tmp, tasks_file=self.tmp / "t.md",
                                       home=self.tmp, plugins=("caveman",))
            with self.assertRaises(SystemExit) as raised:
                main([])
        self.assertIn("marketplace unreachable", str(raised.exception))
        reconcile.assert_called_once_with(("caveman",))
        serve.assert_not_called()
        run.assert_not_called()

    def test_a_clean_reconcile_lets_startup_continue(self):
        with unittest.mock.patch("claudeloop.loop.load_config") as load, \
             unittest.mock.patch("claudeloop.worktree.probe", return_value=None), \
             unittest.mock.patch("claudeloop.plugins.reconcile", return_value=None), \
             unittest.mock.patch("claudeloop.loop._serve_dashboard") as serve, \
             unittest.mock.patch("claudeloop.loop.main_loop",
                                 new_callable=unittest.mock.MagicMock), \
             unittest.mock.patch("claudeloop.loop.asyncio.run") as run, \
             unittest.mock.patch("claudeloop.loop.DEFAULT_CONFIG") as config_path:
            config_path.exists.return_value = True
            load.return_value = Config(repo=self.tmp, tasks_file=self.tmp / "t.md",
                                       home=self.tmp, plugins=("caveman",))
            main([])
        serve.assert_called_once()
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
