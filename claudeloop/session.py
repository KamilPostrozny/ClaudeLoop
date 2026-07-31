"""Spawn one headless Claude Code invocation and stream its output."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from .config import Config

MAX_LINE = 16 * 1024 * 1024
"""asyncio's default 64 KiB line buffer is too small: a single stream-json line
carrying a large tool result overruns it and raises ValueError."""

REAP_TIMEOUT_S = 5
"""asyncio.Process.wait() only resolves once stdout/stderr report EOF, which
never happens if a grandchild (a hung MCP server, say) survives the kill and
keeps the pipe's write end open -- bounded so that alone can't hang run()
forever."""

PROTOCOL = (
    "You are running unattended under ClaudeLoop. Follow this repository's "
    "CLAUDE.md end to end — it defines what \"done\" means here, including its "
    "testing and verification requirements. Nobody is watching, so decide open "
    "questions yourself rather than waiting. When the task is fully complete, "
    "or provably cannot be completed, write a JSON object to the path in the "
    "CLAUDELOOP_RESULT environment variable with keys \"status\" (one of "
    "\"done\", \"failed\", \"blocked\"), \"summary\" (one paragraph on what you "
    "did), and, when blocked, \"question\" (the one thing a human must answer). "
    "Writing that file is what ends the task; do not stop without it."
)

log = logging.getLogger("claudeloop")


def build_command(cfg: Config, session_id: str, prompt: str, resume: bool) -> list[str]:
    command = ["claude", "-p", prompt]
    # --resume and --session-id are alternative ways to name the session;
    # passing both is a conflict.
    command += ["--resume", session_id] if resume else ["--session-id", session_id]
    command += [
        "--append-system-prompt", PROTOCOL,
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", "bypassPermissions",
        "--model", cfg.model,
    ]
    return command


def _overrun_marker(limit: int) -> bytes:
    # The reader has already discarded the over-long line from its internal
    # buffer by the time ValueError reaches us, so the original bytes cannot
    # be recovered -- record that a line was dropped rather than losing the
    # fact entirely.
    return f"<claudeloop: line exceeded {limit} byte limit, discarded>\n".encode()


async def _read_events(
    stream: asyncio.StreamReader, path: Path, out: list[dict], limit: int = MAX_LINE
) -> None:
    with open(path, "ab") as log:
        while True:
            try:
                raw = await stream.readline()
            except ValueError:
                log.write(_overrun_marker(limit))  # keep a durable trace of the drop
                log.flush()
                continue
            if not raw:
                return
            log.write(raw)  # verbatim first: a parser bug never loses the record
            log.flush()
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue  # non-JSON noise on stdout, already on disk
            # decide() and total_cost() only ever look at these two types, and
            # everything is already durable on disk above -- keeping the rest
            # out of memory matters because a multi-hour session under
            # bypassPermissions streams every tool result (full file reads
            # included) through here.
            if event.get("type") in ("rate_limit_event", "result"):
                out.append(event)


async def _drain(stream: asyncio.StreamReader, path: Path, limit: int = MAX_LINE) -> None:
    with open(path, "ab") as log:
        while True:
            try:
                raw = await stream.readline()
            except ValueError:
                # Same overrun as _read_events: an unbroken --verbose diagnostic
                # (e.g. a giant traceback) must not crash the gather and take
                # the whole run() -- and the events already collected -- with it.
                log.write(_overrun_marker(limit))
                log.flush()
                continue
            if not raw:
                return
            log.write(raw)
            log.flush()  # same reason as _read_events: visible while it matters


async def run(
    cfg: Config, run_dir: Path, session_id: str, prompt: str, resume: bool
) -> list[dict]:
    """Run one invocation to completion. Returns only this invocation's events.

    Bounded by cfg.session_timeout_s: a wedged `claude` (stalled network, a
    hung MCP server, a grandchild holding the pipes open) is killed rather
    than parking the orchestrator forever. A killed invocation returns
    whatever partial events it collected, which decide() treats as a normal
    nudge -- the caller never sees the timeout as an exception.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ | {"CLAUDELOOP_RESULT": str(run_dir / "result.json")}
    process = await asyncio.create_subprocess_exec(
        *build_command(cfg, session_id, prompt, resume),
        cwd=cfg.repo,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,  # inherited stdin can block a CLI that reads it
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=MAX_LINE,
        # Its own process group: a kill() below (or on timeout) cannot be
        # dodged by a child the CLI spawns of its own that outlives it.
        start_new_session=True,
    )
    events: list[dict] = []
    try:
        # Both pipes must be drained concurrently: --verbose writes
        # diagnostics to stderr, and a full stderr pipe buffer would deadlock
        # the child.
        await asyncio.wait_for(
            asyncio.gather(
                _read_events(process.stdout, run_dir / "events.jsonl", events),
                _drain(process.stderr, run_dir / "stderr.log"),
                process.wait(),
            ),
            timeout=cfg.session_timeout_s,
        )
    except asyncio.TimeoutError:
        log.warning(
            "session %s timed out after %.0fs, killing it", session_id, cfg.session_timeout_s
        )
    finally:
        # Whatever path got us here -- timeout, exception, or outer
        # cancellation (SIGTERM/Ctrl-C) -- a still-running child must not
        # outlive this function, or a restart races a second live session
        # against it in the same bypassPermissions repo.
        # ponytail: kills only this direct child, not a whole process tree a
        # misbehaving MCP server might spawn under it -- os.killpg(pid,
        # SIGKILL) if a grandchild surviving this shows up in practice.
        if process.returncode is None:
            process.kill()
            try:
                # Bounded: see REAP_TIMEOUT_S -- a surviving grandchild
                # holding the pipes open must not turn a kill into a hang.
                await asyncio.wait_for(process.wait(), timeout=REAP_TIMEOUT_S)
            except asyncio.TimeoutError:
                log.warning(
                    "session %s: killed but did not fully exit within %ss"
                    " (a child of its own may still be holding its pipes open)",
                    session_id,
                    REAP_TIMEOUT_S,
                )
    return events
