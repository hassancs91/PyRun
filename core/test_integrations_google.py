"""
Tests for the Google Sheets and Drive integrations (2.5 Stage 6, T28), beyond their own suites.

Each module's conformance suite, cassette and fake-service tests are in
core/integrations/google_<x>/tests/test_api.py. These cover what T28 added
around them:

- both are shipped (INTEGRATION_MODULES), their generated pages are current
  (the Sheets page names its API host) and help.search finds them;
- the shared client's ``base_url`` option: a module may send a connection's
  credentials to another https host of the same service (Sheets'
  sheets.googleapis.com for a Google connection at www.googleapis.com), and
  nowhere else; ``shared()`` keeps one client per option set;
- ``manage.py integration_smoke google_sheets|google_drive`` with a real
  Connection row (the service a fake): read-only calls, PASS and a masked FAIL;
- both modules work with the standard library alone (no ``requests``).
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
from core.integrations.google_sheets.api import SAMPLE_SPREADSHEET
from core.models import Connection
from core.services import help_docs
from core.test_connections import _Fixture

GOOGLE_TOKEN = "ya29.t28-google-access-token-0001"
SHEETS = "https://sheets.googleapis.com"


class RegistryTests(SimpleTestCase):
    def test_both_are_shipped_with_current_pages(self):
        self.assertEqual(framework.INTEGRATION_MODULES[3:5], ("google_sheets", "google_drive"))
        for key in ("google_sheets", "google_drive"):
            with self.subTest(key):
                integration = framework.load(key)
                self.assertEqual(integration.provider, "google")
                self.assertIn(f"from pyrunner_integrations import {key}", integration.example)
                page = docs.path_for(integration).read_text(encoding="utf-8")
                self.assertEqual(page, docs.render(integration), "run manage.py integration_docs")
                for name in integration.functions:
                    self.assertIn(f"### {name}\n", page)

    def test_each_page_says_where_the_credentials_go(self):
        sheets = docs.render(framework.load("google_sheets"))
        self.assertIn(f"The credentials go only to `{SHEETS}`; a redirect elsewhere", sheets)
        for key in ("google_drive", "airtable"):
            with self.subTest(key):
                self.assertIn("The credentials go only to the connection's base URL;",
                              docs.render(framework.load(key)))

    def test_help_search_finds_each_page(self):
        index = help_docs.build_index(Path(settings.BASE_DIR))
        for query, page in (("google sheets append rows", "docs/integrations/google_sheets.md"),
                            ("upload a file to google drive and share a link", "docs/integrations/google_drive.md")):
            with self.subTest(query):
                results = help_docs.search(query, index=index)["results"]
                self.assertIn(page, [r["doc"] for r in results[:3]], results[:3])


class SiblingBaseTests(testing.IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.c = self.integration_client
        self.conn = self.use_connection("google", provider="google")

    def test_a_sibling_https_host_gets_the_credentials(self):
        fake = self.fake_http(SHEETS)
        fake.add("GET", "/v4/x", json={"ok": 1})
        client = self.c.Client("google", provider="google", base_url="https://Sheets.googleapis.com/")
        self.assertEqual(client.get("/v4/x"), {"ok": 1})
        self.assertEqual(fake.requests[0].url, f"{SHEETS}/v4/x")
        self.assertEqual(fake.requests[0].headers["Authorization"], f"Bearer {self.conn.credentials['access_token']}")

    def test_anything_else_is_refused_before_a_request(self):
        fake = self.fake_http(SHEETS)
        for url in ("http://sheets.googleapis.com", "https://evil.example.com", "https://googleapis.com.evil.io",
                    "https://xgoogleapis.com", "https://sheets.googleapis.com/v4", "https://u:p@sheets.googleapis.com",
                    "https://sheets.googleapis.com?x=1", "sheets.googleapis.com"):
            with self.subTest(url=url):
                with self.assertRaisesRegex(ValueError, "isn't another https host of the connection's service"):
                    self.c.Client("google", provider="google", base_url=url).get("/v4/x")
        self.assertEqual(fake.requests, [])

    def test_a_connection_on_a_bare_domain_has_no_siblings(self):
        self.use_connection("api", auth_kind="bearer", config={"base_url": "https://example.com"})
        with self.assertRaises(ValueError):
            self.c.Client("api", base_url="https://www.example.com").get("/x")
        self.assertEqual(self.c.sibling_base("https://api.example.com/v1", "https://files.example.com"),
                         "https://files.example.com")

    def test_shared_keeps_one_client_per_option_set(self):
        sheets = self.integration("google_sheets")
        drive = self.integration("google_drive")
        self.assertIs(sheets._api(None), sheets._api(None))
        self.assertIsNot(sheets._api(None), drive._api(None))
        self.assertEqual((sheets._api(None).base_url, drive._api(None).base_url), (SHEETS, None))
        fake = self.fake_http()
        fake.add("GET", f"{SHEETS}/v4/spreadsheets/s1", json={"sheets": []})
        fake.add("GET", "/drive/v3/files/f1", json={"id": "f1"})
        sheets.list_sheets("s1")
        drive.get_file("f1")
        self.assertEqual([r.url.split("?")[0] for r in fake.requests],
                         [f"{SHEETS}/v4/spreadsheets/s1", "https://www.googleapis.com/drive/v3/files/f1"])


class SmokeTests(testing.IntegrationTestMixin, _Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self._connection(name="google", provider="google", auth_kind="oauth2",
                         credentials={"access_token": GOOGLE_TOKEN})

    def _smoke(self, key):
        out = io.StringIO()
        call_command("integration_smoke", key, stdout=out)
        return out.getvalue()

    def test_sheets_smoke_reads_googles_sample_spreadsheet(self):
        fake = self.fake_http(SHEETS)
        fake.add("GET", f"/v4/spreadsheets/{SAMPLE_SPREADSHEET}",
                 json={"sheets": [{"properties": {"sheetId": 0, "title": "Class Data"}}]})
        fake.add("GET", f"/v4/spreadsheets/{SAMPLE_SPREADSHEET}/values/%27Class%20Data%27%21A1%3AF3",
                 json={"range": "'Class Data'!A1:F3", "values": [["Student Name"], ["Alexandra"], ["Andrew"]]})
        out = self._smoke("google_sheets")
        self.assertIn("PASS read: list_sheets answered with 1 sheet(s); read_range answered with 3 row(s)", out)
        self.assertEqual([r.method for r in fake.requests], ["GET", "GET"])
        self.assertEqual(fake.requests[0].headers["Authorization"], f"Bearer {GOOGLE_TOKEN}")

    def test_drive_smoke_lists_files_and_a_failure_is_masked(self):
        fake = self.fake_http("https://www.googleapis.com")
        fake.add("GET", "/drive/v3/files", json={"files": [{"id": "f1"}]})
        self.assertIn("PASS list_files: list_files answered with 1 file(s)", self._smoke("google_drive"))
        self.assertEqual(fake.requests[0].params["pageSize"], "5")
        fake.default(status=403, json=testing.echo)
        out = io.StringIO()
        with self.assertRaises(CommandError):
            call_command("integration_smoke", "google_drive", stdout=out)
        self.assertIn("FAIL list_files: FORBIDDEN", out.getvalue())
        self.assertNotIn(GOOGLE_TOKEN, out.getvalue())

    def test_skips_without_a_google_connection(self):
        Connection.objects.filter(provider="google").delete()
        for key in ("google_sheets", "google_drive"):
            with self.subTest(key):
                self.assertIn("SKIP: no google connection", self._smoke(key))


STDLIB_ONLY = r"""
import json, sys
sys.modules["requests"] = None          # as in an environment without requests
sys.path.insert(0, sys.argv[1])
import pyrunner_connections
from pyrunner_integrations import _client, google_drive, google_sheets

MATERIAL = {"base_url": "https://www.googleapis.com", "headers": {"Authorization": "Bearer ya29-x"}, "params": {}}
pyrunner_connections.credentials = lambda name, refused=False: MATERIAL
seen = []

def transport(method, url, headers, body, timeout):
    seen.append([method, url.split("?")[0], headers.get("Authorization")])
    if "sheets" in url:
        reply = {"range": "Leads!A1:B2", "values": [["Name"], ["Ada"]]}
    else:
        reply = {"files": [{"id": "f1"}]}
    return _client.Response(200, {"content-type": "application/json"}, json.dumps(reply).encode(), url)

_client.TRANSPORT = transport
out = {
    "found": google_sheets.find_rows("s1", "Leads", {"Name": "Ada"}),
    "files": [f["id"] for f in google_drive.list_files()],
    "uploaded": google_drive.upload(b"hi", name="hi.txt"),
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
        self.assertEqual((out["found"], out["files"], out["requests"]),
                         ([{"row": 2, "values": {"Name": "Ada"}}], ["f1"], False))
        self.assertEqual(out["seen"], [
            ["GET", "https://sheets.googleapis.com/v4/spreadsheets/s1/values/Leads", "Bearer ya29-x"],
            ["GET", "https://www.googleapis.com/drive/v3/files", "Bearer ya29-x"],
            ["POST", "https://www.googleapis.com/upload/drive/v3/files", "Bearer ya29-x"],
        ])
