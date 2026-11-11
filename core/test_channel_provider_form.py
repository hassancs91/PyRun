"""
Provider-driven channel form + URL-verification challenge (2.5 Stage 8, ticket T35).

What this pins, roughly in order of how much it would hurt to get wrong:

- A provider's ``credential_fields`` drive the channel form: one field per
  credential, stored in the encrypted blob; secret fields are password inputs,
  never shown again and kept when left blank on edit; non-secret ones show the
  stored value. Required fields are enforced on create; the one-account-one-
  channel fingerprint error lands on the provider's ``identity_field``.
- ``register()`` refuses credential fields that can't drive the form (a name
  clashing with a channel field, a bad or duplicate name, a missing
  ``identity_field``).
- The inbound webhook asks ``handle_challenge`` before the signature check:
  a provider can answer a GET (WhatsApp-style ``hub.challenge``) or a POST
  handshake; a GET nobody answers is still 405; a crashing challenge is 400.
- Telegram behaves exactly as before: create, edit (blank keeps the token),
  Test, Find chat ID, inbound registration and an inbound update, all against
  ``FakeTelegram`` (nothing reaches api.telegram.org).

The second provider is a stand-in registered under the ``discord`` key (a
``Channel.Provider`` choice with no built provider); its Test answers without
any network call.
"""

import json
from unittest import mock

from cryptography.fernet import Fernet
from django.core.cache import cache
from django.http import HttpResponse, HttpResponseForbidden
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.forms import ChannelForm
from core.models import (
    Channel,
    ChannelMember,
    Environment,
    GlobalSettings,
    Script,
    User,
    Workspace,
    WorkspaceMembership,
)
from core.services import EncryptionService
from core.services.channels import ChannelProvider, CredentialField, get_provider, list_providers
from core.services.channels import base as channels_base
from core.test_channel_ledger import FakeTelegram
from core.test_channel_ledger import _Resp as _Json
from core.test_channels import _setup_wizard_off

_TEST_KEY = Fernet.generate_key().decode()
_TOKEN = "222:FORMTOKEN"
_UPDATE = {
    "update_id": 77,
    "message": {
        "message_id": 1,
        "from": {"id": 9, "username": "bea", "first_name": "Bea"},
        "chat": {"id": 9, "type": "private"},
        "text": "hi",
    },
}


class StandInProvider(ChannelProvider):
    """A second provider with mixed credential fields; no network at all."""

    provider_key = "discord"
    supports_inbound = True
    credential_fields = (
        CredentialField(name="account_id", label="Account ID", secret=False,
                        help="The account's numeric id.", placeholder="e.g. 1234"),
        CredentialField(name="api_key", label="API key", help="A key with send rights."),
        CredentialField(name="verify_token", label="Verify token", required=False),
    )
    identity_field = "account_id"
    duplicate_identity_error = "This account is already a channel."
    default_target_label = "Default recipient ID"
    default_target_help = "The recipient notifications go to."

    def __init__(self):
        self.tested = []
        self.verified = []

    def identity_for_fingerprint(self, creds):
        return (creds.get("account_id") or "").strip()

    def test_connection(self, channel):
        self.tested.append(channel.get_credentials())
        return True, "Stand-in connected."

    def handle_challenge(self, channel, request):
        if request.method == "GET" and request.GET.get("hub.mode") == "subscribe":
            if request.GET.get("hub.verify_token") != channel.get_credentials().get("verify_token"):
                return HttpResponseForbidden("bad verify token")
            return HttpResponse(request.GET.get("hub.challenge", ""), content_type="text/plain")
        if request.method == "POST" and request.body.startswith(b'{"type": "url_verification"'):
            return HttpResponse(json.loads(request.body)["challenge"], content_type="text/plain")
        return None

    def verify_inbound(self, channel, request):
        self.verified.append(request.method)
        return False


@override_settings(ENCRYPTION_KEY=_TEST_KEY)
class _Base(TestCase):
    def setUp(self):
        EncryptionService.reset()
        self.addCleanup(EncryptionService.reset)
        cache.clear()
        _setup_wizard_off(self)
        self.ws = Workspace.get_default()
        self.user = User.objects.create(email="owner@example.com")
        WorkspaceMembership.ensure(self.user, self.ws, role=WorkspaceMembership.ROLE_OWNER)
        self.client.force_login(self.user)

    def _stand_in(self):
        provider = StandInProvider()
        channels_base.register(provider)
        self.addCleanup(channels_base._REGISTRY.pop, "discord", None)
        return provider

    def _stand_in_channel(self, name="Desk", **creds):
        values = {"account_id": "42", "api_key": "KEY-ONE", "verify_token": "vt-1"}
        values.update(creds)
        ch = Channel(workspace=self.ws, provider="discord", name=name)
        ch.set_credentials(values, identity=values["account_id"])
        ch.save()
        return ch


class RegisterTests(TestCase):
    def _provider(self, *fields, identity=""):
        cls = type("P", (ChannelProvider,), {
            "provider_key": "t35-check", "credential_fields": fields, "identity_field": identity,
        })
        return cls()

    def test_fields_that_cannot_drive_the_form_are_refused(self):
        bad = [
            (CredentialField("name", "Name"),),            # clashes with the channel's name field
            (CredentialField("default_target", "X"),),     # clashes too
            (CredentialField("Bad-Name", "X"),),           # not a field name
            (CredentialField("1st", "X"),),
            (CredentialField("", "X"),),
            (CredentialField("a", "A"), CredentialField("a", "A again")),
        ]
        for fields in bad:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                channels_base.register(self._provider(*fields))
        with self.assertRaises(ValueError):
            channels_base.register(self._provider(CredentialField("a", "A"), identity="b"))
        self.assertNotIn("t35-check", list_providers())

    def test_telegram_declares_its_bot_token(self):
        provider = get_provider("telegram")
        (field,) = provider.credential_fields
        self.assertEqual((field.name, field.label, field.secret, field.required), ("bot_token", "Bot token", True, True))
        self.assertEqual(provider.identity_field, "bot_token")
        self.assertIsNone(provider.handle_challenge(None, None))
        self.assertEqual(CredentialField("x", "Account ID").missing_error, "Account ID is required.")


class ProviderFormTests(_Base):
    def setUp(self):
        super().setUp()
        self.provider = self._stand_in()

    def _data(self, **over):
        data = {
            "name": "Desk", "provider": "discord", "account_id": "42", "api_key": "KEY-ONE",
            "verify_token": "", "default_target": "r-1", "daily_send_cap": "3", "enabled": "on",
        }
        data.update(over)
        return data

    def test_fields_come_from_the_provider(self):
        form = ChannelForm(workspace=self.ws, provider="discord")
        self.assertEqual(form.provider_key, "discord")
        self.assertEqual([f.name for f in form.credential_bound_fields], ["account_id", "api_key", "verify_token"])
        self.assertNotIn("bot_token", form.fields)
        self.assertEqual(form.fields["api_key"].widget.input_type, "password")
        self.assertEqual(form.fields["account_id"].widget.input_type, "text")
        self.assertEqual(form.fields["default_target"].label, "Default recipient ID")
        # The Add form without a choice is the first registered provider's.
        default = ChannelForm(workspace=self.ws)
        self.assertEqual(default.provider_key, list(dict(default.fields["provider"].choices))[0])

    def test_create_stores_every_credential_encrypted(self):
        form = ChannelForm(self._data(verify_token="vt-9"), workspace=self.ws)
        self.assertTrue(form.is_valid(), form.errors)
        ch = form.save(created_by=self.user)
        self.assertEqual(ch.provider, "discord")
        self.assertEqual(ch.get_credentials(), {"account_id": "42", "api_key": "KEY-ONE", "verify_token": "vt-9"})
        self.assertNotIn("KEY-ONE", ch.creds_encrypted)
        self.assertEqual(ch.creds_fingerprint, Channel.fingerprint_for("discord", "42"))
        self.assertEqual((ch.default_target, ch.daily_send_cap), ("r-1", 3))

    def test_required_fields_on_create(self):
        form = ChannelForm(self._data(account_id="", api_key=""), workspace=self.ws)
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["account_id"], ["Account ID is required."])
        self.assertEqual(form.errors["api_key"], ["API key is required."])
        self.assertNotIn("verify_token", form.errors)

    def test_duplicate_identity_lands_on_the_identity_field(self):
        self._stand_in_channel(name="First")
        form = ChannelForm(self._data(name="Second"), workspace=self.ws)
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["account_id"], ["This account is already a channel."])

    def test_edit_keeps_blank_secrets_and_updates_the_rest(self):
        ch = self._stand_in_channel()
        form = ChannelForm(instance=ch, workspace=self.ws)
        self.assertEqual(form.fields["account_id"].initial, "42")
        self.assertIsNone(form.fields["api_key"].initial)
        self.assertEqual(form.fields["api_key"].widget.attrs["placeholder"], "Leave blank to keep current")
        form = ChannelForm(self._data(account_id="43", api_key=""), instance=ch, workspace=self.ws)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        ch.refresh_from_db()
        self.assertEqual(ch.get_credentials(), {"account_id": "43", "api_key": "KEY-ONE", "verify_token": "vt-1"})
        self.assertEqual(ch.creds_fingerprint, Channel.fingerprint_for("discord", "43"))

    def test_edit_with_nothing_changed_leaves_the_blob_alone(self):
        ch = self._stand_in_channel()
        before = ch.creds_encrypted
        form = ChannelForm(self._data(api_key="", name="Renamed"), instance=ch, workspace=self.ws)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        ch.refresh_from_db()
        self.assertEqual((ch.name, ch.creds_encrypted), ("Renamed", before))

    def test_a_required_visible_field_cannot_be_blanked_on_edit(self):
        ch = self._stand_in_channel()
        form = ChannelForm(self._data(account_id="", api_key=""), instance=ch, workspace=self.ws)
        self.assertFalse(form.is_valid())
        self.assertIn("account_id", form.errors)

    def test_add_page_renders_the_chosen_providers_fields(self):
        html = self.client.get(reverse("cpanel:channel_create") + "?provider=discord&name=Desk").content.decode()
        self.assertIn('name="account_id"', html)
        self.assertIn('name="api_key"', html)
        self.assertNotIn('name="bot_token"', html)
        self.assertIn("Default recipient ID", html)
        self.assertIn('value="Desk"', html)
        self.assertNotIn("Telegram setup", html)
        self.assertIn('getElementById("id_provider")', html)  # reloads on a provider change
        telegram = self.client.get(reverse("cpanel:channel_create")).content.decode()
        self.assertIn('name="bot_token"', telegram)
        self.assertIn("Telegram setup", telegram)

    def test_posting_another_provider_without_js_shows_its_fields(self):
        resp = self.client.post(reverse("cpanel:channel_create"), {"name": "X", "provider": "discord", "bot_token": "1:2"})
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('name="account_id"', html)
        self.assertIn("Account ID is required.", html)
        self.assertFalse(Channel.objects.filter(name="X").exists())

    def test_create_edit_and_test_through_the_console(self):
        resp = self.client.post(reverse("cpanel:channel_create"), self._data())
        self.assertEqual(resp.status_code, 302)
        ch = Channel.objects.get(name="Desk")
        page = self.client.get(reverse("cpanel:channel_edit", args=[ch.pk])).content.decode()
        self.assertIn('value="42"', page)  # non-secret shown
        self.assertNotIn("KEY-ONE", page)  # secret never shown
        self.assertIn("Leave blank to keep the current api key.", page)
        self.assertNotIn("Find chat ID", page)  # the stand-in has no discovery
        self.assertIn("Test connection", page)
        test = self.client.post(reverse("cpanel:channel_test", args=[ch.pk])).json()
        self.assertEqual(test, {"success": True, "message": "Stand-in connected.", "error": None})
        self.assertEqual(self.provider.tested, [{"account_id": "42", "api_key": "KEY-ONE"}])


class ChallengeTests(_Base):
    def setUp(self):
        super().setUp()
        self.provider = self._stand_in()
        self.ch = self._stand_in_channel()
        self.ch.inbound_enabled = True
        self.ch.ensure_inbound_token()
        self.ch.ensure_inbound_secret()
        self.ch.save()
        self.url = reverse("channel_webhook", args=[self.ch.inbound_token])

    def test_get_challenge_is_answered_before_the_signature_check(self):
        resp = self.client.get(self.url, {"hub.mode": "subscribe", "hub.verify_token": "vt-1", "hub.challenge": "c-123"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"c-123")
        self.assertEqual(self.provider.verified, [])

    def test_wrong_verify_token_is_refused_by_the_provider(self):
        resp = self.client.get(self.url, {"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "c"})
        self.assertEqual(resp.status_code, 403)

    def test_post_handshake_is_answered_before_the_signature_check(self):
        body = json.dumps({"type": "url_verification", "challenge": "abc"})
        resp = self.client.post(self.url, data=body, content_type="application/json")
        self.assertEqual((resp.status_code, resp.content), (200, b"abc"))
        self.assertEqual(self.provider.verified, [])

    def test_anything_else_goes_on_to_the_signature_check(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        resp = self.client.post(self.url, data="{}", content_type="application/json")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(self.provider.verified, ["POST"])

    def test_a_crashing_challenge_is_a_400(self):
        with mock.patch.object(StandInProvider, "handle_challenge", side_effect=RuntimeError("boom")):
            resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 400)

    def test_disabled_inbound_or_unknown_token_is_404(self):
        self.assertEqual(self.client.get(reverse("channel_webhook", args=["nope"])).status_code, 404)
        Channel.objects.filter(pk=self.ch.pk).update(inbound_enabled=False)
        self.assertEqual(self.client.get(self.url, {"hub.mode": "subscribe"}).status_code, 404)


class TelegramRegressionTests(_Base):
    """Telegram end to end through the refactored form, against FakeTelegram."""

    def setUp(self):
        super().setUp()
        self.tg = FakeTelegram(self)
        env = Environment.objects.create(name="e", path="p")
        self.script = Script.objects.create(name="echo", code="x", environment=env, workspace=self.ws, is_enabled=True)

    def _create(self):
        resp = self.client.post(reverse("cpanel:channel_create"), {
            "name": "Ops", "provider": "telegram", "bot_token": _TOKEN, "default_target": "555", "enabled": "on",
        })
        self.assertEqual(resp.status_code, 302)
        return Channel.objects.get(name="Ops")

    def test_create_edit_test_find_and_inbound(self):
        ch = self._create()
        self.assertEqual(ch.get_credentials(), {"bot_token": _TOKEN})
        self.assertEqual(ch.creds_fingerprint, Channel.fingerprint_for("telegram", _TOKEN))
        self.assertEqual(ch.default_target, "555")

        page = self.client.get(reverse("cpanel:channel_edit", args=[ch.pk])).content.decode()
        for text in ("Find chat ID", "Test connection", "Default chat ID", "Leave blank to keep the current bot token.",
                     "Approval inbox"):
            self.assertIn(text, page)
        self.assertNotIn(_TOKEN, page)

        # Edit with a blank token keeps it.
        resp = self.client.post(reverse("cpanel:channel_edit", args=[ch.pk]), {
            "name": "Ops 2", "bot_token": "", "default_target": "556", "daily_send_cap": "0", "enabled": "on",
        })
        self.assertEqual(resp.status_code, 302)
        ch.refresh_from_db()
        self.assertEqual((ch.name, ch.default_target, ch.get_credentials()), ("Ops 2", "556", {"bot_token": _TOKEN}))

        # Test and Find chat ID go to the (fake) Bot API.
        self.tg.reply(_Json(200, {"ok": True, "result": {"username": "ops_bot"}}))
        test = self.client.post(reverse("cpanel:channel_test", args=[ch.pk])).json()
        self.assertEqual(test["message"], "Connected as @ops_bot.")
        self.tg.reply(_Json(200, {"ok": True, "result": [_UPDATE]}))
        found = self.client.post(reverse("cpanel:channel_discover", args=[ch.pk])).json()
        self.assertEqual(found["chats"][0]["chat_id"], 9)

        # Turning inbound on registers the webhook with the channel's secret.
        self.client.post(reverse("cpanel:channel_inbound", args=[ch.pk]), {
            "inbound_enabled": "on", "inbound_handler": "script", "inbound_target_id": str(self.script.id),
            "inbound_access": "approval", "daily_reply_cap": "0",
        })
        ch.refresh_from_db()
        method, payload = self.tg.calls[-1]
        self.assertEqual(method, "setWebhook")
        self.assertEqual(payload["secret_token"], ch.get_inbound_secret())
        self.assertTrue(payload["url"].endswith(reverse("channel_webhook", args=[ch.inbound_token])))

        # An update with the right secret is queued; a wrong one is 401; GET is 405.
        ChannelMember.objects.create(channel=ch, sender_id="9", status="approved")
        settings = GlobalSettings.get_settings()
        settings.worker_heartbeat_at = timezone.now()
        settings.save(update_fields=["worker_heartbeat_at"])
        url = reverse("channel_webhook", args=[ch.inbound_token])
        with mock.patch("core.views.channel_webhooks.async_task") as queued:
            ok = self.client.post(url, data=json.dumps(_UPDATE), content_type="application/json",
                                  HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=ch.get_inbound_secret())
            bad = self.client.post(url, data=json.dumps(_UPDATE), content_type="application/json",
                                   HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="wrong")
            get = self.client.get(url)
        self.assertEqual(ok.json(), {"status": "queued"})
        self.assertEqual(bad.status_code, 401)
        self.assertEqual(get.status_code, 405)
        queued.assert_called_once()

    def test_a_new_token_moves_the_fingerprint_and_a_taken_one_is_refused(self):
        ch = self._create()
        other = Channel(workspace=self.ws, provider="telegram", name="Other")
        other.set_credentials({"bot_token": "333:TAKEN"}, identity="333:TAKEN")
        other.save()
        resp = self.client.post(reverse("cpanel:channel_edit", args=[ch.pk]), {
            "name": "Ops", "bot_token": "333:TAKEN", "default_target": "555", "enabled": "on",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIn("This bot is already connected as another channel (one bot = one channel).", resp.content.decode())
        resp = self.client.post(reverse("cpanel:channel_edit", args=[ch.pk]), {
            "name": "Ops", "bot_token": "444:NEW", "default_target": "555", "enabled": "on",
        })
        self.assertEqual(resp.status_code, 302)
        ch.refresh_from_db()
        self.assertEqual(ch.creds_fingerprint, Channel.fingerprint_for("telegram", "444:NEW"))

    def test_the_add_page_is_the_telegram_form(self):
        html = self.client.get(reverse("cpanel:channel_create")).content.decode()
        for text in ('name="bot_token"', "Paste the token from @BotFather", "Default chat ID", "e.g. 123456789",
                     "Telegram setup", "@BotFather"):
            self.assertIn(text, html)
        # One provider registered: no reload script.
        self.assertNotIn('getElementById("id_provider")', html)
        resp = self.client.post(reverse("cpanel:channel_create"), {"name": "N", "provider": "telegram", "bot_token": ""})
        self.assertIn("A bot token is required.", resp.content.decode())


    def test_an_undecryptable_token_can_be_entered_again(self):
        ch = self._create()
        Channel.objects.filter(pk=ch.pk).update(creds_encrypted="not-a-fernet-token")
        page = self.client.get(reverse("cpanel:channel_edit", args=[ch.pk]))
        self.assertEqual(page.status_code, 200)
        data = {"name": "Ops", "bot_token": "", "default_target": "555", "enabled": "on"}
        resp = self.client.post(reverse("cpanel:channel_edit", args=[ch.pk]), data)
        self.assertIn("The stored bot token can&#x27;t be decrypted", resp.content.decode())
        resp = self.client.post(reverse("cpanel:channel_edit", args=[ch.pk]), dict(data, bot_token=_TOKEN))
        self.assertEqual(resp.status_code, 302)
        ch.refresh_from_db()
        self.assertEqual(ch.get_credentials(), {"bot_token": _TOKEN})
