"""
Public pages don't reveal how the server is run (2.5 test pass 8, H.01).

The sign-in, password and invite pages and the setup page that is public until
the first admin exists are read by anyone. They must not name management
commands or show raw exception text: the lockout page used to tell every
visitor to run ``manage.py user unlock <email>``, and a failed first-admin
creation put the exception into the setup page. Operator commands stay on the
signed-in admin pages and in the docs.
"""

from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from core.models import Environment, GlobalSettings, User
from core.services.setup_service import SetupService

AUTH_TEMPLATES = Path(settings.BASE_DIR) / "templates" / "auth"
LEAKS = ("manage.py", "Traceback", "PYRUNNER_")


def _setup_done(admin=True):
    gs = GlobalSettings.get_settings()
    gs.setup_completed = True
    gs.email_backend = GlobalSettings.EmailBackend.SMTP
    gs.save()
    if not Environment.objects.filter(is_default=True).exists():
        Environment.objects.create(name="env", path="p", is_active=True, is_default=True)
    if admin:
        User.objects.create(email="owner@example.com", is_superuser=True, is_staff=True)


class PublicTemplatesTests(TestCase):
    def test_no_auth_template_names_a_management_command(self):
        templates = sorted(AUTH_TEMPLATES.glob("*.html"))
        self.assertTrue(templates)
        for path in templates:
            with self.subTest(template=path.name):
                self.assertNotIn("manage.py", path.read_text(encoding="utf-8"))


class PublicResponsesTests(TestCase):
    def setUp(self):
        _setup_done()
        self.user = User.objects.create_user(
            username="someone@example.com", email="someone@example.com", password="correct-horse-91"
        )

    def assertClean(self, resp, status=200):
        self.assertEqual(resp.status_code, status)
        body = resp.content.decode()
        for leak in LEAKS:
            self.assertNotIn(leak, body)

    def _sign_in(self, email, password):
        return self.client.post(reverse("auth:login"), {"action": "password", "email": email, "password": password})

    def test_public_auth_pages_are_clean(self):
        for name, kwargs in (
            ("auth:login", {}),
            ("auth:forgot_password", {}),
            ("auth:reset_password", {"token": "no-such-token"}),
            ("auth:accept_invite", {"token": "no-such-token"}),
        ):
            with self.subTest(page=name):
                self.assertClean(self.client.get(reverse(name, kwargs=kwargs)))

    def test_the_lockout_page_is_clean_and_the_same_for_an_unknown_account(self):
        pages = []
        for email in ("someone@example.com", "nobody@example.com"):
            for _ in range(settings.AXES_FAILURE_LIMIT):
                self._sign_in(email, "wrong")
            resp = self._sign_in(email, "wrong")
            self.assertClean(resp, status=429)
            self.assertContains(resp, "contact your administrator to", status_code=429)
            pages.append(resp.content.decode().replace(email, "<email>"))
        self.assertEqual(pages[0], pages[1])


class SetupErrorTests(TestCase):
    def test_a_failed_first_admin_keeps_the_exception_out_of_the_message(self):
        with mock.patch.object(User.objects, "create_user", side_effect=RuntimeError("db at /srv/secret.sqlite3")):
            with self.assertLogs("core.services.setup_service", level="ERROR"):
                ok, message = SetupService.create_admin_user("first@example.com", "correct-horse-91")
        self.assertFalse(ok)
        self.assertNotIn("secret.sqlite3", message)
        self.assertNotIn("RuntimeError", message)

    def test_the_setup_page_shows_the_neutral_message(self):
        _setup_done(admin=False)
        with mock.patch.object(User.objects, "create_user", side_effect=RuntimeError("db at /srv/secret.sqlite3")):
            with self.assertLogs("core.services.setup_service", level="ERROR"):
                resp = self.client.post(reverse("setup:admin_setup"), {
                    "email": "first@example.com", "password": "Correct-horse-91!", "password_confirm":
                    "Correct-horse-91!",
                })
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("secret.sqlite3", resp.content.decode())
        shown = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertEqual(shown, ["The admin account couldn't be created. The server log has the details."])
