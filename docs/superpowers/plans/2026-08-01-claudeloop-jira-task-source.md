# ClaudeLoop Jira Task Source (S3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a ClaudeLoop instance take its backlog from a Jira Cloud project instead of a markdown checklist, and let the session it runs talk on the ticket while it works.

**Architecture:** One new file, `claudeloop/jira.py`, holding an HTTP client over `urllib`, a `TaskSource` implementation, and a two-subcommand CLI the session calls. Everything else is small wiring: a `source` discriminator in the config, a source-selection branch in `main_loop`, a `PYTHONPATH` entry so the session can import the package, and one prompt section telling it the CLI exists.

**Tech Stack:** Python 3.11+ standard library only — `urllib.request`, `base64`, `argparse`, `sqlite3`, `unittest`, `http.server` for the test fake.

## Global Constraints

- **Python 3.11 or newer.**
- **No third-party packages, ever** — not for the orchestrator, not for the tests. No `requests`, no `jira`, no `atlassian-python-api`.
- **Jira Cloud REST v2**, never v3: v2 returns `description` as a plain string and accepts a plain-string comment body.
- **Nothing blocks the event loop.** Every Jira call from `loop.py` goes through `asyncio.to_thread`; the loop shares its thread with the heartbeat and the dashboard.
- **A config with `source = "file"`, or no `source` key at all, must behave exactly as it does today.** Existing configs stay valid and untouched.
- **`CLAUDELOOP_RESULT` stays the last key merged into the child environment.** The new `PYTHONPATH` entry goes in before it.
- **No trace of ClaudeLoop in the target repository.** Jira credentials are read from `~/.claudeloop/config.toml` by both the orchestrator and the session's CLI; nothing is copied into the repo or into `[session_env]`.
- **The labels are `claudeloop-done` and `claudeloop-blocked`.** Exact strings, used in the JQL guard and the marks.
- Tests run as `python -m unittest discover -s tests -t .` from the repository root. The suite stands at **227 tests** before this plan.
- Reference spec: `docs/superpowers/specs/2026-08-01-claudeloop-jira-task-source-design.md`.

## Deviations from the spec

1. **`mark()` does not add a retry layer of its own.** `JiraClient` already retries 5xx and network faults three times; a second layer on top would multiply into minutes of stalling per failed write. The spec's "retried" is satisfied by the client.
2. **`PYTHONPATH` is set unconditionally, not only when `source = "jira"`.** One line with no branch, and exposing an already-on-disk package to the session costs nothing.

## File Structure

| File | Responsibility |
|---|---|
| `claudeloop/jira.py` | `JiraError`, `JiraClient`, the pure `compose_jql`/`task_text`/`closing_comment` helpers, `JiraSource`, and `main()` for the CLI |
| `claudeloop/config.py` | *Modify:* `source` key, `[jira]` table, `JiraConfig`, conditional `tasks_file` requirement |
| `claudeloop/loop.py` | *Modify:* `build_source`, `source.start(task)`, `to_thread` around `pending`/`start`/`mark` |
| `claudeloop/source.py` | *Modify:* protocol gains `start`; `mark` gains `cost`; `FileSource` no-ops both |
| `claudeloop/session.py` | *Modify:* `child_env` prepends the package parent to `PYTHONPATH` |
| `claudeloop/prompt.py` | *Modify:* a `## Task source` section under `source = "jira"` |
| `claudeloop/state.py` | *Modify:* `terminal_ids()` |
| `tests/fixtures/jira/*.json` | Payloads recorded from the live instance in Task 1 |
| `tests/jira_fake.py` | A `ThreadingHTTPServer` replaying those fixtures and recording what it was sent |
| `tests/test_jira.py` | Client, pure helpers, source, and CLI |
| `tests/test_config.py`, `tests/test_loop.py`, `tests/test_session.py`, `tests/test_prompt.py`, `tests/test_state.py` | *Modify:* append a class each |

---

### Task 1: Probe the live instance and record fixtures

**Nothing else in this plan may start before this task finishes.** Every later task's fixtures come from here. Fixtures written from a design document inherit that document's wrong assumptions — this project has lost five defects to exactly that.

**Files:**
- Create: `tests/fixtures/jira/search.json`, `tests/fixtures/jira/issue.json`, `tests/fixtures/jira/comments.json`, `tests/fixtures/jira/transitions.json`
- Create (throwaway, in the scratchpad, **not** in the repository): `probe.py`

**Interfaces:**
- Produces: the four fixture files, and a decided value for `SEARCH_PATH` used by Task 2.

**This task needs the operator's real credentials.** It is read-only: it issues GETs and one POST to the search endpoint, and writes nothing to Jira. Do not commit credentials; the probe reads them from `~/.claudeloop/config.toml`.

> **DONE — ran 2026-08-01 against a live instance. Findings, which the tasks below already reflect:**
>
> 1. **`SEARCH_PATH = "/search/jql"`.** The old `/rest/api/2/search` answers **410 Gone**: "Żądany interfejs API został usunięty." REST v2 is otherwise alive.
> 2. **`description` and comment `body` are both plain `str`.** No ADF flattener needed.
> 3. **Unbounded JQL is refused with 400** — a query must carry a restriction. The composed label guard is itself a restriction, so every query ClaudeLoop sends passes; an operator's empty `jql` now fails rather than matching everything.
> 4. **The search response carries `issues`, `nextPageToken`, `isLast`** — the new pagination shape. Reading one page of `issues` works either way.
> 5. **`transitions` is a list of `{id, name, to: {...}}`**, as assumed.
> 6. **A comment had to be written to record its shape** — no issue in the instance had one. Posted to the Atlassian-generated sample project with the operator's agreement, read back, then deleted (204, `total` returned to 0).
>
> Fixtures are in `tests/fixtures/jira/`: two issues in `search.json` with the second's `description` `null`, one issue, one comment, six transitions. Structure verbatim, content fake.

- [ ] **Step 1: Write the probe script in the scratchpad**

```python
"""Read-only probe of a live Jira Cloud instance. Throwaway; not committed."""

import base64
import json
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

cfg = tomllib.loads((Path.home() / ".claudeloop" / "config.toml").read_text())
jira = cfg["jira"]
base = jira["site"].rstrip("/") + "/rest/api/2"
auth = base64.b64encode(f"{jira['email']}:{jira['token']}".encode()).decode()


def call(method, path, payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(base + path, data=body, method=method)
    request.add_header("Authorization", f"Basic {auth}")
    request.add_header("Accept", "application/json")
    if body:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()[:400]


jql = jira["jql"]
for path in ("/search/jql", "/search"):
    status, data = call("POST", path, {"jql": jql, "maxResults": 5,
                                       "fields": ["summary", "description"]})
    print(f"POST {path} -> {status}")
    if status == 200:
        print(json.dumps(data, indent=2)[:3000])
        Path("search.json").write_text(json.dumps(data, indent=2))
        key = data["issues"][0]["key"]
        break
else:
    sys.exit("neither search path worked -- read the errors above")

for name, path in (
    ("issue", f"/issue/{key}?fields=summary,description,status,labels"),
    ("comments", f"/issue/{key}/comment"),
    ("transitions", f"/issue/{key}/transitions"),
):
    status, data = call("GET", path)
    print(f"GET {path} -> {status}")
    Path(f"{name}.json").write_text(json.dumps(data, indent=2))
    print(json.dumps(data, indent=2)[:2000])
```

- [ ] **Step 2: Run it and read the output**

```bash
cd "$SCRATCHPAD" && python probe.py
```

Answer these four questions from the output, and write the answers into the plan file as a note under this task:

1. **Which search path returned 200** — `/search/jql` or `/search`. That value becomes `SEARCH_PATH` in Task 2.
2. **The response shape** — does it carry `issues`, and does each issue have `key` and `fields.summary`?
3. **Is `description` a plain string?** If it came back as an ADF object, stop and report: the spec's v2 assumption is wrong and the design needs a flattener.
4. **The transitions payload** — a `transitions` list of objects with `id` and `name`?

- [ ] **Step 3: Sanitise and install the fixtures**

Copy the four files into `tests/fixtures/jira/`. Replace real ticket text, display names, email addresses, account ids and the site URL with obviously-fake values (`OPS-1`, `Fix the widget`, `alice`, `https://example.atlassian.net`). Keep every structural key exactly as Jira returned it — the point of the fixtures is their shape, not their content.

Trim `search.json` to two issues, with the second having `"description": null`. That null is a real case the source must survive, and having it in the fixture means every later test exercises it for free.

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/jira/
git commit -m "test: record Jira Cloud payloads from a live instance

Fixtures for S3, recorded read-only rather than written from the design
document. Content sanitised; structure verbatim."
```

---

### Task 2: `JiraClient`

**Files:**
- Create: `claudeloop/jira.py`
- Create: `tests/jira_fake.py`
- Create: `tests/test_jira.py`

**Interfaces:**
- Consumes: the fixtures from Task 1.
- Produces:
  - `class JiraError(Exception)` — carries `.status: int` and `.body: str`
  - `JiraClient(site: str, email: str, token: str, timeout: float = 30.0, retries: int = 3, sleep=time.sleep)`
  - `client.search(jql: str, max_results: int = 50) -> dict`
  - `client.issue(key: str) -> dict`
  - `client.comments(key: str) -> dict`
  - `client.add_comment(key: str, body: str) -> dict`
  - `client.add_label(key: str, label: str) -> dict`
  - `client.transitions(key: str) -> list[dict]`
  - `client.transition(key: str, transition_id: str) -> dict`
  - `SEARCH_PATH: str` — the value decided in Task 1

**Design notes for the implementer:**

1. **Retry 5xx and network faults; never retry 4xx.** A 401, a 403 or a malformed JQL will answer identically on the third attempt, and an unattended loop polling every 30 seconds must not spend a minute rediscovering that each time.
2. **`sleep` is injectable** purely so the retry tests take milliseconds. Do not add a "test mode" flag.
3. **Every request carries `timeout`.** A hung socket must not park the orchestrator.
4. **`add_label` uses the `update` verb, not `fields`.** `{"update": {"labels": [{"add": "claudeloop-done"}]}}` adds one label atomically; writing `fields.labels` replaces the whole list and would delete the operator's own labels.
5. **A 204 with an empty body is success**, and `PUT /issue/{key}` returns exactly that. Return `{}` rather than exploding on `json.loads("")`.

- [ ] **Step 1: Write the fake Jira server**

Create `tests/jira_fake.py`:

```python
"""A stand-in Jira Cloud, over http.server. Real sockets, real urllib on the
other end -- the same choice tests/ already makes with the fake claude CLI."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "jira"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


class FakeJira:
    """routes maps "METHOD /path/suffix" to (status, payload) or to a list of
    them, consumed one per call so a test can make the first attempt fail and
    the second succeed."""

    def __init__(self, routes: dict):
        self.routes = {key: list(value) if isinstance(value, list) else [value]
                       for key, value in routes.items()}
        self.requests: list[tuple[str, str, dict | None]] = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server = server
        self.url = f"http://127.0.0.1:{server.server_port}"
        self.thread = threading.Thread(target=server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep the test output clean
                pass

            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                payload = json.loads(raw) if raw else None
                path = self.path.split("?")[0].replace("/rest/api/2", "")
                fake.requests.append((self.command, path, payload))
                queue = fake.routes.get(f"{self.command} {path}")
                if not queue:
                    status, body = 404, {"errorMessages": ["no such route"]}
                else:
                    status, body = queue[0] if len(queue) == 1 else queue.pop(0)
                encoded = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            do_GET = do_POST = do_PUT = _serve

        return Handler
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_jira.py`:

```python
import unittest

from claudeloop.jira import SEARCH_PATH, JiraClient, JiraError

from .jira_fake import FakeJira, fixture


class JiraClientTest(unittest.TestCase):
    def client(self, routes, **kwargs):
        self.fake = FakeJira(routes)
        self.addCleanup(self.fake.close)
        return JiraClient(self.fake.url, "me@example.com", "token",
                          sleep=lambda _: None, **kwargs)

    def test_search_posts_the_jql_and_returns_the_payload(self):
        client = self.client({f"POST {SEARCH_PATH}": (200, fixture("search"))})
        data = client.search("project = OPS", max_results=50)
        self.assertEqual(len(data["issues"]), 2)
        method, path, payload = self.fake.requests[0]
        self.assertEqual((method, path), ("POST", SEARCH_PATH))
        self.assertEqual(payload["jql"], "project = OPS")
        self.assertEqual(payload["maxResults"], 50)
        self.assertEqual(payload["fields"], ["summary", "description"])

    def test_sends_basic_auth(self):
        client = self.client({f"POST {SEARCH_PATH}": (200, {"issues": []})})
        client.search("project = OPS")
        # base64("me@example.com:token")
        self.assertEqual(client.header, "Basic bWVAZXhhbXBsZS5jb206dG9rZW4=")

    def test_transitions_returns_the_list(self):
        client = self.client({"GET /issue/OPS-1/transitions": (200, fixture("transitions"))})
        names = [t["name"] for t in client.transitions("OPS-1")]
        self.assertTrue(names)
        self.assertTrue(all("id" in t for t in client.transitions("OPS-1")))

    def test_add_label_uses_update_not_fields(self):
        client = self.client({"PUT /issue/OPS-1": (204, {})})
        client.add_label("OPS-1", "claudeloop-done")
        _, _, payload = self.fake.requests[0]
        self.assertEqual(payload, {"update": {"labels": [{"add": "claudeloop-done"}]}})

    def test_empty_body_is_success_not_a_crash(self):
        client = self.client({"PUT /issue/OPS-1": (204, {})})
        self.assertEqual(client.add_label("OPS-1", "x"), {})

    def test_retries_a_500_then_succeeds(self):
        client = self.client({f"POST {SEARCH_PATH}": [
            (500, {"errorMessages": ["boom"]}),
            (200, {"issues": []}),
        ]})
        self.assertEqual(client.search("project = OPS"), {"issues": []})
        self.assertEqual(len(self.fake.requests), 2)

    def test_gives_up_after_the_retry_budget(self):
        client = self.client({f"POST {SEARCH_PATH}": (500, {"errorMessages": ["boom"]})},
                             retries=3)
        with self.assertRaises(JiraError) as caught:
            client.search("project = OPS")
        self.assertEqual(caught.exception.status, 500)
        self.assertEqual(len(self.fake.requests), 3)

    def test_does_not_retry_a_4xx(self):
        client = self.client({f"POST {SEARCH_PATH}": (400, {"errorMessages": ["bad jql"]})})
        with self.assertRaises(JiraError) as caught:
            client.search("nonsense")
        self.assertEqual(caught.exception.status, 400)
        self.assertIn("bad jql", caught.exception.body)
        self.assertEqual(len(self.fake.requests), 1)

    def test_a_dead_host_raises_jira_error_not_urlerror(self):
        fake = FakeJira({})
        url = fake.url
        fake.close()  # nothing is listening on that port any more
        client = JiraClient(url, "me@example.com", "token", sleep=lambda _: None,
                            timeout=1.0)
        with self.assertRaises(JiraError):
            client.search("project = OPS")
```

- [ ] **Step 3: Run them and watch them fail**

```bash
python -m unittest tests.test_jira -v
```

Expected: `ModuleNotFoundError: No module named 'claudeloop.jira'`.

- [ ] **Step 4: Write `claudeloop/jira.py`**

```python
"""The Jira Cloud task source: an HTTP client, a TaskSource over it, and the
small CLI the session itself calls.

Jira Cloud REST v2, deliberately: v2 returns `description` as a plain string
and takes a plain-string comment body, where v3 returns and demands ADF JSON.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.request

log = logging.getLogger("claudeloop")

SEARCH_PATH = "/search/jql"  # pinned by the live probe in Task 1
"""Atlassian moved the search endpoint; this is the path that answered 200
against a real instance, not the one the documentation happened to show."""

BACKOFF_S = (1.0, 2.0, 4.0)
"""Waits between retries. Only 5xx and network faults get here."""


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
```

- [ ] **Step 5: Run the tests**

```bash
python -m unittest tests.test_jira -v
```

Expected: PASS. If `test_search_posts_the_jql_and_returns_the_payload` fails on the fixture's issue count, the fixture was not trimmed to two issues in Task 1 — fix the fixture, not the test.

- [ ] **Step 6: Commit**

```bash
git add claudeloop/jira.py tests/jira_fake.py tests/test_jira.py
git commit -m "feat: a Jira Cloud REST v2 client

urllib, Basic auth, three retries on 5xx and network faults and none on
4xx. Labels go through the `update` verb so adding one cannot delete the
operator's own."
```

---

### Task 3: `compose_jql`, `task_text`, `closing_comment`

Three pure functions, in their own task because they hold the slice's one genuinely non-obvious trap and deserve a reviewer's full attention.

**Files:**
- Modify: `claudeloop/jira.py`
- Modify: `tests/test_jira.py`

**Interfaces:**
- Produces:
  - `DONE_LABEL = "claudeloop-done"`, `BLOCKED_LABEL = "claudeloop-blocked"`, `GUARD: str`
  - `compose_jql(operator_jql: str) -> str`
  - `task_text(key: str, summary: str | None, description: str | None) -> str`
  - `closing_comment(status: str, summary: str, cost: float) -> str`

**Design notes for the implementer:**

1. **`labels != "x"` excludes issues that have no labels at all.** This is the trap. The correct idiom is `(labels IS EMPTY OR labels NOT IN ("x"))`, and a test pins it so nobody "simplifies" it later.
2. **The operator's `ORDER BY` must survive.** `WHERE ... ORDER BY x` is one string in their config; the guard has to be spliced into the condition, not appended to the whole thing, or the JQL is a syntax error.
3. **The guard is composed, never operator-supplied**, so it cannot be accidentally disabled.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_jira.py`:

```python
from claudeloop.jira import (
    BLOCKED_LABEL, DONE_LABEL, closing_comment, compose_jql, task_text,
)


class ComposeJqlTest(unittest.TestCase):
    def test_appends_the_guard_to_a_plain_query(self):
        self.assertEqual(
            compose_jql("project = OPS AND status = 'To Do'"),
            '(project = OPS AND status = \'To Do\') AND (labels IS EMPTY OR '
            'labels NOT IN ("claudeloop-done", "claudeloop-blocked"))',
        )

    def test_the_guard_keeps_issues_that_have_no_labels(self):
        # labels != "x" silently excludes every unlabelled issue -- which is
        # most of a fresh backlog. This is the whole reason for IS EMPTY.
        self.assertIn("labels IS EMPTY OR", compose_jql("project = OPS"))
        self.assertNotIn("labels !=", compose_jql("project = OPS"))

    def test_preserves_the_operators_ordering(self):
        composed = compose_jql("project = OPS ORDER BY priority DESC")
        self.assertTrue(composed.endswith(" ORDER BY priority DESC"), composed)
        self.assertIn("(project = OPS) AND (labels IS EMPTY", composed)

    def test_order_by_is_matched_case_insensitively(self):
        composed = compose_jql("project = OPS order by created ASC")
        self.assertTrue(composed.endswith(" ORDER BY created ASC"), composed)

    def test_a_query_that_is_only_an_ordering_still_gets_the_guard(self):
        composed = compose_jql("ORDER BY created")
        self.assertTrue(composed.startswith("(labels IS EMPTY"), composed)
        self.assertTrue(composed.endswith(" ORDER BY created"), composed)


class TaskTextTest(unittest.TestCase):
    def test_key_leads_so_the_session_can_find_it(self):
        text = task_text("OPS-1", "Fix the widget", "It is broken.")
        self.assertTrue(text.startswith("OPS-1: Fix the widget"), text)
        self.assertIn("It is broken.", text)

    def test_a_null_description_leaves_key_and_summary_alone(self):
        self.assertEqual(task_text("OPS-2", "Fix the widget", None),
                         "OPS-2: Fix the widget")

    def test_a_whitespace_only_description_counts_as_absent(self):
        self.assertEqual(task_text("OPS-2", "Fix it", "   \n  "), "OPS-2: Fix it")


class ClosingCommentTest(unittest.TestCase):
    def test_carries_status_cost_and_summary(self):
        body = closing_comment("done", "Opened PR #4.", 0.1234)
        self.assertIn("done", body)
        self.assertIn("$0.1234", body)
        self.assertIn("Opened PR #4.", body)
        self.assertIn("ClaudeLoop", body)


class LabelsTest(unittest.TestCase):
    def test_are_the_exact_strings_the_guard_excludes(self):
        self.assertEqual(DONE_LABEL, "claudeloop-done")
        self.assertEqual(BLOCKED_LABEL, "claudeloop-blocked")
        self.assertIn(DONE_LABEL, compose_jql("project = OPS"))
        self.assertIn(BLOCKED_LABEL, compose_jql("project = OPS"))
```

- [ ] **Step 2: Run them and watch them fail**

```bash
python -m unittest tests.test_jira -v
```

Expected: `ImportError: cannot import name 'compose_jql'`.

- [ ] **Step 3: Implement**

Add to `claudeloop/jira.py`, above `JiraClient`:

```python
import re

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
```

- [ ] **Step 4: Run the tests**

```bash
python -m unittest tests.test_jira -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add claudeloop/jira.py tests/test_jira.py
git commit -m "feat: JQL composition, task text and the closing comment

The guard uses (labels IS EMPTY OR labels NOT IN (...)): a bare
labels != excludes every issue with no labels, which is most of a fresh
backlog. Pinned by a test that names the failure."
```

---

### Task 4: `State.terminal_ids`

**Files:**
- Modify: `claudeloop/state.py`
- Modify: `tests/test_state.py`

**Interfaces:**
- Produces: `State.terminal_ids() -> set[str]`

**Design notes for the implementer:**

`interrupted` is deliberately **not** terminal. `State.__init__` rewrites `running` rows to `interrupted` when a previous process died mid-task; that task never finished and must stay eligible. `running` is likewise excluded — the current task is running right now.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_state.py`, before the `if __name__` block:

```python
class TerminalIdsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state = State(self.tmp / "state.db")

    def finished(self, task_id: str, status: str) -> None:
        self.state.start_task(task_id, "jira", "OPS-1", "text")
        self.state.finish_task(task_id, status, "summary", 0.0)

    def test_collects_every_terminal_status(self):
        self.finished("aaaa", "done")
        self.finished("bbbb", "failed")
        self.finished("cccc", "blocked")
        self.assertEqual(self.state.terminal_ids(), {"aaaa", "bbbb", "cccc"})

    def test_running_and_interrupted_are_not_terminal(self):
        self.state.start_task("dddd", "jira", "OPS-2", "text")
        self.state.db.execute("UPDATE tasks SET status='interrupted' WHERE id='dddd'")
        self.state.start_task("eeee", "jira", "OPS-3", "text")
        self.assertEqual(self.state.terminal_ids(), set())

    def test_is_empty_on_a_fresh_database(self):
        self.assertEqual(self.state.terminal_ids(), set())
```

- [ ] **Step 2: Run it and watch it fail**

```bash
python -m unittest tests.test_state -v
```

Expected: `AttributeError: 'State' object has no attribute 'terminal_ids'`.

- [ ] **Step 3: Implement**

Append to `class State` in `claudeloop/state.py`:

```python
    def terminal_ids(self) -> set[str]:
        """Task ids that reached a verdict, for a source that needs a backstop
        against re-running finished work.

        'interrupted' is excluded on purpose: State.__init__ writes it when a
        previous process died mid-task, and that task never finished.
        """
        rows = self.db.execute(
            "SELECT id FROM tasks WHERE status IN ('done', 'failed', 'blocked')"
        )
        return {row["id"] for row in rows}
```

- [ ] **Step 4: Run the tests**

```bash
python -m unittest tests.test_state -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add claudeloop/state.py tests/test_state.py
git commit -m "feat: State.terminal_ids for the Jira source's re-run backstop"
```

---

### Task 5: `JiraSource`

**Files:**
- Modify: `claudeloop/jira.py`
- Modify: `tests/test_jira.py`

**Interfaces:**
- Consumes: `JiraClient`, `compose_jql`, `task_text`, `closing_comment`, `State.terminal_ids`, and `Task`/`task_id` from `claudeloop.source`.
- Produces:
  ```python
  JiraSource(
      client: JiraClient,
      jql: str,
      state=None,                    # anything with .terminal_ids()
      transition_start: str = "",
      transition_done: str = "",
  )
  source.pending() -> list[Task]
  source.start(task: Task) -> None
  source.mark(task: Task, status: str, summary: str, cost: float = 0.0) -> None
  ```

**Design notes for the implementer:**

1. **`pending()` never raises.** A network fault, a 401 or a bad JQL logs and returns `[]`. An unreachable Jira and an empty backlog must look identical to the loop, which then idles and retries — the alternative is a crash loop that burns the task list.
2. **`mark()` never raises either**, and its order encodes what is load-bearing: label first, then the comment, then the transition. The label is what the JQL guard keys on; if it lands, the ticket cannot re-run. The other two are for humans.
3. **`start()` never raises.** Same contract `reset_to_default_branch` holds — an environment fault here must not look different from any other.
4. **Transitions are matched by name, case-insensitively, against what Jira offers *this issue right now*.** A name that is not offered logs and continues: the workflow may simply not allow it from where the issue sits, and that is not a reason to fail finished work.
5. **`task_id` hashes the issue key, not the text** — so editing a ticket does not mint a new task, and the id stays 16 hex characters, which `web.py`'s `TASK_ID_RE` requires as a path-traversal guard.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_jira.py`:

```python
from claudeloop.jira import SEARCH_PATH, JiraSource
from claudeloop.source import task_id


class FakeState:
    def __init__(self, ids=()):
        self.ids = set(ids)

    def terminal_ids(self):
        return self.ids


class JiraSourceTest(unittest.TestCase):
    def source(self, routes, **kwargs):
        self.fake = FakeJira(routes)
        self.addCleanup(self.fake.close)
        client = JiraClient(self.fake.url, "me@example.com", "token",
                            sleep=lambda _: None)
        return JiraSource(client, kwargs.pop("jql", "project = OPS"), **kwargs)

    def test_pending_builds_one_task_per_issue(self):
        source = self.source({f"POST {SEARCH_PATH}": (200, fixture("search"))})
        tasks = source.pending()
        self.assertEqual(len(tasks), 2)
        first = tasks[0]
        self.assertEqual(first.source, "jira")
        self.assertEqual(first.source_ref, "OPS-1")
        self.assertEqual(first.id, task_id("OPS-1"))
        self.assertEqual(len(first.id), 16)
        self.assertTrue(first.text.startswith("OPS-1: "), first.text)

    def test_pending_survives_the_null_description_in_the_fixture(self):
        source = self.source({f"POST {SEARCH_PATH}": (200, fixture("search"))})
        self.assertTrue(all(task.text for task in source.pending()))

    def test_pending_sends_the_composed_jql(self):
        source = self.source({f"POST {SEARCH_PATH}": (200, fixture("search"))})
        source.pending()
        _, _, payload = self.fake.requests[0]
        self.assertIn("labels IS EMPTY", payload["jql"])
        self.assertIn("project = OPS", payload["jql"])

    def test_pending_drops_tasks_already_terminal_in_the_database(self):
        source = self.source({f"POST {SEARCH_PATH}": (200, fixture("search"))},
                             state=FakeState({task_id("OPS-1")}))
        self.assertEqual([t.source_ref for t in source.pending()], ["OPS-2"])

    def test_pending_returns_empty_on_an_http_error_rather_than_raising(self):
        source = self.source({f"POST {SEARCH_PATH}": (401, {"errorMessages": ["nope"]})})
        with self.assertLogs("claudeloop", level="WARNING"):
            self.assertEqual(source.pending(), [])

    def test_pending_returns_empty_when_jira_is_unreachable(self):
        fake = FakeJira({})
        url = fake.url
        fake.close()
        client = JiraClient(url, "me@example.com", "token", sleep=lambda _: None,
                            timeout=1.0)
        source = JiraSource(client, "project = OPS")
        with self.assertLogs("claudeloop", level="WARNING"):
            self.assertEqual(source.pending(), [])

    def test_mark_labels_comments_and_transitions_in_that_order(self):
        source = self.source({
            "PUT /issue/OPS-1": (204, {}),
            "POST /issue/OPS-1/comment": (201, {}),
            "GET /issue/OPS-1/transitions": (200, {"transitions": [
                {"id": "31", "name": "Done"}]}),
            "POST /issue/OPS-1/transitions": (204, {}),
        }, transition_done="Done")
        task = Task(task_id("OPS-1"), "OPS-1: t", "jira", "OPS-1")
        source.mark(task, "done", "went fine", 0.5)
        paths = [(method, path) for method, path, _ in self.fake.requests]
        self.assertEqual(paths[0], ("PUT", "/issue/OPS-1"))
        self.assertEqual(paths[1], ("POST", "/issue/OPS-1/comment"))
        self.assertEqual(paths[-1], ("POST", "/issue/OPS-1/transitions"))
        _, _, label = self.fake.requests[0]
        self.assertEqual(label, {"update": {"labels": [{"add": "claudeloop-done"}]}})
        _, _, comment = self.fake.requests[1]
        self.assertIn("$0.5000", comment["body"])
        _, _, move = self.fake.requests[-1]
        self.assertEqual(move, {"transition": {"id": "31"}})

    def test_mark_uses_the_blocked_label_for_failed_and_blocked(self):
        for status in ("failed", "blocked"):
            with self.subTest(status=status):
                source = self.source({"PUT /issue/OPS-1": (204, {}),
                                      "POST /issue/OPS-1/comment": (201, {})})
                task = Task(task_id("OPS-1"), "OPS-1: t", "jira", "OPS-1")
                source.mark(task, status, "did not finish")
                _, _, label = self.fake.requests[0]
                self.assertEqual(label["update"]["labels"][0]["add"],
                                 "claudeloop-blocked")

    def test_mark_still_labels_when_the_comment_fails(self):
        source = self.source({"PUT /issue/OPS-1": (204, {}),
                              "POST /issue/OPS-1/comment": (500, {"e": 1})})
        task = Task(task_id("OPS-1"), "OPS-1: t", "jira", "OPS-1")
        with self.assertLogs("claudeloop", level="WARNING"):
            source.mark(task, "done", "went fine")
        self.assertEqual(self.fake.requests[0][:2], ("PUT", "/issue/OPS-1"))

    def test_mark_survives_a_label_write_that_never_lands(self):
        source = self.source({"PUT /issue/OPS-1": (403, {"errorMessages": ["no"]}),
                              "POST /issue/OPS-1/comment": (201, {})})
        task = Task(task_id("OPS-1"), "OPS-1: t", "jira", "OPS-1")
        with self.assertLogs("claudeloop", level="WARNING"):
            source.mark(task, "done", "went fine")  # must not raise

    def test_a_transition_jira_does_not_offer_is_a_warning_not_a_failure(self):
        source = self.source({
            "PUT /issue/OPS-1": (204, {}),
            "POST /issue/OPS-1/comment": (201, {}),
            "GET /issue/OPS-1/transitions": (200, {"transitions": [
                {"id": "11", "name": "In Review"}]}),
        }, transition_done="Done")
        task = Task(task_id("OPS-1"), "OPS-1: t", "jira", "OPS-1")
        with self.assertLogs("claudeloop", level="WARNING") as logs:
            source.mark(task, "done", "went fine")
        self.assertIn("Done", "".join(logs.output))
        self.assertNotIn(("POST", "/issue/OPS-1/transitions"),
                         [(m, p) for m, p, _ in self.fake.requests])

    def test_transition_names_match_case_insensitively(self):
        source = self.source({
            "GET /issue/OPS-1/transitions": (200, {"transitions": [
                {"id": "21", "name": "In Progress"}]}),
            "POST /issue/OPS-1/transitions": (204, {}),
        }, transition_start="in progress")
        source.start(Task(task_id("OPS-1"), "OPS-1: t", "jira", "OPS-1"))
        _, _, move = self.fake.requests[-1]
        self.assertEqual(move, {"transition": {"id": "21"}})

    def test_start_does_nothing_when_no_start_transition_is_configured(self):
        source = self.source({})
        source.start(Task(task_id("OPS-1"), "OPS-1: t", "jira", "OPS-1"))
        self.assertEqual(self.fake.requests, [])

    def test_start_survives_jira_being_down(self):
        fake = FakeJira({})
        url = fake.url
        fake.close()
        client = JiraClient(url, "me@example.com", "token", sleep=lambda _: None,
                            timeout=1.0)
        source = JiraSource(client, "project = OPS", transition_start="In Progress")
        with self.assertLogs("claudeloop", level="WARNING"):
            source.start(Task(task_id("OPS-1"), "OPS-1: t", "jira", "OPS-1"))
```

Add `from claudeloop.source import Task, task_id` to the imports at the top of the file.

- [ ] **Step 2: Run them and watch them fail**

```bash
python -m unittest tests.test_jira -v
```

Expected: `ImportError: cannot import name 'JiraSource'`.

- [ ] **Step 3: Implement**

Append to `claudeloop/jira.py`:

```python
from .source import Task, task_id


class JiraSource:
    """A TaskSource over a Jira Cloud project.

    Nothing here may raise into the loop: an unreachable Jira must look like
    an empty backlog (so the loop idles and retries), and a Jira that refuses
    a write must not turn finished work into a failure.
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
        done = self.state.terminal_ids() if self.state is not None else set()
        tasks = []
        for issue in data.get("issues", []):
            key = issue.get("key")
            if not key:
                continue
            identifier = task_id(key)
            if identifier in done:
                # The label write never landed, or someone cleared it. The
                # database says this task already reached a verdict.
                log.warning(
                    "%s is still in the backlog but already finished in"
                    " state.db -- skipping it; ClaudeLoop's label may have"
                    " failed to write", key,
                )
                continue
            fields = issue.get("fields") or {}
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
            self.client.add_comment(key, closing_comment(status, summary, cost))
        except JiraError as error:
            log.warning("could not comment on %s (%s)", key, error)
        if self.transition_done:
            self._transition(key, self.transition_done)

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
                (t for t in offered if str(t.get("name", "")).casefold() == name.casefold()),
                None,
            )
            if match is None:
                log.warning(
                    "%s: Jira does not offer a %r transition from its current"
                    " status (offered: %s) -- leaving the issue where it is",
                    key, name, ", ".join(str(t.get("name")) for t in offered) or "none",
                )
                return
            self.client.transition(key, match["id"])
        except JiraError as error:
            log.warning("could not transition %s to %r (%s)", key, name, error)
```

- [ ] **Step 4: Run the tests**

```bash
python -m unittest tests.test_jira -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add claudeloop/jira.py tests/test_jira.py
git commit -m "feat: JiraSource, a TaskSource over a Jira project

Nothing raises into the loop: an unreachable Jira looks like an empty
backlog, and a refused write never turns finished work into a failure.
mark() writes the label first because that is what the JQL guard reads;
the comment and the transition are for humans."
```

---

### Task 6: The `source` protocol gains `start` and `cost`

**Files:**
- Modify: `claudeloop/source.py`
- Modify: `tests/test_source.py`

**Interfaces:**
- Produces:
  ```python
  class TaskSource(Protocol):
      def pending(self) -> list[Task]: ...
      def start(self, task: Task) -> None: ...
      def mark(self, task: Task, status: str, summary: str, cost: float = 0.0) -> None: ...
  ```
  `FileSource.start` is a no-op; `FileSource.mark` accepts and ignores `cost`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_source.py`, before the `if __name__` block:

```python
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
```

- [ ] **Step 2: Run them and watch them fail**

```bash
python -m unittest tests.test_source -v
```

Expected: `AttributeError: 'FileSource' object has no attribute 'start'`.

- [ ] **Step 3: Implement**

In `claudeloop/source.py`, replace the protocol and add the method:

```python
class TaskSource(Protocol):
    def pending(self) -> list[Task]: ...
    def start(self, task: Task) -> None: ...
    def mark(self, task: Task, status: str, summary: str, cost: float = 0.0) -> None: ...
```

In `FileSource`, add above `mark`:

```python
    def start(self, task: Task) -> None:
        """A checklist has nothing to say when work begins. The Jira source
        uses this to move the issue to its in-progress status."""
```

and change `mark`'s signature to:

```python
    def mark(self, task: Task, status: str, summary: str, cost: float = 0.0) -> None:
```

leaving its body alone — `cost` belongs on the protocol because the Jira source's closing comment carries it, and a checklist line has nowhere to put it.

- [ ] **Step 4: Run the tests**

```bash
python -m unittest tests.test_source tests.test_loop -v
```

Expected: PASS, including the existing `test_loop` cases that call `mark`.

- [ ] **Step 5: Commit**

```bash
git add claudeloop/source.py tests/test_source.py
git commit -m "feat: TaskSource gains start(task) and mark(..., cost)"
```

---

### Task 7: Configuration

**Files:**
- Modify: `claudeloop/config.py`
- Modify: `tests/test_config.py` (append a class; leave the existing ones alone)

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class JiraConfig:
      site: str
      email: str
      token: str
      jql: str                    # composed from project/status when not given
      transition_start: str = ""
      transition_done: str = ""
  ```
  `Config` gains `source: str = "file"` and `jira: JiraConfig | None = None`, and `Config.tasks_file` becomes `Path | None`.

**Design notes for the implementer:**

1. **`tasks_file` is required only when `source = "file"`.** It stays the second field on `Config` so existing keyword construction in tests keeps working, but its type becomes `Path | None` with a `None` default.
2. **The `tasks_file`-inside-`repo` refusal still applies whenever `tasks_file` is set**, `source` notwithstanding. It exists because a session doing branch hygiene can revert ClaudeLoop's own mark.
3. **Validate `[jira]` eagerly.** A missing `token` must fail at startup with a sentence a human can act on — not four hours later inside a subprocess, and not as a 401 on every poll.
4. **`source` must be one of `("file", "jira")`.** A typo silently running the wrong backlog is worse than a startup error.
5. **`project` and `status` are a shorthand for `jql`, composed here.** Writing JQL by hand to start is a barrier, and getting it subtly wrong yields a silently empty backlog with nothing saying why. Exactly one of `jql` or `project` is required; when both are given, `jql` wins outright, because the shorthand cannot express an assignee, a label filter or a priority ordering. `JiraSource` still receives one query string, and `compose_jql` in `claudeloop/jira.py` is untouched.
6. **The composed query must carry a restriction.** The live probe found Jira refuses an unbounded JQL with a 400 — `project = "X"` is a restriction, so the shorthand is always safe, but do not be tempted to make `project` optional too.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`, before the `if __name__` block:

```python
class JiraConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.home = self.tmp / "home"

    def write(self, body: str) -> Path:
        path = self.tmp / "config.toml"
        path.write_text(f'repo = "{self.repo}"\n{body}')
        path.chmod(0o600)
        return path

    JIRA = (
        'source = "jira"\n'
        "[jira]\n"
        'site = "https://example.atlassian.net"\n'
        'email = "me@example.com"\n'
        'token = "secret"\n'
        'jql = "project = OPS ORDER BY created"\n'
    )

    def test_loads_a_jira_source(self):
        cfg = load_config(self.write(self.JIRA), home=self.home)
        self.assertEqual(cfg.source, "jira")
        self.assertEqual(cfg.jira.site, "https://example.atlassian.net")
        self.assertEqual(cfg.jira.email, "me@example.com")
        self.assertEqual(cfg.jira.token, "secret")
        self.assertEqual(cfg.jira.jql, "project = OPS ORDER BY created")
        self.assertEqual(cfg.jira.transition_start, "")
        self.assertEqual(cfg.jira.transition_done, "")

    def test_jira_needs_no_tasks_file(self):
        cfg = load_config(self.write(self.JIRA), home=self.home)
        self.assertIsNone(cfg.tasks_file)

    def test_transitions_are_optional_and_carried_when_present(self):
        cfg = load_config(
            self.write(self.JIRA + 'transition_start = "In Progress"\n'
                                   'transition_done = "Done"\n'),
            home=self.home,
        )
        self.assertEqual(cfg.jira.transition_start, "In Progress")
        self.assertEqual(cfg.jira.transition_done, "Done")

    def test_defaults_to_the_file_source(self):
        tasks = self.tmp / "tasks.md"
        tasks.write_text("")
        cfg = load_config(self.write(f'tasks_file = "{tasks}"\n'), home=self.home)
        self.assertEqual(cfg.source, "file")
        self.assertEqual(cfg.tasks_file, tasks)
        self.assertIsNone(cfg.jira)

    def test_an_unknown_source_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            load_config(self.write('source = "github"\n'), home=self.home)
        self.assertIn("source", str(caught.exception))

    def test_the_file_source_still_requires_tasks_file(self):
        with self.assertRaises(ValueError) as caught:
            load_config(self.write('source = "file"\n'), home=self.home)
        self.assertIn("tasks_file", str(caught.exception))

    def test_the_jira_source_requires_the_jira_table(self):
        with self.assertRaises(ValueError) as caught:
            load_config(self.write('source = "jira"\n'), home=self.home)
        self.assertIn("[jira]", str(caught.exception))

    def test_project_composes_a_query_so_nobody_has_to_write_jql(self):
        cfg = load_config(self.write(
            'source = "jira"\n[jira]\n'
            'site = "https://example.atlassian.net"\n'
            'email = "me@example.com"\n'
            'token = "secret"\n'
            'project = "OPS"\n'
        ), home=self.home)
        self.assertEqual(cfg.jira.jql, 'project = "OPS" ORDER BY created ASC')

    def test_status_narrows_the_composed_query(self):
        cfg = load_config(self.write(
            'source = "jira"\n[jira]\n'
            'site = "https://example.atlassian.net"\n'
            'email = "me@example.com"\n'
            'token = "secret"\n'
            'project = "OPS"\nstatus = "To Do"\n'
        ), home=self.home)
        self.assertEqual(
            cfg.jira.jql,
            'project = "OPS" AND status = "To Do" ORDER BY created ASC',
        )

    def test_an_explicit_jql_wins_over_the_shorthand(self):
        cfg = load_config(self.write(self.JIRA + 'project = "OTHER"\n'), home=self.home)
        self.assertEqual(cfg.jira.jql, "project = OPS ORDER BY created")

    def test_neither_jql_nor_project_is_refused_by_name(self):
        with self.assertRaises(ValueError) as caught:
            load_config(self.write(
                'source = "jira"\n[jira]\n'
                'site = "https://example.atlassian.net"\n'
                'email = "me@example.com"\n'
                'token = "secret"\n'
            ), home=self.home)
        self.assertIn("jql", str(caught.exception))
        self.assertIn("project", str(caught.exception))

    def test_each_missing_jira_key_is_named(self):
        for key in ("site", "email", "token", "jql"):
            with self.subTest(key=key):
                body = "".join(line + "\n" for line in self.JIRA.splitlines()
                               if not line.startswith(f"{key} ="))
                with self.assertRaises(ValueError) as caught:
                    load_config(self.write(body), home=self.home)
                self.assertIn(key, str(caught.exception))

    def test_a_tasks_file_inside_the_repo_is_still_refused_under_jira(self):
        inside = self.repo / "tasks.md"
        inside.write_text("")
        with self.assertRaises(ValueError):
            load_config(self.write(self.JIRA + f'tasks_file = "{inside}"\n'),
                        home=self.home)
```

- [ ] **Step 2: Run them and watch them fail**

```bash
python -m unittest tests.test_config -v
```

Expected: FAIL — `Config` has no `source`, and the required-key check still demands `tasks_file`.

- [ ] **Step 3: Implement**

In `claudeloop/config.py`, replace `REQUIRED_KEYS` with:

```python
REQUIRED_KEYS = ("repo",)
SOURCES = ("file", "jira")
JIRA_KEYS = ("site", "email", "token")
DEFAULT_ORDER = "ORDER BY created ASC"
```

Add above `Config`:

```python
@dataclass(frozen=True)
class JiraConfig:
    site: str
    email: str
    token: str
    jql: str
    transition_start: str = ""
    transition_done: str = ""


def _jql(table: dict, path: Path) -> str:
    """The operator's query, or one composed from project and status.

    Writing JQL by hand to start is a barrier, and getting it subtly wrong
    yields a silently empty backlog with nothing saying why. An explicit jql
    still wins: the shorthand cannot express an assignee, a label filter or a
    priority ordering.
    """
    jql = str(table.get("jql", "")).strip()
    if jql:
        return jql
    project = str(table.get("project", "")).strip()
    if not project:
        raise ValueError(
            f"{path}: [jira] needs either jql, or project (with an optional"
            ' status) for ClaudeLoop to compose one, e.g. project = "OPS"'
        )
    status = str(table.get("status", "")).strip()
    where = f'project = "{project}"'
    if status:
        where += f' AND status = "{status}"'
    # Jira refuses an unbounded JQL outright, so `where` always carries a
    # restriction -- confirmed against a live instance.
    return f"{where} {DEFAULT_ORDER}"


def _jira(data: dict, path: Path) -> JiraConfig:
    """Validated at load, not at first poll: a missing token would otherwise
    surface as a 401 on every 30-second poll forever, with the dashboard
    showing an empty backlog and nothing saying why."""
    table = data.get("jira")
    if not isinstance(table, dict):
        raise ValueError(
            f'{path}: source = "jira" needs a [jira] table with '
            f"{', '.join(JIRA_KEYS)}, and either jql or project"
        )
    missing = [key for key in JIRA_KEYS if not str(table.get(key, "")).strip()]
    if missing:
        raise ValueError(
            f"{path}: [jira] is missing required key(s): {', '.join(missing)}"
        )
    return JiraConfig(
        site=str(table["site"]),
        email=str(table["email"]),
        token=str(table["token"]),
        jql=_jql(table, path),
        transition_start=str(table.get("transition_start", "")),
        transition_done=str(table.get("transition_done", "")),
    )
```

Change the two `Config` fields:

```python
    tasks_file: Path | None = None
    source: str = "file"
    jira: JiraConfig | None = None
```

`source` and `jira` go at the end of the dataclass, after `session_env`, so no existing positional construction shifts.

In `load_config`, replace the `tasks_file` block with:

```python
    source = str(data.get("source", "file"))
    if source not in SOURCES:
        raise ValueError(
            f"{path}: source {source!r} is not one of {', '.join(SOURCES)}"
        )
    if source == "file" and "tasks_file" not in data:
        raise ValueError(f'{path}: source = "file" requires tasks_file')

    tasks_file = Path(str(data["tasks_file"])).expanduser() if "tasks_file" in data else None
    # Resolved so `..` segments and symlinks can't sneak a tasks_file that
    # lands inside repo past this -- but the unresolved path is still what
    # gets stored on Config below, matching repo itself. See the comment this
    # replaces: a session doing ordinary branch hygiene can revert
    # ClaudeLoop's own `- [x]` mark, and the loop then re-runs finished work.
    if tasks_file is not None and tasks_file.resolve().is_relative_to(repo.resolve()):
        raise ValueError(
            f"{path}: tasks_file {tasks_file} is inside repo {repo}. "
            "ClaudeLoop's task list must live outside the repository it "
            "works in."
        )

    jira = _jira(data, path) if source == "jira" else None
```

and add `source=source, jira=jira` to the returned `Config(...)`.

- [ ] **Step 4: Run the tests**

```bash
python -m unittest tests.test_config -v
```

Expected: PASS, existing classes included.

- [ ] **Step 5: Commit**

```bash
git add claudeloop/config.py tests/test_config.py
git commit -m "feat: a source discriminator and the [jira] table

Explicit rather than implicit: a half-written [jira] table must not
silently change which backlog runs. Every [jira] key is validated at
load, so a missing token fails with a sentence instead of a 401 on every
poll."
```

---

### Task 8: Wire the source into the loop

**Files:**
- Modify: `claudeloop/loop.py`
- Modify: `tests/test_loop.py` (append a class)

**Interfaces:**
- Produces: `build_source(cfg: Config, state: State) -> TaskSource`
- `run_task` calls `source.start(task)` after `state.start_task`, and passes `cost` to `source.mark`.

**Design notes for the implementer:**

1. **Every source call from the loop goes through `asyncio.to_thread`.** `pending`, `start` and `mark` all make blocking HTTP calls under the Jira source, and this coroutine shares its thread with the heartbeat task and the dashboard's SSE pump. A 30-second Jira timeout on the loop's thread is 30 seconds of the dashboard showing a dead loop.
2. **`source.start` goes after `state.start_task`**, for the reason the existing comment gives about `reset_to_default_branch`: a fault there must land against a real task row.
3. **`build_source` takes `state`** so the Jira source can consult `terminal_ids()`. That is the loop's own connection on the loop's own thread — not the web layer's read-only one.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_loop.py`, before the `if __name__` block:

```python
class BuildSourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.state = State(self.tmp / "state.db")

    def test_file_config_builds_a_file_source(self):
        cfg = Config(repo=self.repo, tasks_file=self.tmp / "tasks.md", home=self.tmp)
        source = loop.build_source(cfg, self.state)
        self.assertIsInstance(source, FileSource)

    def test_jira_config_builds_a_jira_source_wired_to_the_database(self):
        cfg = Config(
            repo=self.repo,
            home=self.tmp,
            source="jira",
            jira=JiraConfig("https://example.atlassian.net", "me@example.com",
                            "secret", "project = OPS", "In Progress", "Done"),
        )
        source = loop.build_source(cfg, self.state)
        self.assertIsInstance(source, JiraSource)
        self.assertEqual(source.jql, "project = OPS")
        self.assertEqual(source.transition_start, "In Progress")
        self.assertEqual(source.transition_done, "Done")
        self.assertIs(source.state, self.state)


class RecordingSource:
    """A TaskSource that records the lifecycle calls run_task makes on it."""

    def __init__(self):
        self.calls = []

    def pending(self):
        return []

    def start(self, task):
        self.calls.append(("start", task.id))

    def mark(self, task, status, summary, cost=0.0):
        self.calls.append(("mark", status, cost))


class SourceLifecycleTest(unittest.TestCase):
    """run_task must tell the source when work starts, and what it cost.

    Same fake-CLI harness as MainLoopTest above: tests/fake_claude.sh writes
    a done result with summary "fake work" and a total_cost_usd of 0.5.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "repo" / ".git").mkdir(parents=True)
        self.cfg = Config(
            repo=self.tmp / "repo",
            tasks_file=self.tmp / "tasks.md",
            home=self.tmp / "home",
            max_resumes=3,
        )
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        shutil.copy(Path(__file__).parent / "fake_claude.sh", bin_dir / "claude")
        (bin_dir / "claude").chmod(0o755)
        self.old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{self.old_path}"

    def tearDown(self):
        os.environ["PATH"] = self.old_path

    def test_start_comes_first_and_mark_carries_the_cost(self):
        state = State(self.cfg.home / "state.db")
        source = RecordingSource()
        task = Task("abcd1234abcd1234", "OPS-1: do it", "jira", "OPS-1")
        asyncio.run(loop.run_task(self.cfg, state, source, task))
        self.assertEqual(source.calls[0], ("start", task.id))
        self.assertEqual(source.calls[-1][:2], ("mark", "done"))
        self.assertAlmostEqual(source.calls[-1][2], 0.5)
```

**Implementer's note:** the imports these two classes need — `asyncio`, `os`, `shutil`, `tempfile`, `Path`, `Config`, `JiraConfig`, `State`, `Task`, `FileSource`, `JiraSource`, `loop` — are mostly already at the top of `tests/test_loop.py`. Add only the missing ones.

- [ ] **Step 2: Run them and watch them fail**

```bash
python -m unittest tests.test_loop -v
```

Expected: `AttributeError: module 'claudeloop.loop' has no attribute 'build_source'`.

- [ ] **Step 3: Implement**

In `claudeloop/loop.py`, add the import and the builder:

```python
from .jira import JiraClient, JiraSource
from .source import FileSource, Task, TaskSource


def build_source(cfg: Config, state: State) -> TaskSource:
    """The one place that knows which task source a config selects."""
    if cfg.source == "jira" and cfg.jira is not None:
        return JiraSource(
            JiraClient(cfg.jira.site, cfg.jira.email, cfg.jira.token),
            cfg.jira.jql,
            state,
            cfg.jira.transition_start,
            cfg.jira.transition_done,
        )
    return FileSource(cfg.tasks_file)
```

In `run_task`, after the existing `await asyncio.to_thread(reset_to_default_branch, cfg.repo)`:

```python
    # Offloaded for the same reason as the git call above: under the Jira
    # source this is a blocking HTTP round trip, and this coroutine shares
    # its thread with the heartbeat and the dashboard.
    await asyncio.to_thread(source.start, task)
```

and replace the `source.mark(...)` call at the end of `run_task` with:

```python
    await asyncio.to_thread(
        source.mark, task, result["status"], result["summary"], cost
    )
```

In `main_loop`, replace the source construction and the poll:

```python
    state = State(cfg.home / "state.db")
    source = build_source(cfg, state)
```

```python
            pending = await asyncio.to_thread(source.pending)
```

- [ ] **Step 4: Run the whole suite**

```bash
python -m unittest discover -s tests -t .
```

Expected: PASS. `run_task` is now `async` around three more awaits; if an existing test called `source.mark` synchronously through a fake, it still works — `to_thread` calls the same method.

- [ ] **Step 5: Commit**

```bash
git add claudeloop/loop.py tests/test_loop.py
git commit -m "feat: select the task source from config, off the event loop

Every source call is a blocking HTTP round trip under the Jira source, so
pending/start/mark all go through asyncio.to_thread -- the loop's thread
also carries the heartbeat and the dashboard's SSE pump."
```

---

### Task 9: The session can reach the CLI — `PYTHONPATH` and the prompt layer

**Files:**
- Modify: `claudeloop/session.py`
- Modify: `claudeloop/prompt.py`
- Modify: `tests/test_session.py` (append a class)
- Modify: `tests/test_prompt.py` (append a class)

**Interfaces:**
- Produces: `PACKAGE_PARENT: str` in `session.py`; `task_source_section(cfg: Config) -> str` and `JIRA_TASK_SOURCE: str` in `prompt.py`.

**Design notes for the implementer:**

1. **The session runs with `cwd = repo`.** A bare `python -m claudeloop.jira` there is an `ImportError` — the package is not on its path — and running `jira.py` by absolute path breaks its relative imports. Prepending the package's parent to `PYTHONPATH` is the fix.
2. **Prepend, never replace.** An operator may have set `PYTHONPATH` in `[session_env]` for the repository's own needs; ClaudeLoop's entry goes in front of theirs, and theirs survives.
3. **`CLAUDELOOP_RESULT` stays the last key merged.** That ordering is a hard constraint of this project.
4. **The prompt names `sys.executable`, not `python`.** The box may have several interpreters, and only one of them is running ClaudeLoop.
5. **The last sentence of the prompt section is load-bearing.** A session told it may talk on the ticket is exactly the session that ends its turn with a comment instead of the result file. Every live failure this project has had traced back to a sentence that could be read two ways.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_session.py`:

```python
class PythonPathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)

    def cfg(self, **kwargs):
        return Config(repo=self.repo, tasks_file=self.tmp / "t.md", **kwargs)

    def test_the_package_parent_is_importable_from_the_session(self):
        env = session.child_env(self.cfg(), self.tmp / "run")
        first = env["PYTHONPATH"].split(os.pathsep)[0]
        self.assertEqual(Path(first), Path(session.PACKAGE_PARENT))
        self.assertTrue((Path(first) / "claudeloop" / "jira.py").exists())

    def test_an_operators_pythonpath_survives_in_front_of_nothing(self):
        env = session.child_env(
            self.cfg(session_env={"PYTHONPATH": "/opt/theirs"}), self.tmp / "run"
        )
        parts = env["PYTHONPATH"].split(os.pathsep)
        self.assertEqual(Path(parts[0]), Path(session.PACKAGE_PARENT))
        self.assertIn("/opt/theirs", parts)

    def test_claudeloop_result_is_still_merged_last(self):
        env = session.child_env(
            self.cfg(session_env={"CLAUDELOOP_RESULT": "/tmp/hijacked",
                                  "PYTHONPATH": "/opt/theirs"}),
            self.tmp / "run",
        )
        self.assertEqual(env["CLAUDELOOP_RESULT"], str(self.tmp / "run" / "result.json"))
```

Append to `tests/test_prompt.py`:

```python
class TaskSourceSectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)

    def cfg(self, **kwargs):
        return Config(repo=self.repo, tasks_file=self.tmp / "t.md",
                      home=self.tmp, **kwargs)

    def jira_cfg(self):
        return self.cfg(source="jira", jira=JiraConfig(
            "https://example.atlassian.net", "me@example.com", "secret",
            "project = OPS"))

    def test_absent_for_the_file_source(self):
        self.assertNotIn("Task source", compose(self.cfg()))

    def test_present_for_the_jira_source(self):
        text = compose(self.jira_cfg())
        self.assertIn("## Task source", text)
        self.assertIn("claudeloop.jira show", text)
        self.assertIn("claudeloop.jira comment", text)

    def test_names_this_interpreter_not_bare_python(self):
        self.assertIn(sys.executable, compose(self.jira_cfg()))

    def test_says_the_key_is_the_first_token_of_the_task_text(self):
        self.assertIn("first token", compose(self.jira_cfg()))

    def test_forbids_the_session_transitioning_or_relabelling(self):
        text = compose(self.jira_cfg())
        self.assertIn("Do not transition", text)
        self.assertIn("labels", text)

    def test_says_a_comment_is_not_how_a_task_ends(self):
        # A session told it may talk on the ticket is exactly the session
        # that ends its turn with a comment instead of the result file.
        self.assertIn("Commenting is not how a task ends", compose(self.jira_cfg()))

    def test_sits_below_the_protocol(self):
        text = compose(self.jira_cfg())
        self.assertLess(text.index("unattended under ClaudeLoop"),
                        text.index("## Task source"))
```

`tests/test_prompt.py` needs `import sys` and `from claudeloop.config import Config, JiraConfig`; `tests/test_session.py` needs `import os` and the same `JiraConfig`-free `Config` import it already has.

- [ ] **Step 2: Run them and watch them fail**

```bash
python -m unittest tests.test_session tests.test_prompt -v
```

Expected: `AttributeError: module 'claudeloop.session' has no attribute 'PACKAGE_PARENT'` and a missing `## Task source`.

- [ ] **Step 3: Implement**

In `claudeloop/session.py`, add below the imports:

```python
PACKAGE_PARENT = str(Path(__file__).resolve().parent.parent)
"""The directory holding the claudeloop package.

The session runs with cwd=repo, where `python -m claudeloop.jira` is an
ImportError -- and running jira.py by absolute path breaks its relative
imports. This is what makes the session's Jira CLI reachable.
"""
```

and rewrite `child_env`:

```python
def child_env(cfg: Config, run_dir: Path) -> dict[str, str]:
    """The environment the session runs in.

    CLAUDELOOP_RESULT is merged last on purpose: a misconfigured session_env
    must not be able to redirect the result file, which is the only thing the
    loop uses to decide a task is finished.

    PYTHONPATH is prepended rather than replaced, so an operator who set one
    in [session_env] for the repository's own needs keeps it.
    """
    env = os.environ | dict(cfg.session_env)
    inherited = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        os.pathsep.join([PACKAGE_PARENT, inherited]) if inherited else PACKAGE_PARENT
    )
    return env | {"CLAUDELOOP_RESULT": str(run_dir / "result.json")}
```

In `claudeloop/prompt.py`, add `import sys` and:

```python
JIRA_TASK_SOURCE = """## Task source

This task is a Jira issue. Its key is the first token of the task text.

Read the full ticket, including its comments:
    {python} -m claudeloop.jira show <KEY>
Post a comment (its body is read from stdin):
    {python} -m claudeloop.jira comment <KEY> -

Comment when you find something a human should see, or before a long step.
Do not transition the issue or edit its labels -- ClaudeLoop does that when
the task ends. Commenting is not how a task ends: the result file still is."""
"""Read by a literal-minded agent, so the last sentence is not decoration --
a session told it may talk on the ticket is exactly the session that ends its
turn with a comment instead of the result file."""


def task_source_section(cfg: Config) -> str:
    """Empty for every source that needs no explanation, which today is the
    checklist."""
    if cfg.source != "jira":
        return ""
    # sys.executable, not "python": the box may have several interpreters and
    # only this one is running ClaudeLoop, hence only this one has the
    # package parent on its path via PYTHONPATH.
    return JIRA_TASK_SOURCE.format(python=sys.executable)
```

In `compose`, after the `precedence` line:

```python
    task_source = task_source_section(cfg)
    if task_source:
        parts.append(task_source)
```

- [ ] **Step 4: Run the tests**

```bash
python -m unittest tests.test_session tests.test_prompt -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add claudeloop/session.py claudeloop/prompt.py tests/test_session.py tests/test_prompt.py
git commit -m "feat: make the Jira CLI reachable from the session and say so

cwd=repo means `python -m claudeloop.jira` cannot import the package, so
child_env prepends its parent to PYTHONPATH, in front of any the operator
set. The prompt layer names sys.executable and says plainly that a
comment is not how a task ends."
```

---

### Task 10: The session's CLI

**Files:**
- Modify: `claudeloop/jira.py`
- Modify: `tests/test_jira.py`

**Interfaces:**
- Produces: `main(argv: list[str] | None = None) -> int`, and `if __name__ == "__main__": raise SystemExit(main())` at the bottom of the module.

**Design notes for the implementer:**

1. **`--config` defaults to `DEFAULT_CONFIG`** — needed by the tests, and useful to an operator running two instances.
2. **`show` prints for a reader, not a parser**: key, status, labels, summary, description, then comments oldest-first.
3. **`comment` reads the body from stdin when the argument is `-`**, which is the only form the prompt teaches. A literal body as an argument is accepted too; it costs one line.
4. **Errors exit non-zero with a message on stderr.** The session sees a failed command and decides what to do; the task is unaffected either way.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_jira.py`:

```python
import contextlib
import io

from claudeloop.jira import main


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)

    def configured(self, routes):
        self.fake = FakeJira(routes)
        self.addCleanup(self.fake.close)
        path = self.tmp / "config.toml"
        path.write_text(
            f'repo = "{self.repo}"\n'
            'source = "jira"\n'
            "[jira]\n"
            f'site = "{self.fake.url}"\n'
            'email = "me@example.com"\n'
            'token = "secret"\n'
            'jql = "project = OPS"\n'
        )
        path.chmod(0o600)
        return path

    def run_cli(self, args, stdin=""):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with unittest.mock.patch("sys.stdin", io.StringIO(stdin)):
                code = main(args)
        return code, out.getvalue()

    def test_show_prints_the_ticket_and_its_comments(self):
        config = self.configured({
            "GET /issue/OPS-1": (200, fixture("issue")),
            "GET /issue/OPS-1/comment": (200, fixture("comments")),
        })
        code, out = self.run_cli(["--config", str(config), "show", "OPS-1"])
        self.assertEqual(code, 0)
        self.assertIn("OPS-1", out)
        issue_summary = fixture("issue")["fields"]["summary"]
        self.assertIn(issue_summary, out)

    def test_comment_posts_the_body_from_stdin(self):
        config = self.configured({"POST /issue/OPS-1/comment": (201, {})})
        code, _ = self.run_cli(["--config", str(config), "comment", "OPS-1", "-"],
                               stdin="found the cause: a stale lockfile\n")
        self.assertEqual(code, 0)
        _, path, payload = self.fake.requests[0]
        self.assertEqual(path, "/issue/OPS-1/comment")
        self.assertEqual(payload["body"], "found the cause: a stale lockfile")

    def test_comment_accepts_a_literal_body(self):
        config = self.configured({"POST /issue/OPS-1/comment": (201, {})})
        code, _ = self.run_cli(["--config", str(config), "comment", "OPS-1", "hello"])
        self.assertEqual(code, 0)
        self.assertEqual(self.fake.requests[0][2]["body"], "hello")

    def test_an_empty_comment_is_refused_without_calling_jira(self):
        config = self.configured({"POST /issue/OPS-1/comment": (201, {})})
        code, _ = self.run_cli(["--config", str(config), "comment", "OPS-1", "-"],
                               stdin="   \n")
        self.assertEqual(code, 2)
        self.assertEqual(self.fake.requests, [])

    def test_a_jira_error_exits_non_zero(self):
        config = self.configured({"GET /issue/OPS-9": (404, {"errorMessages": ["gone"]})})
        code, _ = self.run_cli(["--config", str(config), "show", "OPS-9"])
        self.assertNotEqual(code, 0)
```

Add `import tempfile`, `import unittest.mock` and `from pathlib import Path` to the file's imports if they are not there already.

- [ ] **Step 2: Run them and watch them fail**

```bash
python -m unittest tests.test_jira -v
```

Expected: `ImportError: cannot import name 'main'`.

- [ ] **Step 3: Implement**

Append to `claudeloop/jira.py`:

```python
import argparse
import sys

from .config import DEFAULT_CONFIG, load_config


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
    client = _client(Path(args.config))
    try:
        if args.command == "show":
            _show(client, args.key)
            return 0
        body = (sys.stdin.read() if args.body == "-" else args.body).strip()
        if not body:
            print("refusing to post an empty comment", file=sys.stderr)
            return 2
        client.add_comment(args.key, body)
        print(f"commented on {args.key}")
        return 0
    except JiraError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
```

`from pathlib import Path` goes with the module's other imports.

- [ ] **Step 4: Run the tests**

```bash
python -m unittest tests.test_jira -v
```

Expected: PASS.

- [ ] **Step 5: Verify it works as `-m` from another directory**

```bash
cd /tmp && PYTHONPATH="$OLDPWD" python -m claudeloop.jira --help
```

Expected: the usage message, listing `show` and `comment`. This is the exact path the session takes, and an `ImportError` here means Task 9's `PYTHONPATH` change is wrong.

- [ ] **Step 6: Commit**

```bash
git add claudeloop/jira.py tests/test_jira.py
git commit -m "feat: python -m claudeloop.jira show/comment for the session

Two subcommands only. Transitions and labels stay with the orchestrator,
so a confused session cannot park a ticket somewhere the operator did not
expect."
```

---

### Task 11: Documentation

**Files:**
- Modify: `README.md`
- Modify: `ROADMAP.md`

- [ ] **Step 1: Document the Jira source in `README.md`**

Add after the existing configuration block, matching the file's voice — second person, no marketing:

````markdown
## Taking tasks from Jira

Instead of a checklist, ClaudeLoop can take its backlog from a Jira Cloud
project:

```toml
source = "jira"

[jira]
site    = "https://yourcompany.atlassian.net"   # no /jira suffix
email   = "you@yourcompany.com"
token   = "ATATT..."          # id.atlassian.com -> Security -> API tokens
project = "OPS"               # which project to take work from
status  = "To Do"             # optional; the exact status name on your board
transition_start = "In Progress"   # optional; skipped if unset or unavailable
transition_done  = "Done"          # optional; same
```

`tasks_file` is not needed under `source = "jira"`.

That composes `project = "OPS" AND status = "To Do" ORDER BY created ASC`. If
you want something the two keys cannot say — an assignee, a label, a priority
ordering — give `jql` instead and it wins outright:

```toml
jql = "project = OPS AND assignee = currentUser() ORDER BY priority DESC"
```

Note that Jira refuses a query with no restriction in it at all, so `jql` must
narrow something.

Each matching issue becomes one task, whose text is the issue key, its summary
and its description. When a task ends, ClaudeLoop labels the issue
`claudeloop-done` or `claudeloop-blocked`, posts a closing comment carrying the
status, the summary and the cost, and moves it to `transition_done` if the
workflow offers that transition from where the issue sits.

**The labels are how ClaudeLoop knows what is finished**, not the status: it
composes `(labels IS EMPTY OR labels NOT IN ("claudeloop-done",
"claudeloop-blocked"))` into your JQL, keeping your `ORDER BY`. You cannot turn
that off — without it a workflow that refuses the done transition would make
the loop run the same ticket forever. To re-run a ticket, remove the label.

The session can read and comment on the ticket while it works:

```bash
python -m claudeloop.jira show OPS-42
python -m claudeloop.jira comment OPS-42 -   # body on stdin
```

It cannot transition issues or change labels — ClaudeLoop does that itself.

An unreachable Jira looks like an empty backlog: ClaudeLoop logs it, idles, and
tries again on the next poll.
````

- [ ] **Step 2: Update `ROADMAP.md`**

- Move S3 from `Next` to `Built`, state `merged`, with a paragraph in the same voice as S1.1's and a pointer to the spec.
- Delete the "Open — one non-obvious JQL trap" and "Unverified" notes; both are settled, and the trap now lives in a test.
- Promote S2b to the head of `Next`, keeping its existing text.
- Add to "Open issues carried across slices": *`JiraSource.pending` fetches one page of 50 issues and never paginates. The loop consumes one task at a time and re-polls constantly, so a longer backlog is simply seen 50 at a time — but a JQL ordering that puts the wanted work past the 50th row would never reach it.*

- [ ] **Step 3: Commit**

```bash
git add README.md ROADMAP.md
git commit -m "docs: the Jira task source"
```

---

### Task 12: Whole-branch review and the live smoke test

**Not optional.** Four slices, four live smoke tests, five real defects the passing suite could not have caught. See `CLAUDE.md`.

- [ ] **Step 1: Run the whole suite**

```bash
python -m unittest discover -s tests -t .
```

Expected: every test passing, with roughly 60 more than the 227 this plan started from.

- [ ] **Step 2: Whole-branch code review**

Use `superpowers:requesting-code-review` against the full branch diff.

- [ ] **Step 3: Set up the live smoke test**

- A scratch git repository, outside this one, with one trivial `CLAUDE.md`.
- A Jira project with **two** tickets matching the JQL — two, not one: several past defects only appeared on the second task, where state left by the first matters.
- A config with `model = "haiku"`, `source = "jira"`, real credentials, and both transitions named.

- [ ] **Step 4: Run it and watch what actually happens**

```bash
python -m claudeloop
```

Confirm each of these, and treat any failure as a defect to fix before merging:

1. The composed JQL returns the two tickets, and the log shows the composition.
2. The first ticket transitions to `transition_start` when its task begins.
3. The session finds and runs `python -m claudeloop.jira show` — this is what the `PYTHONPATH` change exists for, and nothing but a live run tests it.
4. The session posts at least one comment of its own, **and still writes the result file**. A session that comments instead of finishing is the specific failure the prompt's last sentence guards against.
5. On finish: the label lands, the closing comment carries status and cost, the done transition fires.
6. **The second task starts, and is the second ticket** — not the first one again. This is the whole point of the label guard.
7. After both, the loop idles instead of re-offering either ticket.

- [ ] **Step 5: Fix what it found, then re-run it**

If any fix touched prompt text, re-run the smoke test. Text fixes are exactly the kind that come back differently broken — this project has the scar tissue to prove it.

- [ ] **Step 6: Merge**

Use `superpowers:finishing-a-development-branch`, then update `ROADMAP.md` if the smoke test changed anything the docs claim.
