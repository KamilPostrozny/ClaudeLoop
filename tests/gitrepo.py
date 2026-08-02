"""One real git repository on disk, for tests that cannot use a fake `.git`.

Worktrees mean the orchestrator shells out to git for every task, so the
empty-directory `.git` fixtures that used to be enough no longer are.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )


def make_repo(path: Path) -> Path:
    """A git repository at `path` with one commit on `main`.

    commit.gpgsign is disabled locally: this machine signs commits through a
    1Password SSH agent, which hangs forever on a headless prompt inside a
    scratch repository that has nothing to sign for.
    """
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "commit.gpgsign", "false")
    (path / "README.md").write_text("hi\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "init")
    return path
