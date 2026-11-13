"""
Login hardening and security events (2.5 Stage 11, ticket T45).

What this pins, roughly in order of how much it would hurt to get wrong:

- Forgot-password answers every address the same way (status, redirect,
  message, and the same task queued with the same arguments), so it can't be
  used to find accounts; the lookup, token and email happen in the task. It is
  throttled per IP and per email, and a throttled request still looks the same.
- A wrong password, an unknown email and an account without a password get
  the same login error; the "set a first password" hint lives on the reset
  page. The passwordless case burns one password hash like the others.
- A lockout renders the styled template (429) and is recorded.
- A superuser can generate a single-use, 24-hour reset link for a user, with
  email off; using it resets the password and clears the lockout.
- Sign-in alerts are per user, off by default, queued from a task, and the
  email escapes the caller-controlled user agent.
- SecurityEvent rows for sign-in, failure, lockout, password change, reset
  request, reset, reset link and token creation; Users → Security lists and
  filters them for superusers only; the daily housekeeping purges after 90
  days; SESSION_COOKIE_AGE comes from the environment; backups carry the
  sign-in alert opt-in.
"""

import json
import os
import subprocess
import sys
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.contrib.messages import get_messages
from django.core import mail
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import (
    Environment,
    GlobalSettings,
    PasswordResetToken,
    SecurityEvent,
    User,
    Workspace,
    WorkspaceMembership,
)
from core.services import security_events
from core.views import auth as auth_views

PASSWORD = "correct-horse-battery-staple-91"
# A fixed clock for the rate-limit windows, so a test never straddles one.
FIXED_TIME = 1_800_000_000.0


def _setup_done():
    gs = GlobalSettings.get_settings()
    gs.setup_completed = True
    gs.save()
    # Without a default environment and an admin, the setup middleware sends
    # every page to the setup wizard.
    if not Environment.objects.filter(is_default=True).exists():
        Environment.objects.create(name="env", path="p", is_active=True, is_default=True)
    User.objects.create(email="owner@example.com", is_superuser=True, is_staff=True)
    return gs


def _enable_email():
    gs = GlobalSettings.get_settings()
    gs.email_backend = GlobalSettings.EmailBackend.SMTP
    gs.save()


def _messages(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


@mock.patch("core.ratelimit.time.time", return_value=FIXED_TIME)
class ForgotPasswordTests(TestCase):
    def setUp(self):
        _setup_done()
        _enable_email()
        self.user = User.objects.create_user(
            username="known@example.com", email="known@example.com", password=PASSWORD, is_verified=True
        )

    def _forgot(self, email, ip="203.0.113.5", client=None):
        return (client or self.client).post(reverse("auth:forgot_password"), {"email": email}, REMOTE_ADDR=ip)

    def test_known_and_unknown_addresses_get_the_same_answer_and_the_same_task(self, _clock):
        with mock.patch("core.views.auth.async_task") as queued:
            known = self._forgot("known@example.com")
            # A second browser, so each answer carries only its own message.
            unknown = self._forgot("nobody@example.com", ip="203.0.113.6", client=self.client_class())

        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual(known["Location"], unknown["Location"])
        self.assertEqual(_messages(known), [auth_views.FORGOT_SENT_MESSAGE])
        self.assertEqual(_messages(unknown), [auth_views.FORGOT_SENT_MESSAGE])
        self.assertEqual(queued.call_count, 2)
        first, second = queued.call_args_list
        self.assertEqual(first.args[0], "core.services.security_events.send_password_reset")
        self.assertEqual(first.args[0], second.args[0])
        self.assertEqual((first.args[1], second.args[1]), ("known@example.com", "nobody@example.com"))
        self.assertEqual(first.args[2], "http://testserver/")
        self.assertEqual(first.kwargs, second.kwargs)
        # The request itself issues no token and sends nothing.
        self.assertFalse(PasswordResetToken.objects.exists())
        self.assertEqual(mail.outbox, [])

    def test_the_task_sends_the_link_only_to_a_verified_account(self, _clock):
        # The task sends through Django's mail backend (the test outbox).
        gs = GlobalSettings.get_settings()
        gs.email_backend = GlobalSettings.EmailBackend.DISABLED
        gs.save()
        self.assertTrue(security_events.send_password_reset("Known@Example.com", "https://pyrun.example/"))
        self.assertFalse(security_events.send_password_reset("nobody@example.com", "https://pyrun.example/"))
        User.objects.create_user(username="new@example.com", email="new@example.com", password=PASSWORD)
        self.assertFalse(security_events.send_password_reset("new@example.com", "https://pyrun.example/"))

        self.assertEqual(len(mail.outbox), 1)
        token = PasswordResetToken.objects.get(user=self.user)
        self.assertIn(f"https://pyrun.example/auth/reset-password/{token.token}/", mail.outbox[0].body)
        self.assertEqual(mail.outbox[0].to, ["known@example.com"])

    def test_per_email_throttle_drops_the_email_but_not_the_answer(self, _clock):
        with mock.patch("core.views.auth.async_task") as queued:
            answers = [
                self._forgot("known@example.com", ip=f"203.0.113.{i}")
                for i in range(auth_views.FORGOT_EMAIL_LIMIT + 1)
            ]

        self.assertEqual(queued.call_count, auth_views.FORGOT_EMAIL_LIMIT)
        self.assertEqual({a["Location"] for a in answers}, {reverse("auth:login")})
        self.assertEqual(set(_messages(answers[-1])), {auth_views.FORGOT_SENT_MESSAGE})
        last = SecurityEvent.objects.filter(kind="reset_request").order_by("-id").first()
        self.assertEqual(last.detail, "throttled, no email sent")
        self.assertEqual(last.user, self.user)

    def test_per_ip_throttle_covers_different_addresses(self, _clock):
        with mock.patch("core.views.auth.async_task") as queued:
            for i in range(auth_views.FORGOT_IP_LIMIT + 1):
                self._forgot(f"person{i}@example.com")

        self.assertEqual(queued.call_count, auth_views.FORGOT_IP_LIMIT)

    def test_request_is_recorded_without_saying_whether_the_account_exists(self, _clock):
        with mock.patch("core.views.auth.async_task"):
            self._forgot("nobody@example.com")

        event = SecurityEvent.objects.get(kind="reset_request")
        self.assertEqual((event.email, event.user, event.ip), ("nobody@example.com", None, "203.0.113.5"))

    def test_reset_page_carries_the_first_password_hint(self, _clock):
        page = self.client.get(reverse("auth:forgot_password"))
        self.assertContains(page, "This is also how you set your first one.")


class LoginHardeningTests(TestCase):
    def setUp(self):
        _setup_done()
        _enable_email()
        self.user = User.objects.create_user(username="admin@example.com", email="admin@example.com", password=PASSWORD)
        # Created without a password (an old invite, a magic-link account).
        self.passwordless = User.objects.create(email="nopass@example.com")

    def _sign_in(self, email, password, **extra):
        return self.client.post(
            reverse("auth:login"), {"action": "password", "email": email, "password": password}, **extra
        )

    def test_passwordless_unknown_and_wrong_password_look_the_same(self):
        pages = [
            self._sign_in("nopass@example.com", "whatever-1"),
            self._sign_in("ghost@example.com", "whatever-2"),
            self._sign_in("admin@example.com", "whatever-3"),
        ]
        for page in pages:
            self.assertEqual(page.status_code, 200)
            self.assertEqual(page.context["error_message"], "Invalid email or password.")
            self.assertNotContains(page, "have a password set")

    def test_passwordless_failure_hashes_like_an_unknown_email(self):
        with mock.patch("core.views.auth.make_password") as hashed:
            self._sign_in("nopass@example.com", "whatever")
            self._sign_in("admin@example.com", "wrong")
        hashed.assert_called_once_with("whatever")

    def test_failure_and_success_are_recorded_with_ip_and_browser(self):
        self._sign_in("admin@example.com", "wrong", REMOTE_ADDR="198.51.100.7", HTTP_USER_AGENT="Probe/1.0")
        self._sign_in("ghost@example.com", "wrong")
        resp = self._sign_in("admin@example.com", PASSWORD, HTTP_USER_AGENT="Firefox/140")

        self.assertEqual(resp.status_code, 302)
        fail, ghost = SecurityEvent.objects.filter(kind="login_fail").order_by("id")
        self.assertEqual((fail.user, fail.ip, fail.user_agent), (self.user, "198.51.100.7", "Probe/1.0"))
        self.assertEqual((ghost.user, ghost.email), (None, "ghost@example.com"))
        ok = SecurityEvent.objects.get(kind="login_ok")
        self.assertEqual((ok.user, ok.user_agent), (self.user, "Firefox/140"))

    def test_lockout_renders_the_styled_page_and_is_recorded(self):
        for _ in range(settings.AXES_FAILURE_LIMIT):
            self._sign_in("admin@example.com", "wrong")
        resp = self._sign_in("admin@example.com", PASSWORD)

        self.assertEqual(resp.status_code, 429)
        self.assertTemplateUsed(resp, "auth/lockout.html")
        self.assertContains(resp, "Too many sign-in attempts", status_code=429)
        self.assertContains(resp, "Try again in about 15 minutes.", status_code=429)
        self.assertNotIn("_auth_user_id", self.client.session)
        lockouts = SecurityEvent.objects.filter(kind="lockout", user=self.user)
        # The attempt that locked the account and the refused one after it.
        self.assertEqual(lockouts.count(), 2)
        self.assertEqual(
            SecurityEvent.objects.filter(kind="login_fail").count(), settings.AXES_FAILURE_LIMIT - 1
        )

    def test_session_cookie_age_is_the_configured_one(self):
        expected = int(os.environ.get("SESSION_COOKIE_AGE", "1209600"))
        self.assertEqual(settings.SESSION_COOKIE_AGE, expected)
        self._sign_in("admin@example.com", PASSWORD)
        self.assertEqual(self.client.cookies[settings.SESSION_COOKIE_NAME]["max-age"], expected)

    def test_a_bad_session_cookie_age_stops_startup_with_a_clear_message(self):
        env = dict(os.environ, SESSION_COOKIE_AGE="eight hours")
        result = subprocess.run(
            [sys.executable, "-c", "import pyrunner.settings"],
            env=env, capture_output=True, text=True, cwd=settings.BASE_DIR, timeout=120,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SESSION_COOKIE_AGE must be a whole number of seconds", result.stderr)


class AdminResetLinkTests(TestCase):
    def setUp(self):
        _setup_done()
        self.admin = User.objects.create_user(
            username="root@example.com", email="root@example.com", password=PASSWORD, is_superuser=True, is_staff=True
        )
        self.member = User.objects.create_user(username="m@example.com", email="m@example.com", password=PASSWORD)

    def test_superuser_gets_a_single_use_24_hour_link_with_email_off(self):
        self.assertEqual(GlobalSettings.get_settings().email_backend, GlobalSettings.EmailBackend.DISABLED)
        self.client.force_login(self.admin)
        resp = self.client.post(reverse("cpanel:user_reset_link", args=[self.member.pk]))
        self.assertRedirects(resp, reverse("cpanel:user_list"), fetch_redirect_response=False)

        page = self.client.get(reverse("cpanel:user_list"))
        token = PasswordResetToken.objects.get(user=self.member)
        link = f"http://testserver/auth/reset-password/{token.token}/"
        self.assertContains(page, link)
        self.assertContains(page, "expires in 24 hours")
        self.assertAlmostEqual(
            token.expires_at, timezone.now() + timedelta(hours=24), delta=timedelta(minutes=1)
        )
        # Shown once.
        self.assertNotContains(self.client.get(reverse("cpanel:user_list")), link)
        event = SecurityEvent.objects.get(kind="reset_link")
        self.assertEqual((event.user, event.detail), (self.member, "generated by root@example.com"))

        # The member uses it (signed out), with email still off.
        self.client.logout()
        self.assertEqual(self.client.get(link).status_code, 200)
        done = self.client.post(link, {"password": "a-brand-new-pass-77", "password_confirm": "a-brand-new-pass-77"})
        self.assertRedirects(done, reverse("cpanel:dashboard"), fetch_redirect_response=False)
        self.member.refresh_from_db()
        self.assertTrue(self.member.check_password("a-brand-new-pass-77"))
        self.assertTrue(SecurityEvent.objects.filter(kind="reset", user=self.member).exists())
        # Single use.
        self.client.logout()
        self.assertContains(self.client.get(link), "already been used")

    def test_a_new_link_expires_the_earlier_one(self):
        self.client.force_login(self.admin)
        self.client.post(reverse("cpanel:user_reset_link", args=[self.member.pk]))
        first = PasswordResetToken.objects.get(user=self.member)
        self.client.post(reverse("cpanel:user_reset_link", args=[self.member.pk]))
        first.refresh_from_db()
        self.assertFalse(first.is_valid())
        self.assertEqual(PasswordResetToken.objects.filter(user=self.member).count(), 2)

    def test_non_superuser_cannot_generate_one(self):
        self.client.force_login(self.member)
        resp = self.client.post(reverse("cpanel:user_reset_link", args=[self.admin.pk]))
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(PasswordResetToken.objects.exists())

    def test_get_is_refused(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("cpanel:user_reset_link", args=[self.member.pk])).status_code, 405)

    def test_completing_a_reset_clears_the_lockout(self):
        from axes.models import AccessAttempt

        for _ in range(settings.AXES_FAILURE_LIMIT):
            self.client.post(
                reverse("auth:login"), {"action": "password", "email": "m@example.com", "password": "nope"}
            )
        self.assertTrue(AccessAttempt.objects.filter(username="m@example.com").exists())
        token = PasswordResetToken.create_for_user(self.member)
        self.client.post(
            reverse("auth:reset_password", args=[token.token]),
            {"password": "a-brand-new-pass-77", "password_confirm": "a-brand-new-pass-77"},
        )
        self.assertFalse(AccessAttempt.objects.filter(username="m@example.com").exists())


class SignInAlertTests(TestCase):
    def setUp(self):
        _setup_done()
        self.user = User.objects.create_user(username="u@example.com", email="u@example.com", password=PASSWORD)

    def _sign_in(self):
        return self.client.post(
            reverse("auth:login"),
            {"action": "password", "email": "u@example.com", "password": PASSWORD},
            REMOTE_ADDR="198.51.100.9",
            HTTP_USER_AGENT="<script>x</script>",
        )

    def test_off_by_default_and_nothing_is_queued(self):
        self.assertFalse(self.user.notify_on_login)
        with mock.patch("core.views.auth.async_task") as queued:
            self._sign_in()
        queued.assert_not_called()

    def test_user_turns_it_on_and_a_sign_in_queues_the_email(self):
        self.client.force_login(self.user)
        page = self.client.get(reverse("auth:change_password"))
        self.assertContains(page, "Sign-in alerts")
        self.assertContains(page, "Turn on")
        self.client.post(reverse("auth:login_notifications"), {"enabled": "1"})
        self.user.refresh_from_db()
        self.assertTrue(self.user.notify_on_login)
        self.client.logout()

        with mock.patch("core.views.auth.async_task") as queued:
            self._sign_in()
        queued.assert_called_once()
        name, user_id, ip, agent, _when, base = queued.call_args.args
        self.assertEqual(name, "core.services.security_events.send_login_notification")
        self.assertEqual((user_id, ip, agent, base), (self.user.pk, "198.51.100.9", "<script>x</script>", "http://testserver/"))

        self.client.post(reverse("auth:login_notifications"), {"enabled": "0"})
        self.user.refresh_from_db()
        self.assertFalse(self.user.notify_on_login)

    def test_a_queue_failure_does_not_fail_the_sign_in(self):
        self.user.notify_on_login = True
        self.user.save()
        with mock.patch("core.views.auth.async_task", side_effect=RuntimeError("broker down")):
            resp = self._sign_in()
        self.assertEqual(resp.status_code, 302)
        self.assertIn("_auth_user_id", self.client.session)

    def test_toggle_needs_sign_in_and_post(self):
        self.assertEqual(self.client.post(reverse("auth:login_notifications"), {"enabled": "1"}).status_code, 302)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("auth:login_notifications")).status_code, 405)
        self.user.refresh_from_db()
        self.assertFalse(self.user.notify_on_login)

    def test_the_task_escapes_the_browser_and_respects_a_later_opt_out(self):
        self.user.notify_on_login = True
        self.user.save()
        args = (self.user.pk, "198.51.100.9", "<script>x</script>", "2026-11-12 10:00:00 UTC", "https://pyrun.example/")
        self.assertTrue(security_events.send_login_notification(*args))
        message = mail.outbox[0]
        self.assertEqual(message.to, ["u@example.com"])
        self.assertIn("198.51.100.9", message.body)
        self.assertIn("https://pyrun.example/auth/change-password/", message.body)
        html = message.alternatives[0][0]
        self.assertIn("&lt;script&gt;x&lt;/script&gt;", html)
        self.assertNotIn("<script>x</script>", html)

        self.user.notify_on_login = False
        self.user.save()
        self.assertFalse(security_events.send_login_notification(*args))
        self.assertEqual(len(mail.outbox), 1)


class SecurityEventRecordingTests(TestCase):
    def setUp(self):
        _setup_done()
        self.ws = Workspace.get_default()
        self.admin = User.objects.create_user(
            username="root@example.com", email="root@example.com", password=PASSWORD, is_superuser=True, is_staff=True
        )
        WorkspaceMembership.ensure(self.admin, self.ws, role=WorkspaceMembership.ROLE_OWNER)

    def test_password_change_is_recorded(self):
        self.client.force_login(self.admin)
        self.client.post(
            reverse("auth:change_password"),
            {"current_password": PASSWORD, "password": "another-good-pass-42", "password_confirm": "another-good-pass-42"},
        )
        self.assertTrue(SecurityEvent.objects.filter(kind="password_change", user=self.admin).exists())

    def test_cli_password_reset_is_recorded(self):
        from io import StringIO

        from django.core.management import call_command

        with mock.patch("core.management.commands.user.Command._stdin_is_tty", return_value=False):
            call_command("user", "reset-password", "root@example.com", "--generate", stdout=StringIO())
        event = SecurityEvent.objects.get(kind="password_change")
        self.assertEqual((event.user, event.detail), (self.admin, "manage.py user reset-password"))

    def test_token_creation_is_recorded(self):
        self.client.force_login(self.admin)
        self.client.post(reverse("cpanel:api_token_create"), {"name": "ci", "scope": "datastores"})
        event = SecurityEvent.objects.get(kind="token_create")
        self.assertEqual(event.user, self.admin)
        self.assertEqual(event.detail, '"ci" (All datastores)')

    def test_recording_never_raises(self):
        with mock.patch("core.models.SecurityEvent.objects.create", side_effect=RuntimeError("db down")):
            security_events.record("login_ok", user=self.admin)

    def test_security_page_lists_and_filters_for_superusers_only(self):
        security_events.record("login_fail", email="ghost@example.com", detail="")
        security_events.record("login_ok", user=self.admin)
        self.client.force_login(self.admin)

        page = self.client.get(reverse("cpanel:user_security"))
        self.assertContains(page, "ghost@example.com")
        self.assertContains(page, "(no account)")
        self.assertContains(page, "Signed in")
        only_fail = self.client.get(reverse("cpanel:user_security"), {"kind": "login_fail"})
        self.assertEqual([e.kind for e in only_fail.context["events"]], ["login_fail"])
        by_email = self.client.get(reverse("cpanel:user_security"), {"email": "ROOT@"})
        self.assertEqual([e.email for e in by_email.context["events"]], ["root@example.com"])
        bogus = self.client.get(reverse("cpanel:user_security"), {"kind": "nonsense"})
        self.assertEqual(len(bogus.context["events"]), 2)

        member = User.objects.create_user(username="m@example.com", email="m@example.com", password=PASSWORD)
        self.client.force_login(member)
        self.assertEqual(self.client.get(reverse("cpanel:user_security")).status_code, 403)

    def test_purge_after_90_days_runs_in_housekeeping(self):
        from core.tasks import daily_housekeeping_task

        old = SecurityEvent.objects.create(kind="login_ok", email="a@example.com")
        SecurityEvent.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=91))
        SecurityEvent.objects.create(kind="login_ok", email="b@example.com")

        result = daily_housekeeping_task()
        self.assertEqual(result["security_events_deleted"], 1)
        self.assertEqual(list(SecurityEvent.objects.values_list("email", flat=True)), ["b@example.com"])

    def test_deleting_a_user_keeps_their_events(self):
        member = User.objects.create_user(username="m@example.com", email="m@example.com", password=PASSWORD)
        security_events.record("login_ok", user=member)
        member.delete()
        event = SecurityEvent.objects.get()
        self.assertEqual((event.user, event.email), (None, "m@example.com"))


class BackupTests(TestCase):
    def setUp(self):
        _setup_done()
        self.admin = User.objects.create_user(
            username="root@example.com", email="root@example.com", password=PASSWORD, is_superuser=True
        )

    def test_sign_in_alert_opt_in_round_trips(self):
        from core.services.backup_service import BackupService

        self.admin.notify_on_login = True
        self.admin.save()
        data = json.loads(json.dumps(BackupService.create_backup(include_runs=False)))
        exported = {u["email"]: u for u in data["users"]}
        self.assertIs(exported["root@example.com"]["notify_on_login"], True)

        User.objects.filter(pk=self.admin.pk).update(notify_on_login=False)
        BackupService.restore_backup(data, restore_runs=False, current_user=self.admin)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.notify_on_login)

    def test_an_older_backup_leaves_the_preference_alone(self):
        from core.services.backup_service import BackupService

        data = json.loads(json.dumps(BackupService.create_backup(include_runs=False)))
        for user in data["users"]:
            user.pop("notify_on_login")
        self.admin.notify_on_login = True
        self.admin.save()
        BackupService.restore_backup(data, restore_runs=False, current_user=self.admin)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.notify_on_login)


class SecurityEventModelTests(TestCase):
    def test_kinds_cover_the_ticket(self):
        self.assertEqual(
            set(SecurityEvent.Kind.values),
            {
                "login_ok", "login_fail", "lockout", "password_change", "reset_request", "reset", "reset_link",
                "token_create",
                # T46
                "2fa_enroll", "2fa_disable", "2fa_fail", "2fa_codes", "2fa_policy",
            },
        )
