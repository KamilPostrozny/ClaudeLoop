import asyncio
import contextlib
import io
import sqlite3
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from claudeloop.jira import (
    BLOCKED_LABEL, DONE_LABEL, GUARD, SEARCH_PATH, JiraClient, JiraError,
    JiraSource, closing_comment, compose_jql, main, task_text,
)
from claudeloop.source import Task, task_id
from claudeloop.state import State

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
        source = self.source({f"POST {SEARCH_PATH}": (200, fixture("search"))},
                             state=state)
        tasks = asyncio.run(asyncio.to_thread(source.pending))
        self.assertEqual([t.source_ref for t in tasks], ["OPS-2"])

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
        # a state whose terminal_ids() raises sqlite3.Error
        class BrokenState:
            def terminal_ids(self):
                raise sqlite3.OperationalError("database is locked")
        source = self.source({f"POST {SEARCH_PATH}": (200, fixture("search"))},
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
