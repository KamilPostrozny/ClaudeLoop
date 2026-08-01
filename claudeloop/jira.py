"""The Jira Cloud task source: an HTTP client, a TaskSource over it, and the
small CLI the session itself calls.

Jira Cloud REST v2, deliberately: v2 returns `description` as a plain string
and takes a plain-string comment body, where v3 returns and demands ADF JSON.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
import urllib.error
import urllib.request

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


def compose_jql(operator_jql: str) -> str:
    """Splice the guard into the operator's query, keeping their ordering.

    Composed rather than documented: an operator who forgets the exclusion
    gets an infinite loop over one finished ticket, so this must not be
    something they can leave out.
    """
    # ponytail: a literal "ORDER BY" inside a quoted JQL string value would
    # split wrongly. Full tokenisation if that ever shows up in practice.
    parts = _ORDER_BY.split(operator_jql, maxsplit=1)
    where = parts[0].strip()
    order = parts[1].strip() if len(parts) > 1 else ""
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
        # 204 with no body is how a successful PUT /issue/{key} answers.
        return json.loads(raw) if raw else {}

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
        return self._request("GET", f"/issue/{key}?fields=summary,description,status,labels")

    def comments(self, key: str) -> dict:
        return self._request("GET", f"/issue/{key}/comment")

    def add_comment(self, key: str, body: str) -> dict:
        return self._request("POST", f"/issue/{key}/comment", {"body": body})

    def add_label(self, key: str, label: str) -> dict:
        # `update` adds one label atomically. Writing fields.labels instead
        # replaces the whole list, deleting the operator's own labels.
        return self._request("PUT", f"/issue/{key}", {
            "update": {"labels": [{"add": label}]}
        })

    def transitions(self, key: str) -> list[dict]:
        return self._request("GET", f"/issue/{key}/transitions").get("transitions", [])

    def transition(self, key: str, transition_id: str) -> dict:
        return self._request("POST", f"/issue/{key}/transitions", {
            "transition": {"id": transition_id}
        })
