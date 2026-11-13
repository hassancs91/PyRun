"""
Backup format 1.14.0 (2.5 T47).

What this pins, roughly in order of how much it would hurt to get wrong:

- "Include installed plugins" is off by default: a backup made without it
  has no ``plugins`` key, as before 1.14.0, and a restore leaves plugins alone.
- With it on, each plugin row carries its folder as a zip (signature files in,
  ``__pycache__`` out). A restore installs the plugins this server doesn't
  have through the zip installer, after the rows are committed: every install
  check applies (the restored trust policy included) and a restored plugin is
  INSTALLED, never active. An installed plugin is never changed or removed.
- A plugin the backup couldn't carry (dev mode, missing folder) or a damaged
  entry is named in the warnings and nothing is installed for it.
- The console's backup form and the scheduled S3 backup both pass the option.

The real 2.0.0 backups (both engines) are in core/test_backup_from_2_0.py.
"""

import base64
import io
import json
import shutil
import zipfile
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from core.forms import BackupCreateForm, S3BackupScheduleForm
from core.models import Environment, GlobalSettings, Plugin
from core.services import PluginService
from core.services.backup_service import BackupService
from core.test_plugin_signing import demo_files, signed_files, trusted, zip_of
from core.test_plugin_update import SLUG, _Dir, _forget


def _zip_names(row):
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(row["zip_b64"]))) as zf:
        return sorted(zf.namelist())


class PluginBackupTests(_Dir):
    def remove(self, slug=SLUG):
        """This server without the plugin: no row, no folder, no module."""
        Plugin.objects.filter(slug=slug).delete()
        shutil.rmtree(self.root / slug, ignore_errors=True)
        _forget(slug)

    def test_off_by_default_the_backup_has_no_plugins(self):
        self.install()
        data = BackupService.create_backup(created_by_user=self.admin)
        self.assertNotIn("plugins", data)
        self.assertIs(data["backup_metadata"]["include_plugins"], False)
        self.assertEqual(BackupService.get_backup_preview(data)["counts"]["plugins"], 0)
        self.remove()
        result = BackupService.restore_backup(data, current_user=self.admin)
        self.assertTrue(result["success"], result)
        self.assertNotIn("plugins", result["counts"])
        self.assertFalse(Plugin.objects.exists())

    def test_on_each_plugin_carries_its_files(self):
        plugin = self.install()
        cache = self.root / SLUG / "__pycache__"
        cache.mkdir()
        (cache / "apps.cpython-312.pyc").write_bytes(b"\x00compiled")
        data = BackupService.create_backup(created_by_user=self.admin, include_plugins=True)
        self.assertIs(data["backup_metadata"]["include_plugins"], True)
        [row] = data["plugins"]
        self.assertEqual(
            {k: row[k] for k in ("slug", "name", "version", "source", "status", "update_policy", "checksum")},
            {"slug": SLUG, "name": "Demo", "version": "1.0.0", "source": "upload", "status": "installed",
             "update_policy": "manual", "checksum": plugin.checksum},
        )
        self.assertEqual(
            _zip_names(row),
            sorted(f"{SLUG}/{n}" for n in ("CHANGELOG.md", "__init__.py", "apps.py", "marker.txt", "plugin.json")),
        )
        self.assertEqual(BackupService.get_backup_preview(data)["counts"]["plugins"], 1)
        # Survives the file format both ways.
        raw, _ = BackupService.serialize_backup(data, BackupService.FORMAT_GZIP)
        self.assertEqual(BackupService.deserialize_backup(raw, "b.json.gz")["plugins"], data["plugins"])

    def test_restore_installs_a_missing_plugin_turned_off(self):
        plugin = self.install(status=Plugin.Status.ACTIVE)
        Plugin.objects.filter(pk=plugin.pk).update(update_policy=Plugin.UpdatePolicy.AUTO)
        data = BackupService.create_backup(created_by_user=self.admin, include_plugins=True)
        self.remove()

        with mock.patch.object(PluginService, "_run_preflight") as preflight:
            result = BackupService.restore_backup(data, current_user=self.admin)
        preflight.assert_not_called()  # a restore never runs plugin code
        self.assertTrue(result["success"], result)
        self.assertEqual(result["counts"]["plugins"], 1)
        back = Plugin.objects.get(slug=SLUG)
        self.assertEqual(back.status, Plugin.Status.INSTALLED)
        self.assertEqual((back.version, back.source, back.update_policy), ("1.0.0", "upload", "auto"))
        self.assertEqual(back.checksum, plugin.checksum)
        self.assertEqual(self.live(), "version 1.0.0\n")
        self.assertEqual(
            result["warnings"][-1],
            "1 plugin is installed from the backup but not active: activate each one on the Plugins page.",
        )

    def test_an_installed_plugin_is_left_alone(self):
        self.install("1.0.0")
        data = BackupService.create_backup(created_by_user=self.admin, include_plugins=True)
        PluginService.upload(io.BytesIO(_demo_zip_bytes("1.1.0")), user=self.admin)
        result = BackupService.restore_backup(data, current_user=self.admin)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["counts"]["plugins"], 0)
        self.assertEqual(Plugin.objects.get(slug=SLUG).version, "1.1.0")
        self.assertEqual(self.live(), "version 1.1.0\n")
        self.assertIn(
            'Plugin "Demo": this server already has version 1.1.0, so the backup\'s version 1.0.0 was not '
            "installed.",
            result["warnings"],
        )

    def test_the_same_version_installed_gives_no_warning(self):
        self.install()
        data = BackupService.create_backup(created_by_user=self.admin, include_plugins=True)
        result = BackupService.restore_backup(data, current_user=self.admin)
        self.assertEqual((result["counts"]["plugins"], result["warnings"]), (0, []))

    def test_the_restored_trust_policy_applies(self):
        self.install()
        settings_obj = GlobalSettings.get_settings()
        settings_obj.plugin_trust_policy = GlobalSettings.PluginTrustPolicy.SIGNED_ONLY
        settings_obj.save()
        data = BackupService.create_backup(created_by_user=self.admin, include_plugins=True)
        settings_obj.plugin_trust_policy = GlobalSettings.PluginTrustPolicy.ALLOW_UNSIGNED
        settings_obj.save()
        self.remove()

        result = BackupService.restore_backup(data, current_user=self.admin)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["counts"]["plugins"], 0)
        self.assertFalse(Plugin.objects.exists())
        self.assertFalse((self.root / SLUG).exists())
        [warning] = result["warnings"]
        self.assertTrue(warning.startswith('Plugin "Demo" was not installed: '), warning)

    def test_a_signed_plugin_comes_back_verified(self):
        with trusted():
            PluginService.upload(zip_of(signed_files(demo_files())))
            data = BackupService.create_backup(created_by_user=self.admin, include_plugins=True)
            self.assertIn(f"{SLUG}/plugin.sig", _zip_names(data["plugins"][0]))
            self.remove()
            result = BackupService.restore_backup(data, current_user=self.admin)
            self.assertEqual(result["counts"]["plugins"], 1, result)
            back = Plugin.objects.get(slug=SLUG)
            self.assertEqual((back.trust, back.signed_by, back.pinned_publisher), ("verified", "pyrun", "pyrun"))

    def test_a_plugin_without_files_is_named(self):
        self.install()
        shutil.rmtree(self.root / SLUG)
        data = BackupService.create_backup(created_by_user=self.admin, include_plugins=True)
        [row] = data["plugins"]
        self.assertNotIn("zip_b64", row)
        self.assertEqual(row["files_skipped_reason"], "its folder is missing on the source server")
        self.remove()
        result = BackupService.restore_backup(data, current_user=self.admin)
        self.assertEqual(result["counts"]["plugins"], 0)
        self.assertEqual(
            result["warnings"],
            ['Plugin "Demo" was not installed: its folder is missing on the source server. Upload its zip on '
             "the Plugins page."],
        )

    def test_a_dev_mode_plugin_is_listed_without_files(self):
        self.install()
        with override_settings(DEV_PLUGIN=f"plugins.{SLUG}"):
            [row] = BackupService.create_backup(include_plugins=True)["plugins"]
        self.assertEqual(row["files_skipped_reason"], "it is loaded in dev mode from a local folder")

    def test_damaged_or_mismatched_entries_install_nothing(self):
        self.install()
        data = BackupService.create_backup(created_by_user=self.admin, include_plugins=True)
        good = data["plugins"][0]
        other = base64.b64encode(_demo_zip_bytes("1.0.0", slug="otherdemo")).decode()
        data["plugins"] = [
            {**good, "zip_b64": "not base64 !"},
            {**good, "zip_b64": other},
            {**good, "slug": "../evil"},
            "not a dict",
        ]
        self.remove()
        result = BackupService.restore_backup(data, current_user=self.admin)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["counts"]["plugins"], 0)
        self.assertFalse(Plugin.objects.exists())
        self.assertEqual(result["warnings"], [
            'Plugin "Demo" was not installed: its files in the backup are damaged.',
            'Plugin "Demo" was not installed: its files in the backup don\'t match its name.',
            "A plugin in the backup has no valid name and was skipped.",
            "A plugin in the backup has no valid name and was skipped.",
        ])

    def test_an_unreadable_section_is_a_warning(self):
        data = BackupService.create_backup(created_by_user=self.admin, include_plugins=True)
        data["plugins"] = {"slug": SLUG}
        result = BackupService.restore_backup(data, current_user=self.admin)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["warnings"], ["The backup's plugins section isn't readable; no plugin was installed."])

    def test_a_failed_row_restore_installs_no_plugin(self):
        self.install()
        data = BackupService.create_backup(created_by_user=self.admin, include_plugins=True)
        self.remove()
        with mock.patch.object(BackupService, "_import_scripts", side_effect=RuntimeError("boom")):
            result = BackupService.restore_backup(data, current_user=self.admin)
        self.assertFalse(result["success"])
        self.assertFalse(Plugin.objects.exists())
        self.assertFalse((self.root / SLUG).exists())

    def test_the_console_form_passes_the_option(self):
        self.assertIs(BackupCreateForm().fields["include_plugins"].initial, False)
        self.install()
        settings_obj = GlobalSettings.get_settings()
        settings_obj.setup_completed = True
        settings_obj.save()
        Environment.objects.get_or_create(name="default", defaults={"is_default": True, "python_version": "3.12"})
        self.client.force_login(self.admin)
        url = reverse("cpanel:backup_create")
        base = {"backup_format": "json", "max_runs": "10", "include_datastores": "on"}
        off = json.loads(self.client.post(url, base).content)
        self.assertNotIn("plugins", off)
        on = json.loads(self.client.post(url, {**base, "include_plugins": "on"}).content)
        self.assertEqual([row["slug"] for row in on["plugins"]], [SLUG])

    def test_the_not_included_line_points_to_the_option(self):
        # 2.5 test pass 9, F5: the line said "Not included: … installed plugin
        # files …" right above the opt-in that includes them.
        settings_obj = GlobalSettings.get_settings()
        settings_obj.setup_completed = True
        settings_obj.save()
        Environment.objects.get_or_create(name="default", defaults={"is_default": True, "python_version": "3.12"})
        self.client.force_login(self.admin)
        page = self.client.get(reverse("cpanel:settings")).content.decode()
        start = page.index('id="backup-not-included"')
        line = page[start:page.index("</p>", start)]
        self.assertIn("Not included:", line)
        self.assertNotIn("plugin files", line)
        self.assertIn("Installed plugins are included only when you tick", line)
        self.assertIn("Include installed plugins", line)
        self.assertIn(str(BackupCreateForm().fields["include_plugins"].label), page)


def _demo_zip_bytes(version, slug=SLUG):
    from core.test_plugin_update import demo_zip

    return demo_zip(version, slug=slug).getvalue()


class ScheduledBackupOptionTests(TestCase):
    def test_the_schedule_form_saves_the_option(self):
        settings_obj = GlobalSettings.get_settings()
        self.assertIs(settings_obj.s3_backup_include_plugins, False)
        self.assertIs(S3BackupScheduleForm(instance=settings_obj).fields["s3_backup_include_plugins"].initial, False)
        form = S3BackupScheduleForm({
            "s3_backup_enabled": "", "s3_backup_schedule": "daily", "s3_backup_time": "02:00",
            "s3_backup_day": "0", "s3_backup_prefix": "pyrunner-backups/", "s3_backup_retention_count": "7",
            "s3_backup_max_runs": "1000", "s3_backup_include_datastores": "on", "s3_backup_include_plugins": "on",
        }, instance=settings_obj)
        self.assertTrue(form.is_valid(), form.errors)
        form.save(settings_obj)
        self.assertIs(GlobalSettings.get_settings().s3_backup_include_plugins, True)

    def test_the_scheduled_backup_passes_the_option(self):
        from core.services.s3_service import S3Service
        from core.tasks import scheduled_backup_task

        settings_obj = GlobalSettings.get_settings()
        settings_obj.s3_backup_include_plugins = True
        settings_obj.save()
        connection = mock.Mock(enabled=True)
        with mock.patch.object(S3Service, "for_backup", return_value=connection), \
                mock.patch.object(S3Service, "is_configured", return_value=True), \
                mock.patch.object(S3Service, "upload_file", return_value={"success": False, "error": "x"}), \
                mock.patch.object(BackupService, "create_backup", wraps=BackupService.create_backup) as create:
            scheduled_backup_task()
        self.assertIs(create.call_args.kwargs["include_plugins"], True)
