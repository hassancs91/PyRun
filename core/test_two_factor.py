"""
Two-factor sign-in (2.5 Stage 11, ticket T46).

What this pins, roughly in order of how much it would hurt to get wrong:

- A correct password doesn't start a session for an account with two-factor
  on: the user is stashed (5 minutes) and the code step starts the session.
  The same goes for the sign-in a password reset does, and for the Django
  admin's own login form (it sends to the console's sign-in).
- A code is accepted once (replay refused, the confirming code included);
  recovery codes work once each and are stored only as hashes; the secret is
  stored encrypted. Code attempts are limited per account and per IP.
- Turning it off and making new recovery codes need a current code.
- ``require_2fa`` (default off) makes an account without an authenticator set
  one up before its first session; invites and API tokens are unaffected.
- ``manage.py user disable-2fa`` is the escape hatch.
- RFC 4226 / RFC 6238 test vectors; security events for every change and
  refusal; backups carry the encrypted secret and the code hashes.
"""

import hashlib
import io
import json
import time
import uuid
from datetime import timedelta
from unittest import mock

from django.contrib.messages import get_messages
from django.core.management import CommandError, call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import (
    APIToken,
    Environment,
    GlobalSettings,
    PasswordResetToken,
    RecoveryCode,
    SecurityEvent,
    User,
    UserInvite,
    UserTOTP,
    Workspace,
)
from core.services import EncryptionService, two_factor
from core.views import two_factor as two_factor_views

PASSWORD = "correct-horse-battery-staple-46"
# A fixed clock (the rate-limit windows and the TOTP step read the same
# ``time.time``), so no test straddles a window or a 30-second step.
FIXED_TIME = 1_800_000_015.0


def _setup_done():
    gs = GlobalSettings.get_settings()
    gs.setup_completed = True
    gs.email_backend = GlobalSettings.EmailBackend.SMTP
    gs.save()
    # Without a default environment and an admin, the setup middleware sends
    # every page to the setup wizard.
    if not Environment.objects.filter(is_default=True).exists():
        Environment.objects.create(name="env", path="p", is_active=True, is_default=True)
    User.objects.create(email="owner@example.com", is_superuser=True, is_staff=True)
    return gs


def _messages(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


def _enroll(user, now=None):
    """Turn two-factor on for ``user`` (confirmed a minute ago); returns (secret, codes)."""
    now = time.time() - 60 if now is None else now
    secret = two_factor.begin_enrollment(user)
    codes = two_factor.confirm_enrollment(user, _code(secret, now), now=now)
    assert codes
    return secret, codes


def _code(secret, at=None):
    return two_factor.totp(two_factor.secret_bytes(secret), time.time() if at is None else at)


class RfcVectorTests(TestCase):
    """RFC 4226 appendix D and RFC 6238 appendix B."""

    def test_rfc4226_hotp_vectors(self):
        expected = ["755224", "287082", "359152", "969429", "338314",
                    "254676", "287922", "162583", "399871", "520489"]
        key = b"12345678901234567890"
        self.assertEqual([two_factor.hotp(key, n) for n in range(10)], expected)

    def test_rfc6238_totp_vectors(self):
        seeds = {
            hashlib.sha1: b"12345678901234567890",
            hashlib.sha256: b"12345678901234567890123456789012",
            hashlib.sha512: b"1234567890123456789012345678901234567890123456789012345678901234",
        }
        table = [
            (59, "94287082", "46119246", "90693936"),
            (1111111109, "07081804", "68084774", "25091201"),
            (1111111111, "14050471", "67062674", "99943326"),
            (1234567890, "89005924", "91819424", "93441116"),
            (2000000000, "69279037", "90698825", "38618901"),
            (20000000000, "65353130", "77737706", "47863826"),
        ]
        for at, sha1, sha256, sha512 in table:
            for digest, want in ((hashlib.sha1, sha1), (hashlib.sha256, sha256), (hashlib.sha512, sha512)):
                with self.subTest(at=at, digest=digest.__name__):
                    self.assertEqual(two_factor.totp(seeds[digest], at, digits=8, digest=digest), want)

    def test_secret_is_base32_and_round_trips(self):
        secret = two_factor.new_secret()
        self.assertEqual(len(secret), 32)
        self.assertEqual(len(two_factor.secret_bytes(secret)), two_factor.SECRET_BYTES)
        self.assertEqual(two_factor.secret_bytes(secret.lower()), two_factor.secret_bytes(secret))

    def test_drift_of_one_step_is_accepted_two_is_not(self):
        key = two_factor.secret_bytes(two_factor.new_secret())
        now = FIXED_TIME
        step = int(now // 30)
        self.assertEqual(two_factor.matching_step(key, two_factor.totp(key, now - 30), now), step - 1)
        self.assertEqual(two_factor.matching_step(key, two_factor.totp(key, now + 30), now), step + 1)
        self.assertIsNone(two_factor.matching_step(key, two_factor.totp(key, now - 60), now))

    def test_provisioning_uri_names_the_instance_and_account(self):
        uri = two_factor.provisioning_uri("ABCD", "a+b@example.com", "My PyRun")
        self.assertEqual(
            uri,
            "otpauth://totp/My%20PyRun:a%2Bb@example.com?secret=ABCD&issuer=My%20PyRun"
            "&algorithm=SHA1&digits=6&period=30",
        )


class ServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="s@example.com", email="s@example.com", password=PASSWORD)

    def test_secret_is_encrypted_at_rest(self):
        secret = two_factor.begin_enrollment(self.user)
        row = UserTOTP.objects.get(user=self.user)
        self.assertNotIn(secret, row.secret_encrypted)
        self.assertEqual(EncryptionService.decrypt(row.secret_encrypted), secret)
        self.assertIsNone(row.confirmed_at)
        # A pending enrollment keeps its secret (the QR code doesn't change on reload).
        self.assertEqual(two_factor.begin_enrollment(self.user), secret)
        self.assertFalse(two_factor.is_enabled(self.user))

    def test_confirming_spends_the_code_and_issues_hashed_recovery_codes(self):
        secret = two_factor.begin_enrollment(self.user)
        code = _code(secret, FIXED_TIME)
        self.assertIsNone(two_factor.confirm_enrollment(self.user, "000000" if code != "000000" else "111111", now=FIXED_TIME))
        codes = two_factor.confirm_enrollment(self.user, code, now=FIXED_TIME)
        self.assertEqual(len(codes), two_factor.RECOVERY_CODE_COUNT)
        self.assertEqual(len(set(codes)), len(codes))
        self.assertTrue(two_factor.is_enabled(self.user))
        stored = set(RecoveryCode.objects.filter(user=self.user).values_list("code_hash", flat=True))
        self.assertEqual(stored, {hashlib.sha256(c.replace("-", "").encode()).hexdigest() for c in codes})
        for plain in codes:
            self.assertNotIn(plain, stored)
        # The code that confirmed can't sign in afterwards.
        self.assertIsNone(two_factor.check_code(self.user, code, now=FIXED_TIME))
        self.assertEqual(two_factor.check_code(self.user, _code(secret, FIXED_TIME + 30), now=FIXED_TIME), "totp")

    def test_a_code_is_accepted_once_and_older_steps_are_refused(self):
        secret, _codes = _enroll(self.user)
        code = _code(secret, FIXED_TIME)
        self.assertEqual(two_factor.check_code(self.user, code, now=FIXED_TIME), "totp")
        self.assertIsNone(two_factor.check_code(self.user, code, now=FIXED_TIME))
        # The step before is still inside the drift window, but older than the last one used.
        self.assertIsNone(two_factor.check_code(self.user, _code(secret, FIXED_TIME - 30), now=FIXED_TIME))

    def test_recovery_codes_work_once_in_any_case_and_with_spaces(self):
        _secret, codes = _enroll(self.user)
        typed = " " + codes[0].upper().replace("-", " ") + " "
        self.assertEqual(two_factor.check_code(self.user, typed), "recovery")
        self.assertIsNone(two_factor.check_code(self.user, codes[0]))
        self.assertEqual(two_factor.recovery_codes_left(self.user), two_factor.RECOVERY_CODE_COUNT - 1)
        self.assertIsNone(two_factor.check_code(self.user, "not-a-real-code"))

    def test_new_recovery_codes_replace_the_old_ones(self):
        _secret, old = _enroll(self.user)
        new = two_factor.replace_recovery_codes(self.user)
        self.assertIsNone(two_factor.check_code(self.user, old[1]))
        self.assertEqual(two_factor.check_code(self.user, new[1]), "recovery")

    def test_disable_removes_the_secret_and_the_codes(self):
        _enroll(self.user)
        self.assertTrue(two_factor.disable(self.user))
        self.assertFalse(UserTOTP.objects.filter(user=self.user).exists())
        self.assertFalse(RecoveryCode.objects.filter(user=self.user).exists())
        self.assertFalse(two_factor.disable(self.user))

    def test_qr_code_is_an_inline_svg(self):
        svg = two_factor.qr_svg(two_factor.provisioning_uri(two_factor.new_secret(), "s@example.com", "PyRun"))
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn("viewBox", svg)
        self.assertNotIn("<script", svg)
        self.assertNotIn("data:", svg)


@mock.patch("core.ratelimit.time.time", return_value=FIXED_TIME)
class SignInTests(TestCase):
    def setUp(self):
        _setup_done()
        self.user = User.objects.create_user(username="u@example.com", email="u@example.com", password=PASSWORD)
        with mock.patch("core.ratelimit.time.time", return_value=FIXED_TIME):
            self.secret, self.codes = _enroll(self.user)

    def _password(self, **extra):
        return self.client.post(
            reverse("auth:login"), {"action": "password", "email": "u@example.com", "password": PASSWORD}, **extra
        )

    def _code(self, code, **extra):
        return self.client.post(reverse("auth:two_factor_verify"), {"code": code}, **extra)

    def test_the_password_alone_doesnt_start_the_session(self, _clock):
        resp = self._password()
        self.assertRedirects(resp, reverse("auth:two_factor_verify"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(self.client.session[two_factor.STASH_KEY]["user_id"], self.user.pk)
        self.assertFalse(SecurityEvent.objects.filter(kind="login_ok").exists())
        self.assertEqual(self.client.get(reverse("cpanel:dashboard")).status_code, 302)

        page = self.client.get(reverse("auth:two_factor_verify"))
        self.assertContains(page, "Two-factor sign-in")
        self.assertContains(page, 'autocomplete="one-time-code"')

    def test_a_right_code_starts_the_session_and_records_one_sign_in(self, _clock):
        self._password(HTTP_USER_AGENT="Firefox/140")
        with mock.patch("core.views.auth.async_task") as queued:
            resp = self._code(_code(self.secret), HTTP_USER_AGENT="Firefox/140")
        self.assertRedirects(resp, reverse("cpanel:dashboard"), fetch_redirect_response=False)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)
        self.assertNotIn(two_factor.STASH_KEY, self.client.session)
        ok = SecurityEvent.objects.get(kind="login_ok")
        self.assertEqual((ok.user, ok.detail, ok.user_agent), (self.user, "two-factor code", "Firefox/140"))
        # Sign-in alerts are opt-in; with them off nothing is queued.
        queued.assert_not_called()

    def test_the_sign_in_alert_is_queued_after_the_code_not_the_password(self, _clock):
        User.objects.filter(pk=self.user.pk).update(notify_on_login=True)
        with mock.patch("core.views.auth.async_task") as queued:
            self._password()
            queued.assert_not_called()
            self._code(_code(self.secret))
        queued.assert_called_once()
        self.assertEqual(queued.call_args.args[0], "core.services.security_events.send_login_notification")

    def test_a_wrong_code_is_refused_and_recorded(self, _clock):
        self._password()
        right = _code(self.secret)
        wrong = "000000" if right != "000000" else "111111"
        resp = self._code(wrong)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, two_factor_views.WRONG_CODE_MESSAGE.replace("'", "&#x27;"))
        self.assertNotIn("_auth_user_id", self.client.session)
        fail = SecurityEvent.objects.get(kind="2fa_fail")
        self.assertEqual((fail.user, fail.detail), (self.user, "wrong code"))

    def test_a_used_code_cant_sign_in_again(self, _clock):
        code = _code(self.secret)
        self._password()
        self._code(code)
        self.client.post(reverse("auth:logout"))
        self._password()
        resp = self._code(code)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_a_recovery_code_signs_in_once(self, _clock):
        self._password()
        resp = self._code(self.codes[0])
        self.assertEqual(resp.status_code, 302)
        ok = SecurityEvent.objects.get(kind="login_ok")
        self.assertEqual(ok.detail, f"recovery code, {two_factor.RECOVERY_CODE_COUNT - 1} left")
        self.client.post(reverse("auth:logout"))
        self._password()
        self.assertEqual(self._code(self.codes[0]).status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_running_low_on_recovery_codes_says_so(self, _clock):
        for code in self.codes[:-3]:
            two_factor.use_recovery_code(self.user, code)
        self._password()
        resp = self._code(self.codes[-1])
        self.assertIn(
            "You have 2 recovery codes left. Make a new set on the Two-factor sign-in page.", _messages(resp)
        )

    def test_attempts_are_limited_per_account(self, _clock):
        self._password()
        right = _code(self.secret)
        wrong = "000000" if right != "000000" else "111111"
        for _ in range(two_factor.USER_ATTEMPTS):
            self._code(wrong)
        resp = self._code(right)
        self.assertContains(resp, two_factor_views.THROTTLED_MESSAGE)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(SecurityEvent.objects.filter(kind="2fa_fail", detail="too many attempts").count(), 1)

    def test_attempts_are_limited_per_ip_across_accounts(self, _clock):
        with mock.patch.object(two_factor, "IP_ATTEMPTS", 2):
            for _ in range(2):
                self._password(REMOTE_ADDR="198.51.100.20")
                self._code("000000", REMOTE_ADDR="198.51.100.20")
            other = User.objects.create_user(username="o@example.com", email="o@example.com", password=PASSWORD)
            other_secret, _ = _enroll(other)
            self.client.post(
                reverse("auth:login"),
                {"action": "password", "email": "o@example.com", "password": PASSWORD},
                REMOTE_ADDR="198.51.100.20",
            )
            resp = self._code(_code(other_secret), REMOTE_ADDR="198.51.100.20")
        self.assertContains(resp, two_factor_views.THROTTLED_MESSAGE)

    def test_the_stash_expires_after_five_minutes(self, clock):
        self._password()
        clock.return_value = FIXED_TIME + two_factor.STASH_SECONDS + 1
        resp = self._code(_code(self.secret, FIXED_TIME + two_factor.STASH_SECONDS + 1))
        self.assertRedirects(resp, reverse("auth:login"), fetch_redirect_response=False)
        self.assertIn(two_factor_views.EXPIRED_MESSAGE, _messages(resp))
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertNotIn(two_factor.STASH_KEY, self.client.session)

    def test_the_code_step_without_a_password_goes_back_to_sign_in(self, _clock):
        resp = self._code(_code(self.secret))
        self.assertRedirects(resp, reverse("auth:login"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_the_stash_gets_a_new_session_key(self, _clock):
        self.client.get(reverse("auth:login"))
        self.client.session.save()
        before = self.client.session.session_key
        self._password()
        self.assertNotEqual(self.client.session.session_key, before)

    def test_turned_off_from_the_shell_meanwhile_signs_in_on_the_password(self, _clock):
        self._password()
        two_factor.disable(self.user)
        resp = self.client.get(reverse("auth:two_factor_verify"))
        self.assertRedirects(resp, reverse("cpanel:dashboard"), fetch_redirect_response=False)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)

    def test_password_reset_routes_through_the_code_step(self, _clock):
        token = PasswordResetToken.create_for_user(self.user)
        resp = self.client.post(
            reverse("auth:reset_password", kwargs={"token": token.token}),
            {"password": "a-brand-new-password-46", "password_confirm": "a-brand-new-password-46"},
        )
        self.assertRedirects(resp, reverse("auth:two_factor_verify"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertTrue(SecurityEvent.objects.filter(kind="reset", user=self.user).exists())
        resp = self._code(_code(self.secret))
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)
        ok = SecurityEvent.objects.get(kind="login_ok")
        self.assertEqual(ok.detail, "after password reset, two-factor code")

    def test_the_admin_login_form_sends_to_the_console_sign_in(self, _clock):
        from django.contrib import admin

        resp = self.client.post(
            reverse("admin:login"), {"username": "u@example.com", "password": PASSWORD}
        )
        self.assertRedirects(resp, reverse("auth:login"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(admin.site.login.__name__, "_console_sign_in")


class WithoutTwoFactorTests(TestCase):
    """Accounts without two-factor and require_2fa off sign in as before."""

    def setUp(self):
        _setup_done()
        self.user = User.objects.create_user(username="p@example.com", email="p@example.com", password=PASSWORD)

    def test_password_signs_in_directly(self):
        resp = self.client.post(
            reverse("auth:login"), {"action": "password", "email": "p@example.com", "password": PASSWORD}
        )
        self.assertRedirects(resp, reverse("cpanel:dashboard"), fetch_redirect_response=False)
        self.assertEqual(SecurityEvent.objects.get(kind="login_ok").detail, "")

    def test_a_pending_setup_doesnt_guard_sign_in(self):
        two_factor.begin_enrollment(self.user)
        resp = self.client.post(
            reverse("auth:login"), {"action": "password", "email": "p@example.com", "password": PASSWORD}
        )
        self.assertRedirects(resp, reverse("cpanel:dashboard"), fetch_redirect_response=False)

    def test_password_reset_signs_in_directly(self):
        token = PasswordResetToken.create_for_user(self.user)
        self.client.post(
            reverse("auth:reset_password", kwargs={"token": token.token}),
            {"password": "a-brand-new-password-46", "password_confirm": "a-brand-new-password-46"},
        )
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)


@mock.patch("core.ratelimit.time.time", return_value=FIXED_TIME)
class AccountPageTests(TestCase):
    def setUp(self):
        _setup_done()
        self.user = User.objects.create_user(username="a@example.com", email="a@example.com", password=PASSWORD)
        self.client.force_login(self.user)

    def test_change_password_page_links_to_two_factor(self, _clock):
        page = self.client.get(reverse("auth:change_password"))
        self.assertContains(page, "Two-factor sign-in")
        self.assertContains(page, reverse("auth:two_factor"))
        self.assertContains(page, "Set up")

    def test_set_up_shows_an_inline_svg_qr_and_confirms_with_a_code(self, _clock):
        page = self.client.get(reverse("auth:two_factor"))
        self.assertContains(page, "Set up two-factor sign-in")
        self.client.post(reverse("auth:two_factor_start"))
        page = self.client.get(reverse("auth:two_factor"))
        body = page.content.decode()
        secret = two_factor.pending_secret(self.user)
        # CSP-safe: the QR code is markup in the page, not an image URL or a script.
        self.assertIn('<svg viewBox="', body)
        self.assertNotIn("<img", body)
        self.assertNotIn("data:image", body)
        self.assertIn(two_factor.group_secret(secret), body)
        self.assertIn("no-store", page["Cache-Control"])

        resp = self.client.post(reverse("auth:two_factor_confirm"), {"code": _code(secret)})
        self.assertRedirects(resp, reverse("auth:two_factor_codes"), fetch_redirect_response=False)
        codes_page = self.client.get(reverse("auth:two_factor_codes"))
        codes = codes_page.context["codes"]
        self.assertEqual(len(codes), two_factor.RECOVERY_CODE_COUNT)
        for code in codes:
            self.assertContains(codes_page, code)
        # Shown once.
        self.assertRedirects(
            self.client.get(reverse("auth:two_factor_codes")), reverse("auth:two_factor"), fetch_redirect_response=False
        )
        self.assertTrue(two_factor.is_enabled(self.user))
        self.assertEqual(SecurityEvent.objects.get(kind="2fa_enroll").user, self.user)
        self.assertContains(self.client.get(reverse("auth:two_factor")), "On since")

    def test_a_wrong_confirm_code_keeps_it_pending(self, _clock):
        self.client.post(reverse("auth:two_factor_start"))
        right = _code(two_factor.pending_secret(self.user))
        resp = self.client.post(reverse("auth:two_factor_confirm"), {"code": "000000" if right != "000000" else "111111"})
        self.assertRedirects(resp, reverse("auth:two_factor"), fetch_redirect_response=False)
        self.assertFalse(two_factor.is_enabled(self.user))
        self.assertEqual(SecurityEvent.objects.get(kind="2fa_fail").detail, "wrong code while setting up")

    def test_cancel_a_pending_setup_needs_no_code(self, _clock):
        self.client.post(reverse("auth:two_factor_start"))
        self.client.post(reverse("auth:two_factor_disable"))
        self.assertFalse(UserTOTP.objects.filter(user=self.user).exists())
        self.assertFalse(SecurityEvent.objects.filter(kind="2fa_disable").exists())

    def test_turning_off_needs_a_current_code(self, _clock):
        secret, _codes = _enroll(self.user)
        self.client.post(reverse("auth:two_factor_disable"), {"code": ""})
        self.client.post(reverse("auth:two_factor_disable"), {"code": "12345"})
        self.assertTrue(two_factor.is_enabled(self.user))
        self.assertEqual(SecurityEvent.objects.filter(kind="2fa_fail").count(), 2)

        resp = self.client.post(reverse("auth:two_factor_disable"), {"code": _code(secret)})
        self.assertIn("Two-factor sign-in is off.", _messages(resp))
        self.assertFalse(two_factor.is_enabled(self.user))
        self.assertEqual(SecurityEvent.objects.get(kind="2fa_disable").user, self.user)

    def test_turning_off_with_a_recovery_code(self, _clock):
        _secret, codes = _enroll(self.user)
        self.client.post(reverse("auth:two_factor_disable"), {"code": codes[3]})
        self.assertFalse(two_factor.is_enabled(self.user))

    def test_new_recovery_codes_need_a_current_code(self, _clock):
        secret, old = _enroll(self.user)
        self.client.post(reverse("auth:two_factor_codes"), {"code": "000000"})
        self.assertEqual(two_factor.check_code(self.user, old[0]), "recovery")

        resp = self.client.post(reverse("auth:two_factor_codes"), {"code": _code(secret)})
        self.assertRedirects(resp, reverse("auth:two_factor_codes"), fetch_redirect_response=False)
        new = self.client.get(reverse("auth:two_factor_codes")).context["codes"]
        self.assertIsNone(two_factor.check_code(self.user, old[1]))
        self.assertEqual(two_factor.check_code(self.user, new[0]), "recovery")
        self.assertEqual(SecurityEvent.objects.get(kind="2fa_codes").user, self.user)

    def test_account_page_attempts_are_limited_too(self, _clock):
        secret, _codes = _enroll(self.user)
        for _ in range(two_factor.USER_ATTEMPTS):
            self.client.post(reverse("auth:two_factor_disable"), {"code": "000000"})
        resp = self.client.post(reverse("auth:two_factor_disable"), {"code": _code(secret)})
        self.assertIn(two_factor_views.THROTTLED_MESSAGE, _messages(resp))
        self.assertTrue(two_factor.is_enabled(self.user))

    def test_encryption_failure_shows_neutral_text(self, _clock):
        from core.services import EncryptionError

        with mock.patch.object(EncryptionService, "encrypt", side_effect=EncryptionError("ENCRYPTION_KEY not set")):
            resp = self.client.post(reverse("auth:two_factor_start"))
        msgs = _messages(resp)
        self.assertEqual(msgs, [two_factor_views.UNAVAILABLE_MESSAGE])
        self.assertNotIn("ENCRYPTION_KEY", " ".join(msgs))

    def test_an_unreadable_secret_isnt_called_a_wrong_code(self, _clock):
        _enroll(self.user)
        UserTOTP.objects.filter(user=self.user).update(secret_encrypted="gAAAAA-not-a-token")
        resp = self.client.post(reverse("auth:two_factor_disable"), {"code": "123456"})
        self.assertEqual(_messages(resp), [two_factor_views.CANT_CHECK_MESSAGE])
        self.assertFalse(SecurityEvent.objects.filter(kind="2fa_fail").exists())
        self.assertTrue(two_factor.is_enabled(self.user))

    def test_pages_need_a_session(self, _clock):
        self.client.logout()
        for name in ("auth:two_factor", "auth:two_factor_codes"):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 302)
        self.client.post(reverse("auth:two_factor_start"))
        self.assertFalse(UserTOTP.objects.exists())


@mock.patch("core.ratelimit.time.time", return_value=FIXED_TIME)
class RequireTwoFactorTests(TestCase):
    def setUp(self):
        self.gs = _setup_done()
        self.admin = User.objects.create_user(
            username="root@example.com", email="root@example.com", password=PASSWORD, is_superuser=True, is_staff=True
        )
        self.user = User.objects.create_user(username="r@example.com", email="r@example.com", password=PASSWORD)

    def _require(self, on=True):
        GlobalSettings.objects.filter(pk=self.gs.pk).update(require_2fa=on)

    def _password(self, email="r@example.com"):
        return self.client.post(reverse("auth:login"), {"action": "password", "email": email, "password": PASSWORD})

    def test_off_by_default(self, _clock):
        self.assertFalse(GlobalSettings.get_settings().require_2fa)
        self.assertFalse(GlobalSettings._meta.get_field("require_2fa").default)

    def test_on_forces_setup_before_the_session(self, _clock):
        self._require()
        resp = self._password()
        self.assertRedirects(resp, reverse("auth:two_factor_enroll"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)
        page = self.client.get(reverse("auth:two_factor_enroll"))
        self.assertContains(page, "This instance requires a code")
        self.assertContains(page, '<svg viewBox="')
        # Nothing else is reachable meanwhile.
        self.assertEqual(self.client.get(reverse("cpanel:dashboard")).status_code, 302)
        # The code step sends an account without an authenticator to set-up.
        self.assertRedirects(
            self.client.get(reverse("auth:two_factor_verify")), reverse("auth:two_factor_enroll"),
            fetch_redirect_response=False,
        )

        secret = two_factor.pending_secret(self.user)
        resp = self.client.post(reverse("auth:two_factor_enroll"), {"code": _code(secret)})
        self.assertRedirects(resp, reverse("auth:two_factor_codes"), fetch_redirect_response=False)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)
        self.assertEqual(len(self.client.get(reverse("auth:two_factor_codes")).context["codes"]), 10)
        self.assertEqual(SecurityEvent.objects.get(kind="2fa_enroll").detail, "required at sign-in")
        self.assertEqual(SecurityEvent.objects.get(kind="login_ok").detail, "two-factor set up")

    def test_a_wrong_setup_code_is_refused(self, _clock):
        self._require()
        self._password()
        self.client.get(reverse("auth:two_factor_enroll"))
        right = _code(two_factor.pending_secret(self.user))
        resp = self.client.post(reverse("auth:two_factor_enroll"), {"code": "000000" if right != "000000" else "111111"})
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertFalse(two_factor.is_enabled(self.user))

    def test_an_enrolled_account_gets_the_code_step(self, _clock):
        _enroll(self.user)
        self._require()
        self.assertRedirects(self._password(), reverse("auth:two_factor_verify"), fetch_redirect_response=False)

    def test_invites_are_unaffected(self, _clock):
        self._require()
        invite = UserInvite.objects.create(
            email="new@example.com", token=uuid.uuid4().hex, created_by=self.admin,
            expires_at=timezone.now() + timedelta(days=1),
        )
        resp = self.client.post(
            reverse("auth:accept_invite", kwargs={"token": invite.token}),
            {"password": "invited-password-46-x", "password_confirm": "invited-password-46-x"},
        )
        self.assertRedirects(resp, reverse("cpanel:dashboard"), fetch_redirect_response=False)
        self.assertEqual(self.client.session["_auth_user_id"], str(User.objects.get(email="new@example.com").pk))

    def test_api_tokens_are_unaffected(self, _clock):
        _enroll(self.admin)
        self._require()
        raw = APIToken.generate_token(scope="api", kind="bearer")
        APIToken.objects.create(
            name="t46", scope="api", token=raw, workspace=Workspace.get_default(),
            permissions=["scripts:read"], created_by=self.admin,
        )
        resp = self.client.get("/api/v1/_catalog", HTTP_AUTHORIZATION="Bearer " + raw)
        self.assertEqual(resp.status_code, 200)

    def test_superuser_toggles_the_requirement_on_users_security(self, _clock):
        self.client.force_login(self.admin)
        page = self.client.get(reverse("cpanel:user_security"))
        self.assertContains(page, "Require for everyone")
        self.client.post(reverse("cpanel:user_require_2fa"), {"enabled": "1"})
        self.assertTrue(GlobalSettings.get_settings().require_2fa)
        self.assertContains(self.client.get(reverse("cpanel:user_security")), "Make optional")
        self.client.post(reverse("cpanel:user_require_2fa"), {"enabled": "0"})
        self.assertFalse(GlobalSettings.get_settings().require_2fa)
        self.assertEqual(
            list(SecurityEvent.objects.filter(kind="2fa_policy").order_by("id").values_list("detail", flat=True)),
            ["required", "optional"],
        )

    def test_members_cant_change_the_requirement(self, _clock):
        self.client.force_login(self.user)
        resp = self.client.post(reverse("cpanel:user_require_2fa"), {"enabled": "1"})
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(GlobalSettings.get_settings().require_2fa)

    def test_users_list_marks_accounts_with_two_factor(self, _clock):
        _enroll(self.user)
        self.client.force_login(self.admin)
        page = self.client.get(reverse("cpanel:user_list"))
        self.assertContains(page, 'title="Two-factor sign-in is on">2FA</span>', count=1)


class DisableCommandTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="c@example.com", email="c@example.com", password=PASSWORD)

    def test_disable_2fa_turns_it_off_and_records_it(self):
        _enroll(self.user)
        out = io.StringIO()
        call_command("user", "disable-2fa", "C@example.com", stdout=out)
        self.assertIn("Two-factor sign-in is off for c@example.com.", out.getvalue())
        self.assertFalse(two_factor.is_enabled(self.user))
        self.assertFalse(RecoveryCode.objects.filter(user=self.user).exists())
        event = SecurityEvent.objects.get(kind="2fa_disable")
        self.assertEqual((event.user, event.detail), (self.user, "manage.py user disable-2fa"))

    def test_an_account_without_it_is_told_so(self):
        out = io.StringIO()
        call_command("user", "disable-2fa", "c@example.com", stdout=out)
        self.assertIn("doesn't use two-factor sign-in", out.getvalue())
        self.assertFalse(SecurityEvent.objects.exists())

    def test_a_set_up_never_confirmed_counts_as_none_and_is_cleared_quietly(self):
        # Run 7 O1: a pending row (the forced set-up page or "Set up" creates
        # one) never guarded a sign-in, so there is nothing to turn off.
        two_factor.begin_enrollment(self.user)
        out = io.StringIO()
        call_command("user", "disable-2fa", "c@example.com", stdout=out)
        self.assertIn("c@example.com doesn't use two-factor sign-in.", out.getvalue())
        self.assertNotIn("is off", out.getvalue())
        self.assertFalse(UserTOTP.objects.filter(user=self.user).exists())
        self.assertFalse(SecurityEvent.objects.filter(kind="2fa_disable").exists())

    def test_unknown_account(self):
        with self.assertRaises(CommandError):
            call_command("user", "disable-2fa", "ghost@example.com", stdout=io.StringIO())


class BackupTests(TestCase):
    def setUp(self):
        _setup_done()
        self.admin = User.objects.create_user(
            username="root@example.com", email="root@example.com", password=PASSWORD, is_superuser=True
        )

    def _backup(self):
        from core.services.backup_service import BackupService

        return json.loads(json.dumps(BackupService.create_backup(include_runs=False)))

    def _restore(self, data):
        from core.services.backup_service import BackupService

        result = BackupService.restore_backup(data, restore_runs=False, current_user=self.admin)
        self.assertTrue(result["success"], result)
        return result

    def test_export_carries_the_encrypted_secret_and_code_hashes_only(self):
        secret, codes = _enroll(self.admin)
        two_factor.use_recovery_code(self.admin, codes[0])
        two_factor.begin_enrollment(User.objects.get(email="owner@example.com"))  # pending: not exported
        data = self._backup()
        users = {u["email"]: u for u in data["users"]}
        exported = users["root@example.com"]["two_factor"]
        self.assertEqual(EncryptionService.decrypt(exported["secret_encrypted"]), secret)
        self.assertEqual(len(exported["recovery_code_hashes"]), two_factor.RECOVERY_CODE_COUNT - 1)
        self.assertNotIn("two_factor", users["owner@example.com"])
        text = json.dumps(data)
        self.assertNotIn(secret, text)
        for code in codes:
            self.assertNotIn(code, text)

    def test_restore_puts_it_back_onto_the_same_email_account(self):
        secret, codes = _enroll(self.admin)
        data = self._backup()
        two_factor.disable(self.admin)
        self._restore(data)
        self.assertTrue(two_factor.is_enabled(self.admin))
        self.assertEqual(two_factor.check_code(self.admin, codes[2]), "recovery")
        self.assertEqual(two_factor.check_code(self.admin, _code(secret)), "totp")

    def test_restore_never_replaces_an_authenticator_the_account_has(self):
        _enroll(self.admin)
        data = self._backup()
        two_factor.disable(self.admin)
        mine, _ = _enroll(self.admin)
        self._restore(data)
        self.assertEqual(two_factor.pending_secret(self.admin), None)
        row = UserTOTP.objects.get(user=self.admin)
        self.assertEqual(EncryptionService.decrypt(row.secret_encrypted), mine)

    def test_a_secret_this_server_cant_decrypt_is_skipped(self):
        _enroll(self.admin)
        data = self._backup()
        two_factor.disable(self.admin)
        for user in data["users"]:
            if "two_factor" in user:
                user["two_factor"]["secret_encrypted"] = "gAAAAA-not-a-token"
        with self.assertLogs("core.services.backup_service", "WARNING") as logs:
            result = self._restore(data)
        self.assertFalse(two_factor.is_enabled(self.admin))
        # Run 7 O2: the restore's warnings say so too, not only the server log.
        self.assertIn(
            "Two-factor sign-in for 1 account wasn't restored: the backup was made with another "
            "ENCRYPTION_KEY; that account signs in with the password only.",
            result["warnings"],
        )
        self.assertIn("not restored (the secret doesn't decrypt here)", " ".join(logs.output))

    def test_the_warning_counts_every_account_another_key_encrypted(self):
        from cryptography.fernet import Fernet

        other = User.objects.create_user(username="b@example.com", email="b@example.com", password=PASSWORD)
        _enroll(self.admin)
        _enroll(other)
        data = self._backup()
        two_factor.disable(self.admin)
        two_factor.disable(other)
        foreign = Fernet(Fernet.generate_key())
        for user in data["users"]:
            if "two_factor" in user:
                user["two_factor"]["secret_encrypted"] = foreign.encrypt(b"JBSWY3DPEHPK3PXP").decode()
        result = self._restore(data)
        self.assertFalse(two_factor.is_enabled(self.admin) or two_factor.is_enabled(other))
        lines = [w for w in result["warnings"] if w.startswith("Two-factor sign-in")]
        self.assertEqual(lines, [
            "Two-factor sign-in for 2 accounts wasn't restored: the backup was made with another "
            "ENCRYPTION_KEY; those accounts sign in with the password only."
        ])

    def test_a_restore_with_the_same_key_adds_no_two_factor_warning(self):
        _enroll(self.admin)
        data = self._backup()
        two_factor.disable(self.admin)
        result = self._restore(data)
        self.assertTrue(two_factor.is_enabled(self.admin))
        self.assertFalse([w for w in result["warnings"] if "Two-factor" in w], result["warnings"])

    def test_an_older_backup_changes_nothing(self):
        _enroll(self.admin)
        data = self._backup()
        for user in data["users"]:
            user.pop("two_factor", None)
        data["global_settings"].pop("require_2fa", None)
        self._restore(data)
        self.assertTrue(two_factor.is_enabled(self.admin))
        self.assertFalse(GlobalSettings.get_settings().require_2fa)

    def test_the_requirement_round_trips(self):
        GlobalSettings.objects.update(require_2fa=True)
        data = self._backup()
        self.assertIs(data["global_settings"]["require_2fa"], True)
        GlobalSettings.objects.update(require_2fa=False)
        self._restore(data)
        self.assertTrue(GlobalSettings.get_settings().require_2fa)
