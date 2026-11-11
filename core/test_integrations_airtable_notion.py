"""
Tests for the Airtable and Notion integrations (2.5 Stage 6, T27), beyond their own suites.

Each module's conformance suite and cassette tests are in
core/integrations/<key>/tests/test_api.py. These cover what T27 added around
them:

- both are shipped (INTEGRATION_MODULES), their generated pages are current
  and help.search finds them;
- the shared client's ``rate_limit_wait`` (Airtable refuses for 30 s after a
  429 and sends no Retry-After), and the docs saying it;
- ``manage.py integration_smoke airtable|notion`` with a real Connection row
  (the service a fake): read-only calls, PASS and a masked FAIL;
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

NOTION_TOKEN = "ntn_t27notiontoken0001"
AIRTABLE_TOKEN = "patT27airtable.token0002"


class RegistryTests(SimpleTestCase):
    def test_both_are_shipped_with_current_pages(self):
        self.assertEqual(framework.INTEGRATION_MODULES[:3], ("http", "airtable", "notion"))
        for key in ("airtable", "notion"):
            with self.subTest(key):
                integration = framework.load(key)
                self.assertEqual(integration.provider, key)
                self.assertIn(f"from pyrunner_integrations import {key}", integration.example)
                page = docs.path_for(integration).read_text(encoding="utf-8")
                self.assertEqual(page, docs.render(integration), "run manage.py integration_docs")
                for name in integration.functions:
                    self.assertIn(f"### {name}\n", page)

    def test_help_search_finds_each_page(self):
        index = help_docs.build_index(Path(settings.BASE_DIR))
        for query, page in (("airtable find records by email", "docs/integrations/airtable.md"),
                            ("notion append blocks to a page", "docs/integrations/notion.md")):
            with self.subTest(query):
                results = help_docs.search(query, index=index)["results"]
                self.assertIn(page, [r["doc"] for r in results[:3]], results[:3])

    def test_the_errors_section_names_airtables_pause(self):
        airtable = docs.render(framework.load("airtable"))
        self.assertIn("or else 30 seconds after a 429 that doesn't say", airtable)
        for key in ("http", "notion"):
            with self.subTest(key):
                self.assertIn("or else 1, 2 or 4 seconds; other 5xx", docs.render(framework.load(key)))


class RateLimitWaitTests(testing.IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.c = self.integration_client
        self.use_connection("api", auth_kind="bearer", config={"base_url": "https://api.example.test"})
        self.fake = self.fake_http()

    def test_a_429_without_retry_after_waits_the_services_pause(self):
        client = self.c.Client("api", rate_limit_wait=30.0)
        self.fake.add("GET", "/x", status=429).add("GET", "/x", status=429, headers={"Retry-After": "2"})
        self.fake.add("GET", "/x", status=503).add("GET", "/x", json={"ok": 1})
        self.assertEqual(client.get("/x"), {"ok": 1})
        self.assertEqual(self.sleeps, [30.0, 2.0, 4.0], "the pause, then Retry-After, then the usual backoff")

    def test_without_it_the_backoff_is_unchanged(self):
        self.fake.default(status=429)
        with self.assertRaises(self.c.IntegrationError) as ctx:
            self.c.Client("api").get("/x")
        self.assertEqual((ctx.exception.code, self.sleeps), ("RATE_LIMITED", [1.0, 2.0, 4.0]))

    def test_shared_takes_client_options_when_it_makes_the_client(self):
        first = self.c.shared("api", "http", rate_limit_wait=5.0)
        self.assertIs(self.c.shared("api", "http", rate_limit_wait=5.0), first)
        self.assertEqual(first.rate_limit_wait, 5.0)
        self.assertIsNone(self.c.shared("other", "http").rate_limit_wait)

    def test_airtables_client_and_module_calls_use_the_pause(self):
        airtable = self.integration("airtable")
        self.assertEqual(airtable.client().rate_limit_wait, 30.0)
        self.use_connection("airtable", provider="airtable")
        fake = self.fake_http()
        fake.add("GET", "/v0/meta/bases/app1/tables", status=429)
        fake.add("GET", "/v0/meta/bases/app1/tables", json={"tables": []})
        self.assertEqual(airtable.list_tables("app1"), [])
        self.assertEqual(self.sleeps, [30.0])


class SmokeTests(testing.IntegrationTestMixin, _Fixture, TestCase):
    def _smoke(self, key, *args):
        out = io.StringIO()
        call_command("integration_smoke", key, *args, stdout=out)
        return out.getvalue()

    def test_airtable_smoke_reads_whoami_then_bases_when_allowed(self):
        self._connection(credentials={"api_key": AIRTABLE_TOKEN})
        fake = self.fake_http("https://api.airtable.com")
        fake.add("GET", "/v0/meta/whoami", json={"id": "usrT27", "scopes": ["data.records:read"]})
        self.assertIn("PASS whoami: whoami answered with 1 scope(s)", self._smoke("airtable"))
        fake.add("GET", "/v0/meta/whoami", json={"id": "usrT27", "scopes": ["schema.bases:read"]})
        fake.add("GET", "/v0/meta/bases", json={"bases": [{"id": "app1", "name": "CRM"}]})
        self.assertIn("list_bases answered with 1 base(s)", self._smoke("airtable"))
        self.assertEqual([r.method for r in fake.requests], ["GET", "GET", "GET"])
        self.assertEqual(fake.requests[0].headers["Authorization"], f"Bearer {AIRTABLE_TOKEN}")

    def test_notion_smoke_searches_and_a_failure_is_masked(self):
        self._connection(name="notes", provider="notion", auth_kind="oauth2",
                         credentials={"access_token": NOTION_TOKEN})
        fake = self.fake_http("https://api.notion.com")
        fake.add("POST", "/v1/search", json={"object": "list", "results": [{"id": "p1"}], "next_cursor": None})
        self.assertIn("PASS search: search answered with 1 page(s) or database(s)", self._smoke("notion"))
        self.assertEqual(fake.requests[0].json(), {"page_size": 5})
        self.assertEqual(fake.requests[0].headers["Notion-Version"], "2022-06-28")
        fake.default(status=401, json=testing.echo)
        out = io.StringIO()
        with self.assertRaises(CommandError):
            call_command("integration_smoke", "notion", stdout=out)
        self.assertIn("FAIL search: UNAUTHORIZED", out.getvalue())
        self.assertNotIn(NOTION_TOKEN, out.getvalue())

    def test_skips_without_a_connection(self):
        Connection.objects.filter(provider__in=("airtable", "notion")).delete()
        for key in ("airtable", "notion"):
            with self.subTest(key):
                self.assertIn(f"SKIP: no {key} connection", self._smoke(key))


STDLIB_ONLY = r"""
import json, sys
sys.modules["requests"] = None          # as in an environment without requests
sys.path.insert(0, sys.argv[1])
import pyrunner_connections
from pyrunner_integrations import _client, airtable, notion

MATERIAL = {
    "airtable": {"base_url": "https://api.airtable.com", "headers": {"Authorization": "Bearer pat-x"}, "params": {}},
    "notion": {"base_url": "https://api.notion.com",
               "headers": {"Authorization": "Bearer ntn-x", "Notion-Version": "2022-06-28"}, "params": {}},
}
pyrunner_connections.credentials = lambda name, refused=False: MATERIAL[name]
seen = []

def transport(method, url, headers, body, timeout):
    seen.append([method, url, json.loads(body) if body else None])
    reply = {"records": [{"id": "rec1", "fields": {"Name": "Ada"}}]} if "airtable" in url else \
        {"object": "list", "results": [{"id": "p1"}], "next_cursor": None}
    return _client.Response(200, {"content-type": "application/json"}, json.dumps(reply).encode(), url)

_client.TRANSPORT = transport
out = {
    "created": airtable.create_record("app1", "Contacts", {"Name": "Ada"})["id"],
    "listed": [r["id"] for r in airtable.list_records("app1", "Contacts")],
    "queried": [p["id"] for p in notion.query_database("db1")],
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
        self.assertEqual((out["created"], out["listed"], out["queried"], out["requests"]),
                         ("rec1", ["rec1"], ["p1"], False))
        self.assertEqual(out["seen"][0], ["POST", "https://api.airtable.com/v0/app1/Contacts",
                                          {"records": [{"fields": {"Name": "Ada"}}]}])
        self.assertEqual(out["seen"][2][:2], ["POST", "https://api.notion.com/v1/databases/db1/query"])
