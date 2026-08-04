import asyncio
import contextlib
import io
import sqlite3
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from claudeloop import jira
from claudeloop.jira import (
    BLOCKED_LABEL, DONE_LABEL, GUARD, SEARCH_PATH, JiraClient, JiraError,
    JiraSource, closing_comment, compose_jql, main, match_transitions,
    recovery_jql, task_text,
)
from claudeloop.source import Task, task_id
from claudeloop.state import State

from .jira_fake import FakeJira, fixture


def last_page() -> dict:
    """The captured search fixture as a final page.

    The capture is a real response to a real query with more results behind
    it, so it carries a nextPageToken and isLast: false -- which is what
    pins the pagination shape. A test that only wants "one page of two
    issues" has to say so, or pending() rightly keeps asking for more.
    """
    page = dict(fixture("search"))
    page.pop("nextPageToken", None)
    page["isLast"] = True
    return page


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

    def test_issue_percent_encodes_a_key_needing_it(self):
        # A key reaching JiraClient is not always one Jira itself produced --
        # the CLI's is raw argv. Every path-building method must encode it
        # rather than splice it in verbatim.
        client = self.client({"GET /issue/OPS%201": (200, fixture("issue"))})
        client.issue("OPS 1")
        _, path, _ = self.fake.requests[0]
        self.assertIn("OPS%201", path)

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

    def test_a_non_dict_json_body_returns_empty_rather_than_raising(self):
        # A 200 whose body is a JSON list (an SSO interstitial, a gateway
        # answering something that isn't Jira's JSON) must not reach a
        # caller that assumes dict -- pending() does data.get("issues") on
        # whatever this returns, and an AttributeError there escapes past
        # main_loop's try/except.
        client = self.client({f"POST {SEARCH_PATH}": (200, ["not", "a", "dict"])})
        self.assertEqual(client.search("project = OPS"), {})


class RecoveryJqlTest(unittest.TestCase):
    """The query that finds work the operator's own JQL can no longer see,
    because ClaudeLoop's transition_start moved the issue out of it."""

    def test_names_the_keys_and_excludes_closed_and_labelled_issues(self):
        # Pinned whole rather than by substring: S7's live failure was a
        # sentence assembled from fragments where every substring assertion
        # passed and the composed string still said nothing.
        self.assertEqual(
            recovery_jql(["KAN-1", "KAN-13"]),
            'key IN (KAN-1, KAN-13) AND statusCategory != Done AND '
            '(labels IS EMPTY OR labels NOT IN ("claudeloop-done", '
            '"claudeloop-blocked"))',
        )

    def test_the_status_predicate_is_the_category_not_a_status_name(self):
        # Jira translates status names per account -- "Done" displays as
        # "Gotowe" on the instance this slice came from -- so a name here
        # would be the same defect S9.1 fixed in the transition matcher. The
        # three category keys never translate.
        composed = recovery_jql(["KAN-1"])
        self.assertIn("statusCategory != Done", composed)

    def test_carries_the_same_label_guard_as_the_backlog_query(self):
        # A mark() whose label landed while the row stayed non-terminal must
        # still exclude the issue.
        self.assertIn(GUARD, recovery_jql(["KAN-1"]))


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

    def test_an_order_by_inside_a_quoted_value_does_not_split(self):
        composed = compose_jql('summary ~ "please order by priority"')
        self.assertIn('summary ~ "please order by priority"', composed)
        self.assertEqual(composed.count('"') % 2, 0)
        self.assertNotIn("ORDER BY", composed[composed.index("labels IS EMPTY"):])

    def test_a_real_ordering_after_a_quoted_one_still_splits(self):
        composed = compose_jql('summary ~ "order by x" ORDER BY created ASC')
        self.assertTrue(composed.endswith(" ORDER BY created ASC"), composed)
        self.assertIn('summary ~ "order by x"', composed)

    def test_an_empty_query_yields_the_guard_alone(self):
        self.assertEqual(compose_jql("   "), GUARD)

    def test_an_apostrophe_inside_a_double_quoted_value_still_splits(self):
        # Counting " and ' independently sees an "unbalanced" double quote
        # here (the ' inside "Won't Do" throws off nothing, but the old
        # implementation checked both counts were even, and this string's
        # apostrophe count is odd) -- so ORDER BY was never recognised and
        # ended up composed inside the parenthesised WHERE clause.
        composed = compose_jql(
            'project = "OPS" AND status != "Won\'t Do" ORDER BY created ASC'
        )
        self.assertTrue(composed.endswith(" ORDER BY created ASC"), composed)
        self.assertNotIn("ORDER BY", composed[:composed.index(" ORDER BY created ASC")])

    def test_an_apostrophe_in_a_name_still_splits(self):
        composed = compose_jql("assignee = \"o'brien\" ORDER BY created")
        self.assertTrue(composed.endswith(" ORDER BY created"), composed)
        self.assertIn("assignee = \"o'brien\"", composed)


class MatchTransitionsTest(unittest.TestCase):
    """Which offered transitions a configured value could mean.

    The live failure this exists for: a Jira whose built-in statuses display
    in Polish offered `Do zrobienia, W toku, Gotowe` where the operator had
    configured `In Progress`, so nothing ever matched -- while the *pickup*
    side of the same config worked, because JQL resolves the untranslated
    name and this comparison does not.
    """

    POLISH = [
        {"id": "11", "name": "Do zrobienia",
         "to": {"name": "Do zrobienia", "statusCategory": {"key": "new"}}},
        {"id": "21", "name": "W toku",
         "to": {"name": "W toku", "statusCategory": {"key": "indeterminate"}}},
        {"id": "31", "name": "Gotowe",
         "to": {"name": "Gotowe", "statusCategory": {"key": "done"}}},
    ]

    def ids(self, matches):
        return [t["id"] for t in matches]

    def test_the_transition_name_still_matches(self):
        self.assertEqual(self.ids(match_transitions(self.POLISH, "W toku")), ["21"])

    def test_the_name_match_is_case_and_space_insensitive(self):
        self.assertEqual(self.ids(match_transitions(self.POLISH, "  w TOKU ")), ["21"])

    def test_the_transition_id_matches(self):
        # The one value that is stable across locale, rename and workflow
        # edits, and the only one an operator can read straight off the API.
        self.assertEqual(self.ids(match_transitions(self.POLISH, "31")), ["31"])

    def test_the_destination_status_name_matches(self):
        # A custom workflow names the transition for the action and the
        # status for the state: "Finish work" leads to "Done".
        offered = [{"id": "41", "name": "Finish work",
                    "to": {"name": "Done", "statusCategory": {"key": "done"}}}]
        self.assertEqual(self.ids(match_transitions(offered, "Done")), ["41"])

    def test_the_status_category_key_matches(self):
        # `new`, `indeterminate` and `done` are Jira's own vocabulary and are
        # never translated, so they are what an operator on a localised site
        # can configure and rely on.
        self.assertEqual(self.ids(match_transitions(self.POLISH, "done")), ["31"])
        self.assertEqual(
            self.ids(match_transitions(self.POLISH, "indeterminate")), ["21"])

    def test_a_more_specific_tier_wins(self):
        # A transition literally named "done" must beat every transition
        # whose category is done, rather than colliding with them.
        offered = self.POLISH + [
            {"id": "51", "name": "done",
             "to": {"name": "Zrobione", "statusCategory": {"key": "done"}}},
        ]
        self.assertEqual(self.ids(match_transitions(offered, "done")), ["51"])

    def test_an_ambiguous_category_returns_every_candidate(self):
        # The hazard that makes this worth reporting rather than guessing: a
        # Jira board's bin sits in the `done` category alongside the real
        # finished status, so picking the first match could bin the issue.
        offered = self.POLISH + [
            {"id": "61", "name": "Kosz",
             "to": {"name": "Kosz", "statusCategory": {"key": "done"}}},
        ]
        self.assertEqual(self.ids(match_transitions(offered, "done")), ["31", "61"])

    def test_nothing_matching_returns_empty(self):
        self.assertEqual(match_transitions(self.POLISH, "In Progress"), [])

    def test_an_empty_wanted_matches_nothing(self):
        # Not merely tidiness: every tier reads "" out of a transition that
        # omits the key, so an empty value would match the first malformed
        # entry in the payload.
        self.assertEqual(match_transitions(self.POLISH, ""), [])
        self.assertEqual(match_transitions(self.POLISH, "   "), [])

    def test_a_transition_missing_to_does_not_raise(self):
        # `to` is in the payload by default, but a stripped or mocked one
        # must fall through the last two tiers rather than crash in them.
        offered = [{"id": "71", "name": "Finish work"}]
        self.assertEqual(self.ids(match_transitions(offered, "Finish work")), ["71"])
        self.assertEqual(match_transitions(offered, "done"), [])

    def test_a_non_dict_entry_is_skipped(self):
        offered = ["nonsense", None] + self.POLISH
        self.assertEqual(self.ids(match_transitions(offered, "Gotowe")), ["31"])


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

    def test_task_text_tolerates_a_missing_summary(self):
        self.assertEqual(task_text("OPS-3", None, None), "OPS-3:")


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


class FakeState:
    def __init__(self, ids=(), unfinished_rows=(), unfinished_error=None):
        self.ids = set(ids)
        self.rows = list(unfinished_rows)
        self.unfinished_error = unfinished_error

    def terminal_ids(self):
        return self.ids

    def unfinished(self):
        if self.unfinished_error is not None:
            raise self.unfinished_error
        return self.rows


def stranded_row(key: str, source: str = "jira") -> dict:
    """A `tasks` row as State.unfinished() hands it over. sqlite3.Row and dict
    are both subscriptable by column name, which is all _stranded reads."""
    return {"id": task_id(key), "source": source,
            "source_ref": key, "text": f"{key}: do it"}


class JiraSourceTest(unittest.TestCase):
    def source(self, routes, **kwargs):
        self.fake = FakeJira(routes)
        self.addCleanup(self.fake.close)
        client = JiraClient(self.fake.url, "me@example.com", "token",
                            sleep=lambda _: None)
        return JiraSource(client, kwargs.pop("jql", "project = OPS"), **kwargs)

    def test_pending_builds_one_task_per_issue(self):
        source = self.source({f"POST {SEARCH_PATH}": (200, last_page())})
        tasks = source.pending()
        self.assertEqual(len(tasks), 2)
        first = tasks[0]
        self.assertEqual(first.source, "jira")
        self.assertEqual(first.source_ref, "OPS-1")
        self.assertEqual(first.id, task_id("OPS-1"))
        self.assertEqual(len(first.id), 16)
        self.assertTrue(first.text.startswith("OPS-1: "), first.text)

    def test_pending_survives_the_null_description_in_the_fixture(self):
        source = self.source({f"POST {SEARCH_PATH}": (200, last_page())})
        self.assertTrue(all(task.text for task in source.pending()))

    def test_pending_sends_the_composed_jql(self):
        source = self.source({f"POST {SEARCH_PATH}": (200, last_page())})
        source.pending()
        _, _, payload = self.fake.requests[0]
        self.assertIn("labels IS EMPTY", payload["jql"])
        self.assertIn("project = OPS", payload["jql"])

    def test_pending_drops_tasks_already_terminal_in_the_database(self):
        source = self.source({f"POST {SEARCH_PATH}": (200, last_page())},
                             state=FakeState({task_id("OPS-1")}))
        self.assertEqual([t.source_ref for t in source.pending()], ["OPS-2"])

    def test_pending_through_to_thread_drops_a_task_terminal_in_a_real_state(self):
        # The end-to-end shape the live smoke test actually exercised:
        # main_loop calls source.pending() through asyncio.to_thread, and
        # a real State opens its sqlite3 connection on whatever thread
        # constructs it. Without check_same_thread=False on that connection,
        # terminal_ids() raises sqlite3.ProgrammingError here, pending()
        # swallows it as a warning, and the backstop never filters anything
        # -- which is exactly how a finished ticket got served twice.
        tmp = Path(tempfile.mkdtemp())
        state = State(tmp / "state.db")
        state.start_task(task_id("OPS-1"), "jira", "OPS-1", "text")
        state.finish_task(task_id("OPS-1"), "done", "summary", 0.0)
        source = self.source({f"POST {SEARCH_PATH}": (200, last_page())},
                             state=state)
        tasks = asyncio.run(asyncio.to_thread(source.pending))
        self.assertEqual([t.source_ref for t in tasks], ["OPS-2"])

    def test_pending_names_index_lag_before_a_failed_write(self):
        # Reworded after the live smoke test: state.db's backstop matters
        # most in the seconds right after mark() labels a ticket, while
        # Jira's search index has not caught up yet -- not primarily because
        # the label write failed. An operator reading this at 3am should be
        # pointed at index lag first.
        source = self.source({f"POST {SEARCH_PATH}": (200, last_page())},
                             state=FakeState({task_id("OPS-1")}))
        with self.assertLogs("claudeloop", level="WARNING") as logs:
            source.pending()
        self.assertIn("index", "".join(logs.output).lower())

    def test_pending_returns_empty_on_an_http_error_rather_than_raising(self):
        source = self.source({f"POST {SEARCH_PATH}": (401, {"errorMessages": ["nope"]})})
        with self.assertLogs("claudeloop", level="WARNING"):
            self.assertEqual(source.pending(), [])

    def issue(self, key: str) -> dict:
        return {"key": key, "fields": {"summary": f"do {key}", "description": ""}}

    # --- S12: work the operator's own JQL can no longer see ----------------
    #
    # JiraSource.start fires transition_start, which moves the issue to the
    # in-progress status; an operator's JQL selects the backlog one. From that
    # moment the backlog query cannot reach a task ClaudeLoop itself started,
    # and a restart leaves it stranded at 'interrupted' with nothing to offer
    # it back. Observed live on KAN-13, which idled beside a running loop
    # until a human finished it by hand.

    def test_pending_recovers_a_task_the_backlog_query_no_longer_matches(self):
        source = self.source(
            {f"POST {SEARCH_PATH}": [
                (200, {"issues": []}),                       # the backlog
                (200, {"issues": [self.issue("KAN-13")]}),   # the recovery
            ]},
            state=FakeState(unfinished_rows=[stranded_row("KAN-13")]),
        )

        self.assertEqual([t.source_ref for t in source.pending()], ["KAN-13"])

    def test_the_recovery_query_names_this_repositorys_stranded_keys(self):
        source = self.source(
            {f"POST {SEARCH_PATH}": [
                (200, {"issues": []}),
                (200, {"issues": []}),
            ]},
            state=FakeState(unfinished_rows=[stranded_row("KAN-1"),
                                             stranded_row("KAN-13")]),
        )

        source.pending()

        self.assertEqual(self.fake.requests[1][2]["jql"],
                         recovery_jql(["KAN-1", "KAN-13"]))

    def test_recovered_work_is_offered_before_the_backlog(self):
        # main_loop takes pending[0]. Money is already spent on the stranded
        # task, a worktree already exists for it, and its session may still be
        # resumable -- it outranks work nobody has started.
        source = self.source(
            {f"POST {SEARCH_PATH}": [
                (200, {"issues": [self.issue("OPS-1")]}),
                (200, {"issues": [self.issue("KAN-13")]}),
            ]},
            state=FakeState(unfinished_rows=[stranded_row("KAN-13")]),
        )

        self.assertEqual([t.source_ref for t in source.pending()],
                         ["KAN-13", "OPS-1"])

    def test_an_issue_in_both_answers_is_offered_once(self):
        # A transition that Jira refused leaves the issue in the backlog
        # status, so both queries return it.
        source = self.source(
            {f"POST {SEARCH_PATH}": [
                (200, {"issues": [self.issue("KAN-13"), self.issue("OPS-1")]}),
                (200, {"issues": [self.issue("KAN-13")]}),
            ]},
            state=FakeState(unfinished_rows=[stranded_row("KAN-13")]),
        )

        self.assertEqual([t.source_ref for t in source.pending()],
                         ["KAN-13", "OPS-1"])

    def test_nothing_stranded_means_no_second_query(self):
        source = self.source({f"POST {SEARCH_PATH}": (200, last_page())},
                             state=FakeState())

        source.pending()

        self.assertEqual(len(self.fake.requests), 1)

    def test_a_stranded_row_from_another_source_is_not_asked_about(self):
        # One state.db can hold rows a file source wrote. `- [ ] do a thing`
        # is not an issue key and Jira has nothing to say about it.
        source = self.source({f"POST {SEARCH_PATH}": (200, last_page())},
                             state=FakeState(unfinished_rows=[
                                 stranded_row("- [ ] a thing", source="file")]))

        source.pending()

        self.assertEqual(len(self.fake.requests), 1)

    def test_a_source_ref_that_is_not_an_issue_key_never_reaches_the_query(self):
        # source_ref comes out of a database column and is spliced into a
        # query string; JQL has no parameter binding. This is the only thing
        # standing between the two.
        injected = 'KAN-1) OR (project = SECRET'
        source = self.source({f"POST {SEARCH_PATH}": (200, last_page())},
                             state=FakeState(unfinished_rows=[
                                 stranded_row(injected)]))

        with self.assertLogs("claudeloop", level="WARNING") as logs:
            source.pending()

        self.assertEqual(len(self.fake.requests), 1)
        self.assertIn(injected, "".join(logs.output))

    def test_the_recovery_query_is_bounded(self):
        rows = [stranded_row(f"KAN-{index}") for index in range(1, 120)]
        source = self.source(
            {f"POST {SEARCH_PATH}": [(200, {"issues": []}), (200, {"issues": []})]},
            state=FakeState(unfinished_rows=rows),
        )

        source.pending()

        keys = self.fake.requests[1][2]["jql"].split("(", 1)[1].split(")", 1)[0]
        self.assertEqual(len(keys.split(", ")), jira.MAX_RECOVERED)

    def test_an_unreadable_state_db_leaves_the_backlog_working(self):
        source = self.source(
            {f"POST {SEARCH_PATH}": (200, last_page())},
            state=FakeState(unfinished_error=sqlite3.OperationalError("locked")),
        )

        with self.assertLogs("claudeloop", level="WARNING"):
            tasks = source.pending()

        self.assertEqual([t.source_ref for t in tasks], ["OPS-1", "OPS-2"])

    def test_a_recovered_issue_already_terminal_in_the_database_is_dropped(self):
        # Belt and braces with the statusCategory predicate: if a row somehow
        # reads terminal while the issue still answers the recovery query, the
        # existing backstop still has to win.
        source = self.source(
            {f"POST {SEARCH_PATH}": [
                (200, {"issues": []}),
                (200, {"issues": [self.issue("KAN-13")]}),
            ]},
            state=FakeState(ids={task_id("KAN-13")},
                            unfinished_rows=[stranded_row("KAN-13")]),
        )

        with self.assertLogs("claudeloop", level="WARNING"):
            self.assertEqual(source.pending(), [])

    def test_a_failing_recovery_query_says_which_query_failed(self):
        # Two queries run per poll. "could not read the Jira backlog" when it
        # was the recovery that failed sends an operator to the wrong config
        # line at 3am.
        source = self.source(
            {f"POST {SEARCH_PATH}": [
                (200, last_page()),
                (500, {"errorMessages": ["boom"]}),
            ]},
            state=FakeState(unfinished_rows=[stranded_row("KAN-13")]),
        )

        with self.assertLogs("claudeloop", level="WARNING") as logs:
            source.pending()

        self.assertIn("recovery query for stranded work", "".join(logs.output))
        self.assertNotIn("could not read the Jira backlog", "".join(logs.output))

    def test_a_failing_backlog_query_still_names_the_backlog(self):
        source = self.source({f"POST {SEARCH_PATH}": (500, {"errorMessages": ["x"]})})

        with self.assertLogs("claudeloop", level="WARNING") as logs:
            source.pending()

        self.assertIn("could not read the Jira backlog", "".join(logs.output))

    def test_a_failing_recovery_query_leaves_the_backlog_result_intact(self):
        source = self.source(
            {f"POST {SEARCH_PATH}": [
                (200, last_page()),
                (500, {"errorMessages": ["boom"]}),
            ]},
            state=FakeState(unfinished_rows=[stranded_row("KAN-13")]),
        )

        with self.assertLogs("claudeloop", level="WARNING"):
            tasks = source.pending()

        self.assertEqual([t.source_ref for t in tasks], ["OPS-1", "OPS-2"])

    def test_pending_follows_the_next_page_token(self):
        # One page of 50 was all pending() ever read, so an ordering that put
        # wanted work past the 50th row never reached the loop at all.
        source = self.source({f"POST {SEARCH_PATH}": [
            (200, {"issues": [self.issue("OPS-1")], "nextPageToken": "page-2"}),
            (200, {"issues": [self.issue("OPS-2")], "isLast": True}),
        ]})

        self.assertEqual([t.source_ref for t in source.pending()], ["OPS-1", "OPS-2"])

    def test_the_page_token_is_sent_back_on_the_next_request(self):
        source = self.source({f"POST {SEARCH_PATH}": [
            (200, {"issues": [self.issue("OPS-1")], "nextPageToken": "page-2"}),
            (200, {"issues": [self.issue("OPS-2")]}),
        ]})

        source.pending()

        payloads = [payload for _, _, payload in self.fake.requests]
        self.assertNotIn("nextPageToken", payloads[0])
        self.assertEqual(payloads[1]["nextPageToken"], "page-2")

    def test_a_response_without_a_token_is_the_last_page(self):
        # An instance that does not paginate this way answers exactly as it
        # always did, in one request.
        source = self.source({f"POST {SEARCH_PATH}": (200, last_page())})

        source.pending()

        self.assertEqual(len(self.fake.requests), 1)

    def test_pagination_is_bounded(self):
        # A token that never stops coming -- a bug at either end -- must not
        # loop forever on a box nobody is watching.
        source = self.source({f"POST {SEARCH_PATH}":
                              (200, {"issues": [self.issue("OPS-1")],
                                     "nextPageToken": "always-more"})})

        with self.assertLogs("claudeloop", level="WARNING") as logs:
            source.pending()

        self.assertEqual(len(self.fake.requests), jira.MAX_PAGES)
        self.assertIn("more pages", "".join(logs.output))

    def test_a_page_that_fails_keeps_the_pages_already_read(self):
        # Partial work beats none: the loop can start on what did arrive, and
        # the rest is offered on the next poll.
        source = self.source({f"POST {SEARCH_PATH}": [
            (200, {"issues": [self.issue("OPS-1")], "nextPageToken": "page-2"}),
            (500, {"errorMessages": ["boom"]}),
            (500, {"errorMessages": ["boom"]}),
            (500, {"errorMessages": ["boom"]}),
        ]})

        with self.assertLogs("claudeloop", level="WARNING"):
            self.assertEqual([t.source_ref for t in source.pending()], ["OPS-1"])

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
        # The label failing must not stop the comment: the label write and
        # the comment are independent, and only the label is load-bearing.
        self.assertIn(("POST", "/issue/OPS-1/comment"),
                       [(m, p) for m, p, _ in self.fake.requests])

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

    def test_pending_survives_a_non_dict_json_body(self):
        # Same shape as an SSO interstitial answering 200 with JSON that
        # isn't Jira's: JiraClient now folds it to {}, and pending() must
        # treat that like an empty backlog rather than raising.
        source = self.source({f"POST {SEARCH_PATH}": (200, ["nope"])})
        self.assertEqual(source.pending(), [])

    def test_pending_survives_a_malformed_payload(self):
        source = self.source({f"POST {SEARCH_PATH}": (200, {"issues": "not a list"})})
        with self.assertLogs("claudeloop", level="WARNING"):
            self.assertEqual(source.pending(), [])

    def test_pending_skips_an_issue_of_the_wrong_shape(self):
        source = self.source({f"POST {SEARCH_PATH}": (200, {"issues": [
            "junk", {"key": "OPS-7", "fields": None}]})})
        self.assertEqual([t.source_ref for t in source.pending()], ["OPS-7"])

    def test_pending_survives_a_database_that_will_not_answer(self):
        # a state whose every read raises sqlite3.Error
        class BrokenState:
            def terminal_ids(self):
                raise sqlite3.OperationalError("database is locked")

            def unfinished(self):
                raise sqlite3.OperationalError("database is locked")
        source = self.source({f"POST {SEARCH_PATH}": (200, last_page())},
                             state=BrokenState())
        with self.assertLogs("claudeloop", level="WARNING"):
            self.assertEqual(len(source.pending()), 2)

    def test_a_non_dict_entry_among_offered_transitions_does_not_raise(self):
        # A transitions payload with one malformed entry alongside a valid
        # one must still find and use the valid one -- an AttributeError
        # here would escape past `except JiraError` in _transition, out of
        # mark(), after the true verdict was already written, and get
        # overwritten by main_loop's crash handler with status 'failed' and
        # cost 0.0.
        source = self.source({
            "PUT /issue/OPS-1": (204, {}),
            "POST /issue/OPS-1/comment": (201, {}),
            "GET /issue/OPS-1/transitions": (200, {"transitions": [
                "not a dict", {"id": "31", "name": "Done"}]}),
            "POST /issue/OPS-1/transitions": (204, {}),
        }, transition_done="Done")
        task = Task(task_id("OPS-1"), "OPS-1: t", "jira", "OPS-1")
        source.mark(task, "done", "went fine", 0.5)
        _, _, move = self.fake.requests[-1]
        self.assertEqual(move, {"transition": {"id": "31"}})

    def test_a_transition_without_an_id_does_not_raise(self):
        source = self.source({
            "PUT /issue/OPS-1": (204, {}),
            "POST /issue/OPS-1/comment": (201, {}),
            "GET /issue/OPS-1/transitions": (200, {"transitions": [{"name": "Done"}]}),
        }, transition_done="Done")
        task = Task(task_id("OPS-1"), "OPS-1: t", "jira", "OPS-1")
        with self.assertLogs("claudeloop", level="WARNING"):
            source.mark(task, "done", "went fine")

    POLISH_TRANSITIONS = [
        {"id": "21", "name": "W toku",
         "to": {"name": "W toku", "statusCategory": {"key": "indeterminate"}}},
        {"id": "31", "name": "Gotowe",
         "to": {"name": "Gotowe", "statusCategory": {"key": "done"}}},
    ]

    def test_a_status_category_key_moves_a_localised_workflow(self):
        # The live failure, fixed: `done` is Jira's own vocabulary and is
        # never translated, so it works on a board whose statuses display in
        # Polish without the operator transcribing them.
        source = self.source({
            "PUT /issue/OPS-1": (204, {}),
            "POST /issue/OPS-1/comment": (201, {}),
            "GET /issue/OPS-1/transitions": (200,
                                             {"transitions": self.POLISH_TRANSITIONS}),
            "POST /issue/OPS-1/transitions": (204, {}),
        }, transition_done="done")
        task = Task(task_id("OPS-1"), "OPS-1: t", "jira", "OPS-1")

        source.mark(task, "done", "went fine", 0.5)

        _, _, move = self.fake.requests[-1]
        self.assertEqual(move, {"transition": {"id": "31"}})

    def test_a_transition_id_moves_the_issue(self):
        source = self.source({
            "GET /issue/OPS-1/transitions": (200,
                                             {"transitions": self.POLISH_TRANSITIONS}),
            "POST /issue/OPS-1/transitions": (204, {}),
        }, transition_start="21")

        source.start(Task(task_id("OPS-1"), "OPS-1: t", "jira", "OPS-1"))

        _, _, move = self.fake.requests[-1]
        self.assertEqual(move, {"transition": {"id": "21"}})

    def test_an_ambiguous_match_moves_nothing_and_names_the_candidates(self):
        # A board's bin sits in the `done` category alongside the real
        # finished status. Picking the first match could bin a finished
        # ticket, so this refuses and tells the operator what to configure
        # instead.
        source = self.source({
            "PUT /issue/OPS-1": (204, {}),
            "POST /issue/OPS-1/comment": (201, {}),
            "GET /issue/OPS-1/transitions": (200, {"transitions": [
                {"id": "31", "name": "Gotowe",
                 "to": {"name": "Gotowe", "statusCategory": {"key": "done"}}},
                {"id": "61", "name": "Kosz",
                 "to": {"name": "Kosz", "statusCategory": {"key": "done"}}},
            ]}),
        }, transition_done="done")
        task = Task(task_id("OPS-1"), "OPS-1: t", "jira", "OPS-1")

        with self.assertLogs("claudeloop", level="WARNING") as logs:
            source.mark(task, "done", "went fine")

        text = "".join(logs.output)
        self.assertIn("Gotowe", text)
        self.assertIn("Kosz", text)
        self.assertIn("ambiguous", text)
        self.assertNotIn(("POST", "/issue/OPS-1/transitions"),
                         [(m, p) for m, p, _ in self.fake.requests])

    def test_the_unmatched_warning_shows_what_to_configure(self):
        # Listing bare names was what made the live failure hard to act on:
        # the operator could see `Gotowe` but not that `done` would reach it.
        source = self.source({
            "PUT /issue/OPS-1": (204, {}),
            "POST /issue/OPS-1/comment": (201, {}),
            "GET /issue/OPS-1/transitions": (200,
                                             {"transitions": self.POLISH_TRANSITIONS}),
        }, transition_done="In Progress")
        task = Task(task_id("OPS-1"), "OPS-1: t", "jira", "OPS-1")

        with self.assertLogs("claudeloop", level="WARNING") as logs:
            source.mark(task, "done", "went fine")

        text = "".join(logs.output)
        self.assertIn("Gotowe", text)
        self.assertIn("done", text)
        self.assertIn("21", text)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)

    def configured(self, routes):
        self.fake = FakeJira(routes)
        self.addCleanup(self.fake.close)
        path = self.tmp / "config.toml"
        # A real config's site is always https:// (config.py now refuses
        # anything else). FakeJira only ever speaks plain http, so run_cli
        # below points the constructed client at the fake's real address --
        # this string just has to pass load_config's scheme check.
        path.write_text(
            f'repo = "{self.repo}"\n'
            'source = "jira"\n'
            "[jira]\n"
            'site = "https://fake.invalid"\n'
            'email = "me@example.com"\n'
            'token = "secret"\n'
            'jql = "project = OPS"\n'
        )
        path.chmod(0o600)
        return path

    def run_cli(self, args, stdin=""):
        out = io.StringIO()
        fake_url = self.fake.url

        def client_pointed_at_the_fake(site, email, token, *a, **kw):
            return JiraClient(fake_url, email, token, *a, **kw)

        with contextlib.redirect_stdout(out):
            with unittest.mock.patch("sys.stdin", io.StringIO(stdin)):
                with unittest.mock.patch(
                    "claudeloop.jira.JiraClient", client_pointed_at_the_fake
                ):
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

    def test_a_trailing_colon_on_the_key_is_stripped(self):
        # The prompt layer tells the session the key is the part before the
        # colon in the task text ("OPS-42: Fix the widget" -> OPS-42), but a
        # literal-minded session may still pass "OPS-1:" with the colon
        # attached. This must hit /issue/OPS-1, not /issue/OPS-1:.
        config = self.configured({
            "GET /issue/OPS-1": (200, fixture("issue")),
            "GET /issue/OPS-1/comment": (200, fixture("comments")),
        })
        code, _ = self.run_cli(["--config", str(config), "show", "OPS-1: "])
        self.assertEqual(code, 0)
        self.assertEqual(self.fake.requests[0][:2], ("GET", "/issue/OPS-1"))

    def test_comment_strips_a_trailing_colon_from_the_key_too(self):
        config = self.configured({"POST /issue/OPS-1/comment": (201, {})})
        code, _ = self.run_cli(["--config", str(config), "comment", "OPS-1:", "hello"])
        self.assertEqual(code, 0)
        self.assertEqual(self.fake.requests[0][1], "/issue/OPS-1/comment")

    def test_a_path_traversal_key_is_rejected_without_calling_jira(self):
        # If anything in front of Jira normalises dot segments, a key like
        # this could put a comment on a different issue than the one named.
        # It must never reach JiraClient at all.
        config = self.configured({})
        code, _ = self.run_cli(
            ["--config", str(config), "comment", "OPS-1/../../issue/OPS-2", "hello"])
        self.assertNotEqual(code, 0)
        self.assertEqual(self.fake.requests, [])

    def test_a_key_that_is_not_shaped_like_one_is_rejected(self):
        config = self.configured({})
        code, _ = self.run_cli(["--config", str(config), "show", "not a key"])
        self.assertNotEqual(code, 0)
        self.assertEqual(self.fake.requests, [])

    def test_an_empty_key_is_rejected(self):
        config = self.configured({})
        code, _ = self.run_cli(["--config", str(config), "show", ""])
        self.assertNotEqual(code, 0)
        self.assertEqual(self.fake.requests, [])


class QuestionCommentTest(unittest.TestCase):
    def test_it_carries_the_summary_and_teaches_the_reply_syntax(self):
        from claudeloop.jira import QUESTION_HEADING, QUESTION_MARKER, question_comment

        body = question_comment("I stopped.\n\nQuestion: which currency?", 0.25)

        self.assertTrue(body.startswith(QUESTION_HEADING))
        self.assertIn("Question: which currency?", body)
        self.assertIn(QUESTION_MARKER, body)
        self.assertIn("0.2500", body)
        # Jira wiki markup (REST v2) renders backticks literally rather than
        # as monospace, so a human copying the marker would carry a leading
        # backtick into their reply and never match. {{...}} is Jira's own
        # monospace syntax.
        self.assertIn("{{" + QUESTION_MARKER + "}}", body)
        self.assertNotIn("`" + QUESTION_MARKER + "`", body)

    def test_a_blocked_task_gets_the_question_comment_not_the_closing_one(self):
        fake = FakeJira({
            "PUT /issue/OPS-1": (204, {}),
            "POST /issue/OPS-1/comment": (201, {}),
        })
        self.addCleanup(fake.close)
        source = JiraSource(JiraClient(fake.url, "e@x", "t"), "project = OPS")

        source.mark(
            Task("abc", "OPS-1: thing", "jira", "OPS-1"),
            "blocked",
            "I stopped.\n\nQuestion: which currency?",
            0.25,
        )

        comments = [payload for method, path, payload in fake.requests
                    if path == "/issue/OPS-1/comment"]
        self.assertEqual(len(comments), 1)
        from claudeloop.jira import QUESTION_HEADING
        self.assertTrue(comments[0]["body"].startswith(QUESTION_HEADING))
        self.assertNotIn("finished this task", comments[0]["body"])

    def test_a_done_task_still_gets_the_closing_comment(self):
        fake = FakeJira({
            "PUT /issue/OPS-1": (204, {}),
            "POST /issue/OPS-1/comment": (201, {}),
        })
        self.addCleanup(fake.close)
        source = JiraSource(JiraClient(fake.url, "e@x", "t"), "project = OPS")

        source.mark(Task("abc", "OPS-1: thing", "jira", "OPS-1"), "done", "did it", 0.5)

        comments = [payload for method, path, payload in fake.requests
                    if path == "/issue/OPS-1/comment"]
        self.assertIn("finished this task", comments[0]["body"])

    def test_a_failed_task_gets_the_closing_comment(self):
        fake = FakeJira({
            "PUT /issue/OPS-1": (204, {}),
            "POST /issue/OPS-1/comment": (201, {}),
        })
        self.addCleanup(fake.close)
        source = JiraSource(JiraClient(fake.url, "e@x", "t"), "project = OPS")

        source.mark(Task("abc", "OPS-1: thing", "jira", "OPS-1"), "failed", "gave up", 0.5)

        comments = [payload for method, path, payload in fake.requests
                    if path == "/issue/OPS-1/comment"]
        self.assertIn("finished this task", comments[0]["body"])
        self.assertIn("*failed*", comments[0]["body"])


class ReopenTest(unittest.TestCase):
    def test_reopen_removes_only_the_blocked_label(self):
        from claudeloop.jira import BLOCKED_LABEL

        fake = FakeJira({"PUT /issue/OPS-1": (204, {})})
        self.addCleanup(fake.close)
        source = JiraSource(JiraClient(fake.url, "e@x", "t"), "project = OPS")

        source.reopen(Task("abc", "OPS-1: thing", "jira", "OPS-1"))

        puts = [payload for method, path, payload in fake.requests if method == "PUT"]
        self.assertEqual(puts, [{"update": {"labels": [{"remove": BLOCKED_LABEL}]}}])

    def test_reopen_survives_a_jira_that_refuses(self):
        fake = FakeJira({"PUT /issue/OPS-1": (403, {"errorMessages": ["nope"]})})
        self.addCleanup(fake.close)
        source = JiraSource(JiraClient(fake.url, "e@x", "t"), "project = OPS")

        with self.assertLogs("claudeloop", level="WARNING"):
            source.reopen(Task("abc", "OPS-1: thing", "jira", "OPS-1"))

    def test_an_issue_key_is_escaped_into_every_client_url(self):
        # add_label / remove_label / transitions / transition used to
        # interpolate the key raw. Only JiraSource passed keys, always
        # straight from Jira's own search results, so it was safe by
        # accident rather than by design. FakeJira never unquotes the path
        # it matches on, so an unescaped key would 404 here.
        fake = FakeJira({
            "PUT /issue/OPS%201%2Fx": (204, {}),
            "GET /issue/OPS%201%2Fx/transitions": (200, {"transitions": []}),
            "POST /issue/OPS%201%2Fx/transitions": (204, {}),
        })
        self.addCleanup(fake.close)
        client = JiraClient(fake.url, "e@x", "t")

        client.add_label("OPS 1/x", "l")
        client.remove_label("OPS 1/x", "l")
        client.transitions("OPS 1/x")
        client.transition("OPS 1/x", "31")

        self.assertEqual(
            [path for _, path, _ in fake.requests],
            ["/issue/OPS%201%2Fx"] * 2
            + ["/issue/OPS%201%2Fx/transitions"] * 2,
        )


class JiraAnswerTest(unittest.TestCase):
    task = Task("abc", "OPS-1: thing", "jira", "OPS-1")

    def source_for(self, *bodies: str) -> JiraSource:
        self.fake = FakeJira({
            "GET /issue/OPS-1/comment": (200, {
                "comments": [{"body": body} for body in bodies]
            }),
        })
        self.addCleanup(self.fake.close)
        return JiraSource(JiraClient(self.fake.url, "e@x", "t"), "project = OPS")

    def test_the_comment_read_is_bounded_to_the_newest_page(self):
        # This runs once per parked task per poll, indefinitely -- a parked
        # task never expires. Reading every comment on a long-lived ticket
        # every 30s is the payload half of that; the ordering below is what
        # keeps the newest ones in the page that is read.
        source = self.source_for("chatter")

        source.answer(self.task)

        path = self.fake.raw_paths[0]
        self.assertIn(f"maxResults={jira.ANSWER_COMMENTS}", path)
        self.assertIn("orderBy=-created", path)

    def test_a_newest_first_page_is_read_in_the_order_it_was_written(self):
        # orderBy=-created reverses the list, and the whole boundary rule --
        # our newest question, then the first marked comment after it --
        # depends on chronological order.
        from claudeloop.jira import QUESTION_HEADING

        # Stored order, oldest first -- the fake reverses it on the way out,
        # exactly as orderBy=-created makes Jira do.
        self.fake = FakeJira({
            "GET /issue/OPS-1/comment": (200, {
                "comments": [
                    {"id": "1001", "body": f"{QUESTION_HEADING}\n\nwhich currency?"},
                    {"id": "1002", "body": "claudeloop: use EUR"},
                    {"id": "1003", "body": f"{QUESTION_HEADING}\n\nwhich rounding?"},
                    {"id": "1004", "body": "claudeloop: round half up"},
                ]
            }),
        })
        self.addCleanup(self.fake.close)
        source = JiraSource(JiraClient(self.fake.url, "e@x", "t"), "project = OPS")

        self.assertEqual(source.answer(self.task), "round half up")

    def test_a_marked_comment_after_the_question_is_the_answer(self):
        from claudeloop.jira import QUESTION_HEADING

        source = self.source_for(
            "some earlier chatter",
            f"{QUESTION_HEADING}\n\nQuestion: which currency?",
            "claudeloop: use EUR",
        )

        self.assertEqual(source.answer(self.task), "use EUR")

    def test_an_unmarked_comment_is_not_an_answer(self):
        from claudeloop.jira import QUESTION_HEADING

        source = self.source_for(
            f"{QUESTION_HEADING}\n\nQuestion: which currency?",
            "nice catch, I'll look into it",
        )

        self.assertIsNone(source.answer(self.task))

    def test_a_marked_comment_before_the_question_is_ignored(self):
        from claudeloop.jira import QUESTION_HEADING

        source = self.source_for(
            "claudeloop: this answers an older question",
            f"{QUESTION_HEADING}\n\nQuestion: which currency?",
        )

        self.assertIsNone(source.answer(self.task))

    def test_a_task_that_blocked_twice_reads_the_second_answer(self):
        from claudeloop.jira import QUESTION_HEADING

        source = self.source_for(
            f"{QUESTION_HEADING}\n\nQuestion: which currency?",
            "claudeloop: use EUR",
            f"{QUESTION_HEADING}\n\nQuestion: which rounding?",
            "claudeloop: round half up",
        )

        self.assertEqual(source.answer(self.task), "round half up")

    def test_no_question_comment_means_no_answer(self):
        source = self.source_for("claudeloop: an answer to nothing")

        self.assertIsNone(source.answer(self.task))

    def test_the_marker_alone_is_not_an_answer(self):
        from claudeloop.jira import QUESTION_HEADING

        source = self.source_for(
            f"{QUESTION_HEADING}\n\nQuestion: which currency?",
            "claudeloop:   ",
        )

        self.assertIsNone(source.answer(self.task))

    def test_an_unreachable_jira_means_no_answer_yet_not_a_raise(self):
        fake = FakeJira({"GET /issue/OPS-1/comment": (500, {"errorMessages": ["boom"]})})
        self.addCleanup(fake.close)
        source = JiraSource(JiraClient(fake.url, "e@x", "t", retries=1), "project = OPS")

        with self.assertLogs("claudeloop", level="WARNING"):
            self.assertIsNone(source.answer(self.task))

    def test_a_comment_list_of_the_wrong_shape_is_warned_about_not_raised(self):
        fake = FakeJira({"GET /issue/OPS-1/comment": (200, {"comments": "nonsense"})})
        self.addCleanup(fake.close)
        source = JiraSource(JiraClient(fake.url, "e@x", "t"), "project = OPS")

        with self.assertLogs("claudeloop", level="WARNING") as logs:
            self.assertIsNone(source.answer(self.task))

        self.assertIn("OPS-1", logs.output[0])
        self.assertIn("str", logs.output[0])
