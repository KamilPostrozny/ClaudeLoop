"""The Jira Cloud task source: an HTTP client, a TaskSource over it, and the
small CLI the session itself calls.

Jira Cloud REST v2, deliberately: v2 returns `description` as a plain string
and takes a plain-string comment body, where v3 returns and demands ADF JSON.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .config import DEFAULT_CONFIG, load_config
from .source import Task, task_id

log = logging.getLogger("claudeloop")

SEARCH_PATH = "/search/jql"  # pinned by the live probe in Task 1
"""Atlassian moved the search endpoint; this is the path that answered 200
against a real instance, not the one the documentation happened to show."""

BACKOFF_S = (1.0, 2.0, 4.0)
"""Waits between retries. Only 5xx and network faults get here."""

DONE_LABEL = "claudeloop-done"
BLOCKED_LABEL = "claudeloop-blocked"

GUARD = (
    f'(labels IS EMPTY OR labels NOT IN ("{DONE_LABEL}", "{BLOCKED_LABEL}"))'
)
"""Why not `labels != "claudeloop-done"`: in JQL that excludes every issue
with no labels at all, which is most of a fresh backlog. The IS EMPTY
disjunct is not defensive padding, it is the only correct idiom."""

_ORDER_BY = re.compile(r"\bORDER\s+BY\b", re.IGNORECASE)


def _in_string(text: str) -> bool:
    """Whether `text` ends inside a quoted value.

    JQL uses both " and ' as string delimiters, so counting them
    independently is wrong: an apostrophe inside a double-quoted value
    (`status != "Won't Do"`) makes the double-quote count look unbalanced on
    its own even though nothing is actually open. Tracking the single
    delimiter that is currently open handles both quote characters and
    values that mix them. Escaped quotes inside a value remain unhandled.
    """
    quote = None
    for ch in text:
        if quote is None and ch in "\"'":
            quote = ch
        elif ch == quote:
            quote = None
    return quote is not None


def _split_order_by(jql: str) -> tuple[str, str]:
    """Split on the first ORDER BY that is not inside a quoted value.

    JQL string values can contain the words "order by" -- splitting on
    one silently produces an unbalanced quote, which Jira answers with a
    400 and the loop reads as an empty backlog.
    """
    for match in _ORDER_BY.finditer(jql):
        before = jql[:match.start()]
        if not _in_string(before):
            return before, jql[match.end():]
    return jql, ""


def compose_jql(operator_jql: str) -> str:
    """Splice the guard into the operator's query, keeping their ordering.

    Composed rather than documented: an operator who forgets the exclusion
    gets an infinite loop over one finished ticket, so this must not be
    something they can leave out.
    """
    where, order = _split_order_by(operator_jql)
    where = where.strip()
    order = order.strip()
    composed = f"({where}) AND {GUARD}" if where else GUARD
    return f"{composed} ORDER BY {order}" if order else composed


def task_text(key: str, summary: str | None, description: str | None) -> str:
    """The task the session is handed. The key leads so the prompt layer can
    tell the session to read the first token and find it."""
    head = f"{key}: {(summary or '').strip()}".strip()
    body = (description or "").strip()
    return f"{head}\n\n{body}" if body else head


def closing_comment(status: str, summary: str, cost: float) -> str:
    """Posted by the orchestrator, not the session -- so it exists even when
    the session died mid-run and never said anything, which is exactly when a
    record on the ticket matters most."""
    return (
        f"ClaudeLoop finished this task with status *{status}* "
        f"(cost ${cost:.4f}).\n\n{summary}"
    )


QUESTION_MARKER = "claudeloop:"
"""A comment counts as an answer only if it starts with this. The question
comment says so outright, so the human learns the syntax from the message
they are replying to. Without it, a colleague writing "nice catch" would
resume a session and spend real money acting on it -- and identifying our
own account would become necessary, which this avoids entirely: ClaudeLoop's
own comments never carry the prefix."""

QUESTION_HEADING = "ClaudeLoop is blocked on this task and needs an answer."
"""Also the boundary marker for JiraSource.answer: a task can block twice,
and the first answer must not be read again as the answer to the second
question. Locating the newest comment starting with this heading is what
keeps the two rounds straight, with nothing persisted anywhere."""


def question_comment(summary: str, cost: float) -> str:
    """Posted instead of closing_comment when a task parks.

    `summary` already carries the question -- loop.read_result appends it --
    so this adds the heading and the reply instruction rather than repeating
    it.
    """
    return (
        f"{QUESTION_HEADING}\n\n{summary}\n\n"
        f"Reply with a comment starting with `{QUESTION_MARKER}` and ClaudeLoop "
        f"will pick this task back up with your answer.\n\n"
        f"(cost so far ${cost:.4f})"
    )


class JiraError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"Jira returned {status}: {body[:400]}")
        self.status = status
        self.body = body


class JiraClient:
    """Thin, synchronous, and blocking. Every caller inside the loop reaches
    it through asyncio.to_thread."""

    def __init__(
        self,
        site: str,
        email: str,
        token: str,
        timeout: float = 30.0,
        retries: int = 3,
        sleep=time.sleep,
    ):
        self.base = site.rstrip("/") + "/rest/api/2"
        self.header = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()
        self.timeout = timeout
        self.retries = retries
        self.sleep = sleep

    def _once(self, method: str, path: str, payload: dict | None) -> dict:
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self.base + path, data=body, method=method)
        request.add_header("Authorization", self.header)
        request.add_header("Accept", "application/json")
        if body is not None:
            request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw = response.read()
        # 204 with no body is how a successful PUT /issue/{key} answers. A
        # 200 whose body is JSON but not a JSON *object* -- null, a list, a
        # bare string, the shape an SSO interstitial or a misrouting gateway
        # answers with -- must not reach a caller that assumes dict, or
        # pending()'s data.get("issues") raises AttributeError somewhere
        # main_loop does not have wrapped in a try.
        parsed = json.loads(raw) if raw else {}
        return parsed if isinstance(parsed, dict) else {}

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        for attempt in range(self.retries):
            try:
                return self._once(method, path, payload)
            except urllib.error.HTTPError as error:
                body = error.read().decode(errors="replace")
                # A 4xx is a verdict, not a hiccup: a 401, a 403 or a
                # malformed JQL answers the same way every time, and an
                # unattended loop polling every 30s must not spend a minute
                # rediscovering that on each poll.
                if error.code < 500 or attempt == self.retries - 1:
                    raise JiraError(error.code, body) from error
            except (urllib.error.URLError, OSError, json.JSONDecodeError) as error:
                if attempt == self.retries - 1:
                    raise JiraError(0, str(error)) from error
            self.sleep(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
        raise JiraError(0, "retries exhausted")  # unreachable, kept total

    def search(self, jql: str, max_results: int = 50) -> dict:
        return self._request("POST", SEARCH_PATH, {
            "jql": jql,
            "maxResults": max_results,
            "fields": ["summary", "description"],
        })

    def issue(self, key: str) -> dict:
        key = urllib.parse.quote(key, safe="")
        return self._request("GET", f"/issue/{key}?fields=summary,description,status,labels")

    def comments(self, key: str) -> dict:
        key = urllib.parse.quote(key, safe="")
        return self._request("GET", f"/issue/{key}/comment")

    def add_comment(self, key: str, body: str) -> dict:
        key = urllib.parse.quote(key, safe="")
        return self._request("POST", f"/issue/{key}/comment", {"body": body})

    def add_label(self, key: str, label: str) -> dict:
        # `update` adds one label atomically. Writing fields.labels instead
        # replaces the whole list, deleting the operator's own labels.
        key = urllib.parse.quote(key, safe="")
        return self._request("PUT", f"/issue/{key}", {
            "update": {"labels": [{"add": label}]}
        })

    def remove_label(self, key: str, label: str) -> dict:
        key = urllib.parse.quote(key, safe="")
        return self._request("PUT", f"/issue/{key}", {
            "update": {"labels": [{"remove": label}]}
        })

    def transitions(self, key: str) -> list[dict]:
        key = urllib.parse.quote(key, safe="")
        return self._request("GET", f"/issue/{key}/transitions").get("transitions", [])

    def transition(self, key: str, transition_id: str) -> dict:
        key = urllib.parse.quote(key, safe="")
        return self._request("POST", f"/issue/{key}/transitions", {
            "transition": {"id": transition_id}
        })


class JiraSource:
    """A TaskSource over a Jira Cloud project.

    Nothing here may raise into the loop: an unreachable Jira must look like
    an empty backlog (so the loop idles and retries), and a Jira that refuses
    a write must not turn finished work into a failure.

    Jira's search index is eventually consistent: `mark()` can label a ticket
    `claudeloop-done` and the very next poll can still find it in JQL results,
    because the index has not caught up yet. state.db's terminal_ids() backstop
    (below) is what covers that window -- it is not merely insurance against a
    label write that failed outright, it is load-bearing every time the loop
    polls again shortly after finishing a task.
    """

    def __init__(
        self,
        client: JiraClient,
        jql: str,
        state=None,
        transition_start: str = "",
        transition_done: str = "",
    ):
        self.client = client
        self.jql = jql
        self.state = state
        self.transition_start = transition_start
        self.transition_done = transition_done

    def pending(self) -> list[Task]:
        try:
            data = self.client.search(compose_jql(self.jql))
        except JiraError as error:
            # Deliberately indistinguishable from an empty backlog. The loop
            # idles POLL_S and asks again; a raise here would instead crash
            # main_loop's task handler on every poll.
            log.warning("could not read the Jira backlog (%s); retrying later", error)
            return []
        done = set()
        if self.state is not None:
            try:
                # The backstop for Jira's search-index lag: a ticket just
                # labelled claudeloop-done can still match this JQL on the
                # very next poll because the index has not caught up, and
                # state.db is the only thing that catches that case.
                done = self.state.terminal_ids()
            except sqlite3.Error as error:
                # A locked or corrupt database is not a reason to crash the
                # loop -- it just means this poll has no backstop against a
                # label write that never landed.
                log.warning(
                    "could not read state.db terminal ids (%s); continuing"
                    " without that backstop this poll", error,
                )
                done = set()
        issues = data.get("issues")
        if not isinstance(issues, list):
            log.warning(
                "Jira returned no issues list (got %s); treating the"
                " backlog as empty", type(issues).__name__,
            )
            return []
        tasks = []
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            key = issue.get("key")
            if not key:
                continue
            identifier = task_id(key)
            if identifier in done:
                # Most often Jira's search index has not yet caught up with
                # a label write that already landed; less often the label
                # write itself failed, or someone cleared it. Either way the
                # database says this task already reached a verdict.
                log.warning(
                    "%s is still in the backlog but already finished in"
                    " state.db -- skipping it; likely Jira's search index"
                    " has not caught up with ClaudeLoop's label yet (it can"
                    " also mean the label write failed)", key,
                )
                continue
            fields = issue.get("fields")
            if not isinstance(fields, dict):
                fields = {}
            tasks.append(Task(
                identifier,
                task_text(key, fields.get("summary"), fields.get("description")),
                "jira",
                key,
            ))
        return tasks

    def start(self, task: Task) -> None:
        if self.transition_start:
            self._transition(task.source_ref, self.transition_start)

    def mark(self, task: Task, status: str, summary: str, cost: float = 0.0) -> None:
        key = task.source_ref
        label = DONE_LABEL if status == "done" else BLOCKED_LABEL
        # First, and alone in mattering: the JQL guard keys on this label, so
        # once it lands the ticket cannot be picked up again. The comment and
        # the transition below are for humans.
        try:
            self.client.add_label(key, label)
        except JiraError as error:
            log.warning(
                "could not label %s %s (%s) -- the ticket will look pending in"
                " Jira; state.db is what stops it re-running", key, label, error,
            )
        try:
            self.client.add_comment(key, (
                question_comment(summary, cost) if status == "blocked"
                else closing_comment(status, summary, cost)
            ))
        except JiraError as error:
            log.warning("could not comment on %s (%s)", key, error)
        if self.transition_done:
            self._transition(key, self.transition_done)

    def reopen(self, task: Task) -> None:
        """Drop the blocked label so an answered task is offered again.

        A failure here is a warning, never a raise: state.db is what actually
        drives the resume, and the label is for humans.
        """
        try:
            self.client.remove_label(task.source_ref, BLOCKED_LABEL)
        except JiraError as error:
            log.warning(
                "could not remove the %s label from %s (%s) -- the ticket will"
                " look blocked in Jira; the task resumes anyway",
                BLOCKED_LABEL, task.source_ref, error,
            )

    def answer(self, task: Task) -> str | None:
        """The human's reply to this task's question, if one has been posted.

        The boundary is found in the comment list itself rather than stored:
        ClaudeLoop's newest question comment, then the first comment after it
        carrying QUESTION_MARKER. A task that blocked twice therefore reads
        the second answer, not the first, across restarts and with nothing
        persisted.
        """
        try:
            comments = self.client.comments(task.source_ref).get("comments")
        except JiraError as error:
            # Indistinguishable from "no answer yet", exactly as pending()
            # treats an unreachable Jira as an empty backlog.
            log.warning(
                "could not read comments on %s (%s); trying again later",
                task.source_ref, error,
            )
            return None
        if not isinstance(comments, list):
            return None
        bodies = [
            str(comment.get("body") or "")
            for comment in comments
            if isinstance(comment, dict)
        ]
        asked = -1
        for index, body in enumerate(bodies):
            if body.lstrip().startswith(QUESTION_HEADING):
                asked = index
        if asked == -1:
            return None
        for body in bodies[asked + 1:]:
            stripped = body.strip()
            if stripped.casefold().startswith(QUESTION_MARKER):
                answer = stripped[len(QUESTION_MARKER):].strip()
                if answer:
                    return answer
        return None

    def _transition(self, key: str, name: str) -> None:
        """Move an issue by transition name, if Jira offers that name for this
        issue right now.

        Jira, not ClaudeLoop, decides whether a transition is permitted: the
        workflow may not allow it from the issue's current status, its screen
        may demand a field, the account may lack the permission. None of that
        is a reason to fail work that is finished, so every failure here is a
        warning.
        """
        try:
            offered = self.client.transitions(key)
            match = next(
                (t for t in offered
                 if isinstance(t, dict) and str(t.get("name", "")).casefold() == name.casefold()),
                None,
            )
            if match is None:
                log.warning(
                    "%s: Jira does not offer a %r transition from its current"
                    " status (offered: %s) -- leaving the issue where it is",
                    key, name,
                    ", ".join(str(t.get("name")) for t in offered if isinstance(t, dict))
                    or "none",
                )
                return
            transition_id = match.get("id")
            if transition_id is None:
                log.warning(
                    "%s: Jira's %r transition has no id in its payload --"
                    " leaving the issue where it is", key, name,
                )
                return
            self.client.transition(key, transition_id)
        except JiraError as error:
            log.warning("could not transition %s to %r (%s)", key, name, error)


_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*-\d+")


def _normalize_key(key: str) -> str | None:
    """Strip a single trailing colon (and surrounding whitespace) from an
    issue key, then check its shape.

    The prompt layer tells the session the key is the part before the colon
    in the task text ("OPS-42: Fix the widget" -> OPS-42), but a
    literal-minded session may still pass "OPS-42:" with the colon attached.
    Both subcommands go through this so neither hits /issue/OPS-42: instead
    of /issue/OPS-42.

    Also the only gate before a key reaches JiraClient: unlike every other
    caller, which passes keys straight from Jira's own search results, this
    one originates from raw argv in a session running with bypassed
    permissions. Returns None -- rather than raising -- when the result is
    not a Jira issue key (PROJECT-123), so the caller can fail with a
    readable message instead of forwarding something like
    "OPS-1/../../issue/OPS-2" into a URL path.
    """
    normalized = key.strip().removesuffix(":").strip()
    if not _KEY_RE.fullmatch(normalized):
        return None
    return normalized


def _client(config_path) -> JiraClient:
    cfg = load_config(config_path)
    if cfg.jira is None:
        raise SystemExit(
            f'{config_path}: no [jira] table -- this command only works when'
            ' source = "jira"'
        )
    return JiraClient(cfg.jira.site, cfg.jira.email, cfg.jira.token)


def _show(client: JiraClient, key: str) -> None:
    issue = client.issue(key)
    fields = issue.get("fields") or {}
    status = ((fields.get("status") or {}).get("name")) or "unknown"
    labels = ", ".join(fields.get("labels") or []) or "none"
    print(f"{key}  [{status}]  labels: {labels}")
    print(fields.get("summary") or "")
    description = (fields.get("description") or "").strip()
    if description:
        print()
        print(description)
    comments = (client.comments(key).get("comments")) or []
    if comments:
        print("\n--- comments ---")
    for comment in comments:
        author = ((comment.get("author") or {}).get("displayName")) or "someone"
        print(f"\n[{comment.get('created', '')} {author}]")
        print((comment.get("body") or "").strip())


def main(argv: list[str] | None = None) -> int:
    """The session's own Jira access: read a ticket, say something on it.

    Deliberately two subcommands. Transitions and labels belong to the
    orchestrator, so a confused session cannot park a ticket somewhere the
    operator did not expect.
    """
    parser = argparse.ArgumentParser(prog="python -m claudeloop.jira")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    sub = parser.add_subparsers(dest="command", required=True)

    show = sub.add_parser("show", help="print an issue and its comments")
    show.add_argument("key")

    comment = sub.add_parser("comment", help="post a comment")
    comment.add_argument("key")
    comment.add_argument("body", nargs="?", default="-",
                         help="the comment body, or - to read it from stdin")

    args = parser.parse_args(argv)
    key = _normalize_key(args.key)
    if key is None:
        print(f"not a Jira issue key: {args.key!r}", file=sys.stderr)
        return 2
    client = _client(Path(args.config))
    try:
        if args.command == "show":
            _show(client, key)
            return 0
        body = (sys.stdin.read() if args.body == "-" else args.body).strip()
        if not body:
            print("refusing to post an empty comment", file=sys.stderr)
            return 2
        client.add_comment(key, body)
        print(f"commented on {key}")
        return 0
    except JiraError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
