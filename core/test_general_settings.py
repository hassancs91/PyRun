"""
General settings tests — the Settings → General card must DO what it says.

Regression for review 8.1: instance_name, timezone, date_format and
time_format were stored, validated, and backed up but consumed by nothing —
the header hardcoded "PyRun", emails hardcoded "[PyRun]", and all
datetimes rendered in UTC. Now instance_name drives the header/title/email
subjects, timezone drives display (TimezoneMiddleware) and scheduled-backup
times, and the two format fields are gone. Also covers review 6.3: the
timezone dropdown's visual separator used to be a submittable value.
"""

from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone as dj_timezone

from core.forms import GeneralSettingsForm
from core.middleware import TimezoneMiddleware
from core.models import Environment, GlobalSettings, User, Workspace, WorkspaceMembership
from core.services.notification_service import NotificationService


class TimezoneMiddlewareTests(TestCase):
    def tearDown(self):
        dj_timezone.deactivate()

    def _active_tz_during_request(self) -> str:
        captured = {}

        def get_response(request):
            captured["tz"] = dj_timezone.get_current_timezone_name()
            return None

        TimezoneMiddleware(get_response)(RequestFactory().get("/"))
        return captured["tz"]

    def test_instance_timezone_is_activated(self):
        gs = GlobalSettings.get_settings()
        gs.timezone = "Asia/Tokyo"
        gs.save()
        self.assertEqual(self._active_tz_during_request(), "Asia/Tokyo")

    def test_bad_timezone_falls_back_to_utc(self):
        gs = GlobalSettings.get_settings()
        gs.timezone = "Not/AZone"
        gs.save()
        self.assertEqual(self._active_tz_during_request(), "UTC")


class InstanceNameTests(TestCase):
    def test_header_shows_instance_name(self):
        gs = GlobalSettings.get_settings()
        gs.instance_name = "Acme Ops"
        gs.setup_completed = True
        gs.save()
        Environment.objects.get_or_create(
            name="default", defaults={"is_default": True, "python_version": "3.12"}
        )
        admin = User.objects.create(
            email="admin@example.com", is_superuser=True, is_staff=True
        )
        WorkspaceMembership.ensure(
            admin, Workspace.get_default(), role=WorkspaceMembership.ROLE_OWNER
        )
        self.client.force_login(admin)

        resp = self.client.get(reverse("cpanel:services"))

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Acme Ops")

    def test_email_subject_uses_instance_name(self):
        gs = GlobalSettings.get_settings()
        gs.instance_name = "Acme Ops"
        gs.save()
        self.assertEqual(NotificationService._subject("Test Email"), "[Acme Ops] Test Email")

    def test_email_subject_falls_back_to_product_name(self):
        gs = GlobalSettings.get_settings()
        gs.instance_name = ""
        gs.save()
        self.assertEqual(NotificationService._subject("Test Email"), "[PyRun] Test Email")


class TimezoneChoicesTests(TestCase):
    """The dropdown separator must not be a submittable value (review 6.3)."""

    def test_separator_is_not_a_valid_choice(self):
        form = GeneralSettingsForm(data={"timezone": "---"})
        self.assertFalse(form.is_valid())
        self.assertIn("timezone", form.errors)

    def test_real_timezone_validates_and_saves(self):
        form = GeneralSettingsForm(
            data={"timezone": "Asia/Tokyo", "instance_name": "Acme Ops"}
        )
        self.assertTrue(form.is_valid(), form.errors)
        gs = form.save(GlobalSettings.get_settings())
        self.assertEqual(gs.timezone, "Asia/Tokyo")
        self.assertEqual(gs.instance_name, "Acme Ops")


class BrandingTests(TestCase):
    """The product brand is "PyRun" (rebrand from PyRunner, v1.17).

    Guards the user-visible surfaces: the console <title>/header fallback when
    no instance name is set, the login page, the password-reset email subject,
    and the update-check repo. Code identifiers (pyrunner package, PYRUNNER_*
    env vars, pyrunner_* helpers, PyRunnerPlugin) deliberately keep their names.
    """

    def setUp(self):
        gs = GlobalSettings.get_settings()
        gs.instance_name = ""
        gs.setup_completed = True
        gs.save()
        Environment.objects.get_or_create(
            name="default", defaults={"is_default": True, "python_version": "3.12"}
        )
        # An admin must exist or the setup middleware redirects every page.
        self.admin = User.objects.create(
            email="admin@example.com", is_superuser=True, is_staff=True
        )
        WorkspaceMembership.ensure(
            self.admin, Workspace.get_default(), role=WorkspaceMembership.ROLE_OWNER
        )

    def test_console_title_falls_back_to_brand(self):
        self.client.force_login(self.admin)

        resp = self.client.get(reverse("cpanel:services"))

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "<title>Services - PyRun</title>", html=False)
        self.assertNotContains(resp, "PyRunner")

    def test_login_page_shows_brand(self):
        resp = self.client.get(reverse("auth:login"))

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "PyRun")
        self.assertNotContains(resp, "PyRunner")

    def test_password_reset_subject_uses_brand(self):
        from unittest.mock import patch

        from core.email import send_password_reset_email

        with patch("core.email._send_email", return_value=True) as send:
            send_password_reset_email("someone@example.com", "https://pyrun.example/auth/reset-password/abc123/")

        _to, subject, text, html = send.call_args.args
        self.assertEqual(subject, "Reset Your PyRun Password")
        self.assertIn("PyRun", text)
        self.assertNotIn("PyRunner", text)
        self.assertNotIn("PyRunner", html)

    def test_update_check_points_at_renamed_repo(self):
        from core.services.update_service import UpdateService

        self.assertIn("hassancs91/PyRun/", UpdateService.GITHUB_TAGS_URL)


class SmtpTimeoutTests(TestCase):
    """A wrong SMTP port must fail within a bound, not hang the sending thread."""

    def test_smtp_and_resend_backends_have_a_timeout(self):
        gs = GlobalSettings.get_settings()
        gs.smtp_host, gs.smtp_port = "smtp.example.com", 465
        for backend in (GlobalSettings.EmailBackend.SMTP, GlobalSettings.EmailBackend.RESEND):
            gs.email_backend = backend
            connection = NotificationService._get_email_backend(gs)
            self.assertIsNotNone(connection.timeout)
            self.assertLessEqual(connection.timeout, 30)
