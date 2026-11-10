"""
Webhook signatures and per-script rate limits (2.5 Stage 4, ticket T21).

What this pins, roughly in order of how much it would hurt to get wrong:

- A script that requires a signature never gets a run from a call whose
  signature is missing, tampered with, old or made with another secret: 401
  with a ``reason`` and no Run row, decided before the disabled/inputs checks.
  A secret that can't be read refuses every call (fails closed).
- Each preset follows its sender's documented scheme. GitHub's published test
  vector passes; Stripe, Shopify and the custom HMAC have pinned vectors, and
  each is also checked against a signature built the way the docs' recipe
  builds it.
- Bad tokens and bad signatures share one strict per-IP auth-failure budget
  (30 a minute); over it the address gets 429 before any lookup.
- Each script has its own limit (60 a minute, 30 a second by default, with
  Retry-After); the per-IP limit is a 300-a-minute floor, so the old 30 calls
  a minute per address no longer caps one busy sender.
- The secret is stored encrypted and never rendered back; the console form
  validates the custom preset; backups round-trip every field (an older or
  hand-edited backup gets the defaults); a fork starts unsigned.
"""

import base64
import hashlib
import hmac
import json
import time
import uuid
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Run, Script, Workspace, WorkspaceMembership
from core.services import script_service, webhook_signatures
from core.services.backup_service import BackupService
from core.services.encryption_service import EncryptionError
from core.services.webhook_signatures import (
    EXPIRED,
    MALFORMED,
    MISMATCH,
    MISSING,
    NOT_CONFIGURED,
    restored_settings,
    verify,
)
from core.test_webhook_v2 import _Fixture
from core.views import webhooks

User = get_user_model()
Preset = Script.WebhookSignature

# GitHub's own example ("Validating webhook deliveries" in GitHub's docs).
GITHUB_SECRET = "It's a Secret to Everybody"
GITHUB_BODY = b"Hello, World!"
GITHUB_SIGNATURE = "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"

# Pinned vectors, computed once with Python's hmac per each sender's scheme.
STRIPE_SECRET = "whsec_test_secret"
STRIPE_TIME = 1700000000
STRIPE_BODY = b'{"id": "evt_test_webhook", "object": "event"}'
STRIPE_V1 = "aafb6f8becb4b5db5da0da34d6fb419de5f0866d738651a835f821a8a96cf1f3"

SHOPIFY_SECRET = "hush"
SHOPIFY_BODY = b'{"id":820982911946154508}'
SHOPIFY_SIGNATURE = "qBvrsZF7RfB1iS6BX6IHqJTp5L911P7intnPeTPsH0I="

CUSTOM_SECRET = "s3cret"
CUSTOM_BODY = b'{"ok":true}'
CUSTOM_HEX = "543e03a3257ce4cdd34b991bf239f266e18a52dd97f26603483a2f265fc2cfe2"
CUSTOM_TS_BASE64 = "leUy/Q1ITY2xEJQtxms3ndIp84+fXfhW6XcJ2yux46w="  # over "1700000000.<body>"


def _signed(preset, secret, **fields):
    script = Script(webhook_signature=preset, **fields)
    script.set_webhook_signature_secret(secret)
    return script


def _hex(secret, message):
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def _stripe_header(secret, body, timestamp):
    """A Stripe-Signature header built the way Stripe documents it."""
    return f"t={timestamp},v1={_hex(secret, f'{timestamp}.'.encode() + body)}"


class _Clock:
    """Pins the rate limiter's clock so a test never straddles a window."""

    def setUp(self):
        super().setUp()
        self.now = 1_700_000_010.0
        clock = mock.patch("core.ratelimit.time")
        self.clock = clock.start()
        self.clock.time.side_effect = lambda: self.now
        self.addCleanup(clock.stop)


# --------------------------------------------------------------------------- #
# The presets
# --------------------------------------------------------------------------- #


class GitHubTests(TestCase):
    def setUp(self):
        self.script = _signed(Preset.GITHUB, GITHUB_SECRET)

    def test_githubs_published_vector(self):
        self.assertIsNone(verify(self.script, {"X-Hub-Signature-256": GITHUB_SIGNATURE}, GITHUB_BODY))
        self.assertEqual(webhook_signatures.header_for(self.script), "X-Hub-Signature-256")

    def test_hex_case_and_prefix_case_do_not_matter(self):
        self.assertIsNone(verify(self.script, {"X-Hub-Signature-256": GITHUB_SIGNATURE.upper()}, GITHUB_BODY))

    def test_refusals(self):
        cases = {
            MISSING: {},
            MALFORMED: {"X-Hub-Signature-256": GITHUB_SIGNATURE.removeprefix("sha256=")},
        }
        for reason, headers in cases.items():
            with self.subTest(reason):
                self.assertEqual(verify(self.script, headers, GITHUB_BODY), reason)
        headers = {"X-Hub-Signature-256": GITHUB_SIGNATURE}
        self.assertEqual(verify(self.script, headers, GITHUB_BODY + b" "), MISMATCH)
        self.assertEqual(verify(self.script, {"X-Hub-Signature-256": "sha256=zz"}, GITHUB_BODY), MALFORMED)
        # The SHA-1 header GitHub still sends alongside is not enough.
        self.assertEqual(verify(self.script, {"X-Hub-Signature": "sha1=abc"}, GITHUB_BODY), MISSING)

    def test_another_secret_does_not_match(self):
        other = _signed(Preset.GITHUB, "another secret")
        self.assertEqual(verify(other, {"X-Hub-Signature-256": GITHUB_SIGNATURE}, GITHUB_BODY), MISMATCH)


class StripeTests(TestCase):
    def setUp(self):
        self.script = _signed(Preset.STRIPE, STRIPE_SECRET)
        self.header = f"t={STRIPE_TIME},v1={STRIPE_V1}"

    def check(self, header, body=STRIPE_BODY, now=STRIPE_TIME):
        return verify(self.script, {"Stripe-Signature": header}, body, now=now)

    def test_pinned_vector_and_the_documented_scheme(self):
        self.assertIsNone(self.check(self.header))
        self.assertEqual(_stripe_header(STRIPE_SECRET, STRIPE_BODY, STRIPE_TIME), self.header)
        body = json.dumps({"id": "evt_" + uuid.uuid4().hex, "type": "charge.succeeded"}).encode()
        self.assertIsNone(self.check(_stripe_header(STRIPE_SECRET, body, STRIPE_TIME + 5), body=body))

    def test_timestamp_tolerance(self):
        self.assertEqual(self.script.webhook_signature_tolerance, 300)
        self.assertIsNone(self.check(self.header, now=STRIPE_TIME + 300))
        self.assertEqual(self.check(self.header, now=STRIPE_TIME + 301), EXPIRED)
        self.assertEqual(self.check(self.header, now=STRIPE_TIME - 301), EXPIRED)
        self.script.webhook_signature_tolerance = 1000
        self.assertIsNone(self.check(self.header, now=STRIPE_TIME + 999))

    def test_any_v1_may_match_while_a_secret_rolls(self):
        rolled = f"t={STRIPE_TIME},v1={'0' * 64},v1={STRIPE_V1},v0={'1' * 64}"
        self.assertIsNone(self.check(rolled))

    def test_refusals(self):
        self.assertEqual(self.check(self.header, body=STRIPE_BODY + b"\n"), MISMATCH)
        # The signed text includes the timestamp: moving it breaks the signature.
        self.assertEqual(self.check(f"t={STRIPE_TIME + 1},v1={STRIPE_V1}"), MISMATCH)
        self.assertEqual(verify(self.script, {}, STRIPE_BODY, now=STRIPE_TIME), MISSING)
        for header in (f"v1={STRIPE_V1}", f"t={STRIPE_TIME}", f"t={STRIPE_TIME},v0={STRIPE_V1}",
                       f"t={STRIPE_TIME},v1=not-hex", "garbage"):
            with self.subTest(header):
                self.assertEqual(self.check(header), MALFORMED)


class ShopifyTests(TestCase):
    def setUp(self):
        self.script = _signed(Preset.SHOPIFY, SHOPIFY_SECRET)

    def test_pinned_vector_and_the_documented_scheme(self):
        headers = {"X-Shopify-Hmac-Sha256": SHOPIFY_SIGNATURE}
        self.assertIsNone(verify(self.script, headers, SHOPIFY_BODY))
        digest = hmac.new(SHOPIFY_SECRET.encode(), SHOPIFY_BODY, hashlib.sha256).digest()
        self.assertEqual(base64.b64encode(digest).decode(), SHOPIFY_SIGNATURE)

    def test_refusals(self):
        self.assertEqual(verify(self.script, {"X-Shopify-Hmac-Sha256": SHOPIFY_SIGNATURE}, b"{}"), MISMATCH)
        self.assertEqual(verify(self.script, {"X-Shopify-Hmac-Sha256": "%%%"}, SHOPIFY_BODY), MALFORMED)
        self.assertEqual(verify(self.script, {}, SHOPIFY_BODY), MISSING)


class CustomTests(TestCase):
    def test_hex_with_a_prefix(self):
        script = _signed(Preset.HMAC_SHA256, CUSTOM_SECRET, webhook_signature_header="X-Signature",
                         webhook_signature_prefix="sha256=")
        self.assertIsNone(verify(script, {"X-Signature": "sha256=" + CUSTOM_HEX}, CUSTOM_BODY))
        self.assertEqual(verify(script, {"X-Signature": CUSTOM_HEX}, CUSTOM_BODY), MALFORMED)
        self.assertEqual(webhook_signatures.header_for(script), "X-Signature")

    def test_base64_with_a_timestamp_header(self):
        script = _signed(Preset.HMAC_SHA256, CUSTOM_SECRET, webhook_signature_header="X-Sig",
                         webhook_signature_encoding="base64",
                         webhook_signature_timestamp_header="X-Sig-Time",
                         webhook_signature_tolerance=60)
        headers = {"X-Sig": CUSTOM_TS_BASE64, "X-Sig-Time": "1700000000"}
        self.assertIsNone(verify(script, headers, CUSTOM_BODY, now=1700000060))
        self.assertEqual(verify(script, headers, CUSTOM_BODY, now=1700000061), EXPIRED)
        self.assertEqual(verify(script, {"X-Sig": CUSTOM_TS_BASE64}, CUSTOM_BODY, now=1700000000), MISSING)
        headers["X-Sig-Time"] = "1700000001"
        self.assertEqual(verify(script, headers, CUSTOM_BODY, now=1700000000), MISMATCH)

    def test_the_docs_python_sender(self):
        """The sender in docs/run-context.md, step by step."""
        script = _signed(Preset.HMAC_SHA256, "your signing secret",
                         webhook_signature_header="X-Signature", webhook_signature_prefix="sha256=",
                         webhook_signature_timestamp_header="X-Signature-Timestamp")
        secret = b"your signing secret"
        body = json.dumps({"order": 1042}).encode()
        timestamp = "1700000000"
        signature = hmac.new(secret, timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
        headers = {"X-Signature": "sha256=" + signature, "X-Signature-Timestamp": timestamp}
        self.assertIsNone(verify(script, headers, body, now=1700000000))


class NotConfiguredTests(TestCase):
    def test_no_signature_accepts_anything(self):
        self.assertIsNone(verify(Script(), {}, b"x"))
        self.assertFalse(webhook_signatures.required(Script()))

    def test_a_preset_without_a_secret_refuses_every_call(self):
        script = Script(webhook_signature=Preset.GITHUB)
        self.assertEqual(verify(script, {"X-Hub-Signature-256": GITHUB_SIGNATURE}, GITHUB_BODY), NOT_CONFIGURED)
        custom = _signed(Preset.HMAC_SHA256, CUSTOM_SECRET)  # no header named
        self.assertEqual(verify(custom, {}, CUSTOM_BODY), NOT_CONFIGURED)
        unknown = _signed("sha512", "x")
        self.assertEqual(verify(unknown, {}, b""), NOT_CONFIGURED)

    def test_a_secret_that_cannot_be_decrypted_refuses_every_call(self):
        script = _signed(Preset.GITHUB, GITHUB_SECRET)
        with (
            mock.patch("core.services.EncryptionService.decrypt", side_effect=EncryptionError("bad key")),
            self.assertLogs("core.services.webhook_signatures", "ERROR"),
        ):
            reason = verify(script, {"X-Hub-Signature-256": GITHUB_SIGNATURE}, GITHUB_BODY)
        self.assertEqual(reason, NOT_CONFIGURED)

    def test_the_secret_is_stored_encrypted(self):
        script = _signed(Preset.GITHUB, GITHUB_SECRET)
        self.assertNotIn(GITHUB_SECRET, script.webhook_signature_secret_encrypted)
        self.assertEqual(script.get_webhook_signature_secret(), GITHUB_SECRET)


# --------------------------------------------------------------------------- #
# The webhook view
# --------------------------------------------------------------------------- #


class SignedCallTests(_Clock, _Fixture, TestCase):
    # Stripe headers are signed with the real time: only the rate limiter's
    # clock is pinned.
    def _stripe_script(self, **kwargs):
        script = self._webhook_script(webhook_signature=Preset.STRIPE, **kwargs)
        script.set_webhook_signature_secret(STRIPE_SECRET)
        script.save()
        return script

    def _call(self, script, body, expect, headers=None, **kwargs):
        return self._post(script, data=body, content_type="application/json", expect=expect,
                          headers=headers or {}, **kwargs)

    def test_a_stripe_signed_call_runs_and_a_tampered_one_does_not(self):
        script = self._stripe_script()
        body = b'{"id": "evt_1", "type": "invoice.paid"}'
        header = _stripe_header(STRIPE_SECRET, body, int(time.time()))
        resp, run, _ = self._call(script, body, 200, {"Stripe-Signature": header})
        self.assertEqual(resp.json()["status"], "queued")
        self.assertEqual(run.event["headers"]["stripe-signature"], header)
        run.delete()

        tampered = body.replace(b"invoice.paid", b"invoice.void")
        resp, run, _ = self._call(script, tampered, 401, {"Stripe-Signature": header})
        self.assertEqual(resp.json(), {"error": "Invalid webhook signature", "reason": MISMATCH})
        self.assertIsNone(run)

        resp, run, _ = self._call(script, body, 401)
        self.assertEqual(resp.json()["reason"], MISSING)
        self.assertFalse(Run.objects.filter(script=script).exists())

    def test_a_replayed_stripe_call_is_expired(self):
        script = self._stripe_script()
        header = _stripe_header(STRIPE_SECRET, b"{}", int(time.time()) - 600)
        resp, run, _ = self._call(script, b"{}", 401, {"Stripe-Signature": header})
        self.assertEqual(resp.json()["reason"], EXPIRED)
        self.assertIsNone(run)

    def test_a_github_signed_call_runs(self):
        script = self._webhook_script(webhook_signature=Preset.GITHUB)
        script.set_webhook_signature_secret(GITHUB_SECRET)
        script.save()
        resp, run, data = self._post(script, data=GITHUB_BODY, content_type="text/plain", expect=200,
                                     headers={"X-Hub-Signature-256": GITHUB_SIGNATURE,
                                              "X-GitHub-Event": "ping"})
        self.assertEqual(data["body"], "Hello, World!")
        self.assertEqual(run.event["headers"]["x-github-event"], "ping")

    def test_the_signature_is_checked_before_anything_about_the_script(self):
        script = self._stripe_script(is_enabled=False, inputs_schema={
            "type": "object", "required": ["n"], "properties": {"n": {"type": "integer"}},
        })
        resp, run, _ = self._call(script, b"{}", 401)
        self.assertIsNone(run)
        header = _stripe_header(STRIPE_SECRET, b"{}", int(time.time()))
        self._call(script, b"{}", 403, {"Stripe-Signature": header})

    def test_a_body_over_the_limit_is_413_before_the_check(self):
        script = self._stripe_script()
        with override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=10):
            self._call(script, b"x" * 11, 413)

    def test_an_unsigned_script_is_unchanged(self):
        script = self._webhook_script()
        resp, run, data = self._call(script, b'{"a": 1}', 200)
        self.assertEqual(data["body_json"], {"a": 1})


@mock.patch("core.views.webhooks.WEBHOOK_AUTHFAIL_LIMIT", 2)
class AuthFailureLimitTests(_Clock, _Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.script = self._webhook_script(webhook_signature=Preset.GITHUB)
        self.script.set_webhook_signature_secret(GITHUB_SECRET)
        self.script.save()
        self.good = {"X-Hub-Signature-256": GITHUB_SIGNATURE}

    def _sign_call(self, expect, headers=None, ip="198.51.100.4"):
        return self._post(self.script, data=GITHUB_BODY, content_type="text/plain", expect=expect,
                          headers=headers or {}, REMOTE_ADDR=ip)

    def test_bad_signatures_count_and_then_cut_the_address_off(self):
        self._sign_call(401)
        self._sign_call(401)
        resp, run, _ = self._sign_call(429)
        self.assertIn("Retry-After", resp)
        # Over the budget, even a good call from that address waits...
        self._sign_call(429, self.good)
        # ...while another address is unaffected, and the next minute is clean.
        self._sign_call(200, self.good, ip="198.51.100.5")
        self.now += 60
        self._sign_call(200, self.good)

    def test_bad_tokens_and_bad_signatures_share_the_budget(self):
        url = reverse("webhook_trigger", args=["no-such-token"])
        self.assertEqual(self.client.post(url, REMOTE_ADDR="198.51.100.4").status_code, 404)
        self._sign_call(401)
        self.assertEqual(self.client.post(url, REMOTE_ADDR="198.51.100.4").status_code, 429)

    def test_good_calls_do_not_count(self):
        for _ in range(4):
            self._sign_call(200, self.good)
        self._sign_call(401)

    def test_the_limits(self):
        self.assertEqual(webhooks.WEBHOOK_RATE_LIMIT, 300)


class ScriptRateLimitTests(_Clock, _Fixture, TestCase):
    def test_defaults(self):
        script = self._script()
        self.assertEqual((script.webhook_rate_per_minute, script.webhook_rate_burst), (60, 30))
        self.assertEqual(script.webhook_signature, Preset.NONE)
        self.assertEqual(webhooks.WEBHOOK_AUTHFAIL_LIMIT, 30)

    def test_per_minute_limit(self):
        script = self._webhook_script(webhook_rate_per_minute=3)
        other = self._webhook_script(webhook_rate_per_minute=3)
        for _ in range(3):
            self._post(script, expect=200)
            self.now += 2  # different seconds: only the minute counts
        resp, _, _ = self._post(script, expect=429)
        self.assertEqual(resp.json(), {"error": "This script's webhook rate limit is reached. Try again later."})
        self.assertEqual(resp["Retry-After"], str(60 - int(self.now) % 60))
        self.assertEqual(Run.objects.filter(script=script).count(), 3)
        self._post(other, expect=200)
        self.now += 60
        self._post(script, expect=200)

    def test_burst_limit(self):
        script = self._webhook_script(webhook_rate_burst=2)
        self._post(script, expect=200)
        self._post(script, expect=200)
        resp, _, _ = self._post(script, expect=429)
        self.assertEqual(resp["Retry-After"], "1")
        self.now += 1
        self._post(script, expect=200)
        self.assertEqual(Run.objects.filter(script=script).count(), 3)

    def test_a_burst_above_the_minute_is_the_minute(self):
        script = self._webhook_script(webhook_rate_per_minute=2, webhook_rate_burst=30)
        self._post(script, expect=200)
        self._post(script, expect=200)
        self._post(script, expect=429)

    def test_one_busy_sender_is_no_longer_capped_at_30_a_minute(self):
        script = self._webhook_script(webhook_rate_per_minute=100, webhook_rate_burst=100)
        for _ in range(31):
            self._post(script, expect=200)
        self.assertEqual(Run.objects.filter(script=script).count(), 31)

    def test_a_refused_signature_does_not_use_the_scripts_budget(self):
        script = self._webhook_script(webhook_rate_per_minute=1, webhook_signature=Preset.GITHUB)
        script.set_webhook_signature_secret(GITHUB_SECRET)
        script.save()
        self._post(script, data=GITHUB_BODY, content_type="text/plain", expect=401)
        self._post(script, data=GITHUB_BODY, content_type="text/plain", expect=200,
                   headers={"X-Hub-Signature-256": GITHUB_SIGNATURE})


# --------------------------------------------------------------------------- #
# Console, backups, forks
# --------------------------------------------------------------------------- #


class SettingsPageTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create(email="webhook-settings@example.com")
        WorkspaceMembership.objects.update_or_create(
            user=self.user, workspace=self.ws, defaults={"role": WorkspaceMembership.ROLE_OWNER}
        )
        self.client.force_login(self.user)
        self.script = self._webhook_script()
        self.url = reverse("cpanel:webhook_settings", args=[self.script.pk])

    def _save(self, expect_errors=False, **fields):
        data = {
            "webhook_signature": "none", "webhook_signature_encoding": "hex",
            "webhook_signature_tolerance": 300, "webhook_rate_per_minute": 60, "webhook_rate_burst": 30,
        }
        data.update(fields)
        resp = self.client.post(self.url, data)
        if expect_errors:
            self.assertEqual(resp.status_code, 200)
            return resp.context["form"].errors
        self.assertRedirects(resp, reverse("cpanel:script_detail", args=[self.script.pk]))
        self.script.refresh_from_db()
        return None

    def test_set_a_stripe_secret_that_is_never_shown_again(self):
        self._save(webhook_signature="stripe", signature_secret="whsec_abc123",
                   webhook_signature_tolerance=120, webhook_rate_per_minute=10, webhook_rate_burst=5)
        self.assertEqual(self.script.webhook_signature, "stripe")
        self.assertEqual(self.script.get_webhook_signature_secret(), "whsec_abc123")
        self.assertNotIn("whsec_abc123", self.script.webhook_signature_secret_encrypted)
        self.assertEqual((self.script.webhook_signature_tolerance, self.script.webhook_rate_per_minute,
                          self.script.webhook_rate_burst), (120, 10, 5))
        page = self.client.get(self.url).content.decode()
        self.assertNotIn("whsec_abc123", page)
        self.assertIn("A secret is saved. Leave this blank to keep it.", page)
        detail = self.client.get(reverse("cpanel:script_detail", args=[self.script.pk])).content.decode()
        self.assertNotIn("whsec_abc123", detail)
        self.assertIn("Stripe", detail)
        self.assertIn("Stripe-Signature", detail)
        self.assertIn("10/min · 5/s", detail)
        self.assertIn("an unsigned call like this example gets 401", detail)

    def test_a_blank_secret_keeps_the_stored_one_and_none_clears_it(self):
        self._save(webhook_signature="github", signature_secret="first")
        self._save(webhook_signature="shopify", signature_secret="")
        self.assertEqual(self.script.get_webhook_signature_secret(), "first")
        self._save(webhook_signature="none")
        self.assertEqual(self.script.webhook_signature_secret_encrypted, "")

    def test_validation(self):
        errors = self._save(expect_errors=True, webhook_signature="github")
        self.assertIn("Enter the signing secret.", errors["signature_secret"])
        errors = self._save(expect_errors=True, webhook_signature="hmac_sha256", signature_secret="s")
        self.assertIn("Enter the header that carries the signature.", errors["webhook_signature_header"])
        errors = self._save(expect_errors=True, webhook_signature="hmac_sha256", signature_secret="s",
                            webhook_signature_header="X_Sig", webhook_signature_timestamp_header="a b")
        self.assertIn("webhook_signature_header", errors)
        self.assertIn("webhook_signature_timestamp_header", errors)
        # The custom fields are hidden for the other presets: not checked then.
        self._save(webhook_signature="github", signature_secret="s", webhook_signature_header="X_Sig")
        self.assertEqual(self.script.webhook_signature, "github")
        self._save(webhook_signature="none")
        errors = self._save(expect_errors=True, webhook_rate_per_minute=0, webhook_rate_burst=10001,
                            webhook_signature_tolerance=86401)
        self.assertEqual(set(errors), {"webhook_rate_per_minute", "webhook_rate_burst",
                                       "webhook_signature_tolerance"})
        self.script.refresh_from_db()
        self.assertEqual(self.script.webhook_signature, "none")

    def test_custom_preset(self):
        self._save(webhook_signature="hmac_sha256", signature_secret=CUSTOM_SECRET,
                   webhook_signature_header="X-Signature", webhook_signature_prefix="sha256=")
        self._post(self.script, data=CUSTOM_BODY, content_type="application/json", expect=200,
                   headers={"X-Signature": "sha256=" + CUSTOM_HEX})

    def test_another_workspaces_script_is_not_found(self):
        other = Workspace.objects.create(name="Elsewhere")
        script = Script.objects.create(name="x", environment=self.env, workspace=other, code="pass")
        url = reverse("cpanel:webhook_settings", args=[script.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, {"webhook_signature": "none"}).status_code, 404)

    def test_the_card_links_to_the_page(self):
        detail = self.client.get(reverse("cpanel:script_detail", args=[self.script.pk])).content.decode()
        self.assertIn(self.url, detail)
        self.assertIn("60/min · 30/s", detail)
        self.assertNotIn("gets 401", detail)

    def test_the_sidebar_marks_scripts_not_the_global_settings(self):
        # 2.5 test pass: the page's URL name holds "settings" and not
        # "script", which lit the global Settings item instead of Scripts.
        self.user.is_superuser = True  # only superusers see the Settings item
        self.user.save(update_fields=["is_superuser"])
        page = self.client.get(self.url).content.decode()

        def anchor(href):
            return page.split(f'href="{href}"', 1)[1].split(">", 1)[0]

        self.assertIn("font-medium text-ok", anchor(reverse("cpanel:script_list")))
        self.assertNotIn("font-medium text-ok", anchor(reverse("cpanel:settings")))


class BackupAndForkTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create(email="admin-signatures@example.com", is_superuser=True)

    def _restore(self, data):
        result = BackupService.restore_backup(data, restore_runs=False, current_user=self.user)
        self.assertTrue(result["success"], result)
        return result

    def test_backup_round_trips_every_field(self):
        script = self._webhook_script(
            webhook_signature=Preset.HMAC_SHA256, webhook_signature_header="X-Sig",
            webhook_signature_prefix="v1=", webhook_signature_encoding="base64",
            webhook_signature_timestamp_header="X-Sig-Time", webhook_signature_tolerance=90,
            webhook_rate_per_minute=7, webhook_rate_burst=3,
        )
        script.set_webhook_signature_secret("round-trip")
        script.save()
        data = BackupService.create_backup(include_runs=False)
        row = next(s for s in data["scripts"] if s["id"] == str(script.pk))
        self.assertNotIn("round-trip", json.dumps(row))
        self._restore(data)
        restored = Script.objects.get(pk=script.pk)
        for field in ("webhook_signature", "webhook_signature_header", "webhook_signature_prefix",
                      "webhook_signature_encoding", "webhook_signature_timestamp_header",
                      "webhook_signature_tolerance", "webhook_rate_per_minute", "webhook_rate_burst"):
            self.assertEqual(getattr(restored, field), getattr(script, field), field)
        self.assertEqual(restored.get_webhook_signature_secret(), "round-trip")

    def test_an_older_or_hand_edited_backup_gets_the_defaults(self):
        older = self._script()
        edited = self._script()
        data = BackupService.create_backup(include_runs=False)
        rows = {row["id"]: row for row in data["scripts"]}
        for key in [k for k in rows[str(older.pk)] if k.startswith("webhook_") and k != "webhook_token"]:
            rows[str(older.pk)].pop(key)
        rows[str(edited.pk)].update({
            "webhook_signature": "sha512", "webhook_signature_secret_encrypted": 5,
            "webhook_signature_header": ["X"], "webhook_signature_encoding": "hex2",
            "webhook_signature_tolerance": 0, "webhook_rate_per_minute": True, "webhook_rate_burst": "9",
        })
        result = self._restore(data)
        for pk in (older.pk, edited.pk):
            script = Script.objects.get(pk=pk)
            self.assertEqual(
                (script.webhook_signature, script.webhook_signature_secret_encrypted,
                 script.webhook_signature_header, script.webhook_signature_encoding,
                 script.webhook_signature_tolerance, script.webhook_rate_per_minute,
                 script.webhook_rate_burst),
                ("none", "", "", "hex", 300, 60, 30),
            )
        warnings = " ".join(result.get("warnings", []))
        self.assertIn(f'Script "{edited.name}" was restored without its webhook signature', warnings)
        self.assertNotIn(older.name, warnings)

    def test_restored_settings_of_an_older_backup(self):
        fields, dropped = restored_settings({})
        self.assertFalse(dropped)
        self.assertEqual(fields["webhook_signature"], "none")

    def test_a_fork_starts_unsigned(self):
        script = self._webhook_script(webhook_signature=Preset.GITHUB, webhook_rate_per_minute=5)
        script.set_webhook_signature_secret(GITHUB_SECRET)
        script.save()
        fork = script_service.fork_script(script, user=self.user)
        self.assertEqual((fork.webhook_signature, fork.webhook_signature_secret_encrypted,
                          fork.webhook_rate_per_minute), ("none", "", 60))
