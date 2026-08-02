# A git worktree per task — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run every ClaudeLoop task in its own `git worktree`, on a branch ClaudeLoop cuts from the default branch, so there is no shared working tree to reset between tasks.

**Architecture:** A new `claudeloop/worktree.py` takes over the git plumbing that lives in `loop.py` today (`_git`, `_try_git`, `default_branch`) and adds three functions: `probe` (startup check), `ensure` (the task's worktree, created or reused) and `release` (removed, never forced). `run_task` calls `ensure` where it calls `reset_to_default_branch` today, passes the worktree to `session.run` as the session's `cwd`, and calls `release` on a terminal result. A parked task's worktree is left alone, which is what makes a resumed session find its own work where it left it.

**Tech Stack:** Python 3.11+, standard library only, `unittest`, real scratch git repositories, a fake `claude` shell script.

Spec: `docs/superpowers/specs/2026-08-02-claudeloop-worktree-per-task-design.md`

## Global Constraints

- **Python 3.11+, standard library only.** No third-party packages, in the orchestrator or the tests.
- **Strictly serial.** One task, one session, one worktree in use at a time.
- **`git worktree remove` is never run with `--force`.** A dirty tree stays.
- Every git call goes through this module's `_git`/`_try_git`: `stdin=DEVNULL`, `GIT_TERMINAL_PROMPT=0`, `GIT_TIMEOUT_S` bound.
- Scratch git repositories in tests **must** set `commit.gpgsign false` locally — this machine signs through a 1Password SSH agent that hangs headless.
- Branch naming is exactly `claudeloop/<task-id>`; the worktree path is exactly `<cfg.home>/worktrees/<task-id>`.
- Run the whole suite with `python -m unittest discover -s tests -t .` before every commit.

---

### Task 1: `worktree.py` and a scratch-repo test helper

**Files:**
- Create: `claudeloop/worktree.py`
- Create: `tests/gitrepo.py`
- Create: `tests/test_worktree.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces:
  - `worktree.default_branch(repo: Path) -> str | None` (moved verbatim from `loop.py`, together with `_git`, `_try_git`, `GIT_TIMEOUT_S`, `DEFAULT_BRANCH_CANDIDATES`)
  - `worktree.probe(repo: Path) -> str | None`
  - `worktree.ensure(repo: Path, root: Path, task_id: str) -> Path`
  - `worktree.release(repo: Path, path: Path) -> None`
  - `worktree.BRANCH_PREFIX = "claudeloop/"`
  - `tests.gitrepo.make_repo(path: Path) -> Path`

Note against the spec: `release` takes the repository as well as the worktree path, because `git worktree remove` must not run with its own cwd inside the tree being removed. And `probe` runs `git worktree prune` only — a successful prune already proves both that the path is a git repository and that this git has the `worktree` subcommand, so the spec's separate `git worktree list` call is redundant.

- [ ] **Step 1: Write the scratch-repo helper**

`tests/gitrepo.py`:

```python
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
```

- [ ] **Step 2: Write the failing tests**

`tests/test_worktree.py`:

```python
import subprocess
import tempfile
import unittest
from pathlib import Path

from claudeloop import worktree

from .gitrepo import make_repo


def branch_of(path: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=path, check=True, capture_output=True, text=True,
        stdin=subprocess.DEVNULL,
    ).stdout.strip()


class WorktreeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = make_repo(self.tmp / "repo")
        self.root = self.tmp / "worktrees"

    def test_ensure_creates_a_worktree_on_its_own_branch(self):
        path = worktree.ensure(self.repo, self.root, "abc123")

        self.assertEqual(path, self.root / "abc123")
        self.assertTrue((path / "README.md").exists())
        self.assertEqual(branch_of(path), "claudeloop/abc123")

    def test_the_branch_is_cut_from_the_default_branch_not_from_head(self):
        # The repository is left on some other branch, exactly as the old
        # shared-tree flow used to leave it between tasks.
        subprocess.run(["git", "checkout", "-q", "-b", "someone-elses-branch"],
                       cwd=self.repo, check=True, capture_output=True,
                       stdin=subprocess.DEVNULL)
        (self.repo / "stray.txt").write_text("not mine\n")
        subprocess.run(["git", "add", "stray.txt"], cwd=self.repo, check=True,
                       capture_output=True, stdin=subprocess.DEVNULL)
        subprocess.run(["git", "commit", "-q", "-m", "stray"], cwd=self.repo,
                       check=True, capture_output=True, stdin=subprocess.DEVNULL)

        path = worktree.ensure(self.repo, self.root, "abc123")

        self.assertFalse((path / "stray.txt").exists(),
                         "the task's branch must come from main, not from HEAD")

    def test_two_tasks_get_independent_trees(self):
        first = worktree.ensure(self.repo, self.root, "aaa")
        (first / "one.txt").write_text("one\n")
        second = worktree.ensure(self.repo, self.root, "bbb")

        self.assertNotEqual(first, second)
        self.assertFalse((second / "one.txt").exists())

    def test_ensure_reuses_an_existing_tree_with_its_uncommitted_work(self):
        # The park-and-resume case: this is the whole point of the slice.
        path = worktree.ensure(self.repo, self.root, "abc123")
        (path / "half-done.txt").write_text("work in progress\n")

        again = worktree.ensure(self.repo, self.root, "abc123")

        self.assertEqual(again, path)
        self.assertEqual((again / "half-done.txt").read_text(), "work in progress\n")
        self.assertEqual(branch_of(again), "claudeloop/abc123")

    def test_ensure_reattaches_to_an_existing_branch_when_the_tree_is_gone(self):
        path = worktree.ensure(self.repo, self.root, "abc123")
        (path / "committed.txt").write_text("real work\n")
        subprocess.run(["git", "add", "committed.txt"], cwd=path, check=True,
                       capture_output=True, stdin=subprocess.DEVNULL)
        subprocess.run(["git", "commit", "-q", "-m", "work"], cwd=path, check=True,
                       capture_output=True, stdin=subprocess.DEVNULL)
        worktree.release(self.repo, path)
        self.assertFalse(path.exists())

        again = worktree.ensure(self.repo, self.root, "abc123")

        self.assertEqual(branch_of(again), "claudeloop/abc123")
        self.assertEqual((again / "committed.txt").read_text(), "real work\n",
                         "an answered task must land back on its own work")

    def test_ensure_raises_when_git_cannot_create_the_worktree(self):
        not_a_repo = self.tmp / "elsewhere"
        not_a_repo.mkdir()

        with self.assertRaises(RuntimeError):
            worktree.ensure(not_a_repo, self.root, "abc123")

    def test_release_removes_a_clean_tree_and_keeps_the_branch(self):
        path = worktree.ensure(self.repo, self.root, "abc123")

        worktree.release(self.repo, path)

        self.assertFalse(path.exists())
        branches = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/"],
            cwd=self.repo, check=True, capture_output=True, text=True,
            stdin=subprocess.DEVNULL,
        ).stdout.split()
        self.assertIn("claudeloop/abc123", branches)

    def test_release_keeps_a_dirty_tree_and_does_not_raise(self):
        path = worktree.ensure(self.repo, self.root, "abc123")
        (path / "uncommitted.txt").write_text("do not destroy this\n")

        worktree.release(self.repo, path)  # must not raise

        self.assertTrue((path / "uncommitted.txt").exists(),
                        "an unattended loop must never destroy a working tree")

    def test_probe_accepts_a_real_repository(self):
        self.assertIsNone(worktree.probe(self.repo))

    def test_probe_rejects_a_directory_that_is_not_a_repository(self):
        not_a_repo = self.tmp / "elsewhere"
        not_a_repo.mkdir()

        message = worktree.probe(not_a_repo)

        self.assertIsNotNone(message)
        self.assertIn(str(not_a_repo), message)

    def test_probe_rejects_a_repository_with_no_resolvable_default_branch(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=empty, check=True,
                       capture_output=True, stdin=subprocess.DEVNULL)

        message = worktree.probe(empty)

        self.assertIsNotNone(message)
        self.assertIn("default branch", message)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run the tests, confirm they fail**

Run: `python -m unittest tests.test_worktree -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'claudeloop.worktree'`.

- [ ] **Step 4: Write `claudeloop/worktree.py`**

Move `GIT_TIMEOUT_S`, `DEFAULT_BRANCH_CANDIDATES`, `_git`, `_try_git` and `default_branch` out of `loop.py` **unchanged, docstrings included** (they are deleted from `loop.py` in Task 3, not before, so the suite stays green in between).

```python
"""One git worktree per task, so no two tasks share a working tree.

Every task runs in its own checkout on its own branch, cut from the
repository's default branch. That removes the shared state
`reset_to_default_branch` used to compensate for: there is nothing for a task
to inherit from the task before it, and a task parked on a question keeps its
tree -- branch, commits and uncommitted changes -- until its answer arrives.

`git worktree add` writes `.git/worktrees/<task-id>/` into the target
repository. That is the one deliberate exception to the constraint that no
trace of ClaudeLoop lives in a repository it works in: it sits outside every
working tree, no `git add` can stage it, and `git worktree prune` removes it.
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

# GIT_TIMEOUT_S, DEFAULT_BRANCH_CANDIDATES, _git, _try_git and default_branch
# move here verbatim from loop.py.


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
    try:
        result = _git(repo, "worktree", "add", "-b", branch, str(path), base)
        if result.returncode != 0 and _branch_exists(repo, branch):
            result = _git(repo, "worktree", "add", str(path), branch)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"could not run git worktree add in {repo}: {error}") from error
    if result.returncode != 0:
        raise RuntimeError(
            f"could not create a worktree at {path}: {result.stderr.strip()}"
        )
    return path


def release(path_repo: Path, path: Path) -> None:
    """Remove a finished task's worktree. Never forced, never raises.

    Git refuses to remove a tree with uncommitted changes, and that refusal
    is kept: destroying a working tree in an unattended loop is worse than
    leaving a directory behind. The branch and its commits live in the
    repository and survive removal either way.

    Run from the repository, not from the tree being removed.
    """
    result = _try_git(path_repo, "worktree", "remove", str(path))
    if result is not None and result.returncode != 0:
        log.info(
            "keeping the worktree at %s -- git would not remove it (%s)",
            path,
            result.stderr.strip(),
        )
```

Rename the first parameter of `release` to `repo` when writing the file (`path_repo` above only avoids shadowing in this excerpt); the call is `release(repo, path)`.

- [ ] **Step 5: Run the tests, confirm they pass**

Run: `python -m unittest tests.test_worktree -v`
Expected: PASS, 11 tests.

Then the whole suite — `loop.py` still has its own copies, so nothing else moved yet:

Run: `python -m unittest discover -s tests -t .`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add claudeloop/worktree.py tests/gitrepo.py tests/test_worktree.py
git commit -m "feat: a worktree module, one tree per task id"
```

---

### Task 2: The working tree flows through the session and the prompt

**Files:**
- Modify: `claudeloop/session.py:35-55` (`build_command`), `claudeloop/session.py:152-182` (`run`)
- Modify: `claudeloop/prompt.py:138-170` (`compose`)
- Test: `tests/test_session.py`, `tests/test_prompt.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces:
  - `prompt.compose(cfg: Config, tree: Path | None = None) -> str`
  - `session.build_command(cfg, session_id: str, prompt: str, resume: bool, tree: Path | None = None) -> list[str]`
  - `session.run(cfg, run_dir: Path, session_id: str, prompt: str, resume: bool, cwd: Path | None = None) -> list[dict]`

`None` means `cfg.repo`, which is what every existing caller and test gets. Task 3 makes `run_task` pass the worktree.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_prompt.py`:

```python
    def test_the_definition_of_done_names_the_tree_the_session_works_in(self):
        # Under worktrees the session's cwd is not cfg.repo, and pointing a
        # literal-minded agent at a CLAUDE.md outside its own working
        # directory invites it to edit the wrong copy.
        (self.repo / "CLAUDE.md").write_text("repo rules\n")
        tree = self.tmp / "worktrees" / "abc123"
        tree.mkdir(parents=True)
        (tree / "CLAUDE.md").write_text("repo rules\n")

        text = compose(self.cfg(), tree)

        self.assertIn(str(tree / "CLAUDE.md"), text)
        self.assertNotIn(str(self.repo / "CLAUDE.md"), text)
```

(`self.repo` / `self.tmp` follow whatever `tests/test_prompt.py`'s existing `setUp` and `cfg()` helper already provide; read them before writing this test and match their names.)

Add to `tests/test_session.py`:

```python
    def test_the_session_runs_in_the_tree_it_is_given(self):
        # The worktree, not cfg.repo: cfg.repo is only the repository the
        # task's branch was cut from.
        tree = self.tmp / "worktrees" / "abc123"
        tree.mkdir(parents=True)

        asyncio.run(session.run(self.cfg, self.run_dir, "uuid-1", "do it",
                                resume=False, cwd=tree))

        self.assertEqual((self.run_dir / "cwd.txt").read_text().strip(), str(tree))
```

This needs the fake CLI to record its working directory. Add one line to `tests/fake_claude.sh`, immediately after its existing shebang and `set` line:

```bash
pwd > "$(dirname "$CLAUDELOOP_RESULT")/cwd.txt"
```

- [ ] **Step 2: Run the tests, confirm they fail**

Run: `python -m unittest tests.test_prompt tests.test_session -v`
Expected: FAIL — `compose() takes 1 positional argument but 2 were given`, and `run() got an unexpected keyword argument 'cwd'`.

- [ ] **Step 3: Thread the tree through**

`prompt.py`:

```python
def compose(cfg: Config, tree: Path | None = None) -> str:
    """`tree` is the working directory the session will run in -- its own
    worktree. It differs from cfg.repo, which is only the repository that
    tree was cut from, and it is the copy of CLAUDE.md the session can
    actually edit."""
    operator = _read(cfg.instructions_file)
```

and, further down, replace `claude_md = repo_claude_md(cfg.repo)` with:

```python
    claude_md = repo_claude_md(tree or cfg.repo)
```

`session.py`:

```python
def build_command(
    cfg: Config, session_id: str, prompt: str, resume: bool, tree: Path | None = None
) -> list[str]:
    command = ["claude", "-p", prompt]
```

with `compose(cfg)` becoming `compose(cfg, tree)`, and:

```python
async def run(
    cfg: Config,
    run_dir: Path,
    session_id: str,
    prompt: str,
    resume: bool,
    cwd: Path | None = None,
) -> list[dict]:
```

with `*build_command(cfg, session_id, prompt, resume)` becoming
`*build_command(cfg, session_id, prompt, resume, cwd)` and `cwd=cfg.repo` becoming
`cwd=cwd or cfg.repo`.

- [ ] **Step 4: Run the tests, confirm they pass**

Run: `python -m unittest discover -s tests -t .`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add claudeloop/session.py claudeloop/prompt.py tests/test_session.py tests/test_prompt.py tests/fake_claude.sh
git commit -m "feat: a session can be pointed at a working tree other than the repo"
```

---

### Task 3: `run_task` runs each task in its own worktree

**Files:**
- Modify: `claudeloop/loop.py` — delete `DEFAULT_BRANCH_CANDIDATES`, `GIT_TIMEOUT_S`, `_git`, `_try_git`, `default_branch`, `reset_to_default_branch` (lines 276-384) and the now-unused `os`/`subprocess` imports if nothing else uses them; rewrite `run_task` (lines 399-546)
- Modify: `tests/test_loop.py` — delete `ResetToDefaultBranchTest` (672-760), rewrite `RunTaskResetsBranchBeforeEachTaskTest` (762-856), migrate fake-`.git` fixtures to `make_repo`, rewrite the three reset assertions in `ResumeWithAnswerTest` (1002-1037) and the resumed-branch test at 1260

**Interfaces:**
- Consumes: `worktree.ensure(repo, root, task_id) -> Path`, `worktree.release(repo, path) -> None` (Task 1); `session.run(..., cwd=...)` (Task 2).
- Produces: `run_task` unchanged in signature; a task whose worktree cannot be created returns `{"status": "failed", "summary": ...}` and is marked in its source like any other failure.

- [ ] **Step 1: Write the failing tests**

Replace `RunTaskResetsBranchBeforeEachTaskTest` in `tests/test_loop.py` with:

```python
class WorktreePerTaskTest(unittest.TestCase):
    """End to end against a real git repo and a fake `claude` that commits
    wherever it is run. Each task must land on its own branch, in its own
    tree, carrying only its own commit."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = make_repo(self.tmp / "repo")
        self.tasks = self.tmp / "tasks.md"
        self.tasks.write_text("- [ ] first thing\n- [ ] second thing\n")
        self.cfg = Config(
            repo=self.repo, tasks_file=self.tasks, home=self.tmp / "home", max_resumes=3
        )
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "claude"
        fake.write_text(
            "#!/usr/bin/env bash\n"
            "set -e\n"
            'name="$(basename "$(dirname "$CLAUDELOOP_RESULT")")"\n'
            'echo work > "$name.txt"\n'
            'git add "$name.txt"\n'
            'git commit -q -m "$name"\n'
            "git rev-parse --abbrev-ref HEAD >> "
            f'"{self.tmp}/branches.txt"\n'
            'printf \'%s\' \'{"status":"done","summary":"ok"}\' > "$CLAUDELOOP_RESULT"\n'
            "echo '{\"type\":\"result\",\"total_cost_usd\":0.01}'\n"
        )
        fake.chmod(0o755)
        self.old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{self.old_path}"

    def tearDown(self):
        os.environ["PATH"] = self.old_path

    def test_each_task_commits_on_its_own_branch_carrying_only_its_own_commit(self):
        asyncio.run(loop.main_loop(self.cfg, once=True))

        branches = [
            line.strip()
            for line in (self.tmp / "branches.txt").read_text().splitlines()
            if line.strip()
        ]
        self.assertEqual(len(branches), 2)
        self.assertEqual(len(set(branches)), 2, "two tasks must not share a branch")
        for branch in branches:
            self.assertTrue(branch.startswith("claudeloop/"))
            ahead = subprocess.run(
                ["git", "rev-list", "--count", f"main..{branch}"],
                cwd=self.repo, capture_output=True, text=True, check=True,
                stdin=subprocess.DEVNULL,
            ).stdout.strip()
            self.assertEqual(ahead, "1", f"{branch} should carry only its own commit")

    def test_the_repository_itself_is_never_moved_off_its_branch(self):
        asyncio.run(loop.main_loop(self.cfg, once=True))

        head = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=self.repo,
            capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL,
        ).stdout.strip()
        self.assertEqual(head, "main")

    def test_a_finished_tasks_worktree_is_released(self):
        asyncio.run(loop.main_loop(self.cfg, once=True))

        trees = self.cfg.home / "worktrees"
        self.assertEqual(
            [p for p in trees.iterdir() if p.is_dir()] if trees.exists() else [], []
        )
```

Add to `ResumeWithAnswerTest`, replacing `test_a_resume_still_resets_the_working_tree` and the reset half of `test_a_normal_task_still_resets_the_tree_and_fires_start`:

```python
    def test_a_resume_returns_to_the_same_worktree(self):
        # The point of the slice: the parked session finds its own tree,
        # including work it never committed.
        self.park()
        tree = self.cfg.home / "worktrees" / self.task.id
        tree.mkdir(parents=True, exist_ok=True)

        calls = []
        with mock.patch.object(loop.worktree, "ensure",
                               side_effect=lambda repo, root, task_id: (
                                   calls.append((repo, root, task_id)) or tree)):
            asyncio.run(loop.run_task(self.cfg, self.state, self.source, self.task,
                                      resume_with="use EUR"))

        self.assertEqual(calls, [(self.cfg.repo, self.cfg.home / "worktrees",
                                  self.task.id)])

    def test_a_parked_task_keeps_its_worktree_and_a_finished_one_does_not(self):
        released = []
        tree = self.cfg.home / "worktrees" / self.task.id
        tree.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(loop.worktree, "ensure",
                               side_effect=lambda repo, root, task_id: tree), \
             mock.patch.object(loop.worktree, "release",
                               side_effect=lambda repo, path: released.append(path)):
            # fake_claude.sh writes a done result.
            asyncio.run(loop.run_task(self.cfg, self.state, self.source, self.task))
            self.assertEqual(released, [tree])

            released.clear()
            self.fake_blocked()
            asyncio.run(loop.run_task(self.cfg, self.state, self.source, self.task))

        self.assertEqual(released, [], "a parked task must keep its tree")

    def test_a_task_whose_worktree_cannot_be_created_fails(self):
        with mock.patch.object(loop.worktree, "ensure",
                               side_effect=RuntimeError("no disk")):
            result = asyncio.run(
                loop.run_task(self.cfg, self.state, self.source, self.task))

        self.assertEqual(result["status"], "failed")
        self.assertIn("no disk", result["summary"])
        row = self.state.db.execute("SELECT * FROM tasks WHERE id=?",
                                    (self.task.id,)).fetchone()
        self.assertEqual(row["status"], "failed")
```

`fake_blocked()` is a small helper to add to `ResumeWithAnswerTest`, overwriting the fake CLI on `PATH` with one that parks:

```python
    def fake_blocked(self) -> None:
        fake = Path(os.environ["PATH"].split(os.pathsep)[0]) / "claude"
        fake.write_text(
            "#!/usr/bin/env bash\n"
            'printf \'%s\' \'{"status":"blocked","summary":"stuck",'
            '"question":"which currency?"}\' > "$CLAUDELOOP_RESULT"\n'
            "echo '{\"type\":\"result\",\"total_cost_usd\":0.1}'\n"
        )
        fake.chmod(0o755)
```

Rewrite the resumed-branch regression at line 1260 (`test_a_resumed_task_returns_to_the_default_branch_not_the_intervening_tasks`) as the same scenario under worktrees. Its four inline `git init`/`config`/`commit` calls go away — `setUp` now builds a real repository through `make_repo`:

```python
    def test_a_resumed_task_commits_only_its_own_work_not_the_intervening_tasks(self):
        # Regression for the S2b live smoke test: task 1 parked before its
        # first commit (the usual case -- the question that blocks it blocks
        # it early), task 2 then ran and left its own branch checked out, and
        # the resumed task 1 committed onto *that* branch -- observed for real
        # as "File committed to add-gitignore branch". Under one worktree per
        # task there is no shared checkout to inherit. This pins that.
        count_file = self.tmp / "invocations"
        self.fake.write_text(
            "#!/usr/bin/env bash\n"
            "set -e\n"
            f'n=$(( $(cat "{count_file}" 2>/dev/null || echo 0) + 1 ))\n'
            f'echo "$n" > "{count_file}"\n'
            'name="$(basename "$(dirname "$CLAUDELOOP_RESULT")")"\n'
            'if [ "$n" -eq 1 ]; then\n'
            '  printf \'%s\' \'{"status":"blocked","summary":"stuck",'
            '"question":"which currency?"}\' > "$CLAUDELOOP_RESULT"\n'
            "else\n"
            '  echo work > "$name.txt"\n'
            '  git add "$name.txt"\n'
            '  git commit -q -m "$name"\n'
            '  printf \'%s\' \'{"status":"done","summary":"ok"}\' > "$CLAUDELOOP_RESULT"\n'
            "fi\n"
            "echo '{\"type\":\"result\",\"total_cost_usd\":0.1}'\n"
        )
        self.fake.chmod(0o755)
        self.tasks.write_text("- [ ] ambiguous thing\n- [ ] second thing\n")

        asyncio.run(loop.main_loop(self.cfg, once=True))

        self.assertEqual(self.tasks.read_text(),
                         "- [!] ambiguous thing\n- [x] second thing\n")
        state = State(self.cfg.home / "state.db")
        parked = state.blocked()[0]
        others = [row["id"] for row in
                  state.db.execute("SELECT id FROM tasks WHERE status='done'").fetchall()]
        self.assertEqual(len(others), 1)
        self.assertTrue((self.cfg.home / "worktrees" / parked["id"]).exists(),
                        "a parked task must keep its worktree while other tasks run")

        # A human answers the parked task.
        (self.cfg.home / "runs" / parked["id"] / "answer.json").write_text(
            json.dumps({"answer": "use EUR"}))

        asyncio.run(loop.main_loop(self.cfg, once=True))

        # The resumed session committed in its own tree, on its own branch.
        files = subprocess.run(
            ["git", "ls-tree", "--name-only", f"claudeloop/{parked['id']}"],
            cwd=self.cfg.repo, check=True, capture_output=True, text=True,
            stdin=subprocess.DEVNULL,
        ).stdout.split()
        self.assertIn(f"{parked['id']}.txt", files)
        self.assertNotIn(f"{others[0]}.txt", files,
                         "the resumed task must not carry the intervening task's work")
```

The fixtures in `MainLoopTest`, `StatusWiringTest`, `PromptSelectionTest`, `SourceLifecycleTest`, `ResumeWithAnswerTest`, `AnsweredMainLoopTest` and `HeartbeatTest` create `repo/.git` as an empty directory. Every one of them now reaches real git, so replace each

```python
        (repo / ".git").mkdir(parents=True)
```

with

```python
        make_repo(repo)
```

and add `from .gitrepo import make_repo` at the top of `tests/test_loop.py`.

- [ ] **Step 2: Run the tests, confirm they fail**

Run: `python -m unittest tests.test_loop -v`
Expected: FAIL — `AttributeError: module 'claudeloop.loop' has no attribute 'worktree'`, plus the new end-to-end tests failing because tasks still share the repository's tree.

- [ ] **Step 3: Rewrite `run_task`**

Import at the top of `loop.py`: `from . import worktree`. Delete `DEFAULT_BRANCH_CANDIDATES`, `GIT_TIMEOUT_S`, `_git`, `_try_git`, `default_branch` and `reset_to_default_branch`, and the `os`/`subprocess` imports if nothing else in the file uses them (check with `grep -n "os\.\|subprocess\." claudeloop/loop.py`).

In `run_task`, replace the `await asyncio.to_thread(reset_to_default_branch, cfg.repo)` block (and its long comment, which describes a mechanism that no longer exists) with:

```python
    # One worktree per task, so nothing is shared between tasks and there is
    # nothing to reset. reset_to_default_branch lived here until S6: it
    # compensated for a single shared tree by mutating it between tasks, and
    # the S2b live smoke test showed the cost -- a task that parked before
    # its first commit resumed onto the *next* task's branch. A parked task
    # now keeps its own tree, uncommitted work included, until its answer
    # arrives.
    #
    # Offloaded to a thread for the same reason the reset was: it shells out
    # to git synchronously, and this coroutine must not block the event loop
    # the heartbeat and the dashboard share.
    try:
        tree = await asyncio.to_thread(
            worktree.ensure, cfg.repo, cfg.home / "worktrees", task.id
        )
    except Exception as error:
        # A verdict on the task, not a crash: the loop must be able to move
        # on and mark this one, the same as any other failure.
        log.warning("task %s: no worktree (%s)", task.id, error)
        result = {"status": "failed", "summary": f"ClaudeLoop could not create a worktree: {error}"}
        state.finish_task(task.id, result["status"], result["summary"], 0.0)
        await asyncio.to_thread(source.mark, task, result["status"], result["summary"], 0.0)
        return result
```

Pass it to the session inside the loop:

```python
        events = await session.run(
            cfg,
            run_dir,
            session_id,
            prompt=prompt,
            resume=resume,
            cwd=tree,
        )
```

And release it after the verdict is recorded, before returning:

```python
    if result["status"] != "blocked":
        # A parked task keeps its tree -- that is what its resumed session
        # comes back to. Everything else is released, which never forces
        # anything: git refuses to remove a tree with uncommitted changes and
        # that refusal is kept.
        await asyncio.to_thread(worktree.release, cfg.repo, tree)
```

Place it after `state.finish_task` and `source.mark`, immediately before the closing `log.info`/`return result`.

- [ ] **Step 4: Run the tests, confirm they pass**

Run: `python -m unittest discover -s tests -t .`
Expected: PASS. If `tests/test_web.py` or `tests/test_config.py` fixtures also build fake `.git` directories, they do not run tasks and can stay as they are.

- [ ] **Step 5: Commit**

```bash
git add claudeloop/loop.py tests/test_loop.py
git commit -m "feat: every task runs in its own worktree, and reset_to_default_branch is gone"
```

---

### Task 4: Refuse to start on a box that cannot do worktrees

**Files:**
- Modify: `claudeloop/loop.py:717-735` (`main`)
- Test: `tests/test_loop.py` (`MainConfigErrorTest`, line 571)

**Interfaces:**
- Consumes: `worktree.probe(repo) -> str | None` (Task 1).
- Produces: nothing later tasks use.

- [ ] **Step 1: Write the failing test**

```python
    def test_a_repo_that_cannot_do_worktrees_exits_with_the_probe_message(self):
        cfg = Config(repo=Path("/nope"), tasks_file=Path("/tmp/tasks.md"),
                     home=Path("/tmp/home"))
        with mock.patch.object(loop, "load_config", return_value=cfg), \
             mock.patch.object(loop.worktree, "probe",
                               return_value="cannot use git worktrees in /nope"), \
             mock.patch.object(loop, "_serve_dashboard") as serve:
            with self.assertRaises(SystemExit) as raised:
                loop.main()

        self.assertIn("cannot use git worktrees", str(raised.exception))
        serve.assert_not_called()
```

- [ ] **Step 2: Run it, confirm it fails**

Run: `python -m unittest tests.test_loop.MainConfigErrorTest -v`
Expected: FAIL — `loop.main()` runs on and tries to start the dashboard.

- [ ] **Step 3: Add the probe to `main`**

Between the `load_config` block and `_serve_dashboard(cfg)`:

```python
    # Before anything starts listening or runs: a box whose git cannot make
    # worktrees would otherwise fail every task in turn, one paid session at
    # a time, instead of saying so once.
    problem = worktree.probe(cfg.repo)
    if problem:
        raise SystemExit(problem)
```

- [ ] **Step 4: Run the tests, confirm they pass**

Run: `python -m unittest discover -s tests -t .`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add claudeloop/loop.py tests/test_loop.py
git commit -m "feat: refuse to start when the repository cannot do worktrees"
```

---

### Task 5: The three prompt strings

**Files:**
- Modify: `claudeloop/prompt.py:41-62` (`BUILTIN_DEFINITION_OF_DONE`)
- Modify: `claudeloop/loop.py:64-95` (`ANSWER_PROMPT`, `FRESH_ANSWER_PROMPT`)
- Test: `tests/test_prompt.py`, `tests/test_loop.py` (`ResumePromptTest`, line 121)

**Interfaces:**
- Consumes: nothing.
- Produces: nothing structural — but `ResumeWithAnswerTest.test_a_resume_sends_the_answer_prompt` asserts `"check out the branch you were working on"` (test_loop.py:1000) and must be updated in this task.

These strings are the product: a capable but literal-minded agent executes them unsupervised. Pin every new sentence with a test.

- [ ] **Step 1: Write the failing tests**

In `tests/test_loop.py`, replace `test_the_answer_prompt_warns_that_the_branch_may_not_be_checked_out` with:

```python
    def test_the_answer_prompt_says_the_tree_is_as_it_was_left(self):
        # Under one worktree per task the tree does not move while a task is
        # parked, so the old "check out the branch you were working on"
        # instruction became false -- and telling a session to check out a
        # branch it is already on invites it to guess at a name it may have
        # renamed.
        text = loop.ANSWER_PROMPT.format(answer="use EUR")
        self.assertIn("exactly as you left it", text)
        self.assertIn("still on your branch", text)
        self.assertNotIn("check out the branch you were working on", text)

    def test_the_fresh_answer_prompt_says_the_earlier_attempts_commits_are_here(self):
        text = loop.FRESH_ANSWER_PROMPT.format(task="do a thing", answer="use EUR")
        self.assertIn("on the branch that attempt used", text)
        self.assertNotIn("may have left a branch", text)
```

In `tests/test_prompt.py`:

```python
    def test_the_definition_of_done_does_not_ask_the_session_to_branch(self):
        # ClaudeLoop creates the branch now, so the instruction a live smoke
        # test measured at ~50% compliance is gone rather than reworded.
        self.assertNotIn("Create that branch before your first commit",
                         BUILTIN_DEFINITION_OF_DONE)
        self.assertIn("already on a branch", BUILTIN_DEFINITION_OF_DONE)

    def test_the_definition_of_done_still_forbids_the_default_branch(self):
        self.assertIn("never check out the default branch", BUILTIN_DEFINITION_OF_DONE)

    def test_the_definition_of_done_allows_a_rename(self):
        self.assertIn("git branch -m", BUILTIN_DEFINITION_OF_DONE)
```

- [ ] **Step 2: Run them, confirm they fail**

Run: `python -m unittest tests.test_prompt tests.test_loop.ResumePromptTest -v`
Expected: FAIL on each new assertion.

- [ ] **Step 3: Rewrite the strings**

`prompt.py` — the first two sentences of `BUILTIN_DEFINITION_OF_DONE` become:

```python
BUILTIN_DEFINITION_OF_DONE = (
    "Done means: the change is implemented; the repository's own tests and "
    "checks, if it has any, pass; and the work is committed. You are already "
    "on a branch made for this task and cut from the repository's default "
    "branch, so commit there -- you do not need to create one, and you may "
    "rename it to something descriptive with `git branch -m` if you like. "
    "Never check out the default branch and commit onto it. A pull request "
    "is open. If the repository has no remote configured, "
    ...
)
```

Keep the rest of the string exactly as it is, including the no-remote paragraph and the task-tracking-file paragraph.

`loop.py`:

```python
ANSWER_PROMPT = (
    "A human has answered the question you were blocked on.\n\n"
    "Their answer: {answer}\n\n"
    "Act on that answer and finish the task. Your working tree is exactly as "
    "you left it -- still on your branch, with any uncommitted changes still "
    "there -- so carry on from where you stopped. When the work is complete, "
    "write the result file at the path in the CLAUDELOOP_RESULT environment "
    "variable exactly as before; that file, not your last message, is what "
    "ends the task."
)
"""Sent when resuming a parked task whose question has been answered.

Before S6 this had to talk the session back onto its own branch: every task
that ran while this one was parked reset the single shared working tree. Each
task now has its own worktree, which nothing else touches while it is parked
-- so the honest thing to say is the opposite, and saying it stops a resumed
session guessing at a branch name it may have renamed."""

FRESH_ANSWER_PROMPT = (
    "{task}\n\n"
    "A human has already answered a question about this task: {answer}\n\n"
    "The session that asked that question is no longer available, so start "
    "this task from the beginning, using that answer. You are on the branch "
    "that attempt used and its commits are here; look before you redo work "
    "that is already done."
)
```

- [ ] **Step 4: Update the assertion at test_loop.py:1000**

```python
        self.assertIn("exactly as you left it", self.args())
```

- [ ] **Step 5: Run the tests, confirm they pass**

Run: `python -m unittest discover -s tests -t .`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add claudeloop/prompt.py claudeloop/loop.py tests/test_prompt.py tests/test_loop.py
git commit -m "feat: the prompts stop asking a session to branch and to find its branch again"
```

---

### Task 6: The constraint, the roadmap and the manual

**Files:**
- Modify: `CLAUDE.md` (the "No trace of ClaudeLoop lives in a repository it works in" constraint, and the architecture table)
- Modify: `ROADMAP.md` (slice table, a Built section, three open issues removed)
- Modify: `README.md:130-230` (the definition-of-done summary and the parked-task section)

**Interfaces:**
- Consumes: everything above.
- Produces: nothing.

- [ ] **Step 1: Rewrite the hard constraint in `CLAUDE.md`**

Replace the bullet beginning "**No trace of ClaudeLoop lives in a repository it works in.**" with:

```markdown
- **Nothing ClaudeLoop writes into a repository may be committable.** The
  result file, event log and database all live under `~/.claudeloop/`, and
  `load_config` refuses a `tasks_file` that resolves inside `repo`. A session
  doing ordinary branch hygiene — `git add -A`, `git checkout -- .`, `git
  stash` — would otherwise revert ClaudeLoop's own mark and make finished work
  look pending. S6 makes one deliberate, narrow exception: `git worktree add`
  writes `.git/worktrees/<task-id>/` into the target repository, and a `.git`
  file into the worktree. Neither is reachable from any working tree's staging
  area, and `worktree.probe` prunes stale entries at startup. Any further
  exception needs the same justification, recorded in a spec.
```

Add a row to the architecture table, after `session.py`:

```markdown
| `worktree.py` | One git worktree per task: create, reuse, release, and the startup probe |
```

- [ ] **Step 2: Update `ROADMAP.md`**

- Slice table: add `| **S6** | A git worktree per task | merged |` (the row's state becomes `merged` only when the branch is merged; write `in progress` until then).
- Replace the "Proposed — a git worktree per task" section under **Next** with a **Built** section describing what shipped, following the style of the S2b entry: what it is, what the live smoke test found, and the spec path.
- Delete these three open issues, now closed: "**Parking widens the window in which the default branch can be contaminated…**", "A parked task holds a branch in the target repository while other tasks run…", and the sentence in the S2b entry about uncommitted work in a parked tree being lost.
- Add one open issue in their place:

```markdown
- Worktrees accumulate: a parked task's tree persists by design, and a failed
  task's tree persists when it is dirty, since `git worktree remove` is never
  forced. No age or count policy. Bounded in practice by how many questions go
  unanswered, unbounded in principle.
```

- [ ] **Step 3: Update `README.md`**

- In the definition-of-done summary (around line 137), replace "commit on a new branch created from the default branch" with a note that ClaudeLoop creates the branch — `claudeloop/<task-id>`, cut from the default branch, in a worktree under `~/.claudeloop/worktrees/` — and the session commits there.
- In the parked-task section (around lines 212-220), delete the paragraph about the working tree moving off the parked session's branch and the resumed session being told to check it back out. Replace with: the parked task's worktree is untouched while other tasks run, so the resumed session finds its branch, its commits and its uncommitted changes exactly as it left them.
- Add one paragraph on operator-visible behaviour: each task's worktree lives at `~/.claudeloop/worktrees/<task-id>`, is removed when the task finishes, and is kept when the task parks or when it leaves uncommitted changes behind.

- [ ] **Step 4: Run the whole suite one more time**

Run: `python -m unittest discover -s tests -t .`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md ROADMAP.md README.md
git commit -m "docs: the worktree exception, and the manual stops promising a moved tree"
```

---

## After the plan: the live smoke test

Not optional, and not a step a subagent runs. A scratch repository, `model = "haiku"`, **two tasks**, one of which must park on a question that is then answered through the dashboard. What to watch:

- Each session commits on `claudeloop/<task-id>` inside its own worktree, and the operator's repository never leaves its own branch.
- The parked task's worktree still exists, on its branch, while task 2 runs.
- The resumed session picks up in that tree without being told to check anything out — and does not go looking for a branch, which is the failure mode the reworded `ANSWER_PROMPT` is guarding against.
- A finished task's worktree is gone; a task that left uncommitted changes keeps its own.
- `git worktree list` in the target repository is clean afterwards.

Fix what it finds, then re-run it: prompt-text fixes are the kind that come back differently broken.
