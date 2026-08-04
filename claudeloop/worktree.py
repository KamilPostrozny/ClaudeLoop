"""One git worktree per task, so no two tasks share a working tree.

Every task runs in its own checkout on its own branch, cut from the
repository's default branch. That removes the shared state
`reset_to_default_branch` used to compensate for: there is nothing for a task
to inherit from the task before it, and a task parked on a question keeps its
tree -- branch, commits and uncommitted changes -- until its answer arrives.

`git worktree add` writes `.git/worktrees/<task-id>/` into the target
repository, and leaves the branch behind it. That is the deliberate exception
to the constraint that nothing ClaudeLoop writes into a repository may be
committable: none of it sits inside a working tree, so no `git add` can stage
it. `release` takes the administrative entry and the tree back together on
every non-blocked outcome; the `git worktree prune` in `probe` is only the
backstop for entries orphaned from outside git. The branch stays.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger("claudeloop")

BRANCH_PREFIX = "claudeloop/"
"""Namespaced so a task branch is recognisable in the operator's own
repository, and cannot collide with a branch a human made."""

DEFAULT_BRANCH_CANDIDATES = ("main", "master")
"""Checked, in order, when a repository has no remote to ask -- never
assumed outright. Neither name is guaranteed; this only picks one that
actually exists as a local branch."""

GIT_TIMEOUT_S = 10
"""Bounds every git call this module makes. Local git commands finish in
milliseconds; this exists only so a wedged one (a lock held by another
process, anything unforeseen) can't hang an unattended loop forever."""


def _git(
    repo: Path, *args: str, timeout: int = GIT_TIMEOUT_S
) -> subprocess.CompletedProcess:
    """Run one git command, hardened for an unattended caller: no inherited
    stdin (a prompt for credentials or an editor would otherwise block
    forever reading from the loop's own terminal -- the same class of bug
    session.py's stdin=DEVNULL already guards against for the CLI itself),
    no interactive terminal prompting, and a bounded timeout.
    """
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def _try_git(
    repo: Path, *args: str, timeout: int = GIT_TIMEOUT_S
) -> subprocess.CompletedProcess | None:
    """`_git`, or None with a warning already logged if git could not be run
    at all -- a missing binary, a timeout. Distinct from a clean invocation
    that simply exits non-zero, which callers handle themselves."""
    try:
        return _git(repo, *args, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        log.warning(
            "could not run `git %s` in %s (%s); running the next task from"
            " the repository's current state",
            " ".join(args),
            repo,
            error,
        )
        return None


def default_branch(repo: Path) -> str | None:
    """The repository's default branch, without guessing.

    `git symbolic-ref refs/remotes/origin/HEAD` is authoritative when there
    is a remote (set by a clone, or `git remote set-head origin -a`). Most
    repositories driven by an unattended loop have none, so this falls back
    to whichever of the two common initial-branch names actually exists
    locally. If neither does, this gives up rather than guess -- the caller
    then leaves the working tree exactly as it found it.
    """
    origin_head = _try_git(repo, "symbolic-ref", "-q", "refs/remotes/origin/HEAD")
    if origin_head is None:
        return None
    prefix = "refs/remotes/origin/"
    ref = origin_head.stdout.strip()
    if origin_head.returncode == 0 and ref.startswith(prefix):
        return ref[len(prefix):]
    for name in DEFAULT_BRANCH_CANDIDATES:
        check = _try_git(repo, "rev-parse", "-q", "--verify", f"refs/heads/{name}")
        if check is None:
            return None
        if check.returncode == 0:
            return name
    return None


CLONE_TIMEOUT_S = 600
"""A clone crosses the network and can be large, so GIT_TIMEOUT_S -- written
for local commands that finish in milliseconds -- would abort a healthy one.
This is still bounded: an unattended loop must not hang forever on a remote
that accepts the connection and then stalls."""


def clone(url: str, dest: Path) -> str | None:
    """Clone `url` into `dest` if it is not already there. An error message
    written for a human, or None.

    Called once at startup, before `probe`. Cloning per task would re-fetch
    the whole repository every time; the worktrees come out of this one
    checkout exactly as they do out of a local one.
    """
    if (dest / ".git").exists():
        # ponytail: never refreshed. Tasks branch off whatever the default
        # branch held when this clone was made. Add a fetch and a
        # fast-forward here if a long-lived loop starts working stale.
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    log.info("cloning %s into %s", url, dest)
    try:
        result = subprocess.run(
            ["git", "clone", url, str(dest)],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=CLONE_TIMEOUT_S,
            # As in _git: no credential prompt can block an unattended loop.
            # A private repository needs a credential helper, an SSH agent or
            # a token in the URL -- otherwise this fails now, saying so.
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.SubprocessError) as error:
        return f"could not clone {url} into {dest}: {error}"
    if result.returncode != 0:
        return f"could not clone {url} into {dest}: {result.stderr.strip()}"
    return None


def reachable(url: str) -> str | None:
    """Whether `url` is a repository this box can read, without cloning it.

    The wizard's live check for a URL repo: a typo or a missing credential
    otherwise passes setup and only surfaces at startup, after the operator
    has walked away.
    """
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", url],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT_S,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.SubprocessError) as error:
        return f"cannot reach {url}: {error}"
    if result.returncode != 0:
        return f"cannot reach {url}: {result.stderr.strip()}"
    return None


def probe(repo: Path) -> str | None:
    """Whether this box can run tasks in worktrees at all. An error message
    written for a human, or None.

    Called once at startup rather than per task: a git too old for
    `worktree`, a `repo` that is not a repository, or a repository with no
    resolvable default branch are configuration errors, and an unattended
    loop should refuse to start rather than fail every task in turn.

    `prune` doubles as the check -- it only succeeds in a real repository
    whose git has the subcommand -- and clears any registration left behind
    by a home directory that was wiped while worktrees existed.
    """
    result = _try_git(repo, "worktree", "prune")
    if result is None or result.returncode != 0:
        detail = result.stderr.strip() if result is not None else "git could not be run"
        return (
            f"cannot use git worktrees in {repo}: {detail}. ClaudeLoop runs every"
            " task in its own worktree, so this must work before it can start."
        )
    if default_branch(repo) is None:
        return (
            f"cannot determine the default branch of {repo}. ClaudeLoop cuts each"
            " task's branch from it, so it needs a local `main` or `master`, or an"
            " origin with its HEAD set (`git remote set-head origin -a`)."
        )
    return None


FETCH_TIMEOUT_S = 60
"""A fetch crosses the network, so GIT_TIMEOUT_S -- written for local commands
that finish in milliseconds -- would abort a healthy one. Still bounded: an
unattended loop must not hang forever on a remote that accepts the connection
and then stalls."""


def base_ref(repo: Path, branch: str) -> str:
    """The ref a new task branch is cut from: `origin/<branch>` when the
    repository has a remote this can reach, `<branch>` otherwise.

    Where a session publishes its work is the target repository's decision,
    and some tell it to land on the default branch directly. That push never
    moves the local ref, so without this every task after the first cuts from
    the same stale point and silently drops the work in between.

    Every failure degrades to the local branch rather than failing the task:
    no remote, no network, a locked credential agent. A stale base still makes
    progress, and the prompt names the branch the tree was cut from either
    way. Deliberately not `git fetch` with no arguments: one branch is what is
    needed, and a repository with a large remote should not pay for the rest.
    """
    result = _try_git(repo, "fetch", "--quiet", "origin", branch,
                      timeout=FETCH_TIMEOUT_S)
    if result is None or result.returncode != 0:
        return branch
    # The fetch can succeed against a remote that has no such branch, which
    # leaves no remote-tracking ref to cut from.
    check = _try_git(repo, "rev-parse", "-q", "--verify",
                     f"refs/remotes/origin/{branch}")
    if check is None or check.returncode != 0:
        return branch
    return f"origin/{branch}"


def _branch_exists(repo: Path, branch: str) -> bool:
    result = _try_git(repo, "rev-parse", "-q", "--verify", f"refs/heads/{branch}")
    return result is not None and result.returncode == 0


def ensure(repo: Path, root: Path, task_id: str) -> Path:
    """The task's worktree, created or reused. Raises RuntimeError if git
    cannot produce one -- the caller fails that task and moves on.

    Reuse is what makes parking work: a task that blocked on a question comes
    back to the same tree, on the same branch, with its uncommitted changes
    still there. The `-b` retry covers the other order -- the branch outliving
    its worktree, because `release` removed the tree after an earlier attempt
    -- so an answered task lands back on its own commits instead of beside
    them.
    """
    path = root / task_id
    branch = f"{BRANCH_PREFIX}{task_id}"
    if (path / ".git").exists():  # a worktree's .git is a file, not a directory
        return path
    root.mkdir(parents=True, exist_ok=True)
    base = default_branch(repo)
    if base is None:
        raise RuntimeError(f"no default branch to cut {branch} from in {repo}")
    # Creation only. A reused tree -- a parked task coming back with its
    # answer, an interrupted one resuming -- is never refetched and never
    # rebased: it holds uncommitted work, and moving it under an unattended
    # session is worse than a stale base.
    try:
        result = _git(repo, "worktree", "add", "-b", branch, str(path),
                      base_ref(repo, base))
        if result.returncode != 0 and _branch_exists(repo, branch):
            result = _git(repo, "worktree", "add", str(path), branch)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"could not run git worktree add in {repo}: {error}") from error
    if result.returncode != 0:
        raise RuntimeError(
            f"could not create a worktree at {path}: {result.stderr.strip()}"
        )
    return path


def release(repo: Path, path: Path) -> None:
    """Remove a finished task's worktree. Never forced, never raises.

    Git refuses to remove a tree with uncommitted changes, and that refusal
    is kept: destroying a working tree in an unattended loop is worse than
    leaving a directory behind. The branch and its commits live in the
    repository and survive removal either way.

    Run from the repository, not from the tree being removed.
    """
    result = _try_git(repo, "worktree", "remove", str(path))
    if result is not None and result.returncode != 0:
        log.info(
            "keeping the worktree at %s -- git would not remove it (%s)",
            path,
            result.stderr.strip(),
        )
