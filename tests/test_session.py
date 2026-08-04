import asyncio
import contextlib
import json
import os
import shutil
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from claudeloop import session
from claudeloop.config import Config
from claudeloop.prompt import compose

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
        self.assertEqual(
            cmd[cmd.index("--append-system-prompt") + 1], compose(self.cfg)
        )

    def test_the_default_branch_reaches_the_appended_prompt(self):
        # The session cannot check the default branch out and must name HEAD
        # to push to it, so the name has to arrive as fact rather than be
        # inferred -- inferring it is what shipped nothing.
        cmd = session.build_command(
            self.cfg, "uuid-1", "do it", resume=False,
            tree=Path("/worktrees/abc"), default_branch="trunk",
        )
        sent = cmd[cmd.index("--append-system-prompt") + 1]
        self.assertIn("git push origin HEAD:trunk", sent)
        self.assertIn("/worktrees/abc", sent)


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

    def test_returns_only_rate_limit_and_result_events(self):
        # decide() and total_cost() only ever look at these two types; a
        # multi-hour session's `system`/`assistant`/`user` events (including
        # full tool results under bypassPermissions) must not pile up in
        # memory just to be read once and discarded.
        events = self.run_once()
        types = [event.get("type") for event in events]
        self.assertEqual(types, ["result"])

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

    def test_run_dir_is_created_owner_only(self):
        # The permissions guard in config.py refuses a config.toml readable
        # beyond its owner because it holds [session_env] credentials; those
        # same credentials end up in this directory's log files, so the
        # directory itself must not be world- or group-readable either.
        self.run_once()
        self.assertEqual(self.run_dir.stat().st_mode & 0o777, 0o700)

    def test_log_files_are_created_owner_only(self):
        self.run_once()
        self.assertEqual((self.run_dir / "events.jsonl").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.run_dir / "stderr.log").stat().st_mode & 0o777, 0o600)

    def test_run_dir_is_still_locked_down_if_it_pre_existed_world_readable(self):
        # run_task (loop.py) creates run_dir before session.run does, with
        # the default umask -- mkdir(exist_ok=True) alone would leave an
        # already-existing directory's mode untouched.
        self.run_dir.mkdir(parents=True)
        self.run_dir.chmod(0o755)
        self.run_once()
        self.assertEqual(self.run_dir.stat().st_mode & 0o777, 0o700)

    def test_the_session_runs_in_the_tree_it_is_given(self):
        # The worktree, not cfg.repo: cfg.repo is only the repository the
        # task's branch was cut from.
        tree = self.tmp / "worktrees" / "abc123"
        tree.mkdir(parents=True)

        asyncio.run(session.run(self.cfg, self.run_dir, "uuid-1", "do it",
                                resume=False, cwd=tree))

        self.assertEqual((self.run_dir / "cwd.txt").read_text().strip(), str(tree))

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


class HungProcessTest(unittest.TestCase):
    """A wedged `claude` (stalled network, a hung MCP server, ...) must never
    park the orchestrator forever -- it gets killed and treated like a
    plain, resumable nudge instead."""

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
        hang = self.tmp / "bin" / "claude"
        hang.write_text("#!/usr/bin/env bash\nsleep 9999\n")
        hang.chmod(0o755)

    def tearDown(self):
        os.environ["PATH"] = self.old_path

    def test_a_hung_process_is_killed_once_the_timeout_fires(self):
        cfg = replace(self.cfg, session_timeout_s=0.2)
        start = time.monotonic()
        events = asyncio.run(session.run(cfg, self.run_dir, "uuid-1", "do it", resume=False))
        elapsed = time.monotonic() - start
        # Bounded, not "eventually" -- proves the timeout (plus, worst case,
        # one REAP_TIMEOUT_S: this script's `sleep` is a real forked child
        # that can outlive the killed shell and hold the pipes open) fired
        # rather than the process happening to exit for some other reason.
        self.assertLess(elapsed, 0.2 + session.REAP_TIMEOUT_S + 3)
        self.assertEqual(events, [])

    def test_cancelling_the_caller_still_kills_the_child(self):
        # Simulates SIGTERM/Ctrl-C: asyncio.run cancels the running task.
        # Without the try/finally in session.run, the child would keep
        # running after this function returns -- a restart would then race a
        # second live bypassPermissions session against it.
        pid_file = self.tmp / "pid"
        hang = self.tmp / "bin" / "claude"
        hang.write_text(f'#!/usr/bin/env bash\necho $$ > "{pid_file}"\nsleep 9999\n')
        hang.chmod(0o755)

        async def go():
            task = asyncio.ensure_future(
                session.run(self.cfg, self.run_dir, "uuid-1", "do it", resume=False)
            )
            while not pid_file.exists():
                await asyncio.sleep(0.01)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        asyncio.run(go())
        pid = int(pid_file.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_stdin_is_not_inherited(self):
        # A CLI that reads stdin must see EOF immediately, not block forever
        # on a read from a pipe/terminal it was never meant to share. Bounded
        # by a short *test-level* timeout so this fails loudly (instead of
        # hanging the suite) if stdin is ever inherited again.
        cat = self.tmp / "bin" / "claude"
        cat.write_text('#!/usr/bin/env bash\ncat <&0\necho \'{"type":"result"}\'\n')
        cat.chmod(0o755)
        events = asyncio.run(
            asyncio.wait_for(
                session.run(self.cfg, self.run_dir, "uuid-1", "do it", resume=False),
                timeout=5,
            )
        )
        self.assertEqual(events[-1]["type"], "result")


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
        # "before"/"after" would themselves now be filtered out of `events`
        # (see the RunTest kept-event-types change), so use a type _read_events
        # actually keeps -- the overrun handling being tested is orthogonal to
        # that filter.
        stream.feed_data(b'{"type":"result"}\n')
        stream.feed_data(b"x" * (limit * 2) + b"\n")
        stream.feed_data(b'{"type":"result"}\n')
        stream.feed_eof()
        events: list[dict] = []
        path = self.tmp / "events.jsonl"
        await session._read_events(stream, path, events, limit=limit)
        return events, path

    def test_overlong_stdout_line_is_not_silently_dropped(self):
        events, path = asyncio.run(self._read_events_case(64))
        # Events either side of the overrun still come through...
        self.assertEqual([e["type"] for e in events], ["result", "result"])
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


class RotationTest(unittest.TestCase):
    """events.jsonl used to grow without bound. A session under
    bypassPermissions streams every tool result -- full file reads included --
    through it, and a task that nudges or waits out a quota reopens the same
    file for every attempt."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "events.jsonl"

    async def _feed(self, lines: list[bytes], cap: int) -> None:
        stream = asyncio.StreamReader()
        for line in lines:
            stream.feed_data(line)
        stream.feed_eof()
        await session._drain(stream, self.path, cap=cap)

    def rotated(self) -> Path:
        return self.tmp / "events.jsonl.1"

    def test_a_log_past_the_cap_rotates(self):
        asyncio.run(self._feed([b"x" * 40 + b"\n"] * 6, cap=100))

        self.assertTrue(self.rotated().exists())
        # Bounded by 2x the cap: the live file plus one kept generation.
        total = self.path.stat().st_size + self.rotated().stat().st_size
        self.assertLessEqual(total, 2 * 100 + 41)

    def test_rotation_keeps_the_most_recent_lines_live(self):
        lines = [f"line-{n}\n".encode() for n in range(20)]
        asyncio.run(self._feed(lines, cap=40))

        self.assertIn("line-19", self.path.read_text())

    def test_only_one_generation_is_kept(self):
        asyncio.run(self._feed([b"x" * 40 + b"\n"] * 30, cap=100))

        self.assertEqual(
            sorted(p.name for p in self.tmp.iterdir()),
            ["events.jsonl", "events.jsonl.1"],
        )

    def test_a_log_under_the_cap_is_never_rotated(self):
        asyncio.run(self._feed([b"small\n"] * 3, cap=1024))

        self.assertFalse(self.rotated().exists())
        self.assertEqual(self.path.read_text(), "small\nsmall\nsmall\n")

    def test_a_reopened_log_counts_what_is_already_there(self):
        # Every resume of a task reopens the same file. Counting only this
        # invocation's own bytes would let a task that nudges twenty times
        # grow the file twenty caps deep.
        self.path.write_bytes(b"y" * 95 + b"\n")

        asyncio.run(self._feed([b"z" * 40 + b"\n"] * 2, cap=100))

        self.assertTrue(self.rotated().exists())
        self.assertIn("y" * 95, self.rotated().read_text())

    def test_events_are_still_collected_across_a_rotation(self):
        async def case():
            stream = asyncio.StreamReader()
            for _ in range(6):
                stream.feed_data(b'{"type":"result","total_cost_usd":0.1}\n')
            stream.feed_eof()
            events: list[dict] = []
            await session._read_events(stream, self.path, events, cap=100)
            return events

        events = asyncio.run(case())

        self.assertEqual(len(events), 6, "rotation must not lose an event")


class SessionEnvironmentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "repo" / ".git").mkdir(parents=True)
        self.run_dir = self.tmp / "run"

    def cfg(self, **overrides) -> Config:
        base = {
            "repo": self.tmp / "repo",
            "tasks_file": self.tmp / "tasks.md",
            "home": self.tmp / "home",
        }
        return Config(**{**base, **overrides})

    def test_no_new_flags_when_nothing_is_configured(self):
        cmd = session.build_command(self.cfg(), "uuid-1", "do it", resume=False)
        self.assertNotIn("--settings", cmd)
        self.assertNotIn("--mcp-config", cmd)
        self.assertNotIn("--strict-mcp-config", cmd)

    def test_settings_flag_only_when_set(self):
        cfg = self.cfg(settings_file=self.tmp / "settings.json")
        cmd = session.build_command(cfg, "uuid-1", "do it", resume=False)
        self.assertEqual(cmd[cmd.index("--settings") + 1], str(self.tmp / "settings.json"))

    def test_mcp_config_flag_only_when_set(self):
        cfg = self.cfg(mcp_config=self.tmp / "mcp.json")
        cmd = session.build_command(cfg, "uuid-1", "do it", resume=False)
        self.assertEqual(cmd[cmd.index("--mcp-config") + 1], str(self.tmp / "mcp.json"))

    def test_strict_mcp_flag_only_when_set(self):
        cfg = self.cfg(mcp_config=self.tmp / "mcp.json", strict_mcp=True)
        cmd = session.build_command(cfg, "uuid-1", "do it", resume=False)
        self.assertIn("--strict-mcp-config", cmd)

    def test_the_composed_prompt_is_what_is_sent(self):
        (self.tmp / "repo" / "CLAUDE.md").write_text("# rules")
        cfg = self.cfg()
        cmd = session.build_command(cfg, "uuid-1", "do it", resume=False)
        sent = cmd[cmd.index("--append-system-prompt") + 1]
        self.assertEqual(sent, compose(cfg))
        self.assertIn("CLAUDE.md", sent)

    def test_session_env_reaches_the_child(self):
        cfg = self.cfg(session_env={"GH_TOKEN": "ghp_abc"})
        self.assertEqual(session.child_env(cfg, self.run_dir)["GH_TOKEN"], "ghp_abc")

    def test_the_ambient_environment_is_preserved(self):
        env = session.child_env(self.cfg(), self.run_dir)
        self.assertEqual(env["PATH"], os.environ["PATH"])

    def test_claudeloop_result_wins_over_session_env(self):
        cfg = self.cfg(session_env={"CLAUDELOOP_RESULT": "/tmp/hijacked.json"})
        env = session.child_env(cfg, self.run_dir)
        self.assertEqual(env["CLAUDELOOP_RESULT"], str(self.run_dir / "result.json"))


class PythonPathTest(unittest.TestCase):
    """PYTHONPATH is only ClaudeLoop's business under source = "jira" -- that
    is the only source whose sessions call `python -m claudeloop.jira`. Every
    cfg() here is source="jira" on purpose; FileSourcePythonPathTest below
    covers the source = "file" case these used to also (wrongly) cover."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)

    def cfg(self, **kwargs):
        return Config(repo=self.repo, tasks_file=self.tmp / "t.md",
                      source="jira", **kwargs)

    def test_the_package_parent_is_importable_from_the_session(self):
        env = session.child_env(self.cfg(), self.tmp / "run")
        first = env["PYTHONPATH"].split(os.pathsep)[0]
        self.assertEqual(Path(first), Path(session.PACKAGE_PARENT))
        self.assertTrue((Path(first) / "claudeloop" / "jira.py").exists())

    def test_an_operators_pythonpath_survives_in_front_of_nothing(self):
        env = session.child_env(
            self.cfg(session_env={"PYTHONPATH": "/opt/theirs"}), self.tmp / "run"
        )
        parts = env["PYTHONPATH"].split(os.pathsep)
        self.assertEqual(Path(parts[0]), Path(session.PACKAGE_PARENT))
        self.assertIn("/opt/theirs", parts)

    def test_claudeloop_result_is_still_merged_last(self):
        env = session.child_env(
            self.cfg(session_env={"CLAUDELOOP_RESULT": "/tmp/hijacked",
                                  "PYTHONPATH": "/opt/theirs"}),
            self.tmp / "run",
        )
        self.assertEqual(env["CLAUDELOOP_RESULT"], str(self.tmp / "run" / "result.json"))

    def test_an_ambient_pythonpath_survives_in_front_of_nothing(self):
        with patch.dict(os.environ, {"PYTHONPATH": "/opt/ambient"}):
            env = session.child_env(self.cfg(), self.tmp / "run")
        parts = env["PYTHONPATH"].split(os.pathsep)
        self.assertEqual(Path(parts[0]), Path(session.PACKAGE_PARENT))
        self.assertIn("/opt/ambient", parts)

    def test_session_env_pythonpath_wins_over_the_ambient_one(self):
        with patch.dict(os.environ, {"PYTHONPATH": "/opt/ambient"}):
            env = session.child_env(
                self.cfg(session_env={"PYTHONPATH": "/opt/theirs"}), self.tmp / "run"
            )
        parts = env["PYTHONPATH"].split(os.pathsep)
        self.assertEqual(Path(parts[0]), Path(session.PACKAGE_PARENT))
        self.assertIn("/opt/theirs", parts)
        self.assertNotIn("/opt/ambient", parts)


class FileSourcePythonPathTest(unittest.TestCase):
    """source = "file" sessions get no ClaudeLoop-added PYTHONPATH: they have
    no use for `python -m claudeloop.jira`, and putting ClaudeLoop's own repo
    root -- which contains an importable tests/ package -- on the import path
    of a session working in an unrelated repository is just contamination."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)

    def cfg(self, **kwargs):
        return Config(repo=self.repo, tasks_file=self.tmp / "t.md", **kwargs)

    def test_the_package_parent_is_not_added(self):
        env = session.child_env(self.cfg(), self.tmp / "run")
        self.assertNotIn(session.PACKAGE_PARENT, env.get("PYTHONPATH", ""))

    def test_the_ambient_pythonpath_is_untouched(self):
        with patch.dict(os.environ, {"PYTHONPATH": "/opt/ambient"}):
            env = session.child_env(self.cfg(), self.tmp / "run")
        self.assertEqual(env["PYTHONPATH"], "/opt/ambient")

    def test_an_absent_ambient_pythonpath_stays_absent(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PYTHONPATH", None)
            env = session.child_env(self.cfg(), self.tmp / "run")
        self.assertNotIn("PYTHONPATH", env)


if __name__ == "__main__":
    unittest.main()
