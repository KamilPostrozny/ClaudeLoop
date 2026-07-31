"""Load and validate the ClaudeLoop configuration file."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

HOME = Path.home() / ".claudeloop"
DEFAULT_CONFIG = HOME / "config.toml"
REQUIRED_KEYS = ("repo", "tasks_file")


@dataclass(frozen=True)
class Config:
    repo: Path
    tasks_file: Path
    model: str = "opus"
    max_resumes: int = 20
    home: Path = HOME


def load_config(path: Path = DEFAULT_CONFIG, home: Path = HOME) -> Config:
    """Read `path` into a Config.

    The config file is user input, so both the required keys and the repo path
    are validated here rather than failing much later inside a subprocess.
    """
    with open(path, "rb") as handle:
        data = tomllib.load(handle)

    missing = [key for key in REQUIRED_KEYS if key not in data]
    if missing:
        raise ValueError(f"{path}: missing required key(s): {', '.join(missing)}")

    repo = Path(data["repo"]).expanduser()
    if not (repo / ".git").exists():
        raise ValueError(f"{path}: repo {repo} is not a git repository")

    return Config(
        repo=repo,
        tasks_file=Path(data["tasks_file"]).expanduser(),
        model=str(data.get("model", "opus")),
        max_resumes=int(data.get("max_resumes", 20)),
        home=home,
    )
