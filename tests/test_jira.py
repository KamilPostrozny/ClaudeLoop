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
