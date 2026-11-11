"""The slack integration's tests: the conformance suite and exchanges replayed offline (core/integrations/testing.py).

The cassettes are hand-built from Slack's public Web API reference
(api.slack.com/methods): no live workspace was used. The fake-service tests
pin what the cassettes can't show in one place (where the token goes during
an upload, Slack's ``"ok": false`` replies, the re-auth after ``invalid_auth``,
argument checks).
"""

import tempfile
from pathlib import Path

from core.integrations import testing

ALERTS, REPORTS = "C061EG9SL", "C07REPORTS"
OK = {"ok": True, "channel": ALERTS, "ts": "1.2", "message": {"text": "hi"}}


class SlackConformance(testing.ConformanceSuite):
    integration = "slack"
    ok_reply = {"json": OK}
    pages = [
        {"json": {"ok": True, "channels": [{"id": "C1"}, {"id": "C2"}],
                  "response_metadata": {"next_cursor": "dGVhbTpDMg=="}}},
        {"json": {"ok": True, "channels": [{"id": "C3"}], "response_metadata": {"next_cursor": ""}}},
    ]
    page_items = [{"id": "C1"}, {"id": "C2"}, {"id": "C3"}]

    def call(self, module):
        return module.post_message(ALERTS, "hi")

    def paginate_call(self, module, limit=None):
        return module.list_channels(limit=limit)


class _Slack(testing.IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.slack = self.integration("slack")
        self.conn = self.use_connection("slack", provider="slack")

    def _error(self, fn, *args, **kwargs):
        with self.assertRaises(self.integration_client.IntegrationError) as ctx:
            fn(*args, **kwargs)
        self.assertNoSecrets(str(ctx.exception))
        return ctx.exception


class SlackCassetteTests(_Slack):
    def test_post_a_message_a_thread_reply_and_blocks(self):
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "*Deploy finished* <https://example.com|log>"}}]
        with self.cassette("post_message"):
            sent = self.slack.post_message("#alerts", "*Disk almost full* on web-1")
            reply = self.slack.post_message(sent["channel"], "Cleaned up: 40 GB free.", thread_ts=sent["ts"],
                                            reply_broadcast=True)
            rich = self.slack.post_message(ALERTS, "Deploy finished", blocks=blocks, unfurl_links=False)
        self.assertEqual((sent["channel"], sent["ts"]), (ALERTS, "1731340800.000100"))
        self.assertEqual(reply["message"]["thread_ts"], "1731340800.000100")
        # Slack's envelope (ok, warning, response_metadata) is left out.
        self.assertEqual(set(rich), {"channel", "ts", "message"})

    def test_update_a_message(self):
        with self.cassette("update_message"):
            updated = self.slack.update_message(ALERTS, "1731340800.000100", "Disk on web-1 back to 61%")
        self.assertEqual((updated["ts"], updated["text"]), ("1731340800.000100", "Disk on web-1 back to 61%"))

    def test_upload_bytes_to_a_channel_by_name(self):
        with self.cassette("upload_file"):
            uploaded = self.slack.upload_file(b"name,email\nAda,ada@example.com\n", "#reports", filename="leads.csv",
                                              title="Signups this week", comment="This week's signups")
        self.assertEqual(uploaded, {"id": "F07GFQ5TQ9M", "title": "Signups this week"})

    def test_list_channels_and_find_one_by_name(self):
        with self.cassette("list_channels"):
            names = [c["name"] for c in self.slack.list_channels()]
            alerts = self.slack.find_channel("#Alerts")
            missing = self.slack.find_channel("nope")
        self.assertEqual(names, ["general", "alerts", "reports"])
        self.assertEqual((alerts["id"], alerts["topic"]["value"]), (ALERTS, "Pager alerts"))
        self.assertIsNone(missing)

    def test_history_newest_first_with_a_limit(self):
        with self.cassette("history"):
            messages = list(self.slack.history(ALERTS, oldest="1731340000.000000", limit=2))
        self.assertEqual([m["ts"] for m in messages], ["1731340950.000400", "1731340900.000300"])

    def test_list_users_leaves_out_bots_and_deactivated_people(self):
        with self.cassette("list_users"):
            people = [u["name"] for u in self.slack.list_users()]
        self.assertEqual(people, ["ada", "alan"])
        with self.cassette("list_users"):
            everyone = [u["name"] for u in self.slack.list_users(include_bots=True, include_deleted=True)]
        self.assertEqual(everyone, ["slackbot", "ada", "pyrun", "grace", "alan"])

    def test_ok_false_replies_raise_with_slacks_error_name(self):
        with self.cassette("errors"):
            outside = self._error(self.slack.post_message, REPORTS, "hi")
            missing = self._error(self.slack.post_message, "C0MISSING", "hi")
            scope = self._error(lambda: list(self.slack.history(ALERTS)))
            old = self._error(self.slack.update_message, ALERTS, "1731340800.000100", "x")
            bad = self._error(self.slack.update_message, ALERTS, "1.0", "x")
        self.assertEqual((outside.code, outside.status, outside.slack_error), ("FORBIDDEN", 200, "not_in_channel"))
        self.assertIn("chat.postMessage: Slack answered 'not_in_channel'", str(outside))
        self.assertEqual((missing.code, missing.slack_error), ("NOT_FOUND", "channel_not_found"))
        self.assertEqual(scope.code, "FORBIDDEN")
        self.assertIn("needs the channels:history scope; it has chat:write,files:write", str(scope))
        self.assertEqual((old.code, old.slack_error), ("FORBIDDEN", "cant_update_message"))
        self.assertEqual((bad.code, bad.slack_error), ("INVALID_REQUEST", "invalid_ts_latest"))


class SlackRequestTests(_Slack):
    def setUp(self):
        super().setUp()
        self.fake = self.fake_http("https://slack.com/api")

    def test_the_token_goes_to_slack_as_a_bearer_header_and_never_to_the_upload_host(self):
        upload = "https://files.slack.com/upload/v1/abc"
        self.fake.add("POST", "/files.getUploadURLExternal", json={"ok": True, "upload_url": upload, "file_id": "F1"})
        self.fake.add("POST", upload, body=b"OK - 5", headers={"Content-Type": "text/plain"})
        self.fake.add("POST", "/files.completeUploadExternal", json={"ok": True, "files": [{"id": "F1", "title": "a"}]})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "notes.txt"
            path.write_bytes(b"hello")
            self.assertEqual(self.slack.upload_file(path)["id"], "F1")
        ask, put, done = self.fake.requests
        token = self.conn.credentials["access_token"]
        self.assertEqual(ask.headers["Authorization"], f"Bearer {token}")
        self.assertEqual(ask.body, b"filename=notes.txt&length=5")
        self.assertNotIn("Authorization", put.headers)
        self.assertEqual((put.url, put.body, put.headers["Content-Type"]), (upload, b"hello", "application/octet-stream"))
        # No channel: the file stays private to the app (no channel_id, no comment).
        self.assertEqual(done.body, b"files=%5B%7B%22id%22%3A+%22F1%22%2C+%22title%22%3A+%22notes.txt%22%7D%5D")
        self.assertEqual(done.headers["Authorization"], f"Bearer {token}")

    def test_an_upload_to_a_channel_id_needs_no_lookup_and_bytes_need_a_name(self):
        self.fake.add("POST", "/files.getUploadURLExternal",
                      json={"ok": True, "upload_url": "https://files.slack.com/upload/v1/x", "file_id": "F2"})
        self.fake.add("POST", "https://files.slack.com/upload/v1/x", body=b"OK - 2")
        self.fake.add("POST", "/files.completeUploadExternal", json={"ok": True, "files": [{"id": "F2"}]})
        self.slack.upload_file(b"hi", REPORTS, filename="a.txt", thread_ts="1.5", alt_text="A chart")
        ask, _put, done = self.fake.requests
        self.assertIn(b"alt_txt=A+chart", ask.body)
        self.assertIn(b"channel_id=C07REPORTS", done.body)
        self.assertIn(b"thread_ts=1.5", done.body)
        with self.assertRaises(ValueError):
            self.slack.upload_file(b"hi", REPORTS)
        self.assertEqual(len(self.fake.requests), 3)

    def test_a_channel_name_nobody_has_raises_not_found_before_uploading(self):
        self.fake.add("GET", "/conversations.list", json={"ok": True, "channels": [], "response_metadata": {}})
        error = self._error(self.slack.upload_file, b"x", "#nowhere", filename="a.txt")
        self.assertEqual(error.code, "NOT_FOUND")
        self.assertEqual(len(self.fake.requests), 1)

    def test_invalid_auth_gets_new_credentials_and_one_retry(self):
        self.fake.add("POST", "/chat.postMessage", json={"ok": False, "error": "invalid_auth"})
        self.fake.add("POST", "/chat.postMessage", json=OK)
        old = self.conn.credentials["access_token"]
        self.assertEqual(self.slack.post_message(ALERTS, "hi")["ts"], "1.2")
        first, second = self.fake.requests
        self.assertEqual(first.headers["Authorization"], f"Bearer {old}")
        self.assertEqual(second.headers["Authorization"], f"Bearer {self.conn.credentials['access_token']}")
        self.assertNotEqual(old, self.conn.credentials["access_token"])
        self.assertEqual([refused for _name, refused in self.connections.calls].count(True), 1)

    def test_a_second_invalid_auth_or_nothing_new_is_unauthorized(self):
        self.fake.default(json={"ok": False, "error": "token_expired"})
        error = self._error(self.slack.post_message, ALERTS, "hi")
        self.assertEqual((error.code, error.slack_error, len(self.fake.requests)), ("UNAUTHORIZED", "token_expired", 2))
        self.connections.by_name.clear()
        self.use_connection("slack", provider="slack", rotate=False)
        fake = self.fake_http("https://slack.com/api")
        fake.default(json={"ok": False, "error": "invalid_auth"})
        self.assertEqual(self._error(self.slack.post_message, ALERTS, "hi").code, "UNAUTHORIZED")
        self.assertEqual(len(fake.requests), 1)

    def test_other_ok_false_errors_are_not_retried(self):
        for error, code in (("ratelimited", "RATE_LIMITED"), ("internal_error", "SERVER_ERROR"),
                            ("is_archived", "CONFLICT"), ("invalid_blocks", "INVALID_REQUEST"),
                            ("no_text", "INVALID_REQUEST"), ("something_new", "SLACK_ERROR")):
            with self.subTest(error):
                fake = self.fake_http("https://slack.com/api")
                fake.default(json={"ok": False, "error": error})
                raised = self._error(self.slack.post_message, ALERTS, "hi")
                self.assertEqual((raised.code, raised.slack_error, len(fake.requests)), (code, error, 1))

    def test_a_message_needs_text_or_blocks(self):
        with self.assertRaises(ValueError):
            self.slack.post_message(ALERTS)
        with self.assertRaises(ValueError):
            self.slack.update_message(ALERTS, "1.2", "", blocks=[])
        self.assertEqual(self.fake.requests, [])

    def test_limits_ask_for_small_pages_and_stop(self):
        self.fake.add("GET", "/users.list", json={"ok": True, "members": [{"id": "U1"}, {"id": "U2"}],
                                                  "response_metadata": {"next_cursor": "n"}})
        self.assertEqual([u["id"] for u in self.slack.list_users(limit=1)], ["U1"])
        self.assertEqual(list(self.slack.list_users(limit=0)), [])
        self.fake.add("GET", "/conversations.list", json={"ok": True, "channels": [{"id": "C1"}]})
        list(self.slack.list_channels(limit=3, include_archived=True, types="public_channel,private_channel"))
        self.assertEqual(self.fake.requests[-1].params, {"types": "public_channel,private_channel",
                                                         "exclude_archived": "false", "limit": "3"})
        self.assertEqual(len(self.fake.requests), 2)

    def test_client_checks_ok_false_too(self):
        self.fake.add("POST", "/reactions.add", json={"ok": False, "error": "already_reacted"})
        error = self._error(self.slack.client().post, "/reactions.add", json={"name": "eyes"})
        self.assertEqual((error.code, error.slack_error), ("SLACK_ERROR", "already_reacted"))
