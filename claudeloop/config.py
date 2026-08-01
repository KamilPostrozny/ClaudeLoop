"""Load and validate the ClaudeLoop configuration file."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home() / ".claudeloop"
DEFAULT_CONFIG = HOME / "config.toml"
REQUIRED_KEYS = ("repo", "tasks_file")
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
WILDCARD_HOSTS = ("0.0.0.0", "::")


def _secrets_file_guard(path: Path) -> None:
    """Refuse to load a config readable beyond its owner.

    This file holds web_token, [session_env] credentials, and — once S3
    lands — a Jira API token.
    """
    if os.name != "posix":
        return
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise ValueError(
            f"{path}: this file holds secrets but its mode is {mode:03o},"
            f" readable beyond its owner. Run: chmod 600 {path}"
        )


def _session_env(data: dict, path: Path) -> dict[str, str]:
    table = data.get("session_env", {})
    if not isinstance(table, dict):
        raise ValueError(f"{path}: session_env must be a table of name = \"value\"")
    result = {}
    for name, value in table.items():
        if isinstance(value, (dict, list)):
            raise ValueError(
                f"{path}: session_env.{name} must be a single value, not a"
                " table or array — environment variables are strings"
            )
        result[str(name)] = str(value)
    return result


def _optional_path(data: dict, key: str) -> Path | None:
    raw = data.get(key)
    return Path(str(raw)).expanduser() if raw else None


@dataclass(frozen=True)
class Config:
    repo: Path
    tasks_file: Path
    model: str = "opus"
    max_resumes: int = 20
    max_waits: int = 200
    session_timeout_s: float = 4 * 3600
    web_host: str = "127.0.0.1"
    web_port: int = 8765
    web_token: str = ""
    instructions_file: Path | None = None
    definition_of_done_file: Path | None = None
    settings_file: Path | None = None
    mcp_config: Path | None = None
    strict_mcp: bool = False
    session_env: dict[str, str] = field(default_factory=dict)
    home: Path = HOME


def load_config(path: Path = DEFAULT_CONFIG, home: Path = HOME) -> Config:
    """Read `path` into a Config.

    The config file is user input, so both the required keys and the repo path
    are validated here rather than failing much later inside a subprocess.
    """
    with open(path, "rb") as handle:
        data = tomllib.load(handle)

    _secrets_file_guard(path)

    missing = [key for key in REQUIRED_KEYS if key not in data]
    if missing:
        raise ValueError(f"{path}: missing required key(s): {', '.join(missing)}")

    repo = Path(data["repo"]).expanduser()
    if not (repo / ".git").exists():
        raise ValueError(f"{path}: repo {repo} is not a git repository")

    # Resolved so `..` segments and symlinks can't sneak a tasks_file that
    # lands inside repo past this -- but the unresolved path is still what
    # gets stored on Config below, matching repo itself. No trace of
    # ClaudeLoop belongs in a repository it works in, the same reason
    # result.json/events.jsonl/state.db all live under ~/.claudeloop/
    # instead: a session doing ordinary branch hygiene (`git checkout .`,
    # `git stash`, `git checkout main`) can revert ClaudeLoop's own `- [x]`
    # mark, and the loop then re-runs work it already finished.
    tasks_file = Path(data["tasks_file"]).expanduser()
    if tasks_file.resolve().is_relative_to(repo.resolve()):
        raise ValueError(
            f"{path}: tasks_file {tasks_file} is inside repo {repo}. "
            "ClaudeLoop's task list must live outside the repository it "
            "works in."
        )

    web_host = str(data.get("web_host", "127.0.0.1"))
    web_token = str(data.get("web_token", "")).strip()
    if not web_token.isascii():
        raise ValueError(
            f"{path}: web_token must be ASCII -- secrets.compare_digest, used to"
            " check it on every request, raises TypeError on anything else."
        )
    if web_host not in LOOPBACK_HOSTS and not web_token:
        raise ValueError(
            f"{path}: web_host {web_host!r} is not loopback, so web_token must be"
            " set to a non-empty value. The dashboard watches an agent holding"
            " real credentials; exposing it beyond this machine has to be a"
            " deliberate act."
        )

    # Checked here, same as repo above, so a typo'd path fails loudly at
    # startup: unchecked, it makes `claude` exit immediately on every task,
    # and main_loop deliberately does not source.mark on that kind of crash,
    # so the loop would retry every 30s forever with the dashboard stuck in
    # 'error'. Unlike instructions_file/definition_of_done_file, these two
    # are never optional once named -- a missing one is a config mistake,
    # not an absent optional layer.
    settings_file = _optional_path(data, "settings_file")
    if settings_file is not None and not settings_file.exists():
        raise ValueError(f"{path}: settings_file {settings_file} does not exist")

    mcp_config = _optional_path(data, "mcp_config")
    if mcp_config is not None and not mcp_config.exists():
        raise ValueError(f"{path}: mcp_config {mcp_config} does not exist")

    strict_mcp = bool(data.get("strict_mcp", False))
    if strict_mcp and mcp_config is None:
        raise ValueError(
            f"{path}: strict_mcp is set but mcp_config is not. On its own,"
            " --strict-mcp-config tells the CLI to use only the servers from"
            " --mcp-config — of which there would be none — silently disabling"
            " every MCP server this machine has configured."
        )

    return Config(
        repo=repo,
        tasks_file=tasks_file,
        model=str(data.get("model", "opus")),
        max_resumes=int(data.get("max_resumes", 20)),
        max_waits=int(data.get("max_waits", 200)),
        session_timeout_s=float(data.get("session_timeout_s", 4 * 3600)),
        web_host=web_host,
        web_port=int(data.get("web_port", 8765)),
        web_token=web_token,
        instructions_file=_optional_path(data, "instructions_file")
        or home / "instructions.md",
        definition_of_done_file=_optional_path(data, "definition_of_done_file")
        or home / "definition-of-done.md",
        settings_file=settings_file,
        mcp_config=mcp_config,
        strict_mcp=strict_mcp,
        session_env=_session_env(data, path),
        home=home,
    )
