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


class FileSourceProtocolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "tasks.md"
        self.path.write_text("- [ ] first thing\n")
        self.source = FileSource(self.path)

    def test_start_is_a_no_op_that_does_not_touch_the_file(self):
        before = self.path.read_text()
        self.source.start(self.source.pending()[0])
        self.assertEqual(self.path.read_text(), before)

    def test_mark_accepts_and_ignores_cost(self):
        self.source.mark(self.source.pending()[0], "done", "went fine", 1.25)
        self.assertEqual(self.path.read_text(), "- [x] first thing\n")


class ReopenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "tasks.md"

    def source_for(self, body: str) -> FileSource:
        self.path.write_text(body)
        return FileSource(self.path)

    def test_reopen_restores_an_attention_line_to_pending(self):
        source = self.source_for("- [ ] alpha\n- [ ] beta\n")
        task = source.pending()[0]
        source.mark(task, "blocked", "stuck")
        self.assertEqual(self.path.read_text(), "- [!] alpha\n- [ ] beta\n")

        source.reopen(task)

        self.assertEqual(self.path.read_text(), "- [ ] alpha\n- [ ] beta\n")

    def test_reopen_keeps_indentation(self):
        source = self.source_for("    - [ ] alpha\n")
        task = source.pending()[0]
        source.mark(task, "blocked", "stuck")

        source.reopen(task)

        self.assertEqual(self.path.read_text(), "    - [ ] alpha\n")

    def test_reopen_keeps_a_missing_trailing_newline(self):
        # The last line of a file that does not end in one. mark() and
        # reopen() share _rewrite, so this pins the eol handling for both.
        source = self.source_for("- [ ] alpha")
        task = source.pending()[0]
        source.mark(task, "blocked", "stuck")
        self.assertEqual(self.path.read_text(), "- [!] alpha")

        source.reopen(task)

        self.assertEqual(self.path.read_text(), "- [ ] alpha")

    def test_reopen_leaves_a_line_that_has_since_vanished_alone(self):
        source = self.source_for("- [ ] alpha\n")
        task = source.pending()[0]
        self.path.write_text("- [ ] something else entirely\n")

        source.reopen(task)

        self.assertEqual(self.path.read_text(), "- [ ] something else entirely\n")

    def test_reopen_does_not_touch_a_done_line(self):
        source = self.source_for("- [ ] alpha\n")
        task = source.pending()[0]
        source.mark(task, "done", "finished")

        source.reopen(task)

        self.assertEqual(self.path.read_text(), "- [x] alpha\n")

    def test_a_reopened_task_is_pending_again(self):
        source = self.source_for("- [ ] alpha\n")
        task = source.pending()[0]
        source.mark(task, "blocked", "stuck")
        self.assertEqual(source.pending(), [])

        source.reopen(task)

        self.assertEqual([t.id for t in source.pending()], [task.id])

    def test_a_checklist_has_no_answer_channel(self):
        source = self.source_for("- [ ] alpha\n")

        self.assertIsNone(source.answer(source.pending()[0]))

    def test_mark_survives_a_task_file_that_has_been_deleted(self):
        source = self.source_for("- [ ] alpha\n")
        task = source.pending()[0]
        self.path.unlink()

        source.mark(task, "done", "finished")  # must not raise
        source.reopen(task)

    def test_a_crlf_checklist_keeps_its_line_endings(self):
        # Marking one task must not silently rewrite every line ending in
        # the file. read_text()/write_text() would: they translate CRLF to
        # "\n" on the way in and never put it back.
        self.path.write_bytes(b"- [ ] alpha\r\n- [ ] beta\r\n")
        source = FileSource(self.path)
        task = source.pending()[0]

        source.mark(task, "blocked", "stuck")
        self.assertEqual(self.path.read_bytes(), b"- [!] alpha\r\n- [ ] beta\r\n")

        source.reopen(task)
        self.assertEqual(self.path.read_bytes(), b"- [ ] alpha\r\n- [ ] beta\r\n")

    def test_mixed_line_endings_are_each_left_as_they_were(self):
        self.path.write_bytes(b"- [ ] alpha\r\n- [ ] beta\n")
        source = FileSource(self.path)

        source.mark(source.pending()[1], "done", "did it")

        self.assertEqual(self.path.read_bytes(), b"- [ ] alpha\r\n- [x] beta\n")


if __name__ == "__main__":
    unittest.main()
