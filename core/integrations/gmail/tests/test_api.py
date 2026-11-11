"""The gmail integration's tests: the conformance suite and exchanges replayed offline (core/integrations/testing.py).

The cassettes are hand-built from Google's public Gmail API reference
(developers.google.com/gmail/api/reference/rest): no live account was used.
Sent messages are built by the module itself, with boundaries made from their
content, so a cassette pins every byte of what goes to Gmail. The fake-service
tests pin what the cassettes can't show in one place (the API host, label
lookups, reply addressing, large messages, limits, argument checks).
"""

import base64
import email
import email.policy
from unittest import mock

from core.integrations import framework, testing

GMAIL = "https://gmail.googleapis.com"
ME = "/gmail/v1/users/me"
INVOICE = "18f2a6b0c1d2e3f4"


def _sent(request):
    """The message a send request carried, parsed (``raw`` in the JSON body)."""
    raw = request.json()["raw"]
    data = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    return email.message_from_bytes(data.replace(b"\r\n", b"\n"), policy=email.policy.default)


class GmailConformance(testing.ConformanceSuite):
    integration = "gmail"
    ok_reply = {"json": {"labels": []}}
    pages = [
        {"json": {"messages": [{"id": "m1", "threadId": "t1"}, {"id": "m2", "threadId": "t2"}],
                  "nextPageToken": "p2"}},
        {"json": {"messages": [{"id": "m3", "threadId": "t3"}]}},
    ]
    page_items = [{"id": "m1", "threadId": "t1"}, {"id": "m2", "threadId": "t2"}, {"id": "m3", "threadId": "t3"}]

    def call(self, module):
        return module.list_labels()

    def paginate_call(self, module, limit=None):
        return module.search("is:unread", limit=limit)


class _Gmail(testing.IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.gmail = self.integration("gmail")
        self.conn = self.use_connection("google", provider="google")

    def _error(self, fn, *args, **kwargs):
        with self.assertRaises(self.integration_client.IntegrationError) as ctx:
            fn(*args, **kwargs)
        self.assertNoSecrets(str(ctx.exception))
        return ctx.exception


class GmailCassetteTests(_Gmail):
    def test_search_pages_labels_by_name_and_no_match(self):
        with self.cassette("search"):
            found = list(self.gmail.search("is:unread has:attachment"))
            labelled = list(self.gmail.search(labels=["INBOX", "invoices"], limit=5))
            none = list(self.gmail.search("from:nobody@example.com"))
        self.assertEqual([m["id"] for m in found], [INVOICE, "18f29f00aa11bb22", "18f28800cc33dd44"])
        self.assertEqual(labelled, [{"id": INVOICE, "threadId": INVOICE}])
        self.assertEqual(none, [])

    def test_get_message_decodes_headers_bodies_and_attachments(self):
        with self.cassette("get_message"):
            msg = self.gmail.get_message(INVOICE)
            meta = self.gmail.get_message(INVOICE, format="metadata")
            latin = self.gmail.get_message("18f2a6b0c1d2e400")
        self.assertEqual((msg["subject"], msg["from"], msg["to"], msg["cc"]),
                         ("Invoice #1042 – November", "Billing <billing@example.org>", "me@example.com",
                          "Accounts <accounts@example.com>"))
        self.assertEqual((msg["message_id"], msg["date"], msg["internalDate"], msg["threadId"]),
                         ("<CAinv1042@mail.example.org>", "Mon, 9 Nov 2026 18:12:00 +0100", "1794330720000", INVOICE))
        self.assertEqual(msg["labelIds"], ["INBOX", "UNREAD", "Label_12"])
        self.assertEqual(msg["text"], "Hello,\r\n\r\nplease find attached invoice #1042 – 120 €.\r\n")
        self.assertEqual(msg["html"], "<p>Hello,</p><p>please find attached invoice #1042 – 120 €.</p>")
        self.assertEqual(msg["attachments"], [{"attachmentId": "ANGjdJ8wInvoicePdf0001", "partId": "1",
                                               "filename": "invoice-1042.pdf", "mimeType": "application/pdf",
                                               "size": 47120}])
        self.assertEqual(msg["headers"]["delivered-to"], "me@example.com")
        self.assertNotIn("payload", msg)
        self.assertEqual((meta["subject"], meta["text"], meta["html"], meta["attachments"]),
                         (msg["subject"], None, None, []))
        self.assertEqual(latin["text"], "Café ouvert à 8 h.\n")

    def test_send_text_html_bcc_and_an_attachment(self):
        with self.cassette("send"):
            sent = self.gmail.send(["Ada Lovelace <ada@example.com>", "grace@example.com"], "Weekly report",
                                   "The numbers are attached.", html="<p>The numbers are <b>attached</b>.</p>",
                                   bcc="audit@example.com", attachments=[("totals.csv", b"week,total\n45,1200\n")])
        self.assertEqual(sent, {"id": "18f2b0000000aaaa", "threadId": "18f2b0000000aaaa", "labelIds": ["SENT"]})

    def test_reply_all_in_the_thread(self):
        with self.cassette("reply"):
            sent = self.gmail.reply("18f2a7000000bbbb", "Thanks, the quote follows today.", reply_all=True)
        self.assertEqual(sent["threadId"], "18f2a6ff0000cccc")

    def test_labels_create_and_modify_by_name(self):
        with self.cassette("labels"):
            labels = self.gmail.list_labels()
            created = self.gmail.create_label("Processed")
            changed = self.gmail.modify_labels(INVOICE, add=["clients/acme", "STARRED"], remove=["UNREAD", "INBOX"])
        self.assertEqual([label["name"] for label in labels if label["type"] == "user"], ["Invoices", "Clients/Acme"])
        self.assertEqual(created["id"], "Label_14")
        self.assertEqual(changed["labelIds"], ["Label_12", "Label_13", "STARRED"])

    def test_download_an_attachment_to_bytes_and_a_file(self):
        attachment = {"attachmentId": "ANGjdJ8wInvoicePdf0001", "partId": "1", "filename": "invoice-1042.pdf"}
        with self.cassette("download_attachment"):
            data = self.gmail.download_attachment(INVOICE, attachment)
        self.assertEqual(data, b"%PDF-1.4\n% fake invoice 1042\n%%EOF\n")

    def test_new_messages_marks_then_reports_new_mail_once(self):
        with self.cassette("new_messages"):
            first, state = self.gmail.new_messages(None, label="INBOX")
            self.assertEqual((first, state), ([], {"history_id": "1010"}))
            inbox, state = self.gmail.new_messages(state, label="INBOX")
            self.assertEqual(([m["id"] for m in inbox], state), (["18f2c0000000eeee"], {"history_id": "1015"}))
            self.assertEqual((inbox[0]["subject"], inbox[0]["text"]), ("Your order has shipped", None))
            billing, state = self.gmail.new_messages(state, query="from:billing@example.org")
            self.assertEqual(([m["subject"] for m in billing], state), (["Invoice #1043"], {"history_id": "1024"}))
            expired, state = self.gmail.new_messages(state, query="from:billing@example.org")
        self.assertEqual((expired, state), ([], {"history_id": "2090"}))

    def test_errors_keep_googles_message(self):
        with self.cassette("errors"):
            missing = self._error(self.gmail.get_message, "0000000000000000")
            scope = self._error(self.gmail.send, "ada@example.com", "Hi", "x")
            exists = self._error(self.gmail.create_label, "Invoices")
        self.assertEqual((missing.code, missing.status), ("NOT_FOUND", 404))
        self.assertEqual((scope.code, scope.status), ("FORBIDDEN", 403))
        self.assertIn("insufficient authentication scopes", str(scope))
        self.assertEqual((exists.code, exists.status), ("CONFLICT", 409))
        self.assertIn("Label name exists", str(exists))


class GmailRequestTests(_Gmail):
    def setUp(self):
        super().setUp()
        self.fake = self.fake_http(GMAIL)

    def _message(self, mid, labels=("INBOX",), **headers):
        return {"id": mid, "threadId": f"t-{mid}", "labelIds": list(labels), "snippet": "",
                "payload": {"partId": "", "mimeType": "text/plain", "filename": "", "body": {"size": 0},
                            "headers": [{"name": k.replace("_", "-"), "value": v} for k, v in headers.items()]}}

    def test_the_token_goes_to_the_gmail_host_as_a_bearer_header(self):
        self.fake.add("GET", f"{ME}/labels", json={"labels": []})
        self.gmail.list_labels()
        sent = self.fake.requests[0]
        self.assertTrue(sent.url.startswith(f"{GMAIL}{ME}/labels"))
        self.assertEqual(sent.headers["Authorization"], f"Bearer {self.conn.credentials['access_token']}")
        self.assertNoSecrets(repr(self.gmail.client()))

    def test_message_ids_are_quoted_in_paths(self):
        self.fake.add("GET", f"{ME}/messages/a%2Fb", json=self._message("a/b"))
        self.assertEqual(self.gmail.get_message("a/b")["id"], "a/b")

    def test_an_unknown_label_name_is_refused_before_changing_anything(self):
        self.fake.add("GET", f"{ME}/labels", json={"labels": [{"id": "Label_1", "name": "Invoices"}]})
        with self.assertRaisesMessage(ValueError, "No Gmail label 'Nope': create it with gmail.create_label('Nope')"):
            self.gmail.modify_labels(INVOICE, add=["Invoices", "Nope"])
        self.assertEqual([r.path for r in self.fake.requests], [f"{ME}/labels"])
        with self.assertRaisesMessage(ValueError, "needs add= or remove="):
            self.gmail.modify_labels(INVOICE)
        with self.assertRaisesMessage(TypeError, "add is a label"):
            self.gmail.modify_labels(INVOICE, add={"Invoices": 1})

    def test_reply_to_a_message_you_sent_goes_to_its_recipients(self):
        self.fake.add("GET", f"{ME}/messages/s1", json=self._message(
            "s1", labels=("SENT",), From="me@example.com", To="Ada <ada@example.com>", Subject="Re: Plans",
            Message_ID="<s1@mail.gmail.com>"))
        self.fake.add("POST", f"{ME}/messages/send", json={"id": "r1", "threadId": "t-s1"})
        self.gmail.reply("s1", "One more thing.")
        sent = self.fake.requests[-1]
        msg = _sent(sent)
        self.assertEqual((msg["To"], msg["Subject"], msg["In-Reply-To"], msg["References"]),
                         ("Ada <ada@example.com>", "Re: Plans", "<s1@mail.gmail.com>", "<s1@mail.gmail.com>"))
        self.assertEqual(sent.json()["threadId"], "t-s1")
        self.assertNotIn("Cc", msg)

    def test_reply_all_to_mail_with_undisclosed_recipients(self):
        """oss-quality: a To/Cc that isn't a list of addresses raised ValueError instead of being left out."""
        self.fake.add("GET", f"{ME}/messages/n1", json=self._message(
            "n1", From="News <news@example.org>", To="undisclosed-recipients:;", Cc="broken@", Subject="News"))
        self.fake.add("GET", f"{ME}/profile", json={"emailAddress": "me@example.com"})
        self.fake.add("POST", f"{ME}/messages/send", json={"id": "r1"})
        self.gmail.reply("n1", "Unsubscribe me, please.", reply_all=True)
        msg = _sent(self.fake.requests[-1])
        self.assertEqual((msg["To"], msg["Cc"]), ("News <news@example.org>", None))

    def test_reply_without_reply_all_answers_the_sender_only(self):
        self.fake.add("GET", f"{ME}/messages/m1", json=self._message(
            "m1", From="Grace <grace@example.org>", To="me@example.com, linus@example.org", Subject="Lunch"))
        self.fake.add("POST", f"{ME}/messages/send", json={"id": "r1"})
        self.gmail.reply("m1", "Yes!", cc="ops@example.org")
        msg = _sent(self.fake.requests[-1])
        self.assertEqual((msg["To"], msg["Cc"], msg["Subject"], msg["In-Reply-To"]),
                         ("Grace <grace@example.org>", "ops@example.org", "Re: Lunch", None))
        self.assertEqual(len(self.fake.requests), 2)  # no profile lookup without reply_all

    def test_a_large_message_goes_as_an_upload(self):
        self.fake.add("POST", "/upload/gmail/v1/users/me/messages/send", json={"id": "big", "threadId": "big"})
        with mock.patch.object(self.gmail, "SIMPLE_MAX", 100):
            self.assertEqual(self.gmail.send("ada@example.com", "Big", "x" * 500)["id"], "big")
        sent = self.fake.requests[0]
        self.assertEqual(sent.params, {"uploadType": "multipart"})
        self.assertTrue(sent.headers["Content-Type"].startswith("multipart/related; boundary=pyrunner-"))
        self.assertIn(b"Content-Type: message/rfc822\r\n\r\n", sent.body)
        self.assertIn(b"Subject: Big", sent.body)

    def test_an_attachment_kept_inside_the_message(self):
        inline = base64.urlsafe_b64encode(b"tiny").decode().rstrip("=")
        message = self._message("m1")
        message["payload"]["parts"] = [{"partId": "1", "filename": "a.txt", "mimeType": "text/plain",
                                        "body": {"size": 4, "data": inline}}]
        self.fake.add("GET", f"{ME}/messages/m1", json=message, times=2)
        [attachment] = self.gmail.get_message("m1")["attachments"]
        self.assertIsNone(attachment["attachmentId"])
        self.assertEqual(self.gmail.download_attachment("m1", attachment), b"tiny")
        with self.assertRaisesMessage(TypeError, "attachment is one of a message's attachments"):
            self.gmail.download_attachment("m1", None)

    def test_download_attachment_to_a_path(self):
        self.fake.add("GET", f"{ME}/messages/m1/attachments/a1",
                      json={"size": 3, "data": base64.urlsafe_b64encode(b"pdf").decode()})
        path = self.enterContext(mock.patch("builtins.open", mock.mock_open()))
        self.assertEqual(self.gmail.download_attachment("m1", "a1", "/tmp/x.pdf"), "/tmp/x.pdf")
        path.return_value.write.assert_called_once_with(b"pdf")

    def test_new_messages_limit_keeps_the_rest_for_next_time(self):
        records = [
            {"id": "11", "messagesAdded": [{"message": {"id": "a"}}, {"message": {"id": "b"}}, {"message": {"id": "c"}}]},
            {"id": "12", "messagesAdded": [{"message": {"id": "d"}}]},
        ]

        def history(request):  # as Gmail: the records after startHistoryId
            start = int(request.params["startHistoryId"])
            return {"history": [r for r in records if int(r["id"]) > start], "historyId": "20"}

        self.fake.add("GET", f"{ME}/history", json=history, times=5)
        for mid in "abcd":
            self.fake.add("GET", f"{ME}/messages/{mid}", json=self._message(mid), times=4)
        # Stopped inside a record: the mark stays before it, and what was reported from it is kept.
        first, state = self.gmail.new_messages({"history_id": "10"}, limit=1)
        self.assertEqual(([m["id"] for m in first], state), (["a"], {"history_id": "10", "seen": ["a"]}))
        second, state = self.gmail.new_messages(state, limit=1)
        self.assertEqual(([m["id"] for m in second], state), (["b"], {"history_id": "10", "seen": ["a", "b"]}))
        # Stopped after a whole record: the mark moves past it.
        third, state = self.gmail.new_messages(state, limit=1)
        self.assertEqual(([m["id"] for m in third], state), (["c"], {"history_id": "11"}))
        fourth, state = self.gmail.new_messages(state, limit=5)
        self.assertEqual(([m["id"] for m in fourth], state), (["d"], {"history_id": "20"}))
        self.assertEqual(self.gmail.new_messages(state), ([], {"history_id": "20"}))

    def test_new_messages_leaves_out_drafts_spam_and_a_lost_message(self):
        self.fake.add("GET", f"{ME}/history", json={"history": [{"id": "11", "messagesAdded": [
            {"message": {"id": "d"}}, {"message": {"id": "s"}}, {"message": {"id": "gone"}},
            {"message": {"id": "self"}}]}], "historyId": "12"})
        self.fake.add("GET", f"{ME}/messages/d", json=self._message("d", labels=("DRAFT",)))
        self.fake.add("GET", f"{ME}/messages/s", json=self._message("s", labels=("SPAM",)))
        self.fake.add("GET", f"{ME}/messages/gone", status=404, json={"error": {"message": "Not Found"}})
        self.fake.add("GET", f"{ME}/messages/self", json=self._message("self", labels=("SENT", "INBOX")))
        found, state = self.gmail.new_messages({"history_id": "10"})
        self.assertEqual(([m["id"] for m in found], state), (["self"], {"history_id": "12"}))

    def test_new_messages_with_a_label_counts_mail_that_gets_it_later(self):
        self.fake.add("GET", f"{ME}/labels", json={"labels": [{"id": "Label_7", "name": "To do"}]})
        self.fake.add("GET", f"{ME}/history", json={"history": [
            {"id": "11", "labelsAdded": [{"message": {"id": "old"}, "labelIds": ["Label_7"]}]},
            {"id": "12", "labelsAdded": [{"message": {"id": "starred"}, "labelIds": ["STARRED"]}]},
        ], "historyId": "13"})
        self.fake.add("GET", f"{ME}/messages/old", json=self._message("old", labels=("Label_7",)))
        found, state = self.gmail.new_messages({"history_id": "10"}, label="to do")
        self.assertEqual(([m["id"] for m in found], state), (["old"], {"history_id": "13"}))
        query = self.fake.requests[1].url
        self.assertIn("labelId=Label_7", query)
        self.assertIn("historyTypes=messageAdded&historyTypes=labelAdded", query)

    def test_argument_checks(self):
        with self.assertRaisesMessage(ValueError, "format is one of full, metadata"):
            self.gmail.get_message("m1", format="raw")
        with self.assertRaisesMessage(ValueError, "a message needs a recipient"):
            self.gmail.send(None, "Hi", "x")
        with self.assertRaisesMessage(ValueError, "subject can't hold a line break"):
            self.gmail.send("ada@example.com", "Hi\r\nBcc: x@example.com", "x")
        self.assertEqual(self.fake.requests, [])

    def test_a_send_is_not_repeated_after_a_5xx(self):
        self.fake.add("POST", f"{ME}/messages/send", status=500, json={"error": {"message": "backend"}})
        error = self._error(self.gmail.send, "ada@example.com", "Hi", "x")
        self.assertEqual((error.code, len(self.fake.requests)), ("SERVER_ERROR", 1))


class GmailTriggerTests(_Gmail):
    def test_gmail_new_message_events(self):
        [trigger] = framework.load_triggers("gmail")
        self.assertEqual(trigger.key, "gmail_new_message")
        self.assertEqual(set(trigger.config_schema["properties"]), {"label", "query"})
        with self.cassette("new_messages", allow_unused=True):
            first, state = trigger.check(self.gmail, "google", {"label": "INBOX"}, None)
            events, state = trigger.check(self.gmail, "google", {"label": "INBOX"}, state, limit=10)
        self.assertEqual((first, state), ([], {"history_id": "1015"}))
        self.assertEqual([e["id"] for e in events], ["18f2c0000000eeee"])
        item = events[0]["item"]
        self.assertEqual((item["subject"], item["from"], item["labelIds"]),
                         ("Your order has shipped", "Shop <orders@shop.example>", ["INBOX", "UNREAD"]))
        self.assertFalse({"headers", "text", "html", "attachments"} & set(item))
