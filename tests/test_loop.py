import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from claudeloop import loop
from claudeloop.config import Config
from claudeloop.loop import (
    FALLBACK_WAIT_S,
    MAX_WAIT_S,
    RESET_PAD_S,
    Fail,
    ReadResult,
    Resume,
    blocking_reset,
    decide,
    read_result,
    sleep_delay,
    total_cost,
)
from claudeloop.source import FileSource, task_id
from claudeloop.state import State

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> list[dict]:
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines() if line]


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
        (self.tmp / "repo" / ".git").mkdir(parents=True)
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
        state = State(self.cfg.home / "state.db")
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
        state = State(self.cfg.home / "state.db")
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
        state = State(self.cfg.home / "state.db")
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

        state = State(self.cfg.home / "state.db")
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

        state = State(self.cfg.home / "state.db")
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
        os.environ["PATH"] = self.tmp_empty_bin()

        asyncio.run(loop.main_loop(self.cfg, once=True))

        # Deliberately not marked `- [!]`: an environment fault is not a task
        # verdict, and marking it would burn through the whole list.
        self.assertEqual(self.tasks.read_text(), "- [ ] first thing\n- [ ] second thing\n")
        state = State(self.cfg.home / "state.db")
        row = state.db.execute("SELECT * FROM tasks").fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn("ClaudeLoop crashed", row["summary"])

    def tmp_empty_bin(self) -> str:
        """A PATH with no `claude` on it, to force create_subprocess_exec to
        raise FileNotFoundError like a host missing the CLI would."""
        empty = self.tmp / "empty-bin"
        empty.mkdir()
        return str(empty)


if __name__ == "__main__":
    unittest.main()
