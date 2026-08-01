"""Where tasks come from. S1 ships one implementation, over a markdown
checklist; S3 adds a Jira one behind the same protocol."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

UNCHECKED = "- [ ]"
DONE = "- [x]"
ATTENTION = "- [!]"


@dataclass(frozen=True)
class Task:
    id: str
    text: str
    source: str
    source_ref: str


class TaskSource(Protocol):
    def pending(self) -> list[Task]: ...
    def start(self, task: Task) -> None: ...
    def mark(self, task: Task, status: str, summary: str, cost: float = 0.0) -> None: ...
    def reopen(self, task: Task) -> None: ...
    def answer(self, task: Task) -> str | None: ...


def task_id(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class FileSource:
    """A markdown checklist. `- [ ]` is pending, `- [x]` succeeded, `- [!]`
    needs a human. Only `- [ ]` is ever picked up."""

    def __init__(self, path: Path):
        self.path = path

    def pending(self) -> list[Task]:
        try:
            body = self.path.read_text()
        except FileNotFoundError:
            return []
        tasks = []
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped.startswith(UNCHECKED):
                continue
            text = stripped[len(UNCHECKED):].strip()
            if text:
                tasks.append(Task(task_id(text), text, "file", stripped))
        return tasks

    def start(self, task: Task) -> None:
        """A checklist has nothing to say when work begins. The Jira source
        uses this to move the issue to its in-progress status."""

    def _rewrite(self, match: str, marker: str, text: str) -> None:
        """Replace the line reading exactly `match` with `marker text`,
        keeping its indentation and line ending.

        Matched on exact line text rather than index, so a user editing the
        file while the task ran cannot cause the wrong line to be rewritten.
        A line that has since vanished -- or a whole file that has -- is left
        alone; the database still holds the record.
        """
        try:
            lines = self.path.read_text().splitlines(keepends=True)
        except OSError:
            return
        for index, line in enumerate(lines):
            if line.strip() != match:
                continue
            body = line.rstrip("\r\n")
            indent = line[: len(line) - len(line.lstrip())]
            eol = line[len(body):]
            lines[index] = f"{indent}{marker} {text}{eol}"
            self.path.write_text("".join(lines))
            return

    def mark(self, task: Task, status: str, summary: str, cost: float = 0.0) -> None:
        """Rewrite the task's line to its verdict."""
        self._rewrite(task.source_ref, DONE if status == "done" else ATTENTION, task.text)

    def reopen(self, task: Task) -> None:
        """Undo a blocked mark, so an answered task is offered again.

        Matches the `- [!]` line this source itself wrote, not the original
        `- [ ]` source_ref, which no longer exists by the time a task is
        reopened.
        """
        self._rewrite(f"{ATTENTION} {task.text}", UNCHECKED, task.text)

    def answer(self, task: Task) -> str | None:
        """A markdown checklist has no reply channel. Answers for a file-source
        task arrive through the dashboard instead."""
        return None
