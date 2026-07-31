import tempfile
import unittest
from pathlib import Path

from claudeloop.source import FileSource, Task, task_id


class TaskIdTest(unittest.TestCase):
    def test_is_stable_and_distinct(self):
        self.assertEqual(task_id("do it"), task_id("do it"))
        self.assertNotEqual(task_id("do it"), task_id("do it twice"))
        self.assertEqual(len(task_id("do it")), 16)


class FileSourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "tasks.md"

    def source(self, body: str) -> FileSource:
        self.path.write_text(body)
        return FileSource(self.path)

    def test_pending_returns_unchecked_lines_in_order(self):
        source = self.source(
            "# My tasks\n"
            "- [ ] first thing\n"
            "- [x] already done\n"
            "- [!] failed earlier\n"
            "- [ ] second thing\n"
            "\n"
            "some prose\n"
        )
        self.assertEqual([t.text for t in source.pending()], ["first thing", "second thing"])

    def test_pending_handles_indentation_and_empty_items(self):
        source = self.source("  - [ ] indented\n- [ ] \n- [ ]\n")
        self.assertEqual([t.text for t in source.pending()], ["indented"])

    def test_pending_on_missing_file_is_empty(self):
        self.assertEqual(FileSource(self.tmp / "absent.md").pending(), [])

    def test_mark_done_checks_the_line_off(self):
        source = self.source("- [ ] first thing\n- [ ] second thing\n")
        source.mark(source.pending()[0], "done", "went fine")
        self.assertEqual(self.path.read_text(), "- [x] first thing\n- [ ] second thing\n")

    def test_mark_failed_uses_the_attention_marker(self):
        source = self.source("- [ ] first thing\n")
        source.mark(source.pending()[0], "failed", "broke")
        self.assertEqual(self.path.read_text(), "- [!] first thing\n")
        self.assertEqual(source.pending(), [])

    def test_mark_preserves_indentation(self):
        source = self.source("  - [ ] indented\n")
        source.mark(source.pending()[0], "done", "")
        self.assertEqual(self.path.read_text(), "  - [x] indented\n")

    def test_mark_finds_the_line_after_the_file_was_edited_underneath(self):
        source = self.source("- [ ] first thing\n- [ ] second thing\n")
        task = source.pending()[0]
        # The user inserts a task above while the first one is still running.
        self.path.write_text("- [ ] urgent thing\n- [ ] first thing\n- [ ] second thing\n")
        source.mark(task, "done", "")
        self.assertEqual(
            self.path.read_text(),
            "- [ ] urgent thing\n- [x] first thing\n- [ ] second thing\n",
        )

    def test_mark_is_a_no_op_when_the_line_is_gone(self):
        source = self.source("- [ ] first thing\n")
        task = source.pending()[0]
        self.path.write_text("- [ ] something else\n")
        source.mark(task, "done", "")
        self.assertEqual(self.path.read_text(), "- [ ] something else\n")

    def test_mark_without_trailing_newline(self):
        source = self.source("- [ ] only thing")
        source.mark(source.pending()[0], "done", "")
        self.assertEqual(self.path.read_text(), "- [x] only thing")


if __name__ == "__main__":
    unittest.main()
