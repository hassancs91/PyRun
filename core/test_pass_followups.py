"""
2.5 T52: follow-ups from test passes 6-8 (issue #52).

Sign-in
- a reset link's token moves into the session on the first GET and the form
  lives at a token-free address; the tokened address still accepts a POST
- forgot password asks for reCAPTCHA when the login does, with the login's
  message, and sends nothing when the check fails

Plugins
- the example plugins' publisher is ``pyrun`` (and none ships a signature)
- the Verified badge follows the current keys (revoked or removed: the
  doctor's sentence instead)
- an update sets ``Plugin.source`` (Store, URL or upload)
- a clean activation shows no "Doctor warnings" toast
- a triggers.py that fails to load shows a notice on the plugin page
"""

import json
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.messages import get_messages
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from core.models import Environment, GlobalSettings, PasswordResetToken, Plugin, SecurityEvent, User
from core.plugins.trust import keys
from core.plugins.trust.signing import MANIFEST_NAME, SIGNATURE_NAME
from core.services import EncryptionService, plugin_doctor, plugin_updates
from core.services.plugin_service import PluginService
from core.test_plugin_update import SLUG as DEMO_SLUG
from core.test_plugin_update import _Dir, _no_preflight, demo_zip
from core.test_plugin_watchers import SHOP_TRIGGERS, _PluginFolder
from core.test_plugin_watchers import SLUG as SHOP_SLUG
from core.views import auth as auth_views

PASSWORD = "correct-horse-battery-staple-52"
NEW_PASSWORD = "a-brand-new-password-52"


def _setup_done():
    gs = GlobalSettings.get_settings()
    gs.setup_completed = True
    gs.email_backend = GlobalSettings.EmailBackend.SMTP
    gs.save()
    if not Environment.objects.filter(is_default=True).exists():
        Environment.objects.create(name="env", path="p", is_active=True, is_default=True)
    User.objects.create(username="owner@example.com", email="owner@example.com", is_superuser=True, is_staff=True)
    return gs


def _messages(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


class ResetLinkTokenTests(TestCase):
    def setUp(self):
        _setup_done()
        self.user = User.objects.create_user(username="r@example.com", email="r@example.com", password=PASSWORD)
        self.token = PasswordResetToken.create_for_user(self.user)
        self.link = reverse("auth:reset_password", args=[self.token.token])

    def test_first_get_moves_the_token_to_the_session_and_redirects(self):
        resp = self.client.get(self.link)
        form_url = reverse("auth:reset_password", args=[auth_views.RESET_URL_TOKEN])
        self.assertRedirects(resp, form_url, fetch_redirect_response=False)
        self.assertNotIn(self.token.token, resp["Location"])
        self.assertEqual(self.client.session[auth_views.RESET_SESSION_KEY], self.token.token)

        page = self.client.get(resp["Location"])
        self.assertContains(page, "password")
        self.assertIn("form", page.context)
        self.assertNotIn(self.token.token, page.content.decode())

        # The form posts to its own (token-free) address: the browser's next
        # request carries that page as its Referer, never the token.
        done = self.client.post(
            form_url,
            {"password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD},
            HTTP_REFERER=f"http://testserver{form_url}",
        )
        self.assertRedirects(done, reverse("cpanel:dashboard"), fetch_redirect_response=False)
        self.assertNotIn(self.token.token, done["Location"])
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW_PASSWORD))
        self.assertNotIn(auth_views.RESET_SESSION_KEY, self.client.session)
        self.assertTrue(SecurityEvent.objects.filter(kind="reset", user=self.user).exists())

    def test_the_link_works_once(self):
        self.client.get(self.link, follow=True)
        form_url = reverse("auth:reset_password", args=[auth_views.RESET_URL_TOKEN])
        self.client.post(form_url, {"password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD})
        self.client.logout()
        again = self.client.get(self.link, follow=True)
        self.assertContains(again, "Link already used")
        self.assertNotIn("form", again.context)

    def test_the_token_free_address_without_a_session_token_is_invalid(self):
        resp = self.client.get(reverse("auth:reset_password", args=[auth_views.RESET_URL_TOKEN]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "This password reset link is invalid")
        resp = self.client.post(
            reverse("auth:reset_password", args=[auth_views.RESET_URL_TOKEN]),
            {"password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD},
        )
        self.assertContains(resp, "This password reset link is invalid")
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))

    def test_a_bad_token_keeps_todays_answer_without_a_redirect(self):
        resp = self.client.get(reverse("auth:reset_password", args=["no-such-token"]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "This password reset link is invalid")
        self.assertNotIn(auth_views.RESET_SESSION_KEY, self.client.session)

    def test_an_expired_link_redirects_then_says_expired(self):
        PasswordResetToken.objects.filter(pk=self.token.pk).update(expires_at="2000-01-01T00:00:00Z")
        resp = self.client.get(self.link, follow=True)
        self.assertContains(resp, "Link expired")
        self.assertNotIn(self.token.token, resp.wsgi_request.path)

    def test_a_post_to_the_tokened_address_still_works(self):
        resp = self.client.post(self.link, {"password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD})
        self.assertRedirects(resp, reverse("cpanel:dashboard"), fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW_PASSWORD))


class ForgotPasswordRecaptchaTests(TestCase):
    def setUp(self):
        self.gs = _setup_done()
        User.objects.create_user(username="f@example.com", email="f@example.com", password=PASSWORD)

    def _activate(self):
        self.gs.recaptcha_enabled = True
        self.gs.recaptcha_site_key = "site-key-52"
        self.gs.recaptcha_secret_key_encrypted = EncryptionService.encrypt("secret")
        self.gs.save()

    def _post(self):
        return self.client.post(
            reverse("auth:forgot_password"), {"email": "f@example.com", "g-recaptcha-response": "t"}
        )

    def test_the_form_shows_the_widget_only_when_active(self):
        self.assertNotContains(self.client.get(reverse("auth:forgot_password")), "g-recaptcha")
        self._activate()
        page = self.client.get(reverse("auth:forgot_password"))
        self.assertContains(page, 'class="g-recaptcha" data-sitekey="site-key-52"')

    def test_a_failed_check_sends_nothing_and_gets_the_login_message(self):
        self._activate()
        with mock.patch("core.views.auth.RecaptchaService.verify", return_value=False) as verify, \
                mock.patch("core.views.auth.async_task") as queued:
            resp = self._post()
        verify.assert_called_once()
        queued.assert_not_called()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(_messages(resp), [auth_views.RECAPTCHA_FAILED_MESSAGE])
        self.assertEqual(auth_views.RECAPTCHA_FAILED_MESSAGE, "reCAPTCHA verification failed. Please try again.")
        self.assertFalse(SecurityEvent.objects.filter(kind="reset_request").exists())

    def test_a_passed_check_sends_the_link(self):
        self._activate()
        with mock.patch("core.views.auth.RecaptchaService.verify", return_value=True), \
                mock.patch("core.views.auth.async_task") as queued:
            resp = self._post()
        self.assertRedirects(resp, reverse("auth:login"), fetch_redirect_response=False)
        queued.assert_called_once()
        self.assertEqual(_messages(resp), [auth_views.FORGOT_SENT_MESSAGE])

    def test_inactive_recaptcha_isnt_checked(self):
        with mock.patch("core.views.auth.RecaptchaService.verify") as verify, \
                mock.patch("core.views.auth.async_task") as queued:
            self._post()
        verify.assert_not_called()
        queued.assert_called_once()

    def test_the_hourly_limit_still_applies_after_a_passed_check(self):
        self._activate()
        with mock.patch("core.views.auth.RecaptchaService.verify", return_value=True), \
                mock.patch("core.views.auth.async_task") as queued, \
                mock.patch("core.ratelimit.time.time", return_value=1_800_000_000):
            for _ in range(auth_views.FORGOT_EMAIL_LIMIT + 1):
                self._post()
        self.assertEqual(queued.call_count, auth_views.FORGOT_EMAIL_LIMIT)


# --------------------------------------------------------------------------- #
# Plugins
# --------------------------------------------------------------------------- #


class ExamplePublisherTests(SimpleTestCase):
    def test_every_example_plugin_is_published_as_pyrun_and_unsigned(self):
        manifests = sorted((Path(settings.BASE_DIR) / "examples").glob("*/plugin.json"))
        publishers = {p.parent.name: json.loads(p.read_text(encoding="utf-8")).get("publisher") for p in manifests}
        # The examples that name a publisher (the template and the small demos name none).
        for slug in ("brand_tracker", "qdrant_backup", "sdk_showcase", "yt_comments"):
            self.assertEqual(publishers.get(slug), "pyrun", slug)
        for path in manifests:
            with self.subTest(plugin=path.parent.name):
                self.assertIn(publishers[path.parent.name], (None, "pyrun"))
                # Never signed with a real key in the repo.
                self.assertFalse((path.parent / MANIFEST_NAME).exists())
                self.assertFalse((path.parent / SIGNATURE_NAME).exists())


class BadgeFollowsKeysTests(TestCase):
    def _plugin(self, **kw):
        fields = {"slug": "t52badge", "name": "Badge", "version": "1.0.0", "trust": Plugin.Trust.VERIFIED,
                  "signed_by": "pyrun", "signing_key_id": "t52-key"}
        fields.update(kw)
        return Plugin(**fields)

    def _keys(self, **entries):
        return mock.patch.dict(keys.TRUSTED_KEYS, entries, clear=True)

    def _badge(self, plugin):
        return render_to_string("cpanel/plugins/_trust_badge.html", {"plugin": plugin, "show_unsigned": True})

    def test_a_trusted_key_shows_verified(self):
        with self._keys(**{"t52-key": keys.TrustedKey("pyrun", "AAAA")}):
            plugin = self._plugin()
            self.assertTrue(plugin.verified_by_pyrun)
            self.assertEqual(plugin.signature_problem, "")
            self.assertIn("Verified · PyRun", self._badge(plugin))

    def test_a_revoked_key_shows_the_doctors_state(self):
        with self._keys(**{"t52-key": keys.TrustedKey("pyrun", "AAAA", revoked=True)}):
            plugin = self._plugin()
            self.assertFalse(plugin.verified_by_pyrun)
            self.assertEqual(plugin.signature_problem, "It is signed with key id 't52-key', which has been revoked.")
            badge = self._badge(plugin)
        self.assertNotIn("Verified", badge)
        self.assertIn("Signature not trusted", badge)
        self.assertIn("which has been revoked", badge)

    def test_a_removed_key_or_another_publisher_isnt_verified(self):
        with self._keys():
            plugin = self._plugin()
            self.assertFalse(plugin.verified_by_pyrun)
            self.assertIn("which this PyRun does not trust", plugin.signature_problem)
        with self._keys(**{"t52-key": keys.TrustedKey("acme", "AAAA")}):
            plugin = self._plugin(signed_by="acme")
            self.assertEqual(plugin.signature_problem, "")
            self.assertIn("Signed · acme", self._badge(plugin))
            self.assertIn("belongs to the publisher 'acme'", self._plugin().signature_problem)

    def test_an_unsigned_plugin_has_no_problem(self):
        with self._keys():
            plugin = self._plugin(trust=Plugin.Trust.UNSIGNED, signed_by="", signing_key_id="")
            self.assertEqual(plugin.signature_problem, "")
            self.assertIn("Unsigned", self._badge(plugin))


class SourceFollowsUpdatesTests(_Dir):
    def test_each_update_records_where_the_code_came_from(self):
        plugin, _, _ = PluginService.upload(demo_zip("1.0.0"))
        self.assertEqual(plugin.source, Plugin.Source.UPLOAD)
        for version, source in (("1.1.0", "store"), ("1.2.0", "url"), ("1.3.0", "upload")):
            plugin_updates.update(plugin, demo_zip(version), user=self.admin, source=source)
            plugin.refresh_from_db()
            self.assertEqual((plugin.version, plugin.source), (version, source))
        # The history keeps each update's own source.
        self.assertEqual(
            list(plugin.updates.order_by("to_version").values_list("source", flat=True)),
            ["store", "url", "upload"],
        )

    def test_a_refused_update_leaves_the_source(self):
        plugin, _, _ = PluginService.upload(demo_zip("1.0.0"))
        with self.assertRaises(Exception):
            plugin_updates.update(plugin, demo_zip("0.9.0"), source="store")
        plugin.refresh_from_db()
        self.assertEqual(plugin.source, Plugin.Source.UPLOAD)


class _Console(_Dir):
    def setUp(self):
        super().setUp()
        for target in (
            "core.services.setup_service.SetupService.is_setup_needed",
            "core.services.setup_service.SetupService.needs_admin_setup",
        ):
            patch = mock.patch(target, return_value=False)
            patch.start()
            self.addCleanup(patch.stop)
        self.client.force_login(self.admin)


class ActivationToastTests(_Console):
    def _activate(self, report):
        plugin, _, _ = PluginService.upload(demo_zip("1.0.0"))
        with _no_preflight(True, "Preflight OK: plugins.t42demo imported."), \
                mock.patch.object(plugin_doctor, "run_doctor", return_value=report):
            resp = self.client.post(reverse("cpanel:plugin_activate", args=[plugin.pk]))
        return [(m.level_tag, str(m)) for m in get_messages(resp.wsgi_request)]

    def test_a_clean_check_shows_only_the_success_toast(self):
        report = plugin_doctor.DoctorReport(DEMO_SLUG)
        report.add("manifest", plugin_doctor.PASS, "ok")
        shown = self._activate(report)
        self.assertEqual([level for level, _ in shown], ["success"])
        self.assertNotIn("Doctor warnings", " ".join(text for _, text in shown))

    def test_advisory_warnings_still_show(self):
        report = plugin_doctor.DoctorReport(DEMO_SLUG)
        report.add("readme", plugin_doctor.WARN, "No README.md.")
        shown = self._activate(report)
        self.assertEqual([level for level, _ in shown], ["success", "warning"])
        self.assertIn("No README.md.", shown[1][1])
        self.assertNotIn("Preflight OK", shown[1][1])


class TriggersLoadNoticeTests(_Console):
    def _page(self, triggers):
        _PluginFolder(self, triggers=triggers)
        Plugin.objects.create(slug=SHOP_SLUG, name="Shop", version="1.0.0", status=Plugin.Status.ACTIVE)
        return self.client.get(reverse("cpanel:plugin_detail", args=[SHOP_SLUG]))

    def test_a_failed_import_shows_a_notice_with_the_detail_for_superusers(self):
        with self.assertLogs("core.triggers.adapters.plugin", "ERROR") as logs:
            resp = self._page(SHOP_TRIGGERS + "\nraise RuntimeError('broken at import 52')\n")
        self.assertContains(resp, "Watcher types unavailable")
        self.assertContains(resp, "RuntimeError: broken at import 52")
        # The traceback stays in the server log, not on the page.
        self.assertIn("Traceback", "\n".join(logs.output))
        self.assertNotContains(resp, "Traceback")

    def test_the_server_log_masks_the_plugins_secrets(self):
        # Run 7 (O5): the log line and its traceback, like the page's notice.
        from core.models import Secret

        secret = Secret(key="T52_TRIG", owner_plugin=SHOP_SLUG)
        secret.set_value("trig-value-in-message")
        secret.save()
        with self.assertLogs("core.triggers.adapters.plugin", "ERROR") as logs:
            resp = self._page(SHOP_TRIGGERS + "\nraise RuntimeError('token=trig-value-in-message')\n")
        logged = "\n".join(logs.output)
        self.assertIn("RuntimeError: token=[T52_TRIG:MASKED]", logged)
        self.assertIn("Traceback", logged)
        self.assertNotIn("trig-value-in-message", logged)
        self.assertContains(resp, "[T52_TRIG:MASKED]")
        self.assertNotContains(resp, "trig-value-in-message")

    def test_the_log_keeps_only_the_type_when_secrets_cant_be_read(self):
        with mock.patch.object(plugin_updates, "mask_plugin_output", side_effect=RuntimeError("db down")), \
                self.assertLogs("core.triggers.adapters.plugin", "ERROR") as logs:
            _PluginFolder(self, triggers=SHOP_TRIGGERS + "\nraise RuntimeError('unmasked-detail')\n")
            from core.triggers.adapters.plugin import load_problem

            self.assertIn("unmasked-detail", load_problem(SHOP_SLUG))
        logged = "\n".join(logs.output)
        self.assertIn("RuntimeError (the message is not logged", logged)
        self.assertNotIn("unmasked-detail", logged)

    def test_a_triggers_py_that_fails_the_doctor_shows_the_notice(self):
        bad = SHOP_TRIGGERS.replace('title = "New order"', 'title = ""')
        with self.assertLogs("core.triggers.adapters.plugin", "WARNING"):
            resp = self._page(bad)
        self.assertContains(resp, "Watcher types unavailable")

    def test_a_working_triggers_py_shows_nothing(self):
        resp = self._page(SHOP_TRIGGERS)
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Watcher types unavailable")

    def test_the_detail_is_for_superusers_only(self):
        from core.views.plugins import _triggers_problem

        _PluginFolder(self, triggers=SHOP_TRIGGERS + "\nraise RuntimeError('secret detail')\n")
        member = User.objects.create_user(username="m52@example.com", email="m52@example.com", password=PASSWORD)
        request = mock.Mock(user=member)
        with self.assertLogs("core.triggers.adapters.plugin", "ERROR"):
            self.assertEqual(_triggers_problem(request, SHOP_SLUG), {"detail": ""})
        request.user = self.admin
        self.assertIn("secret detail", _triggers_problem(request, SHOP_SLUG)["detail"])
