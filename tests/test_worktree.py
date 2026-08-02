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
