"""Spawn one headless Claude Code invocation and stream its output."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from .config import Config
from .prompt import compose

PACKAGE_PARENT = str(Path(__file__).resolve().parent.parent)
"""The directory holding the claudeloop package.

The session runs with cwd=repo, where `python -m claudeloop.jira` is an
ImportError -- and running jira.py by absolute path breaks its relative
imports. This is what makes the session's Jira CLI reachable.
"""

MAX_LINE = 16 * 1024 * 1024
"""asyncio's default 64 KiB line buffer is too small: a single stream-json line
carrying a large tool result overruns it and raises ValueError."""

REAP_TIMEOUT_S = 5
"""asyncio.Process.wait() only resolves once stdout/stderr report EOF, which
never happens if a grandchild (a hung MCP server, say) survives the kill and
keeps the pipe's write end open -- bounded so that alone can't hang run()
forever."""

log = logging.getLogger("claudeloop")


def build_command(
    cfg: Config, session_id: str, prompt: str, resume: bool, tree: Path | None = None
) -> list[str]:
    command = ["claude", "-p", prompt]
    # --resume and --session-id are alternative ways to name the session;
    # passing both is a conflict.
    command += ["--resume", session_id] if resume else ["--session-id", session_id]
    command += [
        "--append-system-prompt", compose(cfg, tree),
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", "bypassPermissions",
        "--model", cfg.model,
    ]
    # Each of these appears only when configured, so an unconfigured
    # ClaudeLoop produces the same command line it always did.
    if cfg.settings_file:
        command += ["--settings", str(cfg.settings_file)]
    if cfg.mcp_config:
        command += ["--mcp-config", str(cfg.mcp_config)]
    if cfg.strict_mcp:
        command += ["--strict-mcp-config"]
    return command


def child_env(cfg: Config, run_dir: Path) -> dict[str, str]:
    """The environment the session runs in.

    CLAUDELOOP_RESULT is merged last on purpose: a misconfigured session_env
    must not be able to redirect the result file, which is the only thing the
    loop uses to decide a task is finished.

    PYTHONPATH only gets ClaudeLoop's own package parent prepended under the
    Jira source, which is the only one whose sessions call `python -m
    claudeloop.jira`. Under source = "file" this would otherwise put
    ClaudeLoop's repo root -- which contains an importable tests/ package --
    on the import path of a session working in an unrelated repository.
    Prepended rather than replaced, so an operator who set one in
    [session_env] for the repository's own needs keeps it.
    """
    env = os.environ | dict(cfg.session_env)
    if cfg.source == "jira":
        inherited = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            os.pathsep.join([PACKAGE_PARENT, inherited]) if inherited else PACKAGE_PARENT
        )
    return env | {"CLAUDELOOP_RESULT": str(run_dir / "result.json")}


def _overrun_marker(limit: int) -> bytes:
    # The reader has already discarded the over-long line from its internal
    # buffer by the time ValueError reaches us, so the original bytes cannot
    # be recovered -- record that a line was dropped rather than losing the
    # fact entirely.
    return f"<claudeloop: line exceeded {limit} byte limit, discarded>\n".encode()


def _open_log(path: Path):
    """Open (or create) a run log for append, restricted to the owner.

    These logs carry a session's raw stdout/stderr verbatim, and that
    session was handed [session_env] credentials -- a run that executes
    `env`, `git config --list --show-origin`, or echoes a failing `gh`
    invocation writes a credential straight into this file. The default
    umask (0644) would make it world-readable, which is exactly what the
    config.toml permissions guard refuses to allow for the same secrets one
    step earlier. Explicit mode on os.open, not a chmod after: still subject
    to umask, but umask can only clear bits from 0o600, never add ones.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    return os.fdopen(fd, "ab")


async def _read_events(
    stream: asyncio.StreamReader, path: Path, out: list[dict], limit: int = MAX_LINE
) -> None:
    with _open_log(path) as log:
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
    with _open_log(path) as log:
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
    cfg: Config,
    run_dir: Path,
    session_id: str,
    prompt: str,
    resume: bool,
    cwd: Path | None = None,
) -> list[dict]:
    """Run one invocation to completion. Returns only this invocation's events.

    Bounded by cfg.session_timeout_s: a wedged `claude` (stalled network, a
    hung MCP server, a grandchild holding the pipes open) is killed rather
    than parking the orchestrator forever. A killed invocation returns
    whatever partial events it collected, which decide() treats as a normal
    nudge -- the caller never sees the timeout as an exception.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    # Unconditional, not just on creation: mkdir(exist_ok=True) leaves an
    # already-existing directory's mode untouched (e.g. run_task creating it
    # first), and events.jsonl/stderr.log below inherit the same secrets
    # concern _open_log documents -- nothing under here should be group- or
    # world-readable.
    run_dir.chmod(0o700)
    env = child_env(cfg, run_dir)
    process = await asyncio.create_subprocess_exec(
        *build_command(cfg, session_id, prompt, resume, cwd),
        cwd=cwd or cfg.repo,
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
