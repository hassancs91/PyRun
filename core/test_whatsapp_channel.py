"""
WhatsApp channel: text, templates and status tracking (2.5 Stage 8, ticket T36).

What this pins, roughly in order of how much it would hurt to get wrong:

- The webhook answers Meta's GET verification only for the right verify token,
  and accepts a POST only with a valid ``X-Hub-Signature-256`` (HMAC-SHA256 of
  the raw body with the app secret): a tampered body is refused.
- Inbound text, button and list replies, template quick replies, locations,
  contacts and reactions become one InboundMessage each (a batched POST is
  split per message, de-duplicated by wamid); media arrive as metadata only
  (nothing is downloaded); group messages and other phone numbers' events
  are ignored. The run event carries message_type, details and media.
- Outbound: free-form text inside the 24-hour window, the channel's default
  template (the text as its {{1}}) outside it, a clear failure with no
  default template; a script's explicit template goes through the ledger and
  is kept on the row, so a retry sends the same thing.
- Status callbacks move the ledger row forward (sent -> delivered -> read,
  never backwards) and mark an undelivered message failed.
- Errors are classified for the ledger (bad token never retried, rate limits
  and 5xx retried); mark-as-read follows every dispatched message.

Every Graph API call goes to ``FakeWhatsApp``, a stand-in for the WhatsApp
Cloud API patched over ``requests.post``/``requests.get`` in the provider:
nothing reaches graph.facebook.com.
"""

import hashlib
import hmac
import json
from datetime import timedelta
from unittest import mock

import requests
from cryptography.fernet import Fernet
from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import (
    Channel,
    ChannelMember,
    ChannelMessage,
    Environment,
    GlobalSettings,
    Run,
    Script,
    User,
    Workspace,
)
from core.services import EncryptionService
from core.services.channels import ChannelError, OutboundMessage, get_provider, ledger, list_providers
from core.services.channels.inbound import dispatch_inbound
from core.services.channels.whatsapp import (
    BAD_TOKEN_MESSAGE,
    GRAPH_URL,
    OUT_OF_WINDOW_MESSAGE,
    TEXT_LIMIT,
    clean_template,
    template_param,
)
from core.services.datastore_token import mint_datastore_token
from core.services.run_events import channel_event
from core.test_channel_ledger import _Resp
from core.test_channels import _setup_wizard_off

_TEST_KEY = Fernet.generate_key().decode()
PNID = "106540352242922"
WABA = "102290129340398"
TOKEN = "EAAG-test-system-user-token"
APP_SECRET = "app-secret-123"
VERIFY = "my-verify-token"
ANN = "15550001111"
# Where FakeWhatsApp serves inbound media files (WhatsApp's own is lookaside.fbsbx.com).
MEDIA_HOST = "https://media.fake-whatsapp.test/whatsapp_business/attachments/"


class _File:
    """A streamed binary answer (a media download)."""

    def __init__(self, status, data=b"", chunk=4):
        self.status_code = status
        self._data = data
        self._chunk = chunk
        self.closed = False

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self._data), self._chunk):
            yield self._data[i:i + self._chunk]

    def json(self):
        raise ValueError("not JSON")

    def close(self):
        self.closed = True


class FakeWhatsApp:
    """A stand-in for the WhatsApp Cloud API (Graph API), patched over the
    provider's ``requests.post`` and ``requests.get``.

    ``reply(...)`` queues answers in order; with the queue empty every call
    succeeds (a send gets the next ``wamid.N``). ``calls`` keeps
    ``(method, path, json, headers)``; a media download is
    ``("DOWNLOAD", url, kwargs, headers)``. ``add_media(id, data, mime)``
    makes ``GET /{id}`` answer a download link on ``MEDIA_HOST`` that serves
    ``data`` (the SSRF guard passes that host only).
    """

    def __init__(self, test):
        from core.netguard import is_safe_endpoint_url

        self.calls = []
        self._queue = []
        self._next = 0
        self.media = {}
        for name in ("post", "get"):
            patcher = mock.patch(f"core.services.channels.whatsapp.requests.{name}", side_effect=getattr(self, f"_{name}"))
            patcher.start()
            test.addCleanup(patcher.stop)
        guard = mock.patch(
            "core.services.channels.whatsapp.is_safe_endpoint_url",
            side_effect=lambda url: (True, "") if url.startswith(MEDIA_HOST) else is_safe_endpoint_url(url),
        )
        guard.start()
        test.addCleanup(guard.stop)

    def add_media(self, media_id, data, mime="image/jpeg", file_size=None):
        self.media[media_id] = (data, mime, len(data) if file_size is None else file_size)
        return self

    def reply(self, answer):
        self._queue.append(answer)
        return self

    def error(self, status, code, message="Error", **extra):
        return self.reply(_Resp(status, {"error": {"message": message, "type": "OAuthException", "code": code, **extra}}))

    def network_error(self):
        return self.reply(requests.ConnectionError("boom"))

    def _answer(self, default):
        answer = self._queue.pop(0) if self._queue else None
        if isinstance(answer, Exception):
            raise answer
        return answer if answer is not None else default()

    def _post(self, url, json=None, headers=None, timeout=None):
        assert url.startswith(GRAPH_URL + "/"), url
        path = url[len(GRAPH_URL) + 1:]
        self.calls.append(("POST", path, json, headers))

        def ok():
            if (json or {}).get("status") == "read":
                return _Resp(200, {"success": True})
            self._next += 1
            return _Resp(200, {"messaging_product": "whatsapp", "contacts": [{"input": json.get("to"), "wa_id": json.get("to")}],
                               "messages": [{"id": f"wamid.{self._next}"}]})

        return self._answer(ok)

    def _get(self, url, params=None, headers=None, timeout=None, **kwargs):
        if url.startswith(MEDIA_HOST):
            self.calls.append(("DOWNLOAD", url, kwargs, headers))
            media_id = url[len(MEDIA_HOST):].split("?")[0]
            return self._answer(lambda: _File(200, self.media[media_id][0]) if media_id in self.media else _File(404))
        assert url.startswith(GRAPH_URL + "/"), url
        path = url[len(GRAPH_URL) + 1:]
        self.calls.append(("GET", path, params, headers))
        if path in self.media:
            data, mime, size = self.media[path]
            return self._answer(lambda: _Resp(200, {
                "messaging_product": "whatsapp", "url": f"{MEDIA_HOST}{path}?ext=1&hash=abc", "mime_type": mime,
                "sha256": hashlib.sha256(data).hexdigest(), "file_size": size, "id": path,
            }))
        if path.endswith("/phone_numbers"):
            return self._answer(lambda: _Resp(200, {"data": [{"id": "1"}, {"id": PNID}]}))
        return self._answer(lambda: _Resp(200, {"display_phone_number": "+1 555-010-0000", "verified_name": "PyRun Test", "id": PNID}))

    @property
    def sends(self):
        return [body for method, path, body, _ in self.calls if method == "POST" and body.get("type")]

    @property
    def reads(self):
        return [body for method, path, body, _ in self.calls if method == "POST" and body.get("status") == "read"]


def _message(kind="text", sender=ANN, wamid="wamid.IN1", **part):
    body = {"text": {"body": "hello"}}.get(kind, {}) if not part else part
    message = {"from": sender, "id": wamid, "timestamp": "1731400000", "type": kind}
    message[kind] = body
    return message


def _payload(*messages, statuses=None, pnid=PNID, contacts=None):
    value = {"messaging_product": "whatsapp", "metadata": {"display_phone_number": "15550100000", "phone_number_id": pnid}}
    if messages:
        value["contacts"] = contacts if contacts is not None else [{"profile": {"name": "Ann"}, "wa_id": ANN}]
        value["messages"] = list(messages)
    if statuses:
        value["statuses"] = statuses
    return {"object": "whatsapp_business_account", "entry": [{"id": WABA, "changes": [{"field": "messages", "value": value}]}]}


def _sign(body: bytes, secret=APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@override_settings(ENCRYPTION_KEY=_TEST_KEY)
class _Base(TestCase):
    def setUp(self):
        EncryptionService.reset()
        self.addCleanup(EncryptionService.reset)
        cache.clear()
        _setup_wizard_off(self)
        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(name="e", path="p")
        self.script = Script.objects.create(name="bot", code="x", environment=self.env, workspace=self.ws, is_enabled=True)
        self.ch = self._channel()
        self.wa = FakeWhatsApp(self)
        self.provider = get_provider("whatsapp")

    def _channel(self, name="Support", pnid=PNID, **fields):
        creds = {"phone_number_id": pnid, "waba_id": WABA, "access_token": TOKEN, "app_secret": APP_SECRET,
                 "verify_token": VERIFY}
        creds.update(fields.pop("creds", {}))
        ch = Channel(workspace=self.ws, provider="whatsapp", name=name, **fields)
        ch.set_credentials(creds, identity=pnid)
        ch.config = {"default_target": ANN}
        ch.save()
        return ch

    def _inbound(self, when=None, number=ANN):
        row = ChannelMessage.objects.create(
            channel=self.ch, direction="in", text="hi", reply_ref_json={"chat_id": number}, status="received",
        )
        if when is not None:
            ChannelMessage.objects.filter(pk=row.pk).update(created_at=when)
        return row

    def _out(self, **filters):
        return ChannelMessage.objects.filter(direction="out", **filters)


class ProviderSetupTests(_Base):
    def test_whatsapp_is_registered_with_its_credential_fields(self):
        self.assertIn("whatsapp", list_providers())
        self.assertIn(("whatsapp", "WhatsApp"), Channel.Provider.choices)
        fields = {f.name: (f.secret, f.required) for f in self.provider.credential_fields}
        self.assertEqual(fields, {
            "phone_number_id": (False, True), "waba_id": (False, True), "access_token": (True, True),
            "app_secret": (True, False), "verify_token": (True, False),
            "default_template": (False, False), "default_template_language": (False, False),
        })
        self.assertEqual(self.provider.identity_field, "phone_number_id")
        self.assertEqual(self.ch.creds_fingerprint, Channel.fingerprint_for("whatsapp", PNID))

    def test_the_add_page_asks_for_the_whatsapp_fields(self):
        admin = User.objects.create_superuser(username="a", email="a@x.io", password="pw")
        self.client.force_login(admin)
        html = self.client.get(reverse("cpanel:channel_create") + "?provider=whatsapp").content.decode()
        for text in ('name="phone_number_id"', 'name="waba_id"', 'name="access_token"', 'name="app_secret"',
                     'name="verify_token"', 'name="default_template"', "WhatsApp setup", "Default phone number",
                     "whatsapp_business_messaging"):
            self.assertIn(text, html)
        data = {"name": "WA", "provider": "whatsapp", "phone_number_id": "222", "waba_id": "333",
                "access_token": "tok", "default_target": ANN, "daily_send_cap": "0", "enabled": "on"}
        resp = self.client.post(reverse("cpanel:channel_create"), data)
        self.assertEqual(resp.status_code, 302)
        ch = Channel.objects.get(name="WA")
        self.assertEqual(ch.get_credentials(), {"phone_number_id": "222", "waba_id": "333", "access_token": "tok"})
        self.assertNotIn("tok", ch.creds_encrypted)
        # The number is the identity: the same phone number id can't be a second channel.
        resp = self.client.post(reverse("cpanel:channel_create"), {**data, "name": "WA2"})
        self.assertIn("already connected as another channel", resp.content.decode())
        # Edit shows the ids, never the secrets.
        html = self.client.get(reverse("cpanel:channel_edit", args=[ch.pk])).content.decode()
        self.assertIn('value="222"', html)
        self.assertNotIn('value="tok"', html)

    def test_an_undecryptable_blob_reads_as_not_configured(self):
        Channel.objects.filter(pk=self.ch.pk).update(creds_encrypted="not-a-fernet-token")
        self.ch.refresh_from_db()
        self.assertFalse(self.ch.is_configured)
        admin = User.objects.create_superuser(username="a", email="a@x.io", password="pw")
        self.client.force_login(admin)
        html = self.client.get(reverse("cpanel:channel_edit", args=[self.ch.pk])).content.decode()
        self.assertNotIn("Leave blank to keep", html)
        self.assertIn("The stored access token can&#x27;t be decrypted", html)

    def test_test_connection_reports_the_number(self):
        ok, message = self.provider.test_connection(self.ch)
        self.assertEqual((ok, message), (True, "Connected as +1 555-010-0000 (PyRun Test)."))
        method, path, params, headers = self.wa.calls[0]
        self.assertEqual((method, path, headers), ("GET", PNID, {"Authorization": f"Bearer {TOKEN}"}))
        self.assertEqual(self.wa.calls[1][1], f"{WABA}/phone_numbers")
        self.wa.error(401, 190, "Error validating access token")
        self.assertEqual(self.provider.test_connection(self.ch), (False, BAD_TOKEN_MESSAGE))

    def test_test_connection_checks_the_number_is_in_the_business_account(self):
        self.wa.reply(None).reply(_Resp(200, {"data": [{"id": "1"}]}))
        ok, message = self.provider.test_connection(self.ch)
        self.assertFalse(ok)
        self.assertIn(f"isn't in WhatsApp Business Account {WABA}", message)
        # A token without whatsapp_business_management still connects, with a note.
        self.wa.reply(None).error(403, 200, "Permissions error")
        ok, message = self.provider.test_connection(self.ch)
        self.assertTrue(ok)
        self.assertIn("couldn't be checked: WhatsApp: Permissions error (code 200)", message)
        ch = self._channel(name="Bad WABA", pnid="555", creds={"waba_id": "x/y"})
        self.assertEqual(self.provider.test_connection(ch)[0], False)
        self.assertNotIn("x/y/phone_numbers", [call[1] for call in self.wa.calls])

    def test_a_phone_number_id_that_isnt_an_id_never_reaches_the_url(self):
        ch = self._channel(name="Bad", pnid="../me")
        ok, message = self.provider.test_connection(ch)
        self.assertFalse(ok)
        self.assertIn("numeric id", message)
        self.assertEqual(self.wa.calls, [])


class OutboundTests(_Base):
    def test_text_inside_the_window(self):
        self._inbound(timezone.now() - timedelta(hours=2))
        row = ledger.deliver(self.ch, "x" * (TEXT_LIMIT + 10), kind="notification")
        self.assertEqual((row.status, row.provider_message_id, row.delivery_state), ("sent", "wamid.1", "sent"))
        self.assertEqual(row.content, {})
        body = self.wa.sends[0]
        self.assertEqual((body["to"], body["type"], body["recipient_type"]), (ANN, "text", "individual"))
        self.assertEqual(len(body["text"]["body"]), TEXT_LIMIT)
        self.assertEqual(self.wa.calls[0][1], f"{PNID}/messages")

    def test_outside_the_window_the_default_template_carries_the_text(self):
        self._inbound(timezone.now() - timedelta(hours=25))
        ch = self.ch
        creds = ch.get_credentials() | {"default_template": "pyrun_update", "default_template_language": "en_GB"}
        ch.set_credentials(creds, identity=PNID)
        ch.save()
        row = ledger.deliver(ch, "nightly failed\n\n  exit code 1\twith    spaces", kind="notification")
        row.refresh_from_db()
        self.assertEqual(row.status, "sent")
        self.assertEqual(row.content, {"template": {"name": "pyrun_update", "language": "en_GB", "auto": True}})
        body = self.wa.sends[0]
        self.assertEqual(body["type"], "template")
        self.assertEqual(body["template"]["name"], "pyrun_update")
        self.assertEqual(body["template"]["language"], {"code": "en_GB"})
        self.assertEqual(body["template"]["components"], [
            {"type": "body", "parameters": [{"type": "text", "text": "nightly failed | exit code 1 with spaces"}]},
        ])
        # The message log says it went as a template.
        admin = User.objects.create_superuser(username="a", email="a@x.io", password="pw")
        self.client.force_login(admin)
        html = self.client.get(reverse("cpanel:channel_edit", args=[ch.pk])).content.decode()
        self.assertIn('template <span class="font-mono">pyrun_update</span>', html)

    def test_outside_the_window_without_a_default_template_fails_clearly(self):
        row = ledger.deliver(self.ch, "hello", kind="notification")  # never messaged
        self.assertEqual((row.status, row.attempts, row.error), ("failed", 1, OUT_OF_WINDOW_MESSAGE))
        self.assertEqual(self.wa.sends, [])
        self.assertEqual(ledger.drain_due(timezone.now() + timedelta(days=1)), {"sent": 0, "retrying": 0, "failed": 0})

    def test_a_sender_in_the_approval_inbox_is_inside_the_window(self):
        ChannelMember.objects.create(channel=self.ch, sender_id=ANN, status="pending", last_seen_at=timezone.now())
        row = ledger.deliver(self.ch, "You're not approved yet.", kind="system", reply_ref={"chat_id": ANN})
        self.assertEqual(row.status, "sent")
        self.assertEqual(self.wa.sends[0]["type"], "text")

    def test_the_window_is_per_number(self):
        self._inbound(timezone.now(), number="15559990000")
        self.assertTrue(self.provider.window_open(self.ch, "15559990000"))
        self.assertFalse(self.provider.window_open(self.ch, ANN))

    def test_an_explicit_template_is_stored_and_resent_on_retry(self):
        template = {"name": "order_update", "language": "es",
                    "components": [{"type": "body", "parameters": [{"type": "text", "text": "1042"}]}]}
        self.wa.network_error()
        row = ledger.deliver(self.ch, "Template order_update", kind="script", content={"template": template})
        self.assertEqual(row.status, "retrying")
        ledger.drain_due(timezone.now() + timedelta(minutes=2))
        row.refresh_from_db()
        self.assertEqual((row.status, row.attempts), ("sent", 2))
        self.assertEqual(row.content, {"template": template})
        body = self.wa.sends[0]
        self.assertEqual(body["template"], {"name": "order_update", "language": {"code": "es"},
                                            "components": template["components"]})

    def test_errors_are_classified_for_the_ledger(self):
        self._inbound(timezone.now())
        cases = [
            (lambda: self.wa.network_error(), (True, False)),
            (lambda: self.wa.error(401, 190, "Invalid OAuth access token"), (False, True)),
            (lambda: self.wa.error(400, 190, "Session has expired"), (False, True)),
            (lambda: self.wa.error(403, 200, "Permissions error"), (False, True)),
            (lambda: self.wa.error(400, 130429, "Rate limit hit"), (True, False)),
            (lambda: self.wa.error(400, 131056, "Pair rate limit hit"), (True, False)),
            (lambda: self.wa.error(500, 131000, "Something went wrong"), (True, False)),
            (lambda: self.wa.error(400, 131047, "Re-engagement message"), (False, False)),
            (lambda: self.wa.error(400, 100, "Invalid parameter"), (False, False)),
            (lambda: self.wa.reply(_Resp(502, ValueError("not json"))), (True, False)),
            (lambda: self.wa.reply(_Resp(200, ["not", "a", "dict"])), (False, False)),
        ]
        for queue, expected in cases:
            queue()
            with self.assertRaises(ChannelError) as cm:
                self.provider.send(self.ch, OutboundMessage(text="x"))
            self.assertEqual((cm.exception.retryable, cm.exception.auth), expected, cm.exception)
            self.assertNotIn(TOKEN, str(cm.exception))

    def test_a_rejected_token_is_never_retried(self):
        self._inbound(timezone.now())
        self.wa.error(401, 190, "Error validating access token")
        row = ledger.deliver(self.ch, "x", kind="notification")
        self.assertEqual((row.status, row.error), ("failed", BAD_TOKEN_MESSAGE))

    def test_a_bad_recipient_fails_before_any_call(self):
        with self.assertRaises(ChannelError) as cm:
            self.provider.send(self.ch, OutboundMessage(text="x", reply_ref={"chat_id": "not a number"}))
        self.assertIn("isn't a WhatsApp number", str(cm.exception))
        self.assertEqual(self.wa.calls, [])

    def test_a_number_is_normalized(self):
        self._inbound(timezone.now())
        self.provider.send(self.ch, OutboundMessage(text="x", reply_ref={"chat_id": "+1 (555) 000-1111"}))
        self.assertEqual(self.wa.sends[0]["to"], ANN)

    def test_template_helpers(self):
        self.assertEqual(clean_template({"name": "a_b"}), {"name": "a_b", "language": "en_US", "components": []})
        for bad in ({"name": "Bad Name"}, {"name": "ok", "language": "english!"}, {"name": "ok", "components": "x"},
                    {"name": "ok", "extra": 1}, "order_update"):
            with self.assertRaises(ValueError):
                clean_template(bad)
        self.assertEqual(template_param(""), "-")
        self.assertEqual(len(template_param("y" * 5000)), 1024)


@override_settings(ENCRYPTION_KEY=_TEST_KEY)
class InternalTemplateSendTests(_Base):
    def setUp(self):
        super().setUp()
        self.run = Run.objects.create(script=self.script, workspace=self.ws, status=Run.Status.RUNNING)
        self.token = mint_datastore_token(self.run.id)

    def _post(self, body):
        return self.client.post(
            reverse("internal:channels_send"), data=json.dumps(body), content_type="application/json",
            REMOTE_ADDR="127.0.0.1", HTTP_AUTHORIZATION=f"Bearer {self.token}",
        )

    def test_a_script_sends_a_template(self):
        template = {"name": "order_update", "components": [{"type": "body", "parameters": [{"type": "text", "text": "7"}]}]}
        resp = self._post({"target": "Support", "reply_ref": {"chat_id": ANN}, "template": template})
        self.assertEqual(resp.status_code, 200, resp.content)
        row = self._out().get()
        self.assertEqual((row.kind, row.text, row.run_id), ("reply", "Template order_update", self.run.id))
        self.assertEqual(row.content, {"template": {**template, "language": "en_US"}})
        self.assertEqual(self.wa.sends[0]["type"], "template")

    def test_bad_templates_are_refused(self):
        self.assertEqual(self._post({"target": "Support", "template": {"name": "Bad Name"}}).status_code, 400)
        self.assertEqual(self._post({"target": "Support", "template": "order_update"}).status_code, 400)
        self.assertEqual(self._post({"target": "email", "text": "x", "template": {"name": "a"}}).status_code, 400)
        self.assertEqual(self._post({"target": "Support"}).status_code, 400)  # neither text nor template
        tg = Channel(workspace=self.ws, provider="telegram", name="TG")
        tg.set_credentials({"bot_token": "1:2"}, identity="1:2")
        tg.save()
        resp = self._post({"target": "TG", "text": "x", "template": {"name": "a"}})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("doesn't send templates", resp.json()["error"]["message"])
        self.assertEqual(self._out().count(), 0)

    def test_pyrunner_notify_passes_the_template(self):
        from core.script_helpers import pyrunner_notify

        with mock.patch.object(pyrunner_notify, "_post", return_value={"ok": True}) as post:
            pyrunner_notify.send("Support", reply_ref={"chat_id": ANN}, template={"name": "x"})
            pyrunner_notify.send("Ops", "plain")
        self.assertEqual(post.call_args_list[0].args[0],
                         {"target": "Support", "text": "", "reply_ref": {"chat_id": ANN}, "template": {"name": "x"}})
        self.assertEqual(post.call_args_list[1].args[0], {"target": "Ops", "text": "plain"})


class _WebhookBase(_Base):
    def setUp(self):
        super().setUp()
        self.ch.inbound_enabled = True
        self.ch.inbound_handler = "script"
        self.ch.inbound_target_id = self.script.id
        self.ch.inbound_access = Channel.InboundAccess.OPEN
        self.ch.ensure_inbound_token()
        self.ch.save()
        s = GlobalSettings.get_settings()
        s.worker_heartbeat_at = timezone.now()
        s.save(update_fields=["worker_heartbeat_at"])
        p = mock.patch("core.views.channel_webhooks.async_task")
        self.task = p.start()
        self.addCleanup(p.stop)

    def _url(self):
        return reverse("channel_webhook", args=[self.ch.inbound_token])

    def _hook(self, payload, signature=None, body=None):
        raw = body if body is not None else json.dumps(payload).encode()
        headers = {}
        if signature is not False:
            headers["HTTP_X_HUB_SIGNATURE_256"] = signature or _sign(raw)
        return self.client.post(self._url(), data=raw, content_type="application/json", REMOTE_ADDR="1.2.3.4", **headers)

    def _queued(self):
        return [call.args[2] for call in self.task.call_args_list]


class WebhookTests(_WebhookBase):
    def test_the_verification_challenge(self):
        good = {"hub.mode": "subscribe", "hub.verify_token": VERIFY, "hub.challenge": "1158201444"}
        resp = self.client.get(self._url(), good)
        self.assertEqual((resp.status_code, resp.content, resp["Content-Type"]), (200, b"1158201444", "text/plain"))
        self.assertEqual(self.client.get(self._url(), {**good, "hub.verify_token": "nope"}).status_code, 403)
        self.assertEqual(self.client.get(self._url(), {**good, "hub.mode": "unsubscribe"}).status_code, 403)
        self.assertEqual(self.client.get(self._url(), {**good, "hub.challenge": ""}).status_code, 403)
        self.assertEqual(self.client.get(self._url()).status_code, 405)  # not a challenge
        # No verify token stored: nothing verifies.
        self.ch.set_credentials(self.ch.get_credentials() | {"verify_token": ""}, identity=PNID)
        self.ch.save()
        self.assertEqual(self.client.get(self._url(), good).status_code, 403)
        # Inbound off: the URL is unknown.
        self.ch.inbound_enabled = False
        self.ch.save()
        self.assertEqual(self.client.get(self._url(), good).status_code, 404)

    def test_the_signature_is_checked(self):
        payload = _payload(_message())
        raw = json.dumps(payload).encode()
        self.assertEqual(self._hook(payload).json(), {"status": "queued"})
        tampered = raw.replace(b"hello", b"HELLO")
        self.assertEqual(self._hook(None, signature=_sign(raw), body=tampered).status_code, 401)
        self.assertEqual(self._hook(payload, signature=_sign(raw, "other-secret")).status_code, 401)
        self.assertEqual(self._hook(payload, signature=False).status_code, 401)
        self.assertEqual(self._hook(payload, signature=_sign(raw)[7:]).status_code, 401)  # no sha256= prefix
        self.ch.set_credentials(self.ch.get_credentials() | {"app_secret": ""}, identity=PNID)
        self.ch.save()
        self.assertEqual(self._hook(payload).status_code, 401)
        self.assertEqual(len(self._queued()), 1)

    def test_a_batch_is_split_per_message_and_deduplicated(self):
        bob = "15552223333"
        payload = _payload(_message(wamid="wamid.A"), _message(sender=bob, wamid="wamid.B"),
                           contacts=[{"profile": {"name": "Ann"}, "wa_id": ANN}, {"profile": {"name": "Bob"}, "wa_id": bob}])
        self.assertEqual(self._hook(payload).json(), {"status": "batch", "results": ["queued", "queued"]})
        pieces = self._queued()
        self.assertEqual([p["entry"][0]["changes"][0]["value"]["messages"][0]["id"] for p in pieces], ["wamid.A", "wamid.B"])
        self.assertEqual(pieces[1]["entry"][0]["changes"][0]["value"]["contacts"], [{"profile": {"name": "Bob"}, "wa_id": bob}])
        # Meta retries the same delivery: both are duplicates now.
        self.assertEqual(self._hook(payload).json(), {"status": "batch", "results": ["duplicate", "duplicate"]})
        self.assertEqual(len(self._queued()), 2)

    def test_other_numbers_groups_and_unknown_types_are_ignored(self):
        self.assertEqual(self._hook(_payload(_message(), pnid="999")).json(), {"status": "ignored"})
        group = _message(wamid="wamid.G")
        group["group_id"] = "120363"
        self.assertEqual(self._hook(_payload(group)).json(), {"status": "ignored"})
        self.assertEqual(self._hook(_payload(_message("system", wamid="wamid.S", body="changed"))).json(), {"status": "ignored"})
        self.assertEqual(self._hook({"object": "whatsapp_business_account", "entry": "junk"}).json(), {"status": "ignored"})
        self.assertEqual(self._queued(), [])

    def test_approval_inbox_applies(self):
        self.ch.inbound_access = Channel.InboundAccess.APPROVAL
        self.ch.save()
        self.assertEqual(self._hook(_payload(_message())).json(), {"status": "pending"})
        member = ChannelMember.objects.get(channel=self.ch)
        self.assertEqual((member.sender_id, member.display_name, member.status), (ANN, "Ann", "pending"))
        # The "not approved" notice is a reply inside the window: plain text.
        notice = self._out(kind="system").get()
        self.assertEqual((notice.status, notice.reply_ref_json), ("sent", {"chat_id": ANN}))
        self.assertEqual(self.wa.sends[0]["type"], "text")
        member.status = "approved"
        member.save()
        self.assertEqual(self._hook(_payload(_message(wamid="wamid.2"))).json(), {"status": "queued"})


class StatusCallbackTests(_WebhookBase):
    def _sent(self, wamid="wamid.OUT"):
        return ChannelMessage.objects.create(
            channel=self.ch, direction="out", kind="notification", text="x", status="sent",
            provider_message_id=wamid, delivery_state="sent",
        )

    def _status(self, state, wamid="wamid.OUT", **extra):
        return {"id": wamid, "status": state, "timestamp": "1731400000", "recipient_id": ANN, **extra}

    def test_sent_delivered_read(self):
        row = self._sent()
        self.assertEqual(self._hook(_payload(statuses=[self._status("delivered")])).json(), {"status": "ok"})
        row.refresh_from_db()
        self.assertEqual(row.delivery_state, "delivered")
        self._hook(_payload(statuses=[self._status("read")]))
        row.refresh_from_db()
        self.assertEqual(row.delivery_state, "read")
        # Out of order: a late "delivered" or "sent" doesn't move it back.
        self._hook(_payload(statuses=[self._status("delivered"), self._status("sent")]))
        row.refresh_from_db()
        self.assertEqual((row.status, row.delivery_state), ("sent", "read"))
        admin = User.objects.create_superuser(username="a", email="a@x.io", password="pw")
        self.client.force_login(admin)
        html = self.client.get(reverse("cpanel:channel_edit", args=[self.ch.pk])).content.decode()
        self.assertIn(">read</span>", html)

    def test_failed_marks_an_undelivered_message(self):
        row = self._sent()
        errors = [{"code": 131047, "title": "Re-engagement message",
                   "error_data": {"details": "Message failed to send because more than 24 hours have passed."}}]
        self._hook(_payload(statuses=[self._status("failed", errors=errors)]))
        row.refresh_from_db()
        self.assertEqual(row.status, "failed")
        self.assertEqual(row.error, "WhatsApp: Re-engagement message Message failed to send because more than "
                                    "24 hours have passed. (code 131047)")
        # Failed after delivered changes nothing (it was delivered).
        other = self._sent("wamid.OK")
        self._hook(_payload(statuses=[self._status("delivered", "wamid.OK")]))
        self._hook(_payload(statuses=[self._status("failed", "wamid.OK")]))
        other.refresh_from_db()
        self.assertEqual((other.status, other.delivery_state), ("sent", "delivered"))

    def test_unknown_ids_and_other_numbers_change_nothing(self):
        row = self._sent()
        self.assertEqual(self._hook(_payload(statuses=[self._status("read", "wamid.NONE")])).json(), {"status": "ignored"})
        self._hook(_payload(statuses=[self._status("read")], pnid="999"))
        other = self._channel(name="Other", pnid="777")
        ledger.apply_delivery_status(other, "wamid.OUT", "read")
        row.refresh_from_db()
        self.assertEqual(row.delivery_state, "sent")

    def test_statuses_and_a_message_in_one_delivery(self):
        row = self._sent()
        resp = self._hook(_payload(_message(), statuses=[self._status("read")]))
        self.assertEqual(resp.json(), {"status": "queued"})
        row.refresh_from_db()
        self.assertEqual(row.delivery_state, "read")

    def test_a_status_error_doesnt_drop_the_message(self):
        with mock.patch.object(type(self.provider), "apply_status_updates", side_effect=RuntimeError("db down")), \
                self.assertLogs("core.views.channel_webhooks", "ERROR"):
            resp = self._hook(_payload(_message()))
        self.assertEqual(resp.json(), {"status": "queued"})


class ParseTests(_Base):
    def _parse(self, message, **kw):
        return self.provider.parse_inbound(self.ch, _payload(message, **kw))

    def test_text(self):
        msg = self._parse(_message())
        self.assertEqual((msg.text, msg.message_type, msg.message_id, msg.details, msg.media),
                         ("hello", "text", "wamid.IN1", {}, []))
        self.assertEqual(msg.sender, {"id": ANN, "username": "", "display_name": "Ann"})
        self.assertEqual(msg.reply_ref, {"chat_id": ANN})
        self.assertEqual(self._parse(_message(), contacts=[]).sender["display_name"], f"+{ANN}")

    def test_button_and_list_replies(self):
        msg = self._parse(_message("interactive", type="button_reply", button_reply={"id": "yes", "title": "Yes"}))
        self.assertEqual((msg.text, msg.details), ("Yes", {"reply": {"type": "button_reply", "id": "yes", "title": "Yes", "description": ""}}))
        msg = self._parse(_message("interactive", type="list_reply",
                                   list_reply={"id": "s1", "title": "Small", "description": "1 kg"}))
        self.assertEqual(msg.details["reply"], {"type": "list_reply", "id": "s1", "title": "Small", "description": "1 kg"})
        msg = self._parse(_message("button", text="Stop promotions", payload="STOP"))
        self.assertEqual((msg.text, msg.details["reply"]["id"], msg.details["reply"]["type"]), ("Stop promotions", "STOP", "button"))
        self.assertIsNone(self._parse(_message("interactive", type="nfm_reply", nfm_reply={})))

    def test_location_contacts_reaction(self):
        msg = self._parse(_message("location", latitude=40.7, longitude=-74.0, name="Office", address="1 Main St"))
        self.assertEqual(msg.text, "Location: Office, 1 Main St (40.7, -74.0)")
        self.assertEqual(msg.details["location"]["latitude"], 40.7)
        contacts = _message("contacts")
        contacts["contacts"] = [{"name": {"formatted_name": "Bo Li"}, "phones": [{"phone": "+1 555 222", "type": "CELL"}],
                                 "emails": [{"email": "bo@x.io"}]}]
        msg = self._parse(contacts)
        self.assertEqual(msg.text, "Contact: Bo Li +1 555 222")
        self.assertEqual(msg.details, {"contacts": [{"name": "Bo Li", "phones": ["+1 555 222"], "emails": ["bo@x.io"]}]})
        msg = self._parse(_message("reaction", message_id="wamid.OUT", emoji="👍"))
        self.assertEqual((msg.text, msg.details), ("👍", {"reaction": {"emoji": "👍", "message_id": "wamid.OUT"}}))
        self.assertIsNone(self._parse(_message("reaction", message_id="wamid.OUT")))  # reaction removed

    def test_media_is_metadata_only(self):
        msg = self._parse(_message("image", id="MEDIA1", mime_type="image/jpeg", sha256="abc", caption="receipt"))
        self.assertEqual(msg.text, "receipt")
        self.assertEqual(msg.media, [{"kind": "image", "id": "MEDIA1", "mime": "image/jpeg", "caption": "receipt", "sha256": "abc"}])
        msg = self._parse(_message("document", id="D1", mime_type="application/pdf", filename="inv.pdf"))
        self.assertEqual(msg.media[0]["filename"], "inv.pdf")
        for kind in ("audio", "video", "sticker"):
            self.assertEqual(self._parse(_message(kind, id="M", mime_type="x/y")).media[0]["kind"], kind)
        self.assertEqual(self.wa.calls, [])  # nothing downloaded

    def test_a_quoted_reply_keeps_its_context(self):
        message = _message()
        message["context"] = {"from": "15550100000", "id": "wamid.OUT"}
        self.assertEqual(self._parse(message).details, {"context": {"message_id": "wamid.OUT", "from": "15550100000"}})

    def test_the_run_event_carries_the_typed_message(self):
        msg = self._parse(_message("location", latitude=1.5, longitude=2.5))
        event = channel_event(self.ch, msg)
        self.assertEqual((event["message_type"], event["message_id"]), ("location", "wamid.IN1"))
        self.assertEqual(event["details"]["location"]["longitude"], 2.5)
        self.assertNotIn("media", event)
        image = channel_event(self.ch, self._parse(_message("image", id="M", mime_type="image/png")))
        self.assertEqual(image["media"][0]["id"], "M")

    def test_a_telegram_event_is_unchanged(self):
        from core.services.channels import InboundMessage

        msg = InboundMessage(channel_id="1", provider="telegram", text="hi", sender={"id": "7"}, reply_ref={"chat_id": 7}, raw={})
        event = channel_event(self.ch, msg)
        self.assertEqual(set(event), {"kind", "received_at", "channel", "channel_id", "provider", "text", "sender",
                                      "reply_ref", "raw"})


class DispatchTests(_Base):
    def setUp(self):
        super().setUp()
        self.ch.inbound_enabled = True
        self.ch.inbound_handler = "script"
        self.ch.inbound_target_id = self.script.id
        self.ch.save()

    def test_a_message_runs_the_script_and_is_marked_read(self):
        payload = _payload(_message("interactive", type="button_reply", button_reply={"id": "yes", "title": "Yes"}))
        with mock.patch("core.tasks.queue_script_run") as queue:
            dispatch_inbound(str(self.ch.id), payload)
        self.assertEqual(queue.call_count, 1)
        row = ChannelMessage.objects.get(channel=self.ch, direction="in")
        self.assertEqual((row.text, row.provider_message_id, row.reply_ref_json), ("Yes", "wamid.IN1", {"chat_id": ANN}))
        self.assertEqual(self.wa.reads, [{"messaging_product": "whatsapp", "status": "read", "message_id": "wamid.IN1"}])
        run = Run.objects.get(script=self.script)
        self.assertEqual((run.event["provider"], run.event["message_type"]), ("whatsapp", "interactive"))
        self.assertEqual(run.event["details"]["reply"]["id"], "yes")
        _, kwargs = queue.call_args
        self.assertEqual(kwargs["webhook_data"]["inbound"]["text"], "Yes")

    def test_a_failed_read_receipt_doesnt_stop_the_handler(self):
        self.wa.error(500, 131000, "Something went wrong")
        with mock.patch("core.tasks.queue_script_run") as queue, \
                self.assertLogs("core.services.channels.inbound", "WARNING"):
            dispatch_inbound(str(self.ch.id), _payload(_message()))
        self.assertEqual(queue.call_count, 1)

    def test_py_ai_answers_in_the_window(self):
        from core.services.pyai import PyAIService
        from core.services.pyai.runtime import PyAIResult

        self.ch.inbound_handler = "pyai"
        self.ch.save()
        with mock.patch.object(PyAIService, "is_available", return_value=True), \
                mock.patch.object(PyAIService, "respond", return_value=PyAIResult(text="**2** runs failed")) as respond:
            dispatch_inbound(str(self.ch.id), _payload(_message()))
        self.assertEqual(respond.call_args.args[0], "hello")
        self.assertEqual(respond.call_args.kwargs["agent"], f"whatsapp:{self.ch.pk}")
        reply = self._out(kind="reply").get()
        self.assertEqual((reply.status, reply.text, reply.provider_message_id), ("sent", "2 runs failed", "wamid.1"))
        self.assertEqual(self.wa.sends[0], {"messaging_product": "whatsapp", "recipient_type": "individual", "to": ANN,
                                            "type": "text", "text": {"preview_url": False, "body": "2 runs failed"}})


class ConsoleTests(_Base):
    def test_turning_inbound_on_says_where_to_paste_the_url(self):
        admin = User.objects.create_superuser(username="a", email="a@x.io", password="pw")
        self.client.force_login(admin)
        resp = self.client.post(reverse("cpanel:channel_inbound", args=[self.ch.pk]), {
            "inbound_enabled": "on", "inbound_handler": "", "inbound_access": "approval", "daily_reply_cap": "0",
        }, follow=True)
        html = resp.content.decode()
        self.assertIn("paste the callback URL", html)
        self.assertIn("Callback URL (paste it into the provider", html)
        self.assertNotIn("webhook registered", html)
        self.assertEqual(self.wa.calls, [])  # nothing registered by API
        self.assertNotIn("Find chat ID", html)


class ChallengeUnitTests(_Base):
    def test_handle_challenge_ignores_posts_and_plain_gets(self):
        rf = RequestFactory()
        self.assertIsNone(self.provider.handle_challenge(self.ch, rf.post("/x", data={})))
        self.assertIsNone(self.provider.handle_challenge(self.ch, rf.get("/x")))
        long = rf.get("/x", {"hub.mode": "subscribe", "hub.verify_token": VERIFY, "hub.challenge": "9" * 201})
        self.assertEqual(self.provider.handle_challenge(self.ch, long).status_code, 403)
