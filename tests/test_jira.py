import unittest

from claudeloop.jira import (
    BLOCKED_LABEL, DONE_LABEL, GUARD, SEARCH_PATH, JiraClient, JiraError,
    closing_comment, compose_jql, task_text,
)

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
