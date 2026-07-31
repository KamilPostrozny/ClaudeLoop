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
    def mark(self, task: Task, status: str, summary: str) -> None: ...


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

    def mark(self, task: Task, status: str, summary: str) -> None:
        """Rewrite the task's line.

        Matched on exact line text rather than index, so a user editing the
        file while the task ran cannot cause the wrong line to be marked. A
        line that has since vanished is left alone; the database still holds
        the record.
        """
        marker = DONE if status == "done" else ATTENTION
        lines = self.path.read_text().splitlines(keepends=True)
        for index, line in enumerate(lines):
            if line.strip() != task.source_ref:
                continue
            body = line.rstrip("\r\n")
            indent = line[: len(line) - len(line.lstrip())]
            eol = line[len(body):]
            lines[index] = f"{indent}{marker} {task.text}{eol}"
            self.path.write_text("".join(lines))
            return
