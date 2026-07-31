import json
import tempfile
import unittest
from pathlib import Path

from claudeloop.loop import (
    RESET_PAD_S,
    Fail,
    ReadResult,
    Resume,
    blocking_reset,
    decide,
    read_result,
    total_cost,
)

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
        self.assertGreater(blocking_reset(events), 0)


class DecideTest(unittest.TestCase):
    def test_result_file_wins_even_over_a_rate_limit(self):
        action = decide(load("rate_limited.jsonl"), True, 0, 20)
        self.assertIsInstance(action, ReadResult)

    def test_rate_limit_waits_until_the_reset(self):
        action = decide(load("rate_limited.jsonl"), False, 0, 20)
        self.assertEqual(action, Resume(wait_until=1785516000.0 + RESET_PAD_S))

    def test_clean_exit_without_a_result_is_nudged(self):
        action = decide(load("completed.jsonl"), False, 0, 20)
        self.assertEqual(action, Resume(wait_until=0.0))

    def test_exhausted_resumes_fails(self):
        action = decide(load("completed.jsonl"), False, 20, 20)
        self.assertEqual(action, Fail("no_result"))

    def test_exhausted_resumes_fails_even_when_rate_limited(self):
        action = decide(load("rate_limited.jsonl"), False, 20, 20)
        self.assertEqual(action, Fail("no_result"))

    def test_empty_stream_is_nudged(self):
        self.assertEqual(decide([], False, 0, 20), Resume(wait_until=0.0))


class ReadResultTest(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "result.json"

    def test_reads_a_good_file(self):
        self.path.write_text('{"status": "done", "summary": "all green"}')
        self.assertEqual(read_result(self.path), {"status": "done", "summary": "all green"})

    def test_blocked_folds_the_question_into_the_summary(self):
        self.path.write_text(
            '{"status": "blocked", "summary": "stuck", "question": "which currency?"}'
        )
        result = read_result(self.path)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("which currency?", result["summary"])

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


if __name__ == "__main__":
    unittest.main()
