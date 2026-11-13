"""
M4 polish batch (2.0 release test) — auth pages and account CLI.

U1/F12  a field-only error doesn't draw an empty error box
U11     a used reset/invite link is titled "already used", not "expired"
U12     expired invites are marked Expired with a "New link" action
U19     a CSRF failure renders the console's "This form expired" page
F17     ``manage.py recaptcha off`` switches the login reCAPTCHA off
F18     ``manage.py user --generate`` points members at the header key icon
"""

from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Environment, GlobalSettings, PasswordResetToken, User, UserInvite

PW = "correct-horse-battery-staple-91"
NON_FIELD_BOX = "mb-6 p-4 bg-fail/10 border border-fail/30 rounded-lg"


class PolishAuthBase(TestCase):
    def setUp(self):
        gs = GlobalSettings.get_settings()
        gs.setup_completed = True
        gs.save()
        Environment.objects.get_or_create(
            name="default", defaults={"is_default": True, "python_version": "3.12"}
        )
        self.admin = User.objects.create_user(
            username="admin@example.com", email="admin@example.com", password=PW,
            is_superuser=True, is_staff=True,
        )


class FieldErrorBoxTests(PolishAuthBase):
    def test_field_only_error_draws_no_empty_error_box(self):
        self.client.force_login(self.admin)
        resp = self.client.post(reverse("auth:change_password"), {
            "current_password": PW, "password": "password", "password_confirm": "password",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["form"].errors)
        self.assertFalse(resp.context["form"].non_field_errors())
        self.assertNotContains(resp, NON_FIELD_BOX)


class LinkErrorTitleTests(PolishAuthBase):
    def test_used_reset_link_is_titled_already_used_without_the_form_subtitle(self):
        token = PasswordResetToken.create_for_user(self.admin)
        token.used_at = timezone.now()
        token.save()
        resp = self.client.get(reverse("auth:reset_password", args=[token.token]), follow=True)
        self.assertContains(resp, "Link already used")
        self.assertNotContains(resp, "Link expired")
        self.assertNotContains(resp, "Enter your new password")

    def test_expired_reset_link_is_titled_expired(self):
        token = PasswordResetToken.create_for_user(self.admin)
        token.expires_at = timezone.now() - timedelta(minutes=1)
        token.save()
        resp = self.client.get(reverse("auth:reset_password", args=[token.token]), follow=True)
        self.assertContains(resp, "Link expired")

    def test_used_invite_is_titled_already_used(self):
        invite = UserInvite.create_invite("new@example.com", created_by=self.admin)
        invite.used_at = timezone.now()
        invite.save()
        resp = self.client.get(reverse("auth:accept_invite", args=[invite.token]))
        self.assertContains(resp, "Invite already used")
        self.assertNotContains(resp, "Invite expired")


class ExpiredInviteListTests(PolishAuthBase):
    def test_expired_invite_is_badged_with_a_new_link_action(self):
        live = UserInvite.create_invite("live@example.com", created_by=self.admin)
        old = UserInvite.create_invite("old@example.com", created_by=self.admin)
        old.expires_at = timezone.now() - timedelta(hours=2)
        old.save()
        self.client.force_login(self.admin)
        html = self.client.get(reverse("cpanel:user_list")).content.decode()
        old_row = html.split("old@example.com", 1)[1].split("Revoke", 1)[0]
        live_row = html.split("live@example.com", 1)[1].split("Revoke", 1)[0]
        self.assertIn("Expired", old_row)
        self.assertIn("New link", old_row)
        self.assertNotIn("Copy link", old_row)
        self.assertNotIn("Expires 0", html)
        self.assertIn("Copy link", live_row)
        self.assertNotIn("New link", live_row)
        self.assertIn(live.token, live_row)

    def test_new_link_for_an_expired_invite_gives_no_duplicate_warning(self):
        old = UserInvite.create_invite("old@example.com", created_by=self.admin)
        old.expires_at = timezone.now() - timedelta(hours=2)
        old.save()
        self.client.force_login(self.admin)
        resp = self.client.post(reverse("cpanel:invite_user"), {"email": "old@example.com"}, follow=True)
        texts = [str(m) for m in resp.context["messages"]]
        self.assertFalse(any("already exists" in t for t in texts), texts)
        self.assertTrue(UserInvite.objects.get(email="old@example.com").is_valid())


class CsrfFailurePageTests(PolishAuthBase):
    def test_stale_form_shows_the_console_form_expired_page(self):
        client = Client(enforce_csrf_checks=True)
        resp = client.post(reverse("auth:login"), {"action": "password", "email": "x@example.com"})
        self.assertEqual(resp.status_code, 403)
        self.assertContains(resp, "This form expired", status_code=403)
        self.assertNotContains(resp, "CSRF verification failed", status_code=403)


class RecaptchaCommandTests(PolishAuthBase):
    def test_off_switches_recaptcha_off(self):
        gs = GlobalSettings.get_settings()
        gs.recaptcha_enabled = True
        gs.recaptcha_site_key = "site"
        gs.recaptcha_secret_key_encrypted = "enc"
        gs.save()
        out = StringIO()
        call_command("recaptcha", "off", stdout=out)
        self.assertFalse(GlobalSettings.get_settings().recaptcha_enabled)
        self.assertIn("switched off", out.getvalue())
        status = StringIO()
        call_command("recaptcha", "status", stdout=status)
        self.assertIn("reCAPTCHA is off", status.getvalue())


class UserCliCopyTests(PolishAuthBase):
    def test_generated_password_hint_names_the_header_key_icon(self):
        out = StringIO()
        call_command("user", "create", "member@example.com", "--generate", stdout=out, stderr=StringIO())
        self.assertIn("key icon in the header", out.getvalue())
        self.assertNotIn("under Settings", out.getvalue())
