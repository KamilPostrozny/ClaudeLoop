import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from claudeloop import session
from claudeloop.config import Config

FAKE = Path(__file__).parent / "fake_claude.sh"


def fake_path_dir(tmp: Path) -> Path:
    """A directory containing an executable named `claude` that is our fake."""
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    shutil.copy(FAKE, bin_dir / "claude")
    (bin_dir / "claude").chmod(0o755)
    return bin_dir


class BuildCommandTest(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(repo=Path("/repo"), tasks_file=Path("/tasks.md"), model="sonnet")

    def test_first_run_assigns_the_session_id(self):
        cmd = session.build_command(self.cfg, "uuid-1", "do it", resume=False)
        self.assertEqual(cmd[:4], ["claude", "-p", "do it", "--session-id"])
        self.assertEqual(cmd[4], "uuid-1")
        self.assertNotIn("--resume", cmd)

    def test_resume_uses_resume_and_not_session_id(self):
        cmd = session.build_command(self.cfg, "uuid-1", "Continue.", resume=True)
        self.assertIn("--resume", cmd)
        self.assertNotIn("--session-id", cmd)

    def test_carries_the_flags_the_loop_depends_on(self):
        cmd = session.build_command(self.cfg, "uuid-1", "do it", resume=False)
        self.assertIn("--output-format", cmd)
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "stream-json")
        self.assertIn("--verbose", cmd)
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "bypassPermissions")
        self.assertEqual(cmd[cmd.index("--model") + 1], "sonnet")
        self.assertEqual(cmd[cmd.index("--append-system-prompt") + 1], session.PROTOCOL)

    def test_protocol_names_the_result_variable_and_every_status(self):
        for token in ("CLAUDELOOP_RESULT", "CLAUDE.md", "done", "failed", "blocked"):
            self.assertIn(token, session.PROTOCOL)


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "repo" / ".git").mkdir(parents=True)
        self.cfg = Config(
            repo=self.tmp / "repo",
            tasks_file=self.tmp / "tasks.md",
            home=self.tmp / "home",
        )
        self.run_dir = self.tmp / "home" / "runs" / "abc"
        self.old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{fake_path_dir(self.tmp)}{os.pathsep}{self.old_path}"

    def tearDown(self):
        os.environ["PATH"] = self.old_path

    def run_once(self, resume: bool = False) -> list[dict]:
        return asyncio.run(
            session.run(self.cfg, self.run_dir, "uuid-1", "do it", resume=resume)
        )

    def test_returns_parsed_events_and_skips_non_json_lines(self):
        events = self.run_once()
        types = [event.get("type") for event in events]
        self.assertEqual(types, ["system", "result"])

    def test_logs_every_raw_line_including_the_unparseable_one(self):
        self.run_once()
        lines = (self.run_dir / "events.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("this line is not json", lines)

    def test_sets_the_result_path_in_the_environment(self):
        self.run_once()
        result = json.loads((self.run_dir / "result.json").read_text())
        self.assertEqual(result["status"], "done")

    def test_captures_stderr(self):
        self.run_once()
        self.assertIn("diagnostic noise", (self.run_dir / "stderr.log").read_text())

    def test_appends_to_the_event_log_across_invocations(self):
        self.run_once()
        self.run_once(resume=True)
        lines = (self.run_dir / "events.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 6)

    def test_survives_a_non_zero_exit(self):
        flag = self.tmp / "limit.flag"
        flag.write_text("")
        os.environ["FAKE_LIMIT_FLAG"] = str(flag)
        try:
            events = self.run_once()
        finally:
            del os.environ["FAKE_LIMIT_FLAG"]
        self.assertEqual(events[-1]["type"], "rate_limit_event")
        self.assertFalse((self.run_dir / "result.json").exists())


class OverlongLineTest(unittest.TestCase):
    """Covers the over-long-line path on both pipes.

    A real subprocess isn't needed to reproduce this: an asyncio.StreamReader
    built with a small `limit` and fed a line past that limit raises the same
    ValueError from readline() that a real 16 MiB overrun would raise.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    async def _read_events_case(self, limit: int):
        stream = asyncio.StreamReader(limit=limit)
        stream.feed_data(b'{"type":"before"}\n')
        stream.feed_data(b"x" * (limit * 2) + b"\n")
        stream.feed_data(b'{"type":"after"}\n')
        stream.feed_eof()
        events: list[dict] = []
        path = self.tmp / "events.jsonl"
        await session._read_events(stream, path, events, limit=limit)
        return events, path

    def test_overlong_stdout_line_is_not_silently_dropped(self):
        events, path = asyncio.run(self._read_events_case(64))
        # Events either side of the overrun still come through...
        self.assertEqual([e["type"] for e in events], ["before", "after"])
        # ...and the overrun itself leaves a durable trace on disk instead of
        # vanishing with nothing written for it.
        text = path.read_text()
        self.assertIn("exceeded", text)

    async def _drain_case(self, limit: int):
        stream = asyncio.StreamReader(limit=limit)
        stream.feed_data(b"short line\n")
        stream.feed_data(b"x" * (limit * 2) + b"\n")
        stream.feed_data(b"after the overrun\n")
        stream.feed_eof()
        path = self.tmp / "stderr.log"
        await session._drain(stream, path, limit=limit)
        return path

    def test_overlong_stderr_line_does_not_crash_the_drain(self):
        # Before the fix this raised an uncaught ValueError out of _drain,
        # which -- run under the same asyncio.gather as _read_events -- would
        # take down the whole run() and lose every event already collected.
        path = asyncio.run(self._drain_case(64))
        text = path.read_text()
        self.assertIn("short line", text)
        self.assertIn("after the overrun", text)
        self.assertIn("exceeded", text)


if __name__ == "__main__":
    unittest.main()
