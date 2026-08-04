import json
import os
import shutil
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from claudeloop.plugins import marketplace_sources, register_marketplaces


def write_settings(repo: Path, data: dict | str) -> None:
    (repo / ".claude").mkdir(parents=True, exist_ok=True)
    (repo / ".claude" / "settings.json").write_text(
        data if isinstance(data, str) else json.dumps(data)
    )


class MarketplaceSourcesTest(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)

    def test_a_repository_with_no_settings_file_declares_nothing(self):
        self.assertEqual(marketplace_sources(self.repo), {})

    def test_a_settings_file_with_no_marketplaces_declares_nothing(self):
        write_settings(self.repo, {"hooks": {}})
        self.assertEqual(marketplace_sources(self.repo), {})

    def test_broken_json_is_absent_rather_than_fatal(self):
        # The repository's file, not ClaudeLoop's. The CLI ignores a broken
        # one too; the loop must not refuse to start over it.
        write_settings(self.repo, "{not json")
        self.assertEqual(marketplace_sources(self.repo), {})

    def test_a_github_source_becomes_owner_slash_repo(self):
        write_settings(self.repo, {"extraKnownMarketplaces": {
            "ponytail": {"source": {"source": "github", "repo": "DietrichGebert/ponytail"}},
        }})
        self.assertEqual(marketplace_sources(self.repo),
                         {"ponytail": "DietrichGebert/ponytail"})

    def test_a_directory_source_becomes_its_path(self):
        write_settings(self.repo, {"extraKnownMarketplaces": {
            "local": {"source": {"source": "directory", "path": "/srv/mkt"}},
        }})
        self.assertEqual(marketplace_sources(self.repo), {"local": "/srv/mkt"})

    def test_a_url_source_becomes_its_url(self):
        write_settings(self.repo, {"extraKnownMarketplaces": {
            "remote": {"source": {"source": "git", "url": "https://example.test/m.git"}},
        }})
        self.assertEqual(marketplace_sources(self.repo),
                         {"remote": "https://example.test/m.git"})

    def test_a_bare_string_source_is_taken_as_written(self):
        write_settings(self.repo, {"extraKnownMarketplaces": {"m": "owner/repo"}})
        self.assertEqual(marketplace_sources(self.repo), {"m": "owner/repo"})

    def test_a_source_naming_nothing_installable_is_skipped(self):
        write_settings(self.repo, {"extraKnownMarketplaces": {
            "broken": {"source": {"source": "github"}},
            "fine": {"source": {"source": "github", "repo": "o/r"}},
        }})
        self.assertEqual(marketplace_sources(self.repo), {"fine": "o/r"})


class RegisterMarketplacesTest(unittest.TestCase):
    """Against a fake `claude` on PATH, the way the rest of the suite does."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        self.calls = self.tmp / "calls.txt"
        (bin_dir / "claude").write_text(
            "#!/usr/bin/env bash\n"
            'printf "%s\\n" "$*" >> "$FAKE_PLUGIN_CALLS"\n'
            'if [ -n "$FAKE_PLUGIN_FAIL" ]; then echo "fake failure" >&2; exit 1; fi\n'
        )
        (bin_dir / "claude").chmod(0o755)
        patch = unittest.mock.patch.dict(os.environ, {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_PLUGIN_CALLS": str(self.calls),
        })
        patch.start()
        self.addCleanup(patch.stop)

    def calls_made(self) -> list[str]:
        if not self.calls.exists():
            return []
        return [line for line in self.calls.read_text().splitlines() if line]

    def test_a_repository_declaring_nothing_runs_no_command_at_all(self):
        self.assertIsNone(register_marketplaces(self.repo))
        self.assertEqual(self.calls_made(), [])

    def test_each_declared_marketplace_is_added_at_user_scope(self):
        # User scope, never project or local: those write into the target
        # repository's .claude/, which nothing ClaudeLoop writes may do.
        write_settings(self.repo, {"extraKnownMarketplaces": {
            "ponytail": {"source": {"source": "github", "repo": "DietrichGebert/ponytail"}},
            "caveman": {"source": {"source": "github", "repo": "JuliusBrussee/caveman"}},
        }})
        self.assertIsNone(register_marketplaces(self.repo))
        self.assertEqual(sorted(self.calls_made()), sorted([
            "plugin marketplace add DietrichGebert/ponytail --scope user",
            "plugin marketplace add JuliusBrussee/caveman --scope user",
        ]))

    def test_a_failing_add_is_reported_and_stops_the_loop(self):
        write_settings(self.repo, {"extraKnownMarketplaces": {
            "ponytail": {"source": {"source": "github", "repo": "DietrichGebert/ponytail"}},
        }})
        with unittest.mock.patch.dict(os.environ, {"FAKE_PLUGIN_FAIL": "1"}):
            problem = register_marketplaces(self.repo)
        self.assertIsNotNone(problem)
        self.assertIn("fake failure", problem)
        # Both ways out are named, so the operator is not left guessing.
        self.assertIn("settings.json", problem)
        self.assertIn("--scope user", problem)

    def test_a_claude_that_is_not_on_path_is_reported_not_raised(self):
        write_settings(self.repo, {"extraKnownMarketplaces": {"m": "o/r"}})
        with unittest.mock.patch.dict(os.environ, {"PATH": str(self.tmp / "empty")}):
            problem = register_marketplaces(self.repo)
        self.assertIsNotNone(problem)
        self.assertIn("claude", problem)


if __name__ == "__main__":
    unittest.main()
