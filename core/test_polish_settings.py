"""
M4 polish batch (2.0 release test) — Settings tabs and Backup.

U9    saving a tab redirects back to that tab (hash), not General
U69   a refused save names the field by its label, not its attribute name
U10   Send test email falls back to the signed-in admin's address
U71   a failed test email names server, port and the 465 hint
U31   Workers shows the effective retry window when the cluster raised it
F15   Isolation no longer says the sandbox "arrives in a later release"
U61   Active protection doesn't claim the process cap while scripts run as root
F33   Retention copy describes the keep-if-either rule
U72   an invalid Create backup form says why nothing downloaded
F65   the Create backup card lists what a backup leaves out
U73   an ENCRYPTION_KEY mismatch says how to restore anyway
U74   a switched-off backup connection is named and Run now is disabled
U75   a non-backup upload gets a readable reason
F62   the API tab points at the Unified API, MCP and Connect
"""

import gzip
import json
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from core.forms import NotificationSettingsForm, WorkerSettingsForm
from core.models import Environment, GlobalSettings, User
from core.services.backup_service import BackupService
from core.services.worker_config import EFFECTIVE_RETRY_CACHE_KEY

PW = "correct-horse-battery-staple-91"
SETTINGS = "/cpanel/settings/"


def current_values(form_class):
    """POST data that re-saves the settings as they are (a valid submission)."""
    form = form_class(instance=GlobalSettings.get_settings())
    data = {}
    for name in form.fields:
        value = form[name].value()
        if value is True:
            data[name] = "on"
        elif value not in (None, False):
            data[name] = value
    return data


class SettingsBase(TestCase):
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
        self.client.force_login(self.admin)

    def messages_of(self, resp):
        return [str(m) for m in resp.wsgi_request._messages]


class TabRedirectTests(SettingsBase):
    def test_each_tab_returns_to_itself(self):
        cases = [
            ("cpanel:notification_settings", current_values(NotificationSettingsForm), "#notifications"),
            ("cpanel:retention_settings", {"retention_days": "5", "retention_count": "3"}, "#retention"),
            ("cpanel:worker_settings", current_values(WorkerSettingsForm), "#workers"),
            ("cpanel:recaptcha_settings", {}, "#security"),
            ("cpanel:toggle_global_pause", {}, "#schedules"),
        ]
        for name, data, tab in cases:
            with self.subTest(name=name):
                resp = self.client.post(reverse(name), data)
                self.assertEqual(resp.status_code, 302)
                self.assertEqual(resp["Location"], SETTINGS + tab)

    def test_refused_save_shows_the_error_under_the_field(self):
        """U69: the tab re-renders with the typed value and the error inline."""
        resp = self.client.post(reverse("cpanel:retention_settings"), {"retention_days": "-1", "retention_count": "7"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["active_tab"], "retention")
        html = resp.content.decode()
        # the field is marked and its error sits under it
        self.assertRegex(html, r'name="retention_days" value="-1"[^>]*aria-invalid="true"')
        self.assertIn('<div id="id_retention_days_error" class="mt-1.5 space-y-0.5"><p class="text-xs text-fail break-words">'
                      "Ensure this value is greater than or equal to 0.</p></div>", html)
        # the other typed value is kept, and the summary links the field by its label
        self.assertIn('name="retention_count" value="7"', html)
        self.assertIn('data-form-errors', html)
        self.assertIn('href="#id_retention_days"', html)
        self.assertIn(">Retention Days</a>", html)
        self.assertNotIn("retention_days:", html)
        self.assertIn("|| '#retention'", html)  # that tab opens
        self.assertEqual(self.messages_of(resp), [])
        gs = GlobalSettings.get_settings()
        self.assertNotEqual(gs.retention_count, 7)  # nothing saved

    def test_reserved_admin_slug_is_refused_inline(self):
        resp = self.client.post(reverse("cpanel:general_settings"), {
            "instance_name": "PyRun", "timezone": "UTC", "admin_url_slug": "api"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["active_tab"], "general")
        html = resp.content.decode()
        self.assertIn('id="id_admin_url_slug_error"', html)
        self.assertIn("reserved URL path", html)
        self.assertRegex(html, r'name="admin_url_slug" value="api"')

    def test_invalid_backup_form_says_why(self):
        resp = self.client.post(reverse("cpanel:backup_create"), {"max_runs": "10001"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["active_tab"], "backup")
        html = resp.content.decode()
        self.assertIn('id="id_max_runs_error"', html)
        self.assertIn("10000", html)

    def test_unknown_bound_form_is_a_programming_error(self):
        from django.test import RequestFactory

        from core.views.settings import render_settings

        request = RequestFactory().get(SETTINGS)
        request.user = self.admin
        with self.assertRaises(TypeError):
            render_settings(request, "general", nope_form=object())


class TestEmailTests(SettingsBase):
    def test_no_default_address_falls_back_to_the_admin(self):
        gs = GlobalSettings.get_settings()
        gs.email_backend = GlobalSettings.EmailBackend.SMTP
        gs.default_notification_email = ""
        gs.save()
        with mock.patch("core.views.settings.NotificationService.send_test_email") as send:
            data = self.client.post(reverse("cpanel:test_email")).json()
        send.assert_called_once_with("admin@example.com")
        self.assertTrue(data["success"])
        self.assertIn("admin@example.com", data["message"])

    def test_failure_names_host_port_and_the_465_hint(self):
        from core.views.settings import describe_email_failure

        gs = GlobalSettings.get_settings()
        gs.email_backend = GlobalSettings.EmailBackend.SMTP
        gs.smtp_host = "smtp.example.com"
        gs.smtp_port = 465
        msg = describe_email_failure(gs, TimeoutError("timed out"))
        self.assertIn("smtp.example.com:465", msg)
        self.assertIn("timed out", msg)
        self.assertIn("587", msg)


class SettingsPageCopyTests(SettingsBase):
    def page(self):
        return self.client.get(reverse("cpanel:settings")).content.decode()

    def test_copy_fixes(self):
        html = self.page()
        self.assertNotIn("later release", html)                       # F15
        self.assertNotIn("Runs matching either condition", html)      # F33
        self.assertIn("Not included:", html)                          # F65
        self.assertIn(reverse("cpanel:connect"), html)                # F62
        self.assertIn("/mcp", html)
        self.assertNotIn("a hosted email API", html)                  # U71

    def test_effective_retry_is_shown_when_raised(self):
        cache.set(EFFECTIVE_RETRY_CACHE_KEY, 7320, None)
        try:
            self.assertIn("Effective now: 7320 s", self.page())
        finally:
            cache.delete(EFFECTIVE_RETRY_CACHE_KEY)

    def test_active_protection_drops_the_process_cap_for_root(self):
        gs = GlobalSettings.get_settings()
        gs.sandbox_memory_mb = 256
        gs.save()
        with mock.patch("core.views.settings._scripts_run_as_root", return_value=True), \
                mock.patch.object(GlobalSettings, "sandbox_rlimits_configured", True, create=True):
            html = self.page()
        self.assertIn("the process cap is not enforced while scripts run as root", html)
        self.assertNotIn("memory / CPU / process / file-size caps are enforced", html)

    def test_switched_off_backup_connection_is_named_and_run_now_disabled(self):
        status = {
            "s3_configured": True, "s3_enabled": False, "connection_name": "w13-spaces-backups",
            "enabled": False, "last_run": None, "next_run": None,
        }
        with mock.patch(
            "core.services.backup_schedule_service.BackupScheduleService.get_schedule_status",
            return_value=status,
        ):
            html = self.page()
        self.assertIn("w13-spaces-backups", html)
        self.assertIn("switched off", html)
        self.assertNotIn("S3 not configured", html)
        btn = html.split('id="backup-run-now-btn"', 1)[1].split(">", 1)[0]
        self.assertIn("disabled", btn)


class BackupFileMessageTests(TestCase):
    def test_random_file_is_not_a_backup(self):
        for data, name in [(b"\xf7f\x00\x01garbage", "x.json.gz"), (b"\xff\xfe\x00bad", "x.json")]:
            with self.subTest(name=name):
                with self.assertRaises(ValueError) as cm:
                    BackupService.deserialize_backup(data, filename=name)
                self.assertIn("isn't a PyRun backup", str(cm.exception))
                self.assertNotIn("Not a gzipped file", str(cm.exception))

    def test_valid_gzip_still_parses(self):
        raw = gzip.compress(json.dumps({"metadata": {}}).encode())
        self.assertEqual(BackupService.deserialize_backup(raw, filename="b.json.gz"), {"metadata": {}})

    def test_key_mismatch_says_how_to_restore(self):
        with mock.patch.object(BackupService, "validate_encryption_key", return_value=False):
            data = {key: [] for key in (
                "environments", "scripts", "users", "script_schedules", "schedule_history",
                "secrets", "runs",
            )}
            data["backup_metadata"] = {"version": BackupService.BACKUP_VERSION}
            data["global_settings"] = {}
            result = BackupService.validate_backup(data)
        text = " ".join(result["errors"])
        self.assertIn("ENCRYPTION_KEY mismatch", text)
        self.assertIn("set this server's ENCRYPTION_KEY", text)


class RestoreToastTests(SettingsBase):
    def test_restore_reports_one_summary_and_one_warnings_toast(self):
        session = self.client.session
        session["pending_backup"] = {"backup_metadata": {}}
        session.save()
        result = {
            "success": True,
            "counts": {"scripts": 3, "environments": 1, "secrets": 2, "runs": 9},
            "warnings": ["w-one", "w-two", "w-three", "w-four"],
        }
        with mock.patch.object(BackupService, "create_backup", return_value={}), \
                mock.patch.object(BackupService, "serialize_backup", return_value=(b"x", "application/gzip")), \
                mock.patch("core.views.backup.open", mock.mock_open(), create=True), \
                mock.patch.object(BackupService, "restore_backup", return_value=result), \
                mock.patch("core.tasks.queue_environment_rebuild"):
            resp = self.client.post(reverse("cpanel:backup_restore"), {"confirm_delete": "on"})
        texts = self.messages_of(resp)
        self.assertEqual(len(texts), 2, texts)
        self.assertIn("Backup restored successfully", texts[0])
        self.assertIn("auto_backup_", texts[0])
        self.assertIn("4 items need attention", texts[1])
        self.assertIn("(4) w-four", texts[1])
        self.assertNotIn("tables", texts[0])

    def _restore(self, counts):
        session = self.client.session
        session["pending_backup"] = {"backup_metadata": {}}
        session.save()
        result = {"success": True, "counts": counts, "warnings": []}
        with mock.patch.object(BackupService, "create_backup", return_value={}),                 mock.patch.object(BackupService, "serialize_backup", return_value=(b"x", "application/gzip")),                 mock.patch("core.views.backup.open", mock.mock_open(), create=True),                 mock.patch.object(BackupService, "restore_backup", return_value=result),                 mock.patch("core.tasks.queue_environment_rebuild"):
            resp = self.client.post(reverse("cpanel:backup_restore"), {"confirm_delete": "on"})
        return self.messages_of(resp)

    def test_the_upload_preview_warns_about_what_the_restore_leaves_out(self):
        # 2.5 T52: the preview's warnings include the watchers the restore drops.
        from django.core.files.uploadedfile import SimpleUploadedFile

        left_out = ['Watcher "B": another watcher of the "shop" plugin has its key "k", so it was left out.']
        with mock.patch.object(BackupService, "deserialize_backup", return_value={"backup_metadata": {}}), \
                mock.patch.object(BackupService, "validate_backup",
                                  return_value={"valid": True, "errors": [], "warnings": ["from validation"]}), \
                mock.patch.object(BackupService, "get_backup_preview",
                                  return_value={"counts": {"watchers": 1}, "warnings": [], "left_out": left_out}):
            resp = self.client.post(
                reverse("cpanel:backup_upload"),
                {"backup_file": SimpleUploadedFile("b.json", b"{}", content_type="application/json")},
            )
        body = resp.json()
        self.assertTrue(body["success"], body)
        self.assertEqual(body["warnings"], ["from validation", *left_out])

    def test_restore_summary_counts_tables_and_rows(self):
        # 2.5 T52: a backup with Tables says how many tables and rows came back.
        texts = self._restore({"scripts": 3, "environments": 1, "secrets": 2, "runs": 9,
                               "tables": 2, "table_rows": 41})
        self.assertIn("Imported: 3 scripts, 1 environments, 2 secrets, 9 runs, 2 tables with 41 rows.", texts[0])
