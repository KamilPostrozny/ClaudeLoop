import tempfile
import unittest
from pathlib import Path

from claudeloop.config import Config, load_config


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)

    def write(self, body: str) -> Path:
        path = self.tmp / "config.toml"
        path.write_text(body)
        return path

    def test_reads_values_and_applies_defaults(self):
        path = self.write(
            f'repo = "{self.repo}"\n'
            f'tasks_file = "{self.tmp}/tasks.md"\n'
        )
        cfg = load_config(path, home=self.tmp / "home")
        self.assertEqual(cfg.repo, self.repo)
        self.assertEqual(cfg.tasks_file, self.tmp / "tasks.md")
        self.assertEqual(cfg.model, "opus")
        self.assertEqual(cfg.max_resumes, 20)
        self.assertEqual(cfg.home, self.tmp / "home")

    def test_overrides_defaults(self):
        path = self.write(
            f'repo = "{self.repo}"\n'
            f'tasks_file = "{self.tmp}/tasks.md"\n'
            'model = "sonnet"\n'
            'max_resumes = 3\n'
        )
        cfg = load_config(path, home=self.tmp / "home")
        self.assertEqual(cfg.model, "sonnet")
        self.assertEqual(cfg.max_resumes, 3)

    def test_rejects_missing_required_key(self):
        path = self.write(f'repo = "{self.repo}"\n')
        with self.assertRaises(ValueError) as caught:
            load_config(path, home=self.tmp / "home")
        self.assertIn("tasks_file", str(caught.exception))

    def test_rejects_repo_that_is_not_a_git_checkout(self):
        path = self.write(
            f'repo = "{self.tmp}/nope"\n'
            f'tasks_file = "{self.tmp}/tasks.md"\n'
        )
        with self.assertRaises(ValueError) as caught:
            load_config(path, home=self.tmp / "home")
        self.assertIn("git repository", str(caught.exception))

    def test_config_is_frozen(self):
        with self.assertRaises(Exception):
            Config(repo=Path("/a"), tasks_file=Path("/b")).model = "x"


if __name__ == "__main__":
    unittest.main()
