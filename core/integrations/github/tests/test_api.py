"""The github integration's tests: the conformance suite and exchanges replayed offline (core/integrations/testing.py).

The cassettes are hand-built from GitHub's public REST API reference
(docs.github.com/en/rest, API version 2022-11-28): no live account was used.
The fake-service tests pin what the cassettes can't show in one place (headers,
path quoting, the rate limit, retries, argument checks, the watcher adapter).
"""

import base64

from core.integrations import framework, testing

REPO = "octo/shop"
API = "https://api.github.com"
LINK = '<https://api.github.com/repositories/1/issues?per_page=100&page=2>; rel="next"'


class GitHubConformance(testing.ConformanceSuite):
    integration = "github"
    ok_reply = {"json": {"number": 1, "title": "x"}}
    pages = [
        {"json": [{"number": 3}, {"number": 2}], "headers": {"Link": LINK}},
        {"json": [{"number": 1}]},
    ]
    page_items = [{"number": 3}, {"number": 2}, {"number": 1}]

    def call(self, module):
        return module.get_issue(REPO, 1)

    def paginate_call(self, module, limit=None):
        return module.list_issues(REPO, limit=limit)


class _GitHub(testing.IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.github = self.integration("github")
        self.conn = self.use_connection("github", provider="github")

    def _error(self, fn, *args, **kwargs):
        with self.assertRaises(self.integration_client.IntegrationError) as ctx:
            fn(*args, **kwargs)
        self.assertNoSecrets(str(ctx.exception))
        return ctx.exception


class GitHubCassetteTests(_GitHub):
    def test_list_issues_follows_the_link_header_and_leaves_out_pull_requests(self):
        with self.cassette("list_issues"):
            issues = list(self.github.list_issues(REPO, labels="bug"))
        self.assertEqual([i["number"] for i in issues], [12, 9, 4])
        self.assertEqual(issues[2]["user"]["login"], "ada")
        with self.cassette("list_issues"):
            everything = list(self.github.list_issues(REPO, labels=["bug"], pull_requests=True))
        self.assertEqual([i["number"] for i in everything], [12, 11, 9, 4])

    def test_get_issue(self):
        with self.cassette("get_issue"):
            issue = self.github.get_issue(REPO, 12)
        self.assertEqual((issue["title"], issue["assignees"][0]["login"]), ("Checkout fails on Safari", "ada"))
        self.assertEqual([label["name"] for label in issue["labels"]], ["bug"])

    def test_create_an_issue_then_comment_on_it(self):
        with self.cassette("create_issue"):
            issue = self.github.create_issue(REPO, "Nightly check failed", "See the run log in PyRun.",
                                             labels=["bug", "nightly-check"])
            comment = self.github.add_comment(REPO, issue["number"], "Assigning to the on-call person.")
        self.assertEqual((issue["number"], issue["html_url"]), (21, "https://github.com/octo/shop/issues/21"))
        self.assertTrue(comment["html_url"].endswith("#issuecomment-1508000001"))

    def test_close_an_issue_and_clear_labels(self):
        with self.cassette("update_issue"):
            closed = self.github.update_issue(REPO, 12, state="closed", state_reason="completed")
            cleared = self.github.update_issue(REPO, 9, labels=[])
        self.assertEqual((closed["state"], closed["state_reason"]), ("closed", "completed"))
        self.assertEqual(cleared["labels"], [])

    def test_list_comments_page_by_page(self):
        with self.cassette("list_comments"):
            comments = [c["body"] for c in self.github.list_comments(REPO, 12)]
        self.assertEqual(comments, ["Seen on 18.1 too.", "Fixed in #11."])

    def test_get_file_text_bytes_large_and_a_directory(self):
        with self.cassette("get_file"):
            flags = self.github.get_file(REPO, "config/flags.json")
            logo = self.github.get_file(REPO, "docs/logo.png", ref="v2.5", binary=True)
            big = self.github.get_file(REPO, "data/big.csv")
            with self.assertRaisesRegex(ValueError, "is a directory"):
                self.github.get_file(REPO, "docs")
            with self.assertRaisesRegex(ValueError, "binary=True"):
                self.github.get_file(REPO, "docs/logo.png", ref="v2.5")
        self.assertEqual(flags["content"], '{\n  "new_checkout": false\n}\n')
        self.assertEqual((flags["sha"], flags["path"]), ("3d21ec53a331a6f037a91c368710b99387d012c1", "config/flags.json"))
        self.assertTrue(logo["content"].startswith(b"\x89PNG"))
        self.assertEqual(big["content"], "row\n" * 8)

    def test_put_file_replaces_with_the_current_sha_or_creates(self):
        with self.cassette("put_file"):
            saved = self.github.put_file(REPO, "config/flags.json", '{\n  "new_checkout": true\n}\n',
                                         "Turn on new_checkout")
            created = self.github.put_file(REPO, "notes/2026-11-11.md", b"# 11 November\n", "Daily notes",
                                           branch="reports")
        self.assertEqual(saved["commit"]["message"], "Turn on new_checkout")
        self.assertEqual(created["content"]["path"], "notes/2026-11-11.md")

    def test_dispatch_on_the_default_branch_or_a_given_ref(self):
        with self.cassette("dispatch_workflow"):
            self.assertIsNone(self.github.dispatch_workflow(REPO, "deploy.yml", inputs={"environment": "staging"}))
            self.assertIsNone(self.github.dispatch_workflow(REPO, 161335, "v2.5"))

    def test_list_workflow_runs_of_one_workflow_or_all(self):
        with self.cassette("list_workflow_runs"):
            runs = list(self.github.list_workflow_runs(REPO, "deploy.yml", event="workflow_dispatch", limit=2))
            every = list(self.github.list_workflow_runs(REPO))
        self.assertEqual([(r["status"], r["conclusion"]) for r in runs], [("in_progress", None), ("completed", "success")])
        self.assertEqual(len(every), 1)

    def test_new_issues_marks_then_reports_each_new_issue_once(self):
        with self.cassette("new_issues"):
            first, state = self.github.new_issues(REPO, None)
            self.assertEqual((first, state), ([], {"last_number": 14}))
            second, state = self.github.new_issues(REPO, state)
            # Pull requests share the numbers, so #16 moves the mark but isn't reported; #15 is closed already.
            self.assertEqual(([i["number"] for i in second], state), ([15, 17], {"last_number": 17}))
            third, state = self.github.new_issues(REPO, state, limit=1)
            self.assertEqual(([i["number"] for i in third], state), ([18], {"last_number": 18}))
            fourth, state = self.github.new_issues(REPO, state)
        self.assertEqual(([i["number"] for i in fourth], state), ([19], {"last_number": 19}))

    def test_errors_keep_githubs_message_and_the_rate_limit_is_rate_limited(self):
        with self.cassette("errors"):
            missing = self._error(self.github.get_issue, "octo/missing", 1)
            invalid = self._error(self.github.create_issue, REPO, "")
            limited = self._error(self.github.get_issue, REPO, 12)
            no_trigger = self._error(self.github.dispatch_workflow, REPO, "deploy.yml", "main")
        self.assertEqual((missing.code, missing.status), ("NOT_FOUND", 404))
        self.assertIn("Not Found", str(missing))
        self.assertEqual((invalid.code, invalid.status), ("INVALID_REQUEST", 422))
        self.assertIn("Validation Failed", str(invalid))
        self.assertEqual((limited.code, limited.status), ("RATE_LIMITED", 403))
        self.assertGreater(limited.retry_after, 0)
        self.assertIn("API rate limit exceeded", str(limited))
        self.assertIn("'workflow_dispatch' trigger", str(no_trigger))


class GitHubRequestTests(_GitHub):
    def setUp(self):
        super().setUp()
        self.fake = self.fake_http(API)

    def test_the_token_and_api_version_go_to_api_github_com(self):
        self.fake.add("GET", "/repos/octo/shop/issues/7", json={"number": 7})
        self.github.get_issue(REPO, "7")
        sent = self.fake.requests[0]
        self.assertEqual(sent.url, f"{API}/repos/octo/shop/issues/7")
        self.assertEqual(sent.headers["Authorization"], f"Bearer {self.conn.credentials['access_token']}")
        self.assertEqual((sent.headers["Accept"], sent.headers["X-GitHub-Api-Version"]),
                         ("application/vnd.github+json", "2022-11-28"))

    def test_paths_are_quoted_and_repos_checked(self):
        self.fake.add("GET", "/repos/o%20o/s.p/contents/dir%20one/a%23b.txt",
                      json={"type": "file", "encoding": "base64", "content": base64.b64encode(b"x").decode()})
        self.assertEqual(self.github.get_file("o o/s.p", "/dir one//a#b.txt")["content"], "x")
        for bad in ("octo", "octo/", "/shop", "a/b/c", ""):
            with self.subTest(bad), self.assertRaises(ValueError):
                self.github.get_issue(bad, 1)
        with self.assertRaises(ValueError):
            self.github.get_file(REPO, "/")
        with self.assertRaises(ValueError):
            self.github.update_issue(REPO, 1)
        self.assertEqual(len(self.fake.requests), 1)

    def test_put_file_with_a_sha_skips_the_lookup_and_is_not_repeated_after_a_5xx(self):
        self.fake.add("PUT", "/repos/octo/shop/contents/a.txt", status=502)
        error = self._error(self.github.put_file, REPO, "a.txt", "hi", "msg", sha="abc")
        self.assertEqual((error.code, len(self.fake.requests)), ("SERVER_ERROR", 1))
        self.assertEqual(self.fake.requests[0].json(), {"message": "msg", "content": "aGk=", "sha": "abc"})

    def test_a_secondary_rate_limit_with_retry_after_is_rate_limited(self):
        self.fake.add("POST", "/repos/octo/shop/issues", status=403, headers={"Retry-After": "60"},
                      json={"message": "You have exceeded a secondary rate limit."})
        error = self._error(self.github.create_issue, REPO, "x")
        self.assertEqual((error.code, error.retry_after), ("RATE_LIMITED", 60.0))
        # A plain 403 stays FORBIDDEN.
        self.fake.add("POST", "/repos/octo/shop/issues", status=403, json={"message": "Resource not accessible"})
        self.assertEqual(self._error(self.github.create_issue, REPO, "x").code, "FORBIDDEN")

    def test_a_rate_limit_while_paging_is_rate_limited_too(self):
        self.fake.add("GET", "/repos/octo/shop/issues", json=[{"number": 2}], headers={"Link": LINK})
        self.fake.add("GET", "/repositories/1/issues", status=403,
                      headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1"},
                      json={"message": "API rate limit exceeded"})
        found = []
        with self.assertRaises(self.integration_client.IntegrationError) as ctx:
            for issue in self.github.list_issues(REPO):
                found.append(issue["number"])
        self.assertEqual((found, ctx.exception.code, ctx.exception.retry_after), ([2], "RATE_LIMITED", 0.0))

    def test_the_limit_counts_issues_not_pull_requests(self):
        self.fake.add("GET", "/repos/octo/shop/issues",
                      json=[{"number": 3, "pull_request": {}}, {"number": 2}, {"number": 1}])
        self.assertEqual([i["number"] for i in self.github.list_issues(REPO, limit=2)], [2, 1])
        self.assertEqual(list(self.github.list_issues(REPO, limit=0)), [])
        self.assertEqual(len(self.fake.requests), 1)

    def test_new_issues_on_an_empty_repository_and_a_zero_limit(self):
        self.fake.add("GET", "/repos/octo/shop/issues", json=[])
        self.assertEqual(self.github.new_issues(REPO, None), ([], {"last_number": 0}))
        self.assertEqual(self.fake.requests[0].params, {"state": "all", "sort": "created", "direction": "desc",
                                                        "per_page": "1"})
        self.assertEqual(self.github.new_issues(REPO, {"last_number": 5}, limit=0), ([], {"last_number": 5}))
        self.fake.add("GET", "/repos/octo/shop/issues", json=[{"number": 1, "id": 11}])
        issues, state = self.github.new_issues(REPO, {"last_number": 0}, labels=["bug", "p1"])
        self.assertEqual((len(issues), state), (1, {"last_number": 1}))
        self.assertEqual(self.fake.requests[-1].params["labels"], "bug,p1")


class GitHubTriggerTests(_GitHub):
    def test_github_new_issue_reports_each_issue_with_a_stable_id(self):
        (trigger,) = framework.load_triggers("github")
        self.assertEqual(trigger.key, "github_new_issue")
        self.assertEqual(trigger.config_schema["required"], ["repo"])
        config = {"repo": REPO, "labels": "bug, p1"}
        with self.cassette("new_issues", allow_unused=True):
            events, state = trigger.check(self.github, None, {"repo": REPO}, None)
            self.assertEqual((events, state), ([], {"last_number": 14}))
            events, state = trigger.check(self.github, None, {"repo": REPO}, state, limit=5)
        self.assertEqual([e["id"] for e in events], ["2000000015", "2000000017"])
        item = events[1]["item"]
        self.assertEqual((item["repo"], item["number"], item["title"], item["user"]),
                         (REPO, 17, "Refunds page 500s", "octocat"))
        self.assertEqual(events[0]["item"]["labels"], ["docs"])
        self.assertEqual(set(item), {"repo", "id", "number", "title", "body", "state", "html_url", "created_at",
                                     "user", "labels", "assignees"})
        fake = self.fake_http(API)
        fake.add("GET", "/repos/octo/shop/issues", json=[])
        trigger.check(self.github, None, config, None)
        self.assertEqual(fake.requests[0].params["labels"], "bug,p1")
