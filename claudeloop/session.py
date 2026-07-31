"""Spawn one headless Claude Code invocation and stream its output."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from .config import Config

MAX_LINE = 16 * 1024 * 1024
"""asyncio's default 64 KiB line buffer is too small: a single stream-json line
carrying a large tool result overruns it and raises ValueError."""

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
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                pass  # non-JSON noise on stdout, already on disk


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


async def run(
    cfg: Config, run_dir: Path, session_id: str, prompt: str, resume: bool
) -> list[dict]:
    """Run one invocation to completion. Returns only this invocation's events."""
    run_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ | {"CLAUDELOOP_RESULT": str(run_dir / "result.json")}
    process = await asyncio.create_subprocess_exec(
        *build_command(cfg, session_id, prompt, resume),
        cwd=cfg.repo,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=MAX_LINE,
    )
    events: list[dict] = []
    # Both pipes must be drained concurrently: --verbose writes diagnostics to
    # stderr, and a full stderr pipe buffer would deadlock the child.
    await asyncio.gather(
        _read_events(process.stdout, run_dir / "events.jsonl", events),
        _drain(process.stderr, run_dir / "stderr.log"),
    )
    await process.wait()
    return events
