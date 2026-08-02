"""The one value that crosses the loop/web-thread boundary.

The loop *replaces* `current` with a new frozen instance on every transition;
the web thread reads the reference. An atomic reference swap needs no lock and
cannot tear: a reader sees either the old snapshot whole or the new one whole.
Per-field assignment on a shared mutable object would not give that.

That covers readers. Writing is safe today for a narrower reason: exactly one
thread -- the loop -- ever calls set_status(). set_status() is a
read-modify-write (read `current`, dataclasses.replace() it, write the
result back), and a read-modify-write is atomic only under a single writer.
The moment a second writer exists, two concurrent calls can each read the
same `current`, compute their own replace(), and the second write silently
clobbers the first's changes. That needs an actual lock; nothing here
provides one.

S2b -- a human answering a `blocked` task's question from the web thread --
was the exact case this warning was written for, and it did not become that
second writer. The answer route writes a file the loop picks up on its next
poll instead of calling set_status(). So the hazard is **dodged, not
solved**: it is still live for the next route that wants to write from the
web thread, and that route has to add the lock.

One more thing this buys readers, and doesn't: `api_state()` binds one
reference to `current` and reads every field off that single snapshot, so
`status` in the `/api/state` payload is internally consistent -- it never
mixes fields from two different Status instances. `pending` now rides on
that same snapshot. But the payload as a whole is not a consistent snapshot:
`completed` is read from the database *after* `status` is captured, so it
can describe a slightly earlier or later moment than `status` does.
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Status:
    state: str = "idle"  # "idle" | "running" | "waiting" | "error"
    task_id: str | None = None
    task_text: str | None = None
    run_dir: Path | None = None
    session_id: str | None = None
    attempt: int = 0
    started_at: float | None = None
    wait_until: float | None = None  # set while sleeping off a quota block
    rate_limit: dict | None = None  # last rate_limit_info seen, for the gauge
    last_error: str | None = None
    # (task_id, task_text) pairs, source order: the backlog as of the start
    # of the current task, not live -- published once when that task starts,
    # not re-read while it runs. A tuple, not a list: web reads this snapshot
    # from another thread, and a list would be a mutable object the loop
    # still holds open.
    pending: tuple[tuple[str, str], ...] = ()
    heartbeat: float = 0.0


current = Status()


def set_status(**changes) -> Status:
    """Replace `current` with a copy carrying `changes`.

    Always refreshes the heartbeat unless one is passed explicitly: any
    transition at all is proof the loop is still alive. Fields not named are
    carried over, so a caller moving to a state that no longer has a task must
    clear those fields itself.
    """
    global current
    changes.setdefault("heartbeat", time.time())
    current = dataclasses.replace(current, **changes)
    return current


def reset() -> Status:
    """Back to a fresh idle snapshot. For tests."""
    global current
    current = Status()
    return current
