"""
Connection-backed watcher adapters (2.5 Stage 7, ticket T32).

What this pins:

- the integrations' triggers (``sheets_new_row``, ``gmail_new_message``,
  ``github_new_issue``) are watcher adapters: offered by the registry after the
  built-ins, needing a connection of the integration's provider, checked
  through the integration's script-facing module with the WATCHER'S
  connection's credentials (never another workspace's connection of the same
  name), replayed from the integrations' own cassettes;
- ``imap_new_message`` against a real IMAP conversation on a local socket
  (read-only EXAMINE, BODY.PEEK, UID cursor, UIDVALIDITY change, search keys)
  and the new IMAP connection preset (Test, info, credentials);
- ``db_poll`` over the Databases seam: the wrapped read-only query, the cursor,
  rows sharing a cursor value, the workspace's database only; live on Postgres;
- every adapter starts exactly one run per new item across a worker that dies
  after queueing runs and before saving the cursor (the restart re-checks and
  the seen ring skips what already has a run);
- event items map onto script inputs through the input map.
"""

import datetime
import json
import re
import socketserver
import threading
import urllib.parse
import uuid
from pathlib import Path
from unittest import mock, skipUnless

from django.contrib.auth import get_user_model
from django.db import connection as db_connection
from django.test import TestCase, override_settings
from django.utils import timezone

from core.integrations import testing
from core.models import Connection, Database, Environment, Run, Script, Watcher, Workspace
from core.services import connections as connection_service
from core.services.database_explorer import DatabaseExplorerService, QueryResult
from core.triggers import registry, runner
from core.triggers.adapters import AdapterError, CheckContext, validate_config
from core.triggers.adapters import integration as integration_adapters
from core.triggers.adapters.db_poll import DbPollAdapter, _jsonable
from core.triggers.adapters.imap import ImapNewMessageAdapter

User = get_user_model()

INTEGRATIONS = Path(__file__).resolve().parent / "integrations"
GOOGLE_TOKEN = "ya29.t32-watcher-google-token-0001"
OTHER_TOKEN = "ya29.t32-other-workspace-token-0002"
GITHUB_TOKEN = "ghp_t32watchertoken000000000000000000001"
IMAP_PASSWORD = "t32-imap-app-password-0001"
SHEET_ID = "1ExampleSheetId0123456789abcdefghijklmnopqrs"
SHEETS = "https://sheets.googleapis.com"
GMAIL_ME = "https://gmail.googleapis.com/gmail/v1/users/me"


class Died(BaseException):
    """The worker dies (not an Exception: nothing in the runner catches it)."""


def _tasks():
    counter = iter(range(1, 10_000))
    return mock.patch("core.tasks.async_task", side_effect=lambda *a, **k: f"task-{next(counter)}")


def _inline():
    """The adapter called on the test's thread (an adapter reading the database can't see a TestCase's rows from
    the runner's thread)."""
    return mock.patch.object(runner, "_call_adapter",
                             side_effect=lambda adapter, ctx, config, cursor: adapter.check(ctx, config, cursor))


class _Fixture(testing.IntegrationTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.ws = Workspace.get_default() or Workspace.objects.create(name="Default", is_default=True)
        self.env = Environment.objects.create(name="t32env", path=f"t32env{uuid.uuid4().hex[:6]}")
        self.user = User.objects.create(email="t32@example.com", username=f"t32{uuid.uuid4().hex[:6]}")
        self.script = self._script()

    def _script(self, workspace=None, **kwargs):
        return Script.objects.create(
            name=f"w{uuid.uuid4().hex[:6]}", environment=self.env, workspace=workspace or self.ws,
            is_enabled=True, code="pass", **kwargs,
        )

    def _connection(self, name, provider, auth_kind, credentials, config=None, workspace=None, **kwargs):
        row = Connection(workspace=workspace or self.ws, name=name, provider=provider, auth_kind=auth_kind,
                         config=config or {}, created_by=self.user, **kwargs)
        row.set_credentials(credentials)
        row.save()
        return row

    def _google(self, token=GOOGLE_TOKEN, workspace=None, name="google"):
        return self._connection(name, "google", "oauth2", {"access_token": token}, workspace=workspace)

    def _watcher(self, adapter, connection=None, config=None, script=None, **kwargs):
        script = script or self.script
        return Watcher.objects.create(workspace=script.workspace, name=adapter, adapter=adapter, script=script,
                                      connection=connection, config=config or {}, created_by=self.user, **kwargs)

    def _check(self, watcher):
        with _tasks():
            result = runner.check_watcher(watcher.pk)
        Watcher.objects.filter(pk=watcher.pk).update(next_check_at=None)
        return result

    def _crash(self, watcher):
        """A check whose worker dies after queueing its runs, before the cursor is saved."""
        with _tasks(), mock.patch.object(runner, "_release", side_effect=Died):
            with self.assertRaises(Died):
                runner.check_watcher(watcher.pk)
        # The worker is restarted later: its claim has expired.
        Watcher.objects.filter(pk=watcher.pk).update(
            checking_since=timezone.now() - datetime.timedelta(seconds=runner.CLAIM_SECONDS + 1), next_check_at=None,
        )

    def _runs(self, script=None):
        return list(Run.objects.filter(script=script or self.script).order_by("created_at"))

    def _items(self, script=None):
        return [run.event["item"] for run in self._runs(script)]

    def _cassette(self, key, name):
        return testing.Cassette(INTEGRATIONS / key / "tests" / "cassettes" / f"{name}.json", self.connections,
                                allow_unused=True)

    def assertChecked(self, result, runs):  # noqa: N802 - unittest style
        self.assertEqual((result.outcome, result.runs), (runner.OK, runs), result.message)


# --------------------------------------------------------------------------- #
# Integration triggers as adapters
# --------------------------------------------------------------------------- #


class RegistryTests(TestCase):
    def test_the_integrations_triggers_follow_the_builtins(self):
        adapters = registry.adapters()
        self.assertEqual(list(adapters), [
            "http_poll", "rss_new_item", "imap_new_message", "db_poll",
            "sheets_new_row", "gmail_new_message", "github_new_issue",
        ])
        for key, provider in (("sheets_new_row", "google"), ("gmail_new_message", "google"),
                              ("github_new_issue", "github"), ("imap_new_message", "imap")):
            with self.subTest(key):
                self.assertTrue(adapters[key].requires_connection)
                self.assertEqual(adapters[key].providers, (provider,))
        self.assertFalse(adapters["db_poll"].requires_connection)
        self.assertEqual(registry.title("gmail_new_message"), "New email in Gmail")
        self.assertEqual(adapters["sheets_new_row"].config_schema["required"], ["spreadsheet_id", "range"])


class SheetsTests(_Fixture):
    def setUp(self):
        super().setUp()
        self.google = self._google()
        self.use_connection("google", provider="google")  # the cassettes' base URL placeholders

    def test_one_run_per_new_row_from_the_cassette_with_inputs_mapped(self):
        script = self._script(inputs_schema={
            "type": "object", "properties": {"email": {"type": "string"}, "name": {"type": "string"}},
        })
        watcher = self._watcher("sheets_new_row", self.google, script=script,
                                config={"spreadsheet_id": SHEET_ID, "range": "Leads", "key_column": "Email"},
                                input_map={"email": "item.values.Email", "name": "item.values.Name"})
        with self._cassette("google_sheets", "new_rows"):
            self.assertChecked(self._check(watcher), 0)  # marks row 5
            self.assertChecked(self._check(watcher), 1)  # row 6
            self.assertChecked(self._check(watcher), 2)  # rows 7 and 8
        runs = self._runs(script)
        self.assertEqual([run.event["item"]["row"] for run in runs], [6, 7, 8])
        self.assertEqual(runs[1].inputs["email"], "margaret@example.com")
        self.assertEqual(runs[0].trigger_type, Run.TriggerType.WATCHER)
        self.assertEqual(runs[0].event["item"]["spreadsheet_id"], SHEET_ID)
        self.assertTrue(runs[2].event["event_id"].startswith("key:"))
        watcher.refresh_from_db()
        self.assertEqual(watcher.cursor, {"last_row": 8})

    def test_exactly_once_across_a_restart_with_the_watchers_own_credentials(self):
        sheet = [["Name", "Email"], ["Ada", "ada@example.com"]]
        fake = self.fake_http(SHEETS)
        fake.add("GET", f"/v4/spreadsheets/{SHEET_ID}/values/Leads", times=50,
                 json=lambda request: {"range": "Leads!A1:Z1000", "values": sheet})
        watcher = self._watcher("sheets_new_row", self.google, config={"spreadsheet_id": SHEET_ID, "range": "Leads"})
        kit_credentials = self.pyrunner_connections.credentials

        self.assertChecked(self._check(watcher), 0)
        sheet += [["Grace", "grace@example.com"], ["Ken", "ken@example.com"]]
        self._crash(watcher)
        self.assertEqual(len(self._runs()), 2)
        self.assertChecked(self._check(watcher), 0)  # the restarted worker: same rows, no new runs
        sheet.append(["Margaret", "margaret@example.com"])
        self.assertChecked(self._check(watcher), 1)
        self.assertCountEqual([item["values"]["Name"] for item in self._items()], ["Grace", "Ken", "Margaret"])
        watcher.refresh_from_db()
        self.assertEqual((watcher.cursor, watcher.error_count), ({"last_row": 5}, 0))
        # Every request carried the watcher's connection's token, and the kit's stand-in is back.
        self.assertEqual({r.headers["Authorization"] for r in fake.requests}, {f"Bearer {GOOGLE_TOKEN}"})
        self.assertIs(self.pyrunner_connections.credentials, kit_credentials)

    def test_a_connection_named_alike_in_another_workspace_keeps_its_own_credentials(self):
        other_ws = Workspace.objects.create(name="Other T32")
        other_script = self._script(workspace=other_ws)
        other = self._google(OTHER_TOKEN, workspace=other_ws)
        fake = self.fake_http(SHEETS)
        fake.add("GET", f"/v4/spreadsheets/{SHEET_ID}/values/Leads", times=10,
                 json={"range": "Leads!A1:Z1000", "values": [["Name"], ["Ada"]]})
        config = {"spreadsheet_id": SHEET_ID, "range": "Leads"}
        mine = self._watcher("sheets_new_row", self.google, config=config)
        theirs = self._watcher("sheets_new_row", other, config=config, script=other_script)
        for watcher in (mine, theirs, mine, theirs):
            self.assertChecked(self._check(watcher), 0)
        self.assertEqual([r.headers["Authorization"] for r in fake.requests],
                         [f"Bearer {token}" for token in (GOOGLE_TOKEN, OTHER_TOKEN, GOOGLE_TOKEN, OTHER_TOKEN)])

    def test_the_test_button_says_connected_and_saves_nothing(self):
        fake = self.fake_http(SHEETS)
        fake.add("GET", f"/v4/spreadsheets/{SHEET_ID}/values/Leads",
                 json={"range": "Leads!A1:Z1000", "values": [["Name"], ["Ada"]]})
        adapter = registry.get("sheets_new_row")
        ok, message, items = runner.test_adapter(adapter, {"spreadsheet_id": SHEET_ID, "range": "Leads"},
                                                 self.google, workspace=self.ws)
        self.assertEqual((ok, message, items), (True, "Connected; new items from now on start runs.", []))
        self.assertFalse(Watcher.objects.exists())

    def test_a_refused_call_fails_the_check_with_no_token_in_the_error(self):
        fake = self.fake_http(SHEETS)
        fake.add("GET", f"/v4/spreadsheets/{SHEET_ID}/values/Leads", status=403,
                 json={"error": {"code": 403, "message": f"Caller lacks permission ({GOOGLE_TOKEN})",
                                 "status": "PERMISSION_DENIED"}})
        watcher = self._watcher("sheets_new_row", self.google, config={"spreadsheet_id": SHEET_ID, "range": "Leads"})
        result = self._check(watcher)
        self.assertEqual(result.outcome, runner.FAILED)
        watcher.refresh_from_db()
        self.assertIn("Caller lacks permission", watcher.last_error)
        self.assertNotIn(GOOGLE_TOKEN, watcher.last_error)
        self.assertEqual((watcher.error_count, watcher.cursor), (1, None))

    def test_a_401_asks_for_the_stored_credentials_again(self):
        # Someone refreshed the token meanwhile: the refused call re-reads the row and retries with the new one.
        def refused(request):
            row = Connection.objects.get(pk=self.google.pk)
            row.set_credentials({"access_token": OTHER_TOKEN})
            row.save()
            return {"error": {"code": 401, "message": "Invalid Credentials", "status": "UNAUTHENTICATED"}}

        fake = self.fake_http(SHEETS)
        values = f"/v4/spreadsheets/{SHEET_ID}/values/Leads"
        fake.add("GET", values, status=401, json=refused)
        fake.add("GET", values, json={"range": "Leads!A1:Z1000", "values": [["Name"], ["Ada"]]})
        watcher = self._watcher("sheets_new_row", self.google, config={"spreadsheet_id": SHEET_ID, "range": "Leads"})
        with _inline():  # the re-read happens on the runner's thread otherwise
            self.assertChecked(self._check(watcher), 0)
        self.assertEqual([r.headers["Authorization"] for r in fake.requests],
                         [f"Bearer {GOOGLE_TOKEN}", f"Bearer {OTHER_TOKEN}"])

    def test_a_revoked_connection_fails_with_its_reason(self):
        Connection.objects.filter(pk=self.google.pk).update(status=Connection.Status.REVOKED)
        watcher = self._watcher("sheets_new_row", Connection.objects.get(pk=self.google.pk),
                                config={"spreadsheet_id": SHEET_ID, "range": "Leads"})
        self.assertEqual(self._check(watcher).outcome, runner.FAILED)
        watcher.refresh_from_db()
        self.assertIn("was revoked", watcher.last_error)

    def test_a_check_waits_its_turn_and_gives_up_after_its_timeout(self):
        adapter = registry.get("sheets_new_row")
        ctx = CheckContext(connection=self.google, timeout=0.05)
        with integration_adapters._LOCK:
            with self.assertRaisesMessage(AdapterError, "still going"):
                adapter.check(ctx, {"spreadsheet_id": SHEET_ID, "range": "Leads"}, None)
        self.assertEqual(self.connections.calls, [])


class GmailTests(_Fixture):
    def setUp(self):
        super().setUp()
        self.google = self._google()
        self.use_connection("google", provider="google")

    def test_a_label_filter_from_the_cassette(self):
        watcher = self._watcher("gmail_new_message", self.google, config={"label": "INBOX"})
        with self._cassette("gmail", "new_messages"):
            self.assertChecked(self._check(watcher), 0)
            self.assertChecked(self._check(watcher), 1)
        (item,) = self._items()
        self.assertEqual((item["id"], item["subject"]), ("18f2c0000000eeee", "Your order has shipped"))
        self.assertNotIn("payload", item)
        watcher.refresh_from_db()
        self.assertEqual(watcher.cursor, {"history_id": "1015"})

    def _message(self, mid, subject):
        return {"id": mid, "threadId": f"t-{mid}", "labelIds": ["INBOX"], "snippet": "",
                "payload": {"partId": "", "mimeType": "text/plain", "filename": "", "body": {"size": 0},
                            "headers": [{"name": "Subject", "value": subject}]}}

    def test_exactly_once_across_a_restart(self):
        records = []

        def history(request):
            start = int(request.params["startHistoryId"])
            return {"history": [r for r in records if int(r["id"]) > start],
                    "historyId": max([str(start)] + [r["id"] for r in records], key=int)}

        fake = self.fake_http("https://gmail.googleapis.com")
        fake.add("GET", f"{GMAIL_ME}/profile", json={"emailAddress": "me@example.com", "historyId": "10"})
        fake.add("GET", f"{GMAIL_ME}/history", json=history, times=10)
        for mid in ("a", "b", "c"):
            fake.add("GET", f"{GMAIL_ME}/messages/{mid}", json=self._message(mid, f"Mail {mid}"), times=5)
        watcher = self._watcher("gmail_new_message", self.google)

        self.assertChecked(self._check(watcher), 0)
        records += [{"id": "11", "messagesAdded": [{"message": {"id": "a"}}, {"message": {"id": "b"}}]}]
        self._crash(watcher)
        self.assertChecked(self._check(watcher), 0)
        records.append({"id": "12", "messagesAdded": [{"message": {"id": "c"}}]})
        self.assertChecked(self._check(watcher), 1)
        self.assertCountEqual([item["subject"] for item in self._items()], ["Mail a", "Mail b", "Mail c"])
        self.assertCountEqual([run.event["event_id"] for run in self._runs()], ["a", "b", "c"])


class GithubTests(_Fixture):
    def test_new_issues_exactly_once_across_a_restart_from_the_cassette(self):
        github = self._connection("github", "github", "oauth2", {"access_token": GITHUB_TOKEN})
        self.use_connection("github", provider="github")
        watcher = self._watcher("github_new_issue", github, config={"repo": "octo/shop"})
        with self._cassette("github", "new_issues"):
            self.assertChecked(self._check(watcher), 0)  # marks #14
            self.assertChecked(self._check(watcher), 2)  # #15 and #17 (#16 is a pull request)
            self._crash(watcher)  # #18 and #19 queued, the cursor not saved
            self.assertEqual(len(self._runs()), 4)
            self.assertChecked(self._check(watcher), 0)  # the restart sees #18 and #19 again
        self.assertCountEqual([item["number"] for item in self._items()], [15, 17, 18, 19])
        watcher.refresh_from_db()
        self.assertEqual(watcher.cursor, {"last_number": 19})


# --------------------------------------------------------------------------- #
# IMAP
# --------------------------------------------------------------------------- #


class _Mailbox:
    """What the fake IMAP server serves: messages by UID, and every command it got."""

    def __init__(self):
        self.uidvalidity = 1700
        self.messages = []  # (uid, flags, header bytes)
        self.next_uid = 1
        self.commands = []
        self.password = IMAP_PASSWORD

    def deliver(self, subject, sender="ada@example.com", flags=("\\Recent",)):
        uid = self.next_uid
        self.next_uid += 1
        header = (f"Subject: {subject}\r\nFrom: {sender}\r\nTo: me@example.com\r\nDate: Wed, 11 Nov 2026 10:00:00 "
                  f"+0000\r\nMessage-ID: <m{uid}@example.com>\r\n\r\n").encode()
        self.messages.append((uid, " ".join(flags), header))
        return uid


class _ImapHandler(socketserver.StreamRequestHandler):
    """Enough IMAP4rev1 for imaplib: CAPABILITY, LOGIN, EXAMINE, UID SEARCH, UID FETCH, LOGOUT."""

    mailbox = None

    def send(self, text):
        self.wfile.write(text if isinstance(text, bytes) else text.encode())

    def handle(self):
        box = self.mailbox
        self.send("* OK [CAPABILITY IMAP4rev1] fake ready\r\n")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            text = line.decode().rstrip("\r\n")
            tag, _, rest = text.partition(" ")
            box.commands.append(rest)
            word = rest.split(" ", 1)[0].upper()
            if word == "CAPABILITY":
                self.send(f"* CAPABILITY IMAP4rev1\r\n{tag} OK done\r\n")
            elif word == "LOGIN":
                ok = rest.endswith(f'"{box.password}"') or rest.endswith(f" {box.password}")
                self.send(f"{tag} OK logged in\r\n" if ok else f"{tag} NO [AUTHENTICATIONFAILED] Invalid credentials\r\n")
            elif word in ("EXAMINE", "SELECT"):
                if '"INBOX"' not in rest and '"Invoices"' not in rest:
                    self.send(f"{tag} NO no such mailbox\r\n")
                    continue
                self.send(f"* {len(box.messages)} EXISTS\r\n* OK [UIDVALIDITY {box.uidvalidity}] ok\r\n"
                          f"* OK [UIDNEXT {box.next_uid}] ok\r\n{tag} OK [READ-ONLY] done\r\n")
            elif word == "UID" and rest.split()[1].upper() == "SEARCH":
                self.send(f"* SEARCH {' '.join(str(uid) for uid in self._search(rest))}\r\n{tag} OK done\r\n")
            elif word == "UID" and rest.split()[1].upper() == "FETCH":
                wanted = {int(uid) for uid in rest.split()[2].split(",")}
                for number, (uid, flags, header) in enumerate(box.messages, start=1):
                    if uid in wanted:
                        self.send(f'* {number} FETCH (UID {uid} FLAGS ({flags}) INTERNALDATE "11-Nov-2026 10:00:00 '
                                  f'+0000" RFC822.SIZE {len(header) + 100} BODY[HEADER.FIELDS (SUBJECT FROM)] '
                                  f"{{{len(header)}}}\r\n".encode() + header + b")\r\n")
                self.send(f"{tag} OK done\r\n")
            elif word == "LOGOUT":
                self.send(f"* BYE bye\r\n{tag} OK done\r\n")
                return
            else:
                self.send(f"{tag} BAD unknown\r\n")

    def _search(self, rest):
        match = re.search(r"UID (\d+):\*", rest)
        first = int(match.group(1)) if match else 1
        sender = re.search(r'FROM "([^"]+)"', rest)
        uids = [uid for uid, _flags, header in self.mailbox.messages
                if uid >= first and (not sender or sender.group(1).encode() in header)]
        if not uids and self.mailbox.messages and match:
            uids = [self.mailbox.messages[-1][0]]  # n:* always matches the last message (RFC 3501)
        return uids


@override_settings(PYRUNNER_ALLOW_PRIVATE_NETWORK=True)
class ImapTests(_Fixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _ImapHandler)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.box = _ImapHandler.mailbox = _Mailbox()
        self.imap = self._connection(
            "mail", "imap", "basic", {"password": IMAP_PASSWORD},
            config={"host": "127.0.0.1", "port": self.server.server_address[1], "security": "none",
                    "username": "me@example.com"},
        )
        self.adapter = registry.get("imap_new_message")

    def _ctx(self, limit=None):
        return CheckContext(connection=self.imap, limit=limit, timeout=5)

    def test_marks_then_reports_new_mail_read_only(self):
        self.box.deliver("Old news")
        watcher = self._watcher("imap_new_message", self.imap)
        self.assertChecked(self._check(watcher), 0)
        self.box.deliver("=?utf-8?b?Q2Fmw6kgb3JkZXI=?=", sender='"Zoë" <zoe@example.com>')
        self.box.deliver("Invoice 7")
        self.assertChecked(self._check(watcher), 2)
        first, second = self._items()
        self.assertEqual((first["subject"], first["from"], first["uid"], first["mailbox"]),
                         ("Café order", "Zoë <zoe@example.com>", 2, "INBOX"))
        self.assertEqual((second["message_id"], second["flags"], second["internal_date"]),
                         ("<m3@example.com>", ["\\Recent"], "11-Nov-2026 10:00:00 +0000"))
        self.assertEqual([run.event["event_id"] for run in self._runs()], ["1700:2", "1700:3"])
        watcher.refresh_from_db()
        self.assertEqual(watcher.cursor, {"uidvalidity": 1700, "last_uid": 3})
        # Read-only all the way: EXAMINE, never SELECT; headers by BODY.PEEK, nothing stored.
        words = [c.split(" ", 1)[0].upper() for c in self.box.commands]
        self.assertIn("EXAMINE", words)
        self.assertNotIn("SELECT", words)
        self.assertTrue(all("BODY.PEEK[" in c for c in self.box.commands if " FETCH " in c))
        self.assertFalse(any("STORE" in c.upper() for c in self.box.commands))
        self.assertNotIn(IMAP_PASSWORD, json.dumps(self._items()))

    def test_exactly_once_across_a_restart(self):
        watcher = self._watcher("imap_new_message", self.imap)
        self.assertChecked(self._check(watcher), 0)
        self.box.deliver("One")
        self.box.deliver("Two")
        self._crash(watcher)
        self.assertChecked(self._check(watcher), 0)
        self.box.deliver("Three")
        self.assertChecked(self._check(watcher), 1)
        self.assertCountEqual([item["subject"] for item in self._items()], ["One", "Two", "Three"])

    def test_a_search_narrows_mail_as_it_arrives_and_the_cursor_moves_past_the_rest(self):
        config = {"mailbox": "Invoices", "search": 'FROM "billing@example.com"'}
        events, state = self.adapter.check(self._ctx(), config, None)
        self.assertEqual((events, state), ([], {"uidvalidity": 1700, "last_uid": 0}))
        self.box.deliver("Lunch?")
        self.box.deliver("Invoice 8", sender="billing@example.com")
        self.box.deliver("Hello")
        events, state = self.adapter.check(self._ctx(), config, state)
        self.assertEqual([e["item"]["subject"] for e in events], ["Invoice 8"])
        self.assertEqual(state, {"uidvalidity": 1700, "last_uid": 3})
        self.assertIn('UID SEARCH UID 1:* FROM "billing@example.com"', self.box.commands)
        self.assertIn('EXAMINE "Invoices"', self.box.commands)

    def test_the_limit_keeps_the_rest_and_a_new_uidvalidity_marks_again(self):
        _events, state = self.adapter.check(self._ctx(), {}, None)
        for n in range(3):
            self.box.deliver(f"M{n}")
        events, state = self.adapter.check(self._ctx(limit=1), {}, state)
        self.assertEqual(([e["item"]["subject"] for e in events], state["last_uid"]), (["M0"], 1))
        events, state = self.adapter.check(self._ctx(limit=5), {}, state)
        self.assertEqual([e["item"]["subject"] for e in events], ["M1", "M2"])
        self.box.uidvalidity = 1800  # the mailbox was rebuilt
        self.box.deliver("After")
        events, state = self.adapter.check(self._ctx(), {}, state)
        self.assertEqual((events, state), ([], {"uidvalidity": 1800, "last_uid": 4}))

    def test_test_lists_the_newest_mail_and_a_bad_password_fails_without_it(self):
        for subject in ("A", "B", "C", "D"):
            self.box.deliver(subject)
        ok, message, items = runner.test_adapter(self.adapter, {}, self.imap, workspace=self.ws)
        self.assertEqual((ok, message), (True, 'Connected; "INBOX" has 4 messages.'))
        self.assertEqual([item["subject"] for item in items], ["D", "C", "B"])
        self.box.password = "something-else"
        ok, message, items = runner.test_adapter(self.adapter, {}, self.imap, workspace=self.ws)
        self.assertFalse(ok)
        self.assertIn("The mail server refused", message)
        self.assertIn("The mail server refused: [AUTHENTICATIONFAILED] Invalid credentials", message)
        self.assertNotIn(IMAP_PASSWORD, message)
        ok, message, _ = runner.test_adapter(self.adapter, {"mailbox": "Nope"}, self.imap, workspace=self.ws)
        self.assertFalse(ok)

    def test_the_connection_test_logs_in_and_out(self):
        ok, message = connection_service.check_connection(self.imap)
        self.assertEqual((ok, message), (True, "Connected as me@example.com."))
        words = [c.split(" ", 1)[0] for c in self.box.commands]
        # When imaplib asks for CAPABILITY varies by Python version; it only logs in and out.
        self.assertEqual((set(words), words[-1], words.count("LOGIN")), ({"CAPABILITY", "LOGIN", "LOGOUT"}, "LOGOUT", 1))
        self.box.password = "changed"
        ok, message = connection_service.check_connection(self.imap)
        self.assertEqual((ok, message), (False, "[AUTHENTICATIONFAILED] Invalid credentials"))
        self.assertEqual(Connection.objects.get(pk=self.imap.pk).status, Connection.Status.ERROR)

    @override_settings(PYRUNNER_ALLOW_PRIVATE_NETWORK=False)
    def test_a_private_host_is_refused_by_the_ssrf_guard(self):
        ok, message, _ = runner.test_adapter(self.adapter, {}, self.imap, workspace=self.ws)
        self.assertFalse(ok)
        self.assertNotIn("could not be reached", message)
        self.assertEqual(self.box.commands, [])

    def test_scripts_get_the_imap_login_and_listings_never_the_password(self):
        info = connection_service.info(self.imap)
        self.assertEqual(info["imap"], {"host": "127.0.0.1", "port": self.server.server_address[1],
                                        "security": "none", "username": "me@example.com"})
        self.assertIsNone(info["base_url"])
        material = connection_service.credentials_for(self.imap)
        self.assertEqual((material["imap"]["password"], material["basic"]["password"]),
                         (IMAP_PASSWORD, IMAP_PASSWORD))
        self.assertIn(IMAP_PASSWORD, connection_service.masks_for(self.imap).values())

    def test_a_line_break_in_the_settings_never_reaches_the_server(self):
        for config in ({"mailbox": "INBOX\r\nA1 DELETE INBOX"}, {"search": "UNSEEN\nA1 LOGOUT"}):
            with self.subTest(config), self.assertRaisesMessage(AdapterError, "line breaks"):
                self.adapter.check(self._ctx(), config, {"uidvalidity": 1700, "last_uid": 0})
        self.assertEqual(self.box.commands, [])

    def test_settings_are_validated(self):
        schema = ImapNewMessageAdapter.config_schema
        self.assertEqual(validate_config(schema, {"search": "UNSEEN\r\nA1 DELETE INBOX"})[1],
                         {"search": "Doesn't have the expected form."})
        self.assertEqual(validate_config(schema, {"mailbox": "Invoices", "search": "UNSEEN"}),
                         ({"mailbox": "Invoices", "search": "UNSEEN"}, {}))


# --------------------------------------------------------------------------- #
# db_poll
# --------------------------------------------------------------------------- #


def _after(value, cursor) -> bool:
    """``value > cursor`` as Postgres compares a column with the cursor's text."""
    if isinstance(value, int):
        return value > int(cursor)
    return (value.isoformat() if hasattr(value, "isoformat") else str(value)) > cursor


class _Table:
    """A stand-in for ``DatabaseExplorerService.query`` over one table's rows (dicts)."""

    def __init__(self, rows, columns=("id", "email", "created_at")):
        self.rows = rows
        self.columns = list(columns)
        self.calls = []

    def __call__(self, database, text, *, row_cap=200, timeout_ms=5000, params=None):
        self.calls.append((text, params))
        column = re.search(r"max\((\w+)\)", text)
        if column:
            values = [row[column.group(1)] for row in self.rows if row[column.group(1)] is not None]
            return QueryResult(["max"], [[max(values) if values else None]])
        order = re.search(r"ORDER BY (\w+)( DESC)?", text)
        key, desc = order.group(1), bool(order.group(2))
        rows = [row for row in self.rows if row[key] is not None]
        if params:
            rows = [row for row in rows if _after(row[key], params[0])]
        rows.sort(key=lambda row: row[key], reverse=desc)
        limit = int(re.search(r"LIMIT (\d+)", text).group(1))
        return QueryResult(self.columns, [[row[c] for c in self.columns] for row in rows[:min(limit, row_cap)]])


@override_settings(PYRUNNER_DATA_DB_URL="postgres://data.invalid/pyrun")
class DbPollTests(_Fixture):
    def setUp(self):
        super().setUp()
        self.database = Database(name="crm", workspace=self.ws, schema_name="db_t32", role_name="db_t32",
                                 status=Database.STATUS_READY)
        self.database.set_password("pw")
        self.database.save()
        self.adapter = registry.get("db_poll")
        self.config = {"database": "crm", "query": "SELECT id, email, created_at FROM signups WHERE email LIKE '%@x.io'",
                       "cursor_column": "id"}

    def _table(self, rows, columns=("id", "email", "created_at")):
        table = _Table(rows, columns)
        patcher = mock.patch.object(DatabaseExplorerService, "query", side_effect=table)
        patcher.start()
        self.addCleanup(patcher.stop)
        return table

    def _ctx(self, limit=None, workspace=None):
        return CheckContext(workspace=workspace or self.ws, limit=limit, timeout=5)

    def test_marks_then_reports_rows_past_the_cursor_through_a_wrapped_read_only_query(self):
        now = timezone.now()
        rows = [{"id": 1, "email": "a@x.io", "created_at": now}]
        table = self._table(rows)
        events, state = self.adapter.check(self._ctx(), self.config, None)
        self.assertEqual((events, state), ([], {"cursor": "1"}))
        self.assertEqual(table.calls[-1], (
            "SELECT max(id) FROM (\nSELECT id, email, created_at FROM signups WHERE email LIKE '%%@x.io'\n) AS pyrun_poll",
            None))
        rows += [{"id": 2, "email": "b@x.io", "created_at": now}, {"id": 3, "email": "c@x.io", "created_at": now}]
        events, state = self.adapter.check(self._ctx(), self.config, state)
        self.assertEqual([e["id"] for e in events], ["2", "3"])
        self.assertEqual(events[0]["item"], {"id": 2, "email": "b@x.io", "created_at": now.isoformat()})
        self.assertEqual(state, {"cursor": "3"})
        self.assertEqual(table.calls[-1], (
            "SELECT * FROM (\nSELECT id, email, created_at FROM signups WHERE email LIKE '%%@x.io'\n) AS pyrun_poll "
            "WHERE id > %s ORDER BY id LIMIT 101", ("1",)))
        self.assertEqual(_jsonable(float("nan")), "nan")  # jsonb refuses NaN: the databases.query rule

    def test_rows_sharing_a_cursor_value_are_never_split_by_the_limit(self):
        at = [datetime.datetime(2026, 11, 11, 10, minute, tzinfo=datetime.timezone.utc) for minute in range(3)]
        rows = [{"id": 1, "email": "a", "created_at": at[0]}, {"id": 2, "email": "b", "created_at": at[1]},
                {"id": 3, "email": "c", "created_at": at[1]}, {"id": 4, "email": "d", "created_at": at[2]}]
        self._table(rows)
        config = {**self.config, "cursor_column": "created_at", "id_column": "id"}
        state = {"cursor": None}  # an empty table at the first check: every row is new
        events, state = self.adapter.check(self._ctx(limit=2), config, state)
        self.assertEqual(([e["id"] for e in events], state), (["1"], {"cursor": at[0].isoformat()}))
        events, state = self.adapter.check(self._ctx(limit=2), config, state)
        self.assertEqual(([e["id"] for e in events], state), (["2", "3"], {"cursor": at[1].isoformat()}))
        with self.assertRaisesMessage(AdapterError, "More than 1 rows share the cursor value"):
            self.adapter.check(self._ctx(limit=1), config, {"cursor": at[0].isoformat()})

    def test_exactly_once_across_a_restart(self):
        rows = [{"id": 1, "email": "a@x.io", "created_at": None}]
        self._table(rows)
        self.script.inputs_schema = {"type": "object", "properties": {"email": {"type": "string"}}}
        self.script.save()
        watcher = self._watcher("db_poll", config=self.config, input_map={"email": "item.email"})
        with _inline():
            self.assertChecked(self._check(watcher), 0)
            rows += [{"id": 2, "email": "b@x.io", "created_at": None}, {"id": 3, "email": "c@x.io", "created_at": None}]
            self._crash(watcher)
            self.assertChecked(self._check(watcher), 0)
            rows.append({"id": 4, "email": "d@x.io", "created_at": None})
            self.assertChecked(self._check(watcher), 1)
        # Exactly once, in any order: runs made by different checks can share a timestamp
        # (or a container clock step can reorder them), so created_at order is not compared.
        runs = {run.event["event_id"]: run for run in self._runs()}
        self.assertCountEqual([run.event["event_id"] for run in self._runs()], ["2", "3", "4"])
        self.assertEqual(runs["4"].inputs, {"email": "d@x.io"})
        watcher.refresh_from_db()
        self.assertEqual(watcher.cursor, {"cursor": "4"})

    def test_only_a_ready_database_of_the_watchers_workspace(self):
        self._table([])
        other = Workspace.objects.create(name="Other T32")
        for ctx, config, words in (
            (self._ctx(workspace=other), self.config, 'no database named "crm"'),
            (self._ctx(), {**self.config, "database": "nope"}, 'no database named "nope"'),
            (CheckContext(timeout=5), self.config, "No workspace"),
        ):
            with self.subTest(words), self.assertRaisesMessage(AdapterError, words):
                self.adapter.check(ctx, config, None)
        Database.objects.filter(pk=self.database.pk).update(status=Database.STATUS_ERROR)
        with self.assertRaisesMessage(AdapterError, "isn't ready"):
            self.adapter.check(self._ctx(), self.config, None)
        with override_settings(PYRUNNER_DATA_DB_URL=""), self.assertRaisesMessage(AdapterError, "no data server"):
            self.adapter.check(self._ctx(), self.config, None)

    def test_the_query_must_be_one_select_and_columns_plain_names(self):
        self._table([])
        with self.assertRaisesMessage(AdapterError, "Only a SELECT"):
            self.adapter.check(self._ctx(), {**self.config, "query": "DELETE FROM signups"}, None)
        with self.assertRaisesMessage(AdapterError, "plain column name"):
            self.adapter.check(self._ctx(), {**self.config, "cursor_column": "id) x; --"}, None)
        self.assertEqual(validate_config(DbPollAdapter.config_schema, {**self.config, "cursor_column": "id;"})[1],
                         {"cursor_column": "Doesn't have the expected form."})
        self._table([{"id": 1}], columns=("id",))
        with self.assertRaisesMessage(AdapterError, 'no column "uid" (the id column)'):
            self.adapter.check(self._ctx(), {**self.config, "id_column": "uid"}, {"cursor": "0"})

    def test_test_shows_the_newest_rows(self):
        self._table([{"id": n, "email": f"{n}@x.io", "created_at": None} for n in range(1, 6)])
        ok, message, items = runner.test_adapter(self.adapter, self.config, workspace=self.ws)
        self.assertEqual((ok, message), (True, "The query runs; the newest id is '5'."))
        self.assertEqual([item["id"] for item in items], [5, 4, 3])


@skipUnless(db_connection.vendor == "postgresql", "needs a Postgres server (the test database's)")
class LiveDbPollTests(TestCase):
    """The real data server: a role and schema provisioned in the test database itself."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        s = db_connection.settings_dict
        netloc = f"{urllib.parse.quote(s['USER'])}:{urllib.parse.quote(s['PASSWORD'])}@{s['HOST']}"
        cls.server = override_settings(PYRUNNER_DATA_DB_URL=f"postgres://{netloc}:{s['PORT'] or 5432}/{s['NAME']}")
        cls.server.enable()
        from core.services.database_service import DatabaseService

        cls.ws = Workspace.objects.create(name="T32 live")
        ident = f"db_t32_{uuid.uuid4().hex[:8]}"
        cls.db = Database(name="crm", workspace=cls.ws, schema_name=ident, role_name=ident,
                          status=Database.STATUS_PROVISIONING)
        cls.db.set_password("pw")
        cls.db.save()
        DatabaseService.provision(cls.db)
        cls.sql("CREATE TABLE signups (id serial PRIMARY KEY, email text, created_at timestamptz DEFAULT now());"
                "INSERT INTO signups (email) VALUES ('a@x.io'), ('b@y.io')")

    @classmethod
    def tearDownClass(cls):
        from core.services.database_service import DatabaseService

        try:
            DatabaseService.deprovision(cls.db)
        finally:
            cls.server.disable()
            super().tearDownClass()

    @classmethod
    def sql(cls, text):
        DatabaseExplorerService.execute(cls.db, text, read_only=False, row_cap=None)

    def test_ids_and_timestamps_as_cursors_with_a_percent_in_the_query(self):
        adapter = DbPollAdapter()
        ctx = CheckContext(workspace=self.ws, timeout=10)
        config = {"database": "crm", "query": "SELECT * FROM signups WHERE email LIKE '%@x.io' -- the x.io ones",
                  "cursor_column": "id"}
        _events, by_id = adapter.check(ctx, config, None)
        stamped = {**config, "cursor_column": "created_at", "id_column": "id"}
        _events, by_time = adapter.check(ctx, stamped, None)
        self.assertEqual(by_id, {"cursor": "1"})
        self.sql("INSERT INTO signups (email, created_at) VALUES ('c@x.io', now() + interval '1 minute'), "
                 "('d@y.io', now() + interval '2 minutes'), ('e@x.io', now() + interval '3 minutes')")
        events, by_id = adapter.check(ctx, config, by_id)
        self.assertEqual(([e["item"]["email"] for e in events], by_id), (["c@x.io", "e@x.io"], {"cursor": "5"}))
        events, by_time = adapter.check(ctx, stamped, by_time)
        self.assertEqual([e["id"] for e in events], ["3", "5"])
        self.assertEqual(adapter.check(ctx, stamped, by_time), ([], by_time))
        message, items = adapter.test(ctx, config)
        self.assertEqual((message, [item["email"] for item in items]),
                         ("The query runs; the newest id is '5'.", ["e@x.io", "c@x.io", "a@x.io"]))
        with self.assertRaisesMessage(AdapterError, "read-only"):
            adapter.check(ctx, {**config, "query": "SELECT * FROM signups FOR UPDATE"}, None)
