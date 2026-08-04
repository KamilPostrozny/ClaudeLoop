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


def make_repo_with_remote(path: Path, remote: Path) -> Path:
    """A repository whose `origin` is a real bare repository on disk.

    Enough to exercise a fetch without a network, which is what the base a
    task branch is cut from now depends on.
    """
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(remote)],
        check=True, capture_output=True, stdin=subprocess.DEVNULL,
    )
    repo = make_repo(path)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "-u", "origin", "main")
    return repo


def commit_to_remote(remote: Path, name: str, scratch: Path) -> None:
    """Land a commit on the remote's `main` from somewhere else entirely --
    which is what a previous task pushing its own work looks like from the
    repository ClaudeLoop cuts worktrees out of."""
    subprocess.run(
        ["git", "clone", "-q", str(remote), str(scratch)],
        check=True, capture_output=True, stdin=subprocess.DEVNULL,
    )
    _git(scratch, "config", "user.email", "other@example.com")
    _git(scratch, "config", "user.name", "Other")
    _git(scratch, "config", "commit.gpgsign", "false")
    (scratch / name).write_text("from elsewhere\n")
    _git(scratch, "add", name)
    _git(scratch, "commit", "-q", "-m", f"add {name}")
    _git(scratch, "push", "-q", "origin", "main")


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
