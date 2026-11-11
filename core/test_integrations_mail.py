"""
Tests for the Gmail and SMTP integrations (2.5 Stage 6, T29), beyond their own suites.

Each module's conformance suite, cassette and fake-service tests are in
core/integrations/{gmail,smtp}/tests/test_api.py. These cover what T29 added
around them:

- both are shipped (INTEGRATION_MODULES), their generated pages are current
  (Gmail's names its API host; SMTP's has its own error table) and
  help.search finds them; no page has a code span cut across lines;
- ``manage.py integration_smoke gmail|smtp`` with a real Connection row (the
  service a fake): read-only calls, nothing sent, PASS and a masked FAIL;
- ``smtp.send`` through a real SMTP conversation (smtplib against a small
  server on a local socket): the login, the envelope (Bcc included), the
  message without its Bcc header, the attachments;
- the Gmail cassettes' sent messages are what the module builds;
- both modules work with the standard library alone (no ``requests``).
"""

import base64
import io
import json
import re
import socketserver
import subprocess
import sys
import threading
from email import message_from_bytes, policy
from pathlib import Path

from django.conf import settings
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase

from core.integrations import docs, framework, testing
from core.models import Connection
from core.services import help_docs
from core.test_connections import _Fixture

GOOGLE_TOKEN = "ya29.t29-google-access-token-0001"
SMTP_PASSWORD = "t29-smtp-password-0001"
GMAIL = "https://gmail.googleapis.com"


class RegistryTests(SimpleTestCase):
    def test_both_are_shipped_with_current_pages(self):
        self.assertEqual(framework.INTEGRATION_MODULES[5:7], ("gmail", "smtp"))
        for key, provider in (("gmail", "google"), ("smtp", "smtp")):
            with self.subTest(key):
                integration = framework.load(key)
                self.assertEqual(integration.provider, provider)
                self.assertIn(f"from pyrunner_integrations import {key}", integration.example)
                page = docs.path_for(integration).read_text(encoding="utf-8")
                self.assertEqual(page, docs.render(integration), "run manage.py integration_docs")
                for name in integration.functions:
                    self.assertIn(f"### {name}\n", page)

    def test_gmail_names_its_host_and_smtp_has_its_own_errors(self):
        gmail = docs.render(framework.load("gmail"))
        self.assertIn(f"The credentials go only to `{GMAIL}`; a redirect elsewhere", gmail)
        self.assertIn("| `RATE_LIMITED` |", gmail)
        smtp = docs.render(framework.load("smtp"))
        self.assertNotIn("The credentials go only to", smtp)
        self.assertNotIn("| `RATE_LIMITED` |", smtp)
        self.assertIn("`status` (the server's SMTP reply code", smtp)
        self.assertIn("| `RECIPIENTS_REFUSED` |", smtp)

    def test_no_page_cuts_a_code_span_across_lines(self):
        for integration in framework.integrations():
            with self.subTest(integration.key):
                outside = re.sub(r"```.*?```", "", docs.render(integration), flags=re.S)
                self.assertNotIn("``", outside)

    def test_help_search_finds_each_page(self):
        index = help_docs.build_index(Path(settings.BASE_DIR))
        for query, page in (("reply to a gmail message", "docs/integrations/gmail.md"),
                            ("send an email with an attachment over smtp", "docs/integrations/smtp.md")):
            with self.subTest(query):
                results = help_docs.search(query, index=index)["results"]
                self.assertIn(page, [r["doc"] for r in results[:3]], results[:3])

    def test_the_gmail_trigger_is_declared(self):
        self.assertEqual([t.key for t in framework.load_triggers("gmail")], ["gmail_new_message"])
        self.assertEqual(framework.load_triggers("smtp"), ())


class CassetteBodiesTests(SimpleTestCase):
    """The hand-built cassettes hold what the module sends: each raw message parses back as written."""

    def test_sent_messages_in_the_cassettes(self):
        folder = Path(settings.BASE_DIR) / "core/integrations/gmail/tests/cassettes"
        sent = {}
        for name in ("send", "reply"):
            for interaction in json.loads((folder / f"{name}.json").read_text(encoding="utf-8"))["interactions"]:
                body = interaction["request"].get("json") or {}
                if "raw" in body:
                    raw = body["raw"]
                    data = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
                    sent[name] = message_from_bytes(data.replace(b"\r\n", b"\n"), policy=policy.default)
        self.assertEqual((sent["send"]["To"], sent["send"]["Bcc"]),
                         ("Ada Lovelace <ada@example.com>, grace@example.com", "audit@example.com"))
        self.assertEqual([p.get_filename() for p in sent["send"].iter_attachments()], ["totals.csv"])
        reply = sent["reply"]
        self.assertEqual((reply["To"], reply["Cc"], reply["Subject"]),
                         ("sales@example.org", "Linus <linus@example.org>, ops@example.org", "RE: Quote request"))
        self.assertEqual(reply["References"], "<CAquote1@mail.example.org> <CAquote2@mail.example.org>")


class SmokeTests(testing.IntegrationTestMixin, _Fixture, TestCase):
    def _smoke(self, key):
        out = io.StringIO()
        call_command("integration_smoke", key, stdout=out)
        return out.getvalue()

    def test_gmail_smoke_lists_labels_and_one_message(self):
        self._connection(name="google", provider="google", auth_kind="oauth2",
                         credentials={"access_token": GOOGLE_TOKEN})
        fake = self.fake_http(GMAIL)
        fake.add("GET", "/gmail/v1/users/me/labels", json={"labels": [{"id": "INBOX", "name": "INBOX"}]})
        fake.add("GET", "/gmail/v1/users/me/messages", json={"messages": [{"id": "m1", "threadId": "m1"}]})
        out = self._smoke("gmail")
        self.assertIn("PASS read: list_labels answered with 1 label(s); search answered with 1 message(s)", out)
        self.assertEqual([r.method for r in fake.requests], ["GET", "GET"])
        self.assertEqual(fake.requests[1].params["maxResults"], "1")
        self.assertEqual(fake.requests[0].headers["Authorization"], f"Bearer {GOOGLE_TOKEN}")
        fake.default(status=403, json=testing.echo)
        out = io.StringIO()
        with self.assertRaises(CommandError):
            call_command("integration_smoke", "gmail", stdout=out)
        self.assertIn("FAIL read: FORBIDDEN", out.getvalue())
        self.assertNotIn(GOOGLE_TOKEN, out.getvalue())

    def test_smtp_smoke_logs_in_and_sends_nothing(self):
        self._connection(name="mail", provider="smtp", auth_kind="smtp",
                         config={"host": "smtp.example.com", "port": 587, "security": "starttls",
                                 "username": "robot@example.com", "from_address": ""},
                         credentials={"password": SMTP_PASSWORD})
        server = self.fake_smtp()
        out = self._smoke("smtp")
        self.assertIn("PASS login: connected to smtp.example.com:587 (starttls) and logged in; nothing sent", out)
        self.assertEqual(server.sessions[0]["login"], ("robot@example.com", SMTP_PASSWORD))
        self.assertEqual((server.sessions[0]["starttls"], server.messages), (True, []))
        import smtplib

        server.fail("login", smtplib.SMTPAuthenticationError(535, f"5.7.8 {SMTP_PASSWORD} refused".encode()))
        out = io.StringIO()
        with self.assertRaises(CommandError):
            call_command("integration_smoke", "smtp", stdout=out)
        self.assertIn("FAIL login: UNAUTHORIZED: SMTP smtp.example.com:587: the login was refused: 535", out.getvalue())
        self.assertNotIn(SMTP_PASSWORD, out.getvalue())

    def test_skips_without_a_connection(self):
        for key, provider in (("gmail", "google"), ("smtp", "smtp")):
            with self.subTest(key):
                self.assertIn(f"SKIP: no {provider} connection", self._smoke(key))


# --------------------------------------------------------------------------- #
# A real SMTP conversation, on a local socket
# --------------------------------------------------------------------------- #


class _Handler(socketserver.StreamRequestHandler):
    """Just enough SMTP: EHLO, AUTH PLAIN, MAIL, RCPT, DATA, NOOP, QUIT."""

    def _say(self, line):
        self.wfile.write(line.encode("ascii") + b"\r\n")

    def handle(self):
        box = self.server.box
        self._say("220 t29.test ESMTP")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            command = line.decode("ascii").rstrip("\r\n")
            box["commands"].append(command.split(" ")[0].upper())
            verb, _sep, rest = command.partition(" ")
            verb = verb.upper()
            if verb == "EHLO":
                self._say("250-t29.test")
                self._say("250-AUTH PLAIN")
                self._say("250 8BITMIME")
            elif verb == "AUTH":
                parts = base64.b64decode(rest.split(" ", 1)[1]).split(b"\0")
                box["login"] = (parts[1].decode(), parts[2].decode())
                self._say("235 2.7.0 Authentication successful" if box["login"][1] == SMTP_PASSWORD
                          else "535 5.7.8 Authentication failed")
            elif verb == "MAIL":
                box["sender"] = re.search(r"<(.*?)>", rest).group(1)
                self._say("250 2.1.0 OK")
            elif verb == "RCPT":
                address = re.search(r"<(.*?)>", rest).group(1)
                if address.startswith("nobody@"):
                    self._say("550 5.1.1 No such user")
                else:
                    box["recipients"].append(address)
                    self._say("250 2.1.5 OK")
            elif verb == "DATA":
                self._say("354 End data with <CR><LF>.<CR><LF>")
                data = []
                while True:
                    chunk = self.rfile.readline()
                    if chunk in (b".\r\n", b""):
                        break
                    data.append(chunk[1:] if chunk.startswith(b"..") else chunk)
                box["data"] = b"".join(data)
                self._say("250 2.0.0 Queued")
            elif verb == "QUIT":
                self._say("221 2.0.0 Bye")
                return
            else:
                self._say("250 2.0.0 OK")


class RealSmtpTests(SimpleTestCase):
    def setUp(self):
        self.box = {"commands": [], "recipients": [], "login": None, "sender": None, "data": None}
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler)
        server.daemon_threads = True
        server.box = self.box
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        pc, client = framework.script_helpers()
        material = {"auth_kind": "smtp", "base_url": None, "smtp": {
            "host": "127.0.0.1", "port": server.server_address[1], "security": "none",
            "username": "robot@example.com", "password": SMTP_PASSWORD, "from_address": "Robot <robot@example.com>"}}
        original = pc.credentials
        pc.credentials = lambda name, refused=False: material
        self.addCleanup(setattr, pc, "credentials", original)
        client.reset()
        self.material = material
        self.smtp = framework.script_module("smtp")

    def test_a_message_with_bcc_and_attachments_over_a_real_conversation(self):
        result = self.smtp.send(["ada@example.com", "nobody@example.com"], "Report – May", "Attached.",
                                html="<p>Attached.</p>", bcc="audit@example.com",
                                attachments=[("totals.csv", b".leading dot\nmonth,total\n")])
        self.assertEqual(self.box["login"], ("robot@example.com", SMTP_PASSWORD))
        self.assertEqual(self.box["sender"], "robot@example.com")
        self.assertEqual(self.box["recipients"], ["ada@example.com", "audit@example.com"])
        self.assertEqual(self.box["commands"][-1], "QUIT")
        msg = message_from_bytes(self.box["data"].replace(b"\r\n", b"\n"), policy=policy.default)
        self.assertNotIn("Bcc", msg)
        self.assertEqual((msg["From"], msg["To"], msg["Subject"]),
                         ("Robot <robot@example.com>", "ada@example.com, nobody@example.com", "Report – May"))
        [attachment] = msg.iter_attachments()
        self.assertEqual(attachment.get_payload(decode=True), b".leading dot\nmonth,total\n")
        self.assertEqual(result["accepted"], ["ada@example.com", "audit@example.com"])
        self.assertEqual(result["refused"], {"nobody@example.com": "550 5.1.1 No such user"})
        self.assertEqual(result["message_id"], msg["Message-ID"])

    def test_a_refused_login_over_a_real_conversation(self):
        self.material["smtp"]["password"] = "wrong-password-0001"
        with self.assertRaises(self.smtp._client.IntegrationError) as ctx:
            self.smtp.send("ada@example.com", "Hi", "x")
        self.assertEqual((ctx.exception.code, ctx.exception.status), ("UNAUTHORIZED", 535))
        self.assertNotIn("wrong-password-0001", str(ctx.exception))
        self.assertIsNone(self.box["data"])

    def test_nothing_listening(self):
        self.material["smtp"]["port"] = 1  # nothing listens there
        with self.assertRaises(self.smtp._client.IntegrationError) as ctx:
            self.smtp.send("ada@example.com", "Hi", "x", timeout=5)
        self.assertEqual(ctx.exception.code, "UNREACHABLE")


STDLIB_ONLY = r"""
import base64, json, sys
sys.modules["requests"] = None          # as in an environment without requests
sys.path.insert(0, sys.argv[1])
import pyrunner_connections
from pyrunner_integrations import _client, gmail, smtp

MATERIAL = {"base_url": "https://www.googleapis.com", "headers": {"Authorization": "Bearer ya29-x"}, "params": {}}
pyrunner_connections.credentials = lambda name, refused=False: MATERIAL
seen = []

def transport(method, url, headers, body, timeout):
    seen.append([method, url.split("?")[0], headers.get("Authorization")])
    reply = {"messages": [{"id": "m1", "threadId": "m1"}]} if method == "GET" else {"id": "s1"}
    return _client.Response(200, {"content-type": "application/json"}, json.dumps(reply).encode(), url)

_client.TRANSPORT = transport
out = {
    "found": list(gmail.search("is:unread")),
    "sent": gmail.send("ada@example.com", "Hi", "x", attachments=[("a.txt", b"hi")]),
    "smtp": smtp.PROVIDER,
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
        self.assertEqual((out["found"], out["sent"], out["smtp"], out["requests"]),
                         ([{"id": "m1", "threadId": "m1"}], {"id": "s1"}, "smtp", False))
        self.assertEqual(out["seen"], [
            ["GET", f"{GMAIL}/gmail/v1/users/me/messages", "Bearer ya29-x"],
            ["POST", f"{GMAIL}/gmail/v1/users/me/messages/send", "Bearer ya29-x"],
        ])
