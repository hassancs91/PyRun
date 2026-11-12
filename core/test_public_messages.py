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
from django.test import TestCase, override_settings
from django.urls import reverse

from core.executor import EnvironmentNotFoundError, _validate_environment
from core.models import Environment, GlobalSettings, Run, Script, User, Workspace, WorkspaceMembership
from core.services import EncryptionService
from core.services.setup_service import SetupService
from core.views.decorators import DATA_SERVER_MISSING_MESSAGE

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


# --------------------------------------------------------------------------- #
# Signed-in pages (F4): server commands, setting names and file paths are for
# superusers. A workspace member or admin is told to ask an administrator.
# --------------------------------------------------------------------------- #

WORKER = "core.services.system_info_service.SystemInfoService.get_worker_status"
MEMBER_LEAKS = ("manage.py", "ENCRYPTION_KEY", "PYRUNNER_", ".env.example", "Fernet")


class SignedInMessagesTests(TestCase):
    def setUp(self):
        _setup_done(admin=False)
        self.ws = Workspace.get_default()
        self.admin = User.objects.create_user(
            username="root@example.com", email="root@example.com", password="correct-horse-91",
            is_superuser=True, is_staff=True,
        )
        self.member = User.objects.create_user(
            username="member@example.com", email="member@example.com", password="correct-horse-91",
        )
        self.ws_admin = User.objects.create_user(
            username="wsadmin@example.com", email="wsadmin@example.com", password="correct-horse-91",
        )
        for user, role in ((self.member, WorkspaceMembership.ROLE_MEMBER),
                           (self.ws_admin, WorkspaceMembership.ROLE_ADMIN)):
            WorkspaceMembership.objects.update_or_create(
                user=user, workspace=self.ws, defaults={"role": role}
            )

    def get(self, user, name, **kwargs):
        self.client.force_login(user)
        return self.client.get(reverse(name, kwargs=kwargs or None), follow=True)

    def assertNoLeak(self, resp):
        body = resp.content.decode()
        for leak in MEMBER_LEAKS:
            self.assertNotIn(leak, body)

    def test_workers_stopped_outside_docker_shows_the_command_to_superusers_only(self):
        stopped = {"status": "stopped", "status_text": "Stopped", "queued_tasks": 0}
        with mock.patch(WORKER, return_value=stopped), \
                mock.patch("core.views.dashboard._in_container", return_value=False):
            member = self.get(self.member, "cpanel:dashboard")
            admin = self.get(self.admin, "cpanel:dashboard")
        self.assertContains(member, "Workers stopped")
        self.assertContains(member, "Ask an administrator to start the workers.")
        self.assertNoLeak(member)
        self.assertContains(admin, "python manage.py qcluster")

    def test_encryption_banners_and_messages_name_the_env_var_to_superusers_only(self):
        with mock.patch.object(EncryptionService, "is_configured", return_value=False):
            for user in (self.member, self.ws_admin):
                for name in ("cpanel:channel_list", "cpanel:connection_list", "cpanel:secret_list",
                             "cpanel:channel_create", "cpanel:connection_create", "cpanel:secret_create",
                             "cpanel:oauth_app_create"):
                    with self.subTest(user=user.email, page=name):
                        resp = self.get(user, name)
                        self.assertEqual(resp.status_code, 200)
                        self.assertNoLeak(resp)
                        self.assertContains(resp, "Ask an administrator to set up encryption")
            for name in ("cpanel:channel_list", "cpanel:secret_create"):
                with self.subTest(user="superuser", page=name):
                    self.assertContains(self.get(self.admin, name), "ENCRYPTION_KEY")

    @override_settings(PYRUNNER_DATA_DB_URL="")
    def test_no_data_server_names_the_setting_to_superusers_only(self):
        # Databases are for workspace Owners/Admins (a plain member gets 403).
        resp = self.get(self.ws_admin, "cpanel:database_list")
        self.assertNoLeak(resp)
        self.assertContains(resp, "Ask an administrator to attach one.")
        resp = self.get(self.ws_admin, "cpanel:database_create")
        self.assertNoLeak(resp)
        self.assertEqual(
            [str(m) for m in get_messages(resp.wsgi_request)], [DATA_SERVER_MISSING_MESSAGE]
        )
        self.assertContains(self.get(self.admin, "cpanel:database_list"), "PYRUNNER_DATA_DB_URL")

    def test_environment_path_is_shown_to_superusers_only(self):
        env = Environment.objects.create(name="pathenv", path="secret-dir-91", is_active=True)
        member = self.get(self.member, "cpanel:environment_detail", pk=env.pk)
        self.assertEqual(member.status_code, 200)
        self.assertNotContains(member, "secret-dir-91")
        self.assertContains(member, "Ask an administrator to recreate it.")
        self.assertContains(self.get(self.admin, "cpanel:environment_detail", pk=env.pk), "secret-dir-91")

    def test_a_missing_environment_names_it_not_its_folder(self):
        env = Environment.objects.create(name="goneenv", path="secret-dir-92", is_active=True)
        script = Script.objects.create(name="s-gone", code="pass", environment=env, workspace=self.ws)
        run = Run.objects.create(script=script, workspace=self.ws)
        with self.assertRaises(EnvironmentNotFoundError) as caught:
            _validate_environment(run)
        self.assertNotIn("secret-dir-92", str(caught.exception))
        self.assertIn('environment "goneenv"', str(caught.exception))

    def test_a_queue_failure_keeps_the_exception_out_of_the_run_and_message(self):
        script = Script.objects.create(
            name="s-queue", code="pass", environment=Environment.objects.get(is_default=True), workspace=self.ws,
        )
        self.client.force_login(self.member)
        with mock.patch("core.views.scripts.queue_script_run", side_effect=RuntimeError("db at /srv/q.sqlite3")), \
                self.assertLogs("core.views.scripts", level="ERROR"):
            resp = self.client.post(reverse("cpanel:script_run", args=[script.pk]))
        run = Run.objects.get(script=script)
        self.assertEqual(run.status, Run.Status.FAILED)
        self.assertNotIn("q.sqlite3", run.stderr)
        shown = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertEqual(shown, ["Failed to queue script. The server log has the details."])
