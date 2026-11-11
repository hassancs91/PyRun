"""The smtp integration's tests: the conformance suite's SMTP checks and the fake server (core/integrations/testing.py).

SMTP has no HTTP exchanges, so no cassettes: ``fake_smtp()`` stands in for
the server through smtplib's own classes (``send_message`` builds the
envelope as it would). core/test_integrations_mail.py also sends through a
real SMTP conversation on a local socket.
"""

import smtplib

from core.integrations import testing


class SmtpConformance(testing.SmtpConformanceSuite):
    integration = "smtp"

    def call(self, module):
        return module.send("ada@example.com", "Conformance", "One message.")


class _Smtp(testing.IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.smtp = self.integration("smtp")
        self.conn = self.use_connection("mail", provider="smtp",
                                        config={"host": "smtp.example.com", "port": 587,
                                                "from_address": "Reports <reports@example.com>"})
        self.server = self.fake_smtp()


class SmtpSendTests(_Smtp):
    def test_text_html_attachments_and_every_recipient_kind(self):
        result = self.smtp.send(
            "Ada Lovelace <ada@example.com>, grace@example.com", "Monthly report", "See attached.",
            html="<p>See <b>attached</b>.</p>", cc=["linus@example.com"], bcc="audit@example.com",
            reply_to="help@example.com", headers={"X-Campaign": "may"},
            attachments=[("totals.csv", b"month,total\n2026-10,1200\n"),
                         {"filename": "notes.txt", "content": "café"}],
        )
        sent = self.server.messages[0]
        self.assertEqual(sent.sender, "reports@example.com")
        self.assertEqual(sent.recipients, ["ada@example.com", "grace@example.com", "linus@example.com",
                                           "audit@example.com"])
        msg = sent.message
        self.assertNotIn("Bcc", msg)  # the envelope has it, the message doesn't
        self.assertEqual((msg["From"], msg["To"], msg["Cc"], msg["Reply-To"], msg["Subject"], msg["X-Campaign"]),
                         ("Reports <reports@example.com>", "Ada Lovelace <ada@example.com>, grace@example.com",
                          "linus@example.com", "help@example.com", "Monthly report", "may"))
        self.assertEqual(msg.get_body(("plain",)).get_content(), "See attached.\n")
        self.assertEqual(msg.get_body(("html",)).get_content(), "<p>See <b>attached</b>.</p>\n")
        files = {part.get_filename(): (part.get_content_type(), part.get_param("charset"),
                                       part.get_payload(decode=True))
                 for part in msg.iter_attachments()}
        self.assertEqual(files, {"totals.csv": ("text/csv", "utf-8", b"month,total\n2026-10,1200\n"),
                                 "notes.txt": ("text/plain", "utf-8", "café".encode())})
        self.assertEqual(result, {"message_id": msg["Message-ID"], "from": "reports@example.com",
                                  "accepted": sent.recipients, "refused": {}})
        self.assertTrue(msg["Message-ID"].endswith("@example.com>"))
        self.assertTrue(msg["Date"])

    def test_a_file_path_and_a_single_attachment(self):
        path = self.enterContext(_tempfile(b"%PDF-1.4 fake"))
        self.smtp.send("ada@example.com", "One file", attachments=path)
        self.smtp.send("ada@example.com", "One tuple", attachments=("a.bin", b"\x00\x01", "application/x-thing"))
        first, second = (m.message for m in self.server.messages)
        [pdf] = first.iter_attachments()
        self.assertEqual((pdf.get_content_type(), pdf.get_content()), ("application/pdf", b"%PDF-1.4 fake"))
        [thing] = second.iter_attachments()
        self.assertEqual((thing.get_filename(), thing.get_content_type()), ("a.bin", "application/x-thing"))

    def test_the_login_and_starttls_as_the_connection_says(self):
        self.smtp.send("ada@example.com", "Hi", "x", connection="mail")
        session = self.server.sessions[0]
        self.assertEqual((session["host"], session["port"], session["starttls"], session["ssl"]),
                         ("smtp.example.com", 587, True, False))
        self.assertEqual(session["login"], ("pyrun@example.test", self.conn.credentials["password"]))

    def test_from_address_argument_then_the_username(self):
        self.smtp.send("ada@example.com", "Hi", "x", from_address="Ops <ops@example.com>")
        self.assertEqual((self.server.messages[0].sender, self.server.messages[0].message["From"]),
                         ("ops@example.com", "Ops <ops@example.com>"))
        self.conn.config["from_address"] = ""
        self.smtp.send("ada@example.com", "Hi", "x")
        self.assertEqual(self.server.messages[1].sender, "pyrun@example.test")
        self.conn.config["username"] = "apikey"  # SendGrid-style: not an address
        with self.assertRaisesMessage(ValueError, "Connection 'smtp' has no From address"):
            self.smtp.send("ada@example.com", "Hi", "x")
        self.assertEqual(len(self.server.sessions), 2)  # refused before connecting

    def test_some_recipients_refused_is_a_result_not_an_error(self):
        self.server.refuse("nobody@example.com")
        result = self.smtp.send(["ada@example.com", "nobody@example.com"], "Hi", "x")
        self.assertEqual(result["accepted"], ["ada@example.com"])
        self.assertEqual(result["refused"], {"nobody@example.com": "550 5.1.1 No such user here"})
        self.assertEqual(self.server.messages[0].recipients, ["ada@example.com"])

    def test_a_goodbye_the_server_drops_after_the_message_is_no_error(self):
        """oss-quality: a failed QUIT after the server took the message raised, inviting a second send."""
        self.server.fail("quit", smtplib.SMTPServerDisconnected("Connection unexpectedly closed"))
        result = self.smtp.send("ada@example.com", "Hi", "x")
        self.assertEqual((result["accepted"], len(self.server.messages)), (["ada@example.com"], 1))
        self.assertTrue(self.server.sessions[0]["closed"])

    def test_every_recipient_refused_names_them(self):
        self.server.refuse("*", 554, "5.7.1 Relay access denied")
        with self.assertRaises(self.integration_client.IntegrationError) as ctx:
            self.smtp.send(["ada@example.com", "bob@example.com"], "Hi", "x")
        error = ctx.exception
        self.assertEqual((error.code, error.status, error.retry_after), ("RECIPIENTS_REFUSED", 554, None))
        self.assertIn("ada@example.com: 554 5.7.1 Relay access denied", str(error))

    def test_bad_arguments_fail_before_connecting(self):
        for kwargs, error, text in (
            ({"to": None}, ValueError, "a message needs a recipient"),
            ({"to": "not-an-address"}, ValueError, "'not-an-address' is not an email address"),
            ({"to": {"ada@example.com": 1}}, TypeError, "to is an email address or a list"),
            ({"to": "ada@example.com", "subject": "Hi\nBcc: x@example.com"}, ValueError, "subject can't hold"),
            ({"to": "ada@example.com", "headers": {"Subject": "x"}}, ValueError, "use the argument"),
            ({"to": "ada@example.com", "attachments": [42]}, TypeError, "an attachment is a file path"),
            ({"to": "ada@example.com", "attachments": [("a.txt", 42)]}, TypeError, "content is bytes or text"),
            ({"to": "ada@example.com", "text": b"bytes"}, TypeError, "text is a string"),
        ):
            with self.subTest(kwargs=kwargs), self.assertRaisesMessage(error, text):
                self.smtp.send(**{"subject": "s", **kwargs})
        self.assertEqual(self.server.sessions, [])

    def test_ssl_connections_use_smtp_ssl_and_none_skips_tls(self):
        self.conn.config.update(security="ssl", port=465)
        self.smtp.send("ada@example.com", "Hi", "x")
        self.conn.config.update(security="none", port=25, username="")
        self.smtp.send("ada@example.com", "Hi", "x")
        tls, plain = self.server.sessions
        self.assertEqual((tls["ssl"], tls["starttls"], tls["port"]), (True, False, 465))
        self.assertEqual((plain["ssl"], plain["starttls"], plain["login"]), (False, False, None))

    def test_client_is_logged_in_for_your_own_message(self):
        with self.smtp.client() as server:
            self.assertEqual(server.from_address, "Reports <reports@example.com>")
            server.sendmail("reports@example.com", ["ada@example.com"], b"Subject: raw\r\n\r\nhello\r\n")
        self.assertEqual(self.server.messages[0].message["Subject"], "raw")
        self.assertTrue(self.server.sessions[0]["closed"])
        self.server.fail("login", smtplib.SMTPAuthenticationError(535, b"5.7.8 no"))
        with self.assertRaises(self.integration_client.IntegrationError) as ctx:
            self.smtp.client()
        self.assertEqual((ctx.exception.code, ctx.exception.status), ("UNAUTHORIZED", 535))

    def test_an_expired_or_missing_connection(self):
        with self.assertRaises(self.integration_client.IntegrationError) as ctx:
            self.smtp.send("ada@example.com", "Hi", "x", connection="other")
        self.assertEqual(ctx.exception.code, "CONNECTION_NOT_FOUND")
        self.use_connection("old", provider="smtp", error=("CONNECTION_EXPIRED", 409))
        with self.assertRaises(self.integration_client.IntegrationError) as ctx:
            self.smtp.send("ada@example.com", "Hi", "x", connection="old")
        self.assertEqual((ctx.exception.code, ctx.exception.status), ("CONNECTION_EXPIRED", 409))

    def test_a_real_smtp_connection_is_refused_by_the_kit(self):
        self.pyrunner_connections.smtplib = testing._SMTP_GUARD
        with self.assertRaisesMessage(AssertionError, "opened a real SMTP connection (smtp.example.com:587)"):
            self.smtp.send("ada@example.com", "Hi", "x")


class _tempfile:
    """A temporary file holding ``data``, deleted afterwards (a context manager giving its path)."""

    def __init__(self, data, suffix=".pdf"):
        self.data, self.suffix = data, suffix

    def __enter__(self):
        import os
        import tempfile

        fd, self.path = tempfile.mkstemp(suffix=self.suffix)
        with os.fdopen(fd, "wb") as f:
            f.write(self.data)
        return self.path

    def __exit__(self, *exc):
        import os

        os.unlink(self.path)
        return False
