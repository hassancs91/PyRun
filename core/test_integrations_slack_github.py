"""
Tests for the Slack and GitHub integrations (2.5 Stage 6, T30), beyond their own suites.

Each module's conformance suite and cassette tests are in
core/integrations/<key>/tests/test_api.py. These cover what T30 added around
them:

- both are shipped (INTEGRATION_MODULES), their generated pages are current
  (Slack's errors section explains ``"ok": false``) and help.search finds them;
- the shared client's ``check=`` re-auth: a 2xx reply the check calls
  UNAUTHORIZED gets new credentials and one retry, like an HTTP 401;
- ``github_new_issue`` is a watcher adapter (``framework.load_triggers``);
- ``manage.py integration_smoke slack|github`` with a real Connection row (the
  service a fake): read-only calls, PASS and a masked FAIL;
- both modules work with the standard library alone (no ``requests``), as a
  run's environment may be.
"""

import io
import json
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase

from core.integrations import docs, framework, testing
from core.models import Connection
from core.services import help_docs
from core.test_connections import _Fixture

SLACK_TOKEN = "xoxb-t30-slack-bot-token-0001"
GITHUB_TOKEN = "ghp_t30githubtoken000000000000000000002"


class RegistryTests(SimpleTestCase):
    def test_both_are_shipped_with_current_pages(self):
        self.assertEqual(framework.INTEGRATION_MODULES[-2:], ("slack", "github"))
        for key in ("slack", "github"):
            with self.subTest(key):
                integration = framework.load(key)
                self.assertEqual(integration.provider, key)
                self.assertIn(f"from pyrunner_integrations import {key}", integration.example)
                page = docs.path_for(integration).read_text(encoding="utf-8")
                self.assertEqual(page, docs.render(integration), "run manage.py integration_docs")
                for name in integration.functions:
                    self.assertIn(f"### {name}\n", page)

    def test_slacks_page_explains_ok_false_and_its_own_code(self):
        slack = docs.render(framework.load("slack"))
        self.assertIn('Slack answers most failures with HTTP 200 and `"ok": false`', slack)
        self.assertIn("| `SLACK_ERROR` |", slack)
        github = docs.render(framework.load("github"))
        self.assertNotIn("SLACK_ERROR", github)
        self.assertNotIn('"ok": false', github)

    def test_help_search_finds_each_page(self):
        index = help_docs.build_index(Path(settings.BASE_DIR))
        for query, page in (("slack post a message to a channel", "docs/integrations/slack.md"),
                            ("github create an issue and comment", "docs/integrations/github.md"),
                            ("dispatch a github actions workflow", "docs/integrations/github.md")):
            with self.subTest(query):
                results = help_docs.search(query, index=index)["results"]
                self.assertIn(page, [r["doc"] for r in results[:3]], results[:3])

    def test_github_offers_the_new_issue_watcher_and_slack_none(self):
        self.assertEqual([t.key for t in framework.load_triggers("github")], ["github_new_issue"])
        self.assertEqual(framework.load_triggers("slack"), ())


class CheckReauthTests(testing.IntegrationTestCase):
    """``Client(check=)``: a check that says UNAUTHORIZED for a 2xx reply is handled like a 401."""

    def setUp(self):
        super().setUp()
        self.c = self.integration_client
        self.conn = self.use_connection("api", auth_kind="bearer", config={"base_url": "https://api.example.test"})
        self.fake = self.fake_http()

    def _check(self, response, body):
        if body.get("ok") is False:
            raise self.c.IntegrationError("refused", body["error"], response.status_code, response=response)

    def test_unauthorized_from_the_check_gets_new_credentials_and_one_retry(self):
        old = self.conn.credentials["token"]
        self.fake.add("POST", "/send", json={"ok": False, "error": "UNAUTHORIZED"})
        self.fake.add("POST", "/send", json={"ok": True})
        self.assertEqual(self.c.Client("api", check=self._check).post("/send", json={"a": 1}), {"ok": True})
        first, second = self.fake.requests
        self.assertEqual((first.headers["Authorization"], second.json()), (f"Bearer {old}", {"a": 1}))
        self.assertEqual(second.headers["Authorization"], f"Bearer {self.conn.credentials['token']}")

    def test_paged_lists_get_it_too(self):
        self.fake.add("GET", "/items", json={"ok": False, "error": "UNAUTHORIZED"})
        self.fake.add("GET", "/items", json={"ok": True, "items": [1], "next": None})
        client = self.c.Client("api", check=self._check)
        self.assertEqual(list(client.paginate("/items", cursor="next")), [1])
        self.assertEqual(len(self.fake.requests), 2)

    def test_other_codes_and_a_second_refusal_are_raised(self):
        self.fake.default(json={"ok": False, "error": "FORBIDDEN"})
        with self.assertRaises(self.c.IntegrationError) as ctx:
            self.c.Client("api", check=self._check).get("/x")
        self.assertEqual((ctx.exception.code, len(self.fake.requests)), ("FORBIDDEN", 1))
        fake = self.fake_http()
        fake.default(json={"ok": False, "error": "UNAUTHORIZED"})
        with self.assertRaises(self.c.IntegrationError):
            self.c.Client("api", check=self._check).get("/x")
        self.assertEqual(len(fake.requests), 2)
        self.assertEqual([refused for _name, refused in self.connections.calls].count(True), 1)


class SmokeTests(testing.IntegrationTestMixin, _Fixture, TestCase):
    def _smoke(self, key):
        out = io.StringIO()
        call_command("integration_smoke", key, stdout=out)
        return out.getvalue()

    def _fails(self, key):
        out = io.StringIO()
        with self.assertRaises(CommandError):
            call_command("integration_smoke", key, stdout=out)
        return out.getvalue()

    def test_slack_smoke_asks_who_it_is_then_lists_channels_when_allowed(self):
        self._connection(name="slack", provider="slack", auth_kind="oauth2", credentials={"access_token": SLACK_TOKEN})
        fake = self.fake_http("https://slack.com/api")
        fake.add("POST", "/auth.test", json={"ok": True, "team": "Acme", "user": "pyrun"},
                 headers={"X-OAuth-Scopes": "chat:write,files:write"})
        self.assertIn("PASS auth: auth.test answered for workspace 'Acme' with 2 scope(s)", self._smoke("slack"))
        fake.add("POST", "/auth.test", json={"ok": True, "team": "Acme"},
                 headers={"X-OAuth-Scopes": "chat:write,channels:read"})
        fake.add("GET", "/conversations.list", json={"ok": True, "channels": [{"id": "C1"}]})
        self.assertIn("list_channels answered with 1 channel(s)", self._smoke("slack"))
        self.assertEqual([r.method for r in fake.requests], ["POST", "POST", "GET"])
        self.assertEqual(fake.requests[0].headers["Authorization"], f"Bearer {SLACK_TOKEN}")
        self.assertEqual(fake.requests[2].params["limit"], "5")
        # A revoked token: Slack says so with HTTP 200.
        fake.default(json={"ok": False, "error": "token_revoked"})
        out = self._fails("slack")
        self.assertIn("FAIL auth: UNAUTHORIZED", out)
        self.assertNotIn(SLACK_TOKEN, out)

    def test_github_smoke_reads_the_public_sample_repository(self):
        self._connection(name="gh", provider="github", auth_kind="oauth2", credentials={"access_token": GITHUB_TOKEN})
        fake = self.fake_http("https://api.github.com")
        fake.add("GET", "/user", json={"login": "octocat"})
        fake.add("GET", "/repos/octocat/Hello-World/issues", json=[{"number": 3}, {"number": 2, "pull_request": {}}])
        fake.add("GET", "/repos/octocat/Hello-World/contents/README",
                 json={"type": "file", "encoding": "base64", "content": "SGVsbG8gV29ybGQhCg==", "size": 13})
        out = self._smoke("github")
        self.assertIn("PASS read: signed in as 'octocat'; list_issues answered with 1 issue(s) and get_file with "
                      "13 byte(s) of octocat/Hello-World", out)
        self.assertEqual([r.method for r in fake.requests], ["GET", "GET", "GET"])
        self.assertEqual(fake.requests[0].headers["Authorization"], f"Bearer {GITHUB_TOKEN}")
        self.assertEqual(fake.requests[0].headers["X-GitHub-Api-Version"], "2022-11-28")
        fake.default(status=401, json=testing.echo)
        out = self._fails("github")
        self.assertIn("FAIL read: UNAUTHORIZED", out)
        self.assertNotIn(GITHUB_TOKEN, out)

    def test_skips_without_a_connection(self):
        Connection.objects.filter(provider__in=("slack", "github")).delete()
        for key in ("slack", "github"):
            with self.subTest(key):
                self.assertIn(f"SKIP: no {key} connection", self._smoke(key))


STDLIB_ONLY = r"""
import json, sys
sys.modules["requests"] = None          # as in an environment without requests
sys.path.insert(0, sys.argv[1])
import pyrunner_connections
from pyrunner_integrations import _client, github, slack

MATERIAL = {
    "slack": {"base_url": "https://slack.com/api", "headers": {"Authorization": "Bearer xoxb-x"}, "params": {}},
    "github": {"base_url": "https://api.github.com", "headers": {"Authorization": "Bearer ghp-x"}, "params": {}},
}
pyrunner_connections.credentials = lambda name, refused=False: MATERIAL[name]
seen = []

def transport(method, url, headers, body, timeout):
    seen.append([method, url.split("?")[0], headers.get("Authorization")])
    if "files.slack.com" in url:
        return _client.Response(200, {"content-type": "text/plain"}, b"OK - 2", url)
    if "getUploadURLExternal" in url:
        reply = {"ok": True, "upload_url": "https://files.slack.com/upload/v1/x", "file_id": "F1"}
    elif "slack.com" in url:
        reply = {"ok": True, "channel": "C1", "ts": "1.2", "files": [{"id": "F1"}]}
    else:
        reply = [{"number": 1, "id": 11}]
    return _client.Response(200, {"content-type": "application/json"}, json.dumps(reply).encode(), url)

_client.TRANSPORT = transport
out = {
    "posted": slack.post_message("C1", "hi")["ts"],
    "uploaded": slack.upload_file(b"hi", "C1", filename="a.txt")["id"],
    "issues": [i["number"] for i in github.list_issues("octo/shop")],
    "requests": "requests" in sys.modules and sys.modules["requests"] is not None,
    "seen": seen,
}
print(json.dumps(out))
"""


class StdlibOnlyTests(SimpleTestCase):
    def test_both_modules_work_without_requests(self):
        helpers = str(Path(settings.BASE_DIR) / "core" / "script_helpers")
        result = subprocess.run([sys.executable, "-I", "-c", STDLIB_ONLY, helpers], capture_output=True, text=True,
                                timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual((out["posted"], out["uploaded"], out["issues"], out["requests"]), ("1.2", "F1", [1], False))
        self.assertEqual(out["seen"], [
            ["POST", "https://slack.com/api/chat.postMessage", "Bearer xoxb-x"],
            ["POST", "https://slack.com/api/files.getUploadURLExternal", "Bearer xoxb-x"],
            ["POST", "https://files.slack.com/upload/v1/x", None],
            ["POST", "https://slack.com/api/files.completeUploadExternal", "Bearer xoxb-x"],
            ["GET", "https://api.github.com/repos/octo/shop/issues", "Bearer ghp-x"],
        ])
