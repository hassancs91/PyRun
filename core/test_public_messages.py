"""
Public pages don't reveal how the server is run (2.5 test pass 8, H.01).

The sign-in, password and invite pages and the setup page that is public until
the first admin exists are read by anyone. They must not name management
commands or show raw exception text: the lockout page used to tell every
visitor to run ``manage.py user unlock <email>``, and a failed first-admin
creation put the exception into the setup page. Operator commands stay on the
signed-in admin pages and in the docs.
"""

import time
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.messages import get_messages
from django.test import TestCase, override_settings
from django.urls import reverse

from core.executor import EnvironmentNotFoundError, _validate_environment
from core.models import Database, Environment, GlobalSettings, Run, Script, User, Workspace, WorkspaceMembership
from core.services import DatabaseProvisionError, DatabaseService, EncryptionService
from core.services.database_explorer import DatabaseUnavailable, QueryTimeout
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

    def test_two_factor_sign_in_pages_are_clean(self):
        # 2.5 T46: the code step, its wrong-code and too-many-attempts answers,
        # the forced set-up page and its "can't set up" answer.
        from core.services import EncryptionError, two_factor

        secret = two_factor.begin_enrollment(self.user)
        two_factor.confirm_enrollment(self.user, two_factor.totp(two_factor.secret_bytes(secret), time.time()))
        self._sign_in("someone@example.com", "correct-horse-91")
        url = reverse("auth:two_factor_verify")
        self.assertClean(self.client.get(url))
        for _ in range(two_factor.USER_ATTEMPTS + 1):
            resp = self.client.post(url, {"code": "not-a-code"})
            self.assertClean(resp)
        self.assertContains(resp, "Too many attempts. Wait a few minutes, then try again.")

        GlobalSettings.objects.update(require_2fa=True)
        other = User.objects.create_user(username="new@example.com", email="new@example.com", password="correct-horse-91")
        self.client.logout()
        self._sign_in(other.email, "correct-horse-91")
        url = reverse("auth:two_factor_enroll")
        self.assertClean(self.client.get(url))
        self.assertClean(self.client.post(url, {"code": "000000"}))
        two_factor.disable(other)
        with mock.patch.object(EncryptionService, "encrypt", side_effect=EncryptionError("ENCRYPTION_KEY is not set")):
            resp = self.client.get(url)
        self.assertClean(resp)
        self.assertNotContains(resp, "ENCRYPTION_KEY")
        self.assertContains(resp, "Two-factor sign-in can&#x27;t be set up right now. Contact your administrator.")

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


DB_URL = "postgresql://prov:pw@10.9.8.7:5432/pyrun_data"
RAW = 'connection to server at "10.9.8.7", port 5432 failed: FATAL: password authentication failed (psycopg 3.2)'


@override_settings(PYRUNNER_DATA_DB_URL=DB_URL)
class DatabaseMessagesTests(TestCase):
    """F7: a workspace Owner/Admin sees neutral text on the Databases pages, a superuser the detail."""

    setUp = SignedInMessagesTests.setUp
    get = SignedInMessagesTests.get

    def shown(self, resp):
        return [str(m) for m in get_messages(resp.wsgi_request)]

    def post(self, user, name, data=None, **kwargs):
        self.client.force_login(user)
        return self.client.post(reverse(name, kwargs=kwargs or None), data or {}, follow=True)

    def test_test_connection_hides_the_version_and_driver_text(self):
        ok = (True, "Connected to 'pyrun_data' (PostgreSQL 16.4); pg_stat_statements available.")
        with mock.patch.object(DatabaseService, "test_connection", return_value=ok):
            self.assertEqual(self.shown(self.post(self.ws_admin, "cpanel:database_server_test")),
                             ["Connected to the data server."])
            self.assertIn("PostgreSQL 16.4", self.shown(self.post(self.admin, "cpanel:database_server_test"))[0])
        with mock.patch.object(DatabaseService, "test_connection", return_value=(False, RAW)):
            with self.assertLogs("core.views.databases", level="WARNING") as logs:
                resp = self.post(self.ws_admin, "cpanel:database_server_test")
            self.assertEqual(self.shown(resp), ["Could not connect to the data server. Ask your administrator."])
            self.assertIn("password authentication failed", "".join(logs.output))
            self.assertEqual(self.shown(self.post(self.admin, "cpanel:database_server_test")), [RAW])

    def test_a_provisioning_failure_hides_the_server_text(self):
        def fail(name, workspace, **kwargs):
            Database.objects.create(
                name=name, workspace=workspace, schema_name="db_x", role_name="db_x",
                encrypted_password="x", status=Database.STATUS_ERROR, last_error=f"Provisioning failed: {RAW}",
            )
            raise DatabaseProvisionError(RAW)

        with mock.patch.object(DatabaseService, "create_database", side_effect=fail),                 self.assertLogs("core.views.databases", level="WARNING"):
            resp = self.post(self.ws_admin, "cpanel:database_create", {"name": "crm", "description": ""})
        body = resp.content.decode()
        self.assertNotIn("password authentication failed", body)
        self.assertIn("Could not set up the database on the data server.", self.shown(resp)[0])
        self.assertContains(resp, "The data server could not set up this database.")
        database = Database.objects.get(name="crm")
        self.assertContains(self.get(self.admin, "cpanel:database_detail", pk=database.pk), "password authentication failed")

        with mock.patch.object(DatabaseService, "provision", side_effect=DatabaseProvisionError(RAW)),                 self.assertLogs("core.views.databases", level="WARNING"):
            resp = self.post(self.ws_admin, "cpanel:database_retry", pk=database.pk)
        self.assertNotIn("password authentication failed", resp.content.decode())

    def test_a_read_failure_hides_the_server_text_but_a_timeout_says_so(self):
        database = Database.objects.create(
            name="crm", workspace=self.ws, schema_name="db_x", role_name="db_x",
            encrypted_password="x", status=Database.STATUS_READY,
        )
        tables = "core.services.DatabaseExplorerService.tables"
        with mock.patch(tables, side_effect=DatabaseUnavailable(RAW)),                 self.assertLogs("core.views.databases", level="WARNING"):
            resp = self.get(self.ws_admin, "cpanel:database_detail", pk=database.pk)
        self.assertNotIn("password authentication failed", resp.content.decode())
        self.assertContains(resp, "Could not read the database. Ask your administrator.")
        with mock.patch(tables, side_effect=DatabaseUnavailable(RAW)):
            self.assertContains(self.get(self.admin, "cpanel:database_detail", pk=database.pk), "password authentication failed")
        with mock.patch(tables, side_effect=QueryTimeout("Reading the database took longer than 15 seconds.")):
            self.assertContains(self.get(self.ws_admin, "cpanel:database_detail", pk=database.pk),
                                "took longer than 15 seconds")
