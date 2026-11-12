"""
In-place plugin update with rollback (2.5 T42).

What this pins, roughly in order of how much it would hurt to get wrong:

- An update never touches what the plugin owns: brand_tracker 1.1.2 -> 1.1.3
  keeps its settings, secret values and weekly schedule, and its script gets
  the new code once the update steps run (after the restart).
- Only a higher semver version is accepted (pre-releases sort first); the same
  or a lower one, a doctor failure on the STAGED copy, a min_pyrunner or SDK
  api gate, a dev-mode plugin and an active plugin whose new code fails the
  isolated preflight are refused, recorded, and leave the live folder as it was.
- The swap keeps the old folder in plugins/.previous/<slug>-<version>/ (only the
  latest), the row keeps previous_version/checksum, the restart is pending.
- Rollback checks the kept folder's checksum, puts folder + row back, cancels
  the removed version's waiting steps and asks for a restart.
- Update steps run once per restart, from the oldest waiting version, in an
  isolated child, claimed with a conditional UPDATE; failures are recorded with
  the plugin's secret values masked; inactive plugins wait.
- Doctor: update rule (version increase, CHANGELOG.md) and lifecycle.py rule.
- The console: upload updates, history card, Rollback button.
- A backup + restore leaves plugin rows and their history alone.
"""

import io
import json
import shutil
import sys
import tempfile
import time
import zipfile
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

import plugins as plugins_package
from core.models import Environment, Plugin, PluginUpdate, Secret, User
from core.services import PluginInstallError, PluginService, plugin_updates
from core.services.backup_service import BackupService
from core.services.plugin_doctor import run_doctor, semver_key

SLUG = "t42demo"
EXAMPLES = Path(settings.BASE_DIR) / "examples"

APPS_PY = '''from core.plugins import PluginAppConfig, PyRunnerPlugin


class DemoConfig(PluginAppConfig):
    name = "plugins.{slug}"
    label = "{slug}"
    plugin = PyRunnerPlugin(slug="{slug}", name="Demo", version="1.0.0")
'''

LIFECYCLE_PY = '''import plugins.{slug} as package


def reprovision():
    package.CALLS.append("reprovision")


def on_update(from_version):
    package.CALLS.append(("on_update", from_version))
'''


def _zip(files: dict) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    buf.seek(0)
    return buf


def demo_zip(version="1.0.0", slug=SLUG, extra=None, changelog=True, **manifest_extra):
    manifest = {"slug": slug, "name": "Demo", "version": version, **manifest_extra}
    files = {
        f"{slug}/plugin.json": json.dumps(manifest),
        f"{slug}/__init__.py": "CALLS = []\n",
        f"{slug}/apps.py": APPS_PY.format(slug=slug),
        f"{slug}/marker.txt": f"version {version}\n",
    }
    if changelog:
        files[f"{slug}/CHANGELOG.md"] = f"## [{version}]\n"
    for name, content in (extra or {}).items():
        files[f"{slug}/{name}"] = content
    return _zip(files)


def folder_zip(folder: Path, *, version=None, drop=(), replace=None) -> io.BytesIO:
    """A zip of a plugin folder (no caches/tests), with plugin.json's version set."""
    slug = folder.name
    files = {}
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder).as_posix()
        if not path.is_file() or "__pycache__" in rel or rel in drop:
            continue
        files[f"{slug}/{rel}"] = path.read_bytes()
    if version is not None:
        manifest = json.loads(files[f"{slug}/plugin.json"])
        manifest["version"] = version
        files[f"{slug}/plugin.json"] = json.dumps(manifest)
    for rel, content in (replace or {}).items():
        files[f"{slug}/{rel}"] = content
    return _zip(files)


def _forget(slug):
    for name in [n for n in sys.modules if n == f"plugins.{slug}" or n.startswith(f"plugins.{slug}.")]:
        del sys.modules[name]


def _no_preflight(ok=True, output=""):
    return mock.patch.object(PluginService, "_run_preflight", return_value=(ok, output))


class _Dir(TestCase):
    """A temporary PLUGINS_DIR that is also on the ``plugins`` package path."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="t42-plugins-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        override = override_settings(PLUGINS_DIR=self.root)
        override.enable()
        self.addCleanup(override.disable)
        plugins_package.__path__.insert(0, str(self.root))
        self.addCleanup(plugins_package.__path__.remove, str(self.root))
        self.addCleanup(_forget, SLUG)
        self.admin = User.objects.create(email="admin@example.com", is_superuser=True, is_staff=True)

    def install(self, version="1.0.0", status=Plugin.Status.INSTALLED, **kw):
        plugin, update, _ = PluginService.upload(demo_zip(version, **kw))
        self.assertIsNone(update)
        if status != plugin.status:
            Plugin.objects.filter(pk=plugin.pk).update(status=status)
            plugin.refresh_from_db()
        return plugin

    def live(self, name="marker.txt"):
        return (self.root / SLUG / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Versions and gates
# --------------------------------------------------------------------------- #


class VersionTests(SimpleTestCase):
    def test_semver_order(self):
        ordered = [
            "1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2",
            "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0", "1.0.1", "1.9.0", "1.10.0", "2.0.0",
        ]
        keys = [semver_key(v) for v in ordered]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(set(keys)), len(keys))
        self.assertEqual(semver_key("1.0.0+build.7"), semver_key("1.0.0"))
        for bad in ("1.0", "v1.0.0", "", None, "1.0.0.0", "01.x.0"):
            self.assertIsNone(semver_key(bad), bad)

    def test_version_problem(self):
        self.assertIsNone(plugin_updates.version_problem("1.1.2", "1.1.3", "demo"))
        self.assertIsNone(plugin_updates.version_problem("1.2.0-rc.1", "1.2.0", "demo"))
        self.assertIn("the same version as", plugin_updates.version_problem("1.1.2", "1.1.2", "demo"))
        self.assertIn("older than", plugin_updates.version_problem("1.1.2", "1.1.1", "demo"))
        self.assertIn("older than", plugin_updates.version_problem("1.2.0", "1.2.0-rc.1", "demo"))
        self.assertIn("not semver", plugin_updates.version_problem("1.0.0", "1.1", "demo"))
        self.assertIn("installed version '1.0' is not semver",
                      plugin_updates.version_problem("1.0", "1.1.0", "demo"))

    def test_api_gate(self):
        with mock.patch("core.plugins.api.API_VERSION", "2.7"):
            for ok in ("2.7", "2.3", "2", "", None, "x.y"):
                self.assertIsNone(plugin_updates.api_version_problem({"api": ok}, "demo"), ok)
            self.assertIn("needs plugin SDK 2.8", plugin_updates.api_version_problem({"api": "2.8"}, "demo"))
            self.assertIn("3.0", plugin_updates.api_version_problem({"api": "3.0"}, "demo"))


# --------------------------------------------------------------------------- #
# Update
# --------------------------------------------------------------------------- #


class UpdateTests(_Dir):
    def test_higher_version_updates_in_place(self):
        plugin = self.install("1.0.0")
        old_checksum = plugin.checksum

        plugin, row, warnings = PluginService.upload(demo_zip("1.1.0"), user=self.admin)

        self.assertEqual(plugin.version, "1.1.0")
        self.assertEqual(plugin.manifest["version"], "1.1.0")
        self.assertEqual(plugin.previous_version, "1.0.0")
        self.assertEqual(plugin.previous_checksum, old_checksum)
        self.assertEqual(plugin.checksum, PluginService._checksum_dir(self.root / SLUG))
        self.assertNotEqual(plugin.checksum, old_checksum)
        self.assertEqual(plugin.status, Plugin.Status.INSTALLED)
        self.assertEqual(self.live(), "version 1.1.0\n")
        kept = self.root / ".previous" / f"{SLUG}-1.0.0"
        self.assertEqual((kept / "marker.txt").read_text(encoding="utf-8"), "version 1.0.0\n")
        self.assertEqual(
            (row.from_version, row.to_version, row.source, row.initiated_by, row.user, row.outcome),
            ("1.0.0", "1.1.0", "upload", "user", self.admin, PluginUpdate.Outcome.PENDING),
        )
        self.assertEqual((row.from_checksum, row.to_checksum), (old_checksum, plugin.checksum))
        self.assertIsInstance(warnings, str)
        # Nothing staged is left behind.
        self.assertEqual(list((self.root / ".staging").iterdir()), [])

    def test_a_second_update_keeps_only_the_latest_previous_folder(self):
        self.install("1.0.0")
        PluginService.upload(demo_zip("1.1.0"))
        plugin, _, _ = PluginService.upload(demo_zip("1.2.0"))
        names = sorted(p.name for p in (self.root / ".previous").iterdir())
        self.assertEqual(names, [f"{SLUG}-1.1.0"])
        self.assertEqual(plugin.previous_version, "1.1.0")

    def test_same_or_lower_version_is_refused_and_recorded(self):
        plugin = self.install("1.1.0")
        before = plugin.checksum
        for version, words in (("1.1.0", "the same version as"), ("1.0.9", "older than")):
            with self.subTest(version=version):
                with self.assertRaises(PluginInstallError) as caught:
                    PluginService.upload(demo_zip(version), user=self.admin)
                self.assertIn(words, str(caught.exception))
                self.assertIn("Upload a higher version", str(caught.exception))
        plugin.refresh_from_db()
        self.assertEqual((plugin.version, plugin.checksum, plugin.previous_version), ("1.1.0", before, ""))
        self.assertEqual(self.live(), "version 1.1.0\n")
        refused = list(plugin.updates.order_by("created_at"))
        self.assertEqual([r.outcome for r in refused], ["refused", "refused"])
        self.assertEqual([r.to_version for r in refused], ["1.1.0", "1.0.9"])
        self.assertIn("older than", refused[1].error)
        self.assertIsNotNone(refused[1].finished_at)
        self.assertFalse((self.root / ".previous").exists())

    def test_doctor_runs_on_the_staged_copy(self):
        self.install("1.0.0")
        with self.assertRaises(PluginInstallError) as caught:
            PluginService.upload(demo_zip("1.1.0", extra={"models.py": "x = 1\n"}))
        self.assertIn("plugin doctor refused", str(caught.exception))
        self.assertIn("models.py", str(caught.exception))
        self.assertEqual(self.live(), "version 1.0.0\n")
        self.assertFalse((self.root / SLUG / "models.py").exists())
        self.assertEqual(list((self.root / ".staging").iterdir()), [])

    def test_min_pyrunner_and_api_gates(self):
        self.install("1.0.0")
        with self.assertRaises(PluginInstallError) as caught:
            PluginService.upload(demo_zip("1.1.0", min_pyrunner="99.0.0"))
        self.assertIn("99.0.0", str(caught.exception))
        with mock.patch("core.plugins.api.API_VERSION", "2.7"):
            with self.assertRaises(PluginInstallError) as caught:
                PluginService.upload(demo_zip("1.1.0", api="2.9"))
        self.assertIn("plugin SDK 2.9", str(caught.exception))
        self.assertEqual(self.live(), "version 1.0.0\n")

    def test_dev_mode_plugin_is_never_updated(self):
        plugin = self.install("1.0.0")
        with override_settings(DEV_PLUGIN=f"plugins.{SLUG}"):
            with self.assertRaises(PluginInstallError) as caught:
                PluginService.update(plugin, demo_zip("1.1.0"))
        self.assertIn("dev mode", str(caught.exception))
        self.assertEqual(self.live(), "version 1.0.0\n")

    def test_zip_of_another_plugin_is_refused(self):
        plugin = self.install("1.0.0")
        with self.assertRaises(PluginInstallError) as caught:
            PluginService.update(plugin, demo_zip("1.1.0", slug="t42other"))
        self.assertIn("not 't42demo'", str(caught.exception))

    def test_active_plugin_new_code_must_pass_preflight(self):
        plugin = self.install("1.0.0", status=Plugin.Status.ACTIVE)
        with _no_preflight(False, "ImportError: boom") as preflight:
            with self.assertRaises(PluginInstallError) as caught:
                PluginService.update(plugin, demo_zip("1.1.0"))
        preflight.assert_called_once_with(SLUG)
        self.assertIn("put back", str(caught.exception))
        self.assertIn("ImportError: boom", str(caught.exception))
        plugin.refresh_from_db()
        self.assertEqual((plugin.version, plugin.previous_version), ("1.0.0", ""))
        self.assertEqual(self.live(), "version 1.0.0\n")
        self.assertEqual([p.name for p in (self.root / ".previous").iterdir()], [])
        self.assertEqual(plugin.updates.get().outcome, PluginUpdate.Outcome.REFUSED)

    def test_refusal_output_is_masked(self):
        plugin = self.install("1.0.0", status=Plugin.Status.ACTIVE)
        secret = Secret(key="T42_KEY", owner_plugin=SLUG)
        secret.set_value("key-leaks-in-traceback")
        secret.save()
        with _no_preflight(False, "RuntimeError: key-leaks-in-traceback"):
            with self.assertRaises(PluginInstallError):
                PluginService.update(plugin, demo_zip("1.1.0"))
        error = plugin.updates.get().error
        self.assertIn("[T42_KEY:MASKED]", error)
        self.assertNotIn("key-leaks-in-traceback", error)

    def test_inactive_plugin_skips_the_preflight(self):
        plugin = self.install("1.0.0")
        with _no_preflight() as preflight:
            PluginService.update(plugin, demo_zip("1.1.0"))
        preflight.assert_not_called()

    def test_update_of_an_active_plugin_needs_a_restart(self):
        plugin = self.install("1.0.0", status=Plugin.Status.ACTIVE)
        loaded = [f"plugins.{SLUG}"]
        with override_settings(INSTALLED_PLUGINS=loaded, PLUGINS_LOADED_AT=time.time() - 60):
            self.assertFalse(PluginService.pending_restart())
            with _no_preflight():
                PluginService.update(plugin, demo_zip("1.1.0"))
            self.assertTrue(PluginService.pending_restart())
        # A process that loaded its plugins after the update has the new code.
        with override_settings(INSTALLED_PLUGINS=loaded, PLUGINS_LOADED_AT=time.time() + 60):
            self.assertFalse(PluginService.pending_restart())

    def test_concurrent_update_is_refused(self):
        plugin = self.install("1.0.0")
        lock = self.root / ".staging" / f"{SLUG}.lock"
        lock.mkdir(parents=True)
        with self.assertRaises(PluginInstallError) as caught:
            PluginService.update(plugin, demo_zip("1.1.0"))
        self.assertIn("in progress", str(caught.exception))
        # A lock left by a dead request stops blocking.
        old = time.time() - plugin_updates.LOCK_STALE_AFTER - 5
        import os

        os.utime(lock, (old, old))
        PluginService.update(plugin, demo_zip("1.1.0"))
        self.assertFalse(lock.exists())

    def test_update_can_run_without_a_request(self):
        plugin = self.install("1.0.0")
        row, _ = PluginService.update(
            plugin, demo_zip("1.1.0"), initiated_by=PluginUpdate.InitiatedBy.AUTO,
        )
        self.assertEqual((row.initiated_by, row.user), ("auto", None))

    def test_delete_removes_the_kept_folder(self):
        self.install("1.0.0")
        plugin, _, _ = PluginService.upload(demo_zip("1.1.0"))
        PluginService.delete(plugin)
        self.assertEqual(list((self.root / ".previous").iterdir()), [])
        self.assertFalse(PluginUpdate.objects.exists())


# --------------------------------------------------------------------------- #
# Rollback
# --------------------------------------------------------------------------- #


class RollbackTests(_Dir):
    def test_rollback_restores_folder_and_row(self):
        plugin = self.install("1.0.0")
        first_checksum = plugin.checksum
        plugin, update, _ = PluginService.upload(demo_zip("1.1.0"))

        row = PluginService.rollback(plugin, user=self.admin)

        plugin.refresh_from_db()
        self.assertEqual((plugin.version, plugin.checksum), ("1.0.0", first_checksum))
        self.assertEqual(plugin.manifest["version"], "1.0.0")
        self.assertEqual((plugin.previous_version, plugin.previous_checksum), ("", ""))
        self.assertEqual(self.live(), "version 1.0.0\n")
        self.assertEqual(list((self.root / ".previous").iterdir()), [])
        self.assertEqual(
            (row.source, row.from_version, row.to_version, row.outcome, row.user),
            ("rollback", "1.1.0", "1.0.0", PluginUpdate.Outcome.PENDING, self.admin),
        )
        update.refresh_from_db()
        self.assertEqual(update.outcome, PluginUpdate.Outcome.CANCELLED)
        # Nothing left to roll back to.
        self.assertEqual(plugin_updates.rollback_problem(plugin), "There is no previous version to roll back to.")
        # And it can be updated again.
        PluginService.upload(demo_zip("1.1.0"))
        plugin.refresh_from_db()
        self.assertEqual(plugin.version, "1.1.0")

    def test_tampered_kept_folder_is_not_restored(self):
        self.install("1.0.0")
        plugin, _, _ = PluginService.upload(demo_zip("1.1.0"))
        (self.root / ".previous" / f"{SLUG}-1.0.0" / "marker.txt").write_text("evil\n", encoding="utf-8")
        with self.assertRaises(PluginInstallError) as caught:
            PluginService.rollback(plugin)
        self.assertIn("does not match the checksum", str(caught.exception))
        plugin.refresh_from_db()
        self.assertEqual(plugin.version, "1.1.0")
        self.assertEqual(self.live(), "version 1.1.0\n")

    def test_missing_kept_folder(self):
        self.install("1.0.0")
        plugin, _, _ = PluginService.upload(demo_zip("1.1.0"))
        shutil.rmtree(self.root / ".previous" / f"{SLUG}-1.0.0")
        self.assertIn("is gone", plugin_updates.rollback_problem(plugin))
        with self.assertRaises(PluginInstallError):
            PluginService.rollback(plugin)

    def test_active_rollback_needs_a_restart(self):
        plugin = self.install("1.0.0", status=Plugin.Status.ACTIVE)
        with _no_preflight():
            PluginService.update(plugin, demo_zip("1.1.0"))
        # A process that loaded its plugins after the update...
        with override_settings(INSTALLED_PLUGINS=[f"plugins.{SLUG}"], PLUGINS_LOADED_AT=time.time()):
            self.assertFalse(PluginService.pending_restart())
            plugin.refresh_from_db()
            PluginService.rollback(plugin)
            # ...has the wrong code again once it is rolled back.
            self.assertTrue(PluginService.pending_restart())


# --------------------------------------------------------------------------- #
# Update steps after the restart
# --------------------------------------------------------------------------- #


class StepsTests(_Dir):
    def _calls(self):
        return sys.modules[f"plugins.{SLUG}"].CALLS

    def test_run_steps_calls_reprovision_then_on_update(self):
        self.install("1.0.0", extra={"lifecycle.py": LIFECYCLE_PY.format(slug=SLUG)})
        lines = []
        plugin_updates.run_steps(SLUG, "0.9.0", write=lines.append)
        self.assertEqual(self._calls(), ["reprovision", ("on_update", "0.9.0")])
        self.assertEqual(lines, ["reprovision(): done", "on_update('0.9.0'): done"])

    def test_reprovision_only(self):
        self.install("1.0.0", extra={"lifecycle.py": LIFECYCLE_PY.format(slug=SLUG)})
        plugin_updates.run_steps(SLUG, "", reprovision_only=True, write=lambda _: None)
        self.assertEqual(self._calls(), ["reprovision"])

    def test_no_lifecycle_module_means_no_steps(self):
        self.install("1.0.0")
        lines = []
        plugin_updates.run_steps(SLUG, "0.9.0", write=lines.append)
        self.assertIn("no lifecycle.py", lines[0])

    def test_a_broken_import_inside_lifecycle_propagates(self):
        self.install("1.0.0", extra={"lifecycle.py": "import t42_missing_module\n"})
        with self.assertRaises(ModuleNotFoundError):
            plugin_updates.run_steps(SLUG, "0.9.0", write=lambda _: None)

    def _two_updates(self, status=Plugin.Status.ACTIVE):
        plugin = self.install("1.0.0", status=status)
        with _no_preflight():
            PluginService.update(plugin, demo_zip("1.1.0"))
            plugin.refresh_from_db()
            PluginService.update(plugin, demo_zip("1.2.0"))
        return plugin

    def test_pending_steps_run_once_from_the_oldest_version(self):
        plugin = self._two_updates()
        with mock.patch.object(plugin_updates, "_spawn_steps", return_value=(True, "ok")) as spawn:
            results = plugin_updates.run_pending_steps(write=lambda _: None)
        spawn.assert_called_once_with(SLUG, "1.0.0", False)
        self.assertEqual(results, {SLUG: PluginUpdate.Outcome.OK})
        outcomes = set(plugin.updates.values_list("outcome", flat=True))
        self.assertEqual(outcomes, {PluginUpdate.Outcome.OK})
        self.assertFalse(plugin.updates.filter(finished_at__isnull=True).exists())
        # Done once: nothing waits any more.
        with mock.patch.object(plugin_updates, "_spawn_steps") as spawn:
            self.assertEqual(plugin_updates.run_pending_steps(write=lambda _: None), {})
        spawn.assert_not_called()

    def test_inactive_plugin_steps_wait(self):
        plugin = self._two_updates(status=Plugin.Status.DISABLED)
        with mock.patch.object(plugin_updates, "_spawn_steps") as spawn:
            self.assertEqual(plugin_updates.run_pending_steps(write=lambda _: None), {})
        spawn.assert_not_called()
        self.assertEqual(plugin.updates.filter(outcome="pending").count(), 2)

    def test_after_a_rollback_only_reprovision_runs(self):
        plugin = self.install("1.0.0", status=Plugin.Status.ACTIVE)
        with _no_preflight():
            PluginService.update(plugin, demo_zip("1.1.0"))
        plugin.refresh_from_db()
        PluginService.rollback(plugin)
        with mock.patch.object(plugin_updates, "_spawn_steps", return_value=(True, "")) as spawn:
            plugin_updates.run_pending_steps(write=lambda _: None)
        spawn.assert_called_once_with(SLUG, "", True)
        self.assertEqual(
            sorted(plugin.updates.values_list("outcome", flat=True)),
            [PluginUpdate.Outcome.CANCELLED, PluginUpdate.Outcome.OK],
        )

    def test_failure_is_recorded_with_secrets_masked(self):
        plugin = self._two_updates()
        secret = Secret(key="T42_TOKEN", owner_plugin=SLUG)
        secret.set_value("tok-very-secret-42")
        secret.save()
        output = "Traceback ...\nValueError: bad token tok-very-secret-42"
        with mock.patch.object(plugin_updates, "_spawn_steps", return_value=(False, output)):
            results = plugin_updates.run_pending_steps(write=lambda _: None)
        self.assertEqual(results, {SLUG: PluginUpdate.Outcome.STEPS_FAILED})
        row = plugin.updates.first()
        self.assertEqual(row.outcome, PluginUpdate.Outcome.STEPS_FAILED)
        self.assertIn("[T42_TOKEN:MASKED]", row.error)
        self.assertNotIn("tok-very-secret-42", row.error)

    def test_a_claimed_row_is_not_run_twice(self):
        plugin = self._two_updates()
        plugin.updates.update(outcome=PluginUpdate.Outcome.RUNNING)
        with mock.patch.object(plugin_updates, "_spawn_steps") as spawn:
            plugin_updates.run_pending_steps(write=lambda _: None)
        spawn.assert_not_called()
        self.assertEqual(plugin.updates.filter(outcome="running").count(), 2)

    def test_rows_left_running_by_a_dead_process_fail(self):
        plugin = self._two_updates()
        old = timezone.now() - timedelta(seconds=plugin_updates.STEPS_TIMEOUT * 3)
        plugin.updates.update(outcome=PluginUpdate.Outcome.RUNNING, created_at=old)
        plugin_updates.run_pending_steps(write=lambda _: None)
        self.assertEqual(set(plugin.updates.values_list("outcome", flat=True)), {"steps_failed"})
        self.assertIn("Interrupted", plugin.updates.first().error)

    def test_spawn_runs_an_isolated_child(self):
        done = mock.Mock(returncode=0, stdout="reprovision(): done\n", stderr="")
        with mock.patch("core.services.plugin_updates.subprocess.run", return_value=done) as run:
            ok, output = plugin_updates._spawn_steps(SLUG, "1.0.0", False)
        self.assertTrue(ok)
        args, kwargs = run.call_args
        self.assertEqual(args[0][-4:], ["plugin_post_update", SLUG, "--from-version", "1.0.0"])
        self.assertEqual(kwargs["env"]["PYRUNNER_PREFLIGHT_SLUG"], SLUG)
        self.assertNotIn("PYRUNNER_DISABLE_PLUGINS", kwargs["env"])
        with mock.patch("core.services.plugin_updates.subprocess.run", return_value=done) as run:
            plugin_updates._spawn_steps(SLUG, "", True)
        self.assertEqual(run.call_args[0][0][-2:], [SLUG, "--reprovision-only"])

    def test_command_child_mode_refuses_outside_the_isolated_process(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("plugin_post_update", SLUG, from_version="1.0.0", stdout=io.StringIO())

    def test_command_orchestrator_runs_pending_steps(self):
        self._two_updates()
        out = io.StringIO()
        with mock.patch.object(plugin_updates, "_spawn_steps", return_value=(True, "")):
            call_command("plugin_post_update", stdout=out)
        self.assertIn(SLUG, out.getvalue())


# --------------------------------------------------------------------------- #
# Doctor
# --------------------------------------------------------------------------- #


class DoctorTests(_Dir):
    def _folder(self, version="1.1.0", **kw):
        PluginService.upload(demo_zip(version, **kw))
        return self.root / SLUG

    def _findings(self, report, rule):
        return [(f.severity, f.message) for f in report.findings if f.rule == rule]

    def test_update_rule_only_for_an_update(self):
        folder = self._folder("1.1.0", changelog=False)
        self.assertEqual(self._findings(run_doctor(folder), "update"), [])
        report = run_doctor(folder, installed_version="1.1.0")
        found = self._findings(report, "update")
        self.assertEqual([s for s, _ in found], ["warn", "warn"])
        self.assertIn("not higher than the installed '1.1.0'", found[0][1])
        self.assertIn("No CHANGELOG.md", found[1][1])
        self.assertTrue(report.ok)

    def test_update_rule_quiet_when_all_is_well(self):
        folder = self._folder("1.1.0")
        self.assertEqual(self._findings(run_doctor(folder, installed_version="1.0.0"), "update"), [])

    def test_lifecycle_rule(self):
        cases = {
            "def reprovision():\n    pass\n\ndef on_update(from_version):\n    pass\n": ("pass", ""),
            "def on_update(from_version, extra=None):\n    pass\n": ("pass", ""),
            "def on_update(*args):\n    pass\n": ("pass", ""),
            "def reprovision(data):\n    pass\n": ("fail", "reprovision must be callable with no arguments"),
            "def on_update():\n    pass\n": ("fail", "on_update must be callable with one argument"),
            "def on_update(a, b):\n    pass\n": ("fail", "on_update must be callable with one argument"),
            "def on_update(a, *, b):\n    pass\n": ("fail", "on_update must be callable with one argument"),
            "async def reprovision():\n    pass\n": ("fail", "not async"),
            "X = 1\n": ("warn", "neither"),
            "def (:\n": ("fail", "does not parse"),
        }
        folder = self._folder()
        for source, (severity, words) in cases.items():
            with self.subTest(source=source):
                (folder / "lifecycle.py").write_text(source, encoding="utf-8")
                found = self._findings(run_doctor(folder), "lifecycle")
                self.assertEqual(len(found), 1, found)
                self.assertEqual(found[0][0], severity)
                self.assertIn(words, found[0][1])

    def test_doctor_command_update_from(self):
        folder = self._folder("1.1.0", changelog=False)
        out = io.StringIO()
        call_command("plugin_doctor", path=str(folder), update_from="1.1.0", stdout=out)
        self.assertIn("No CHANGELOG.md", out.getvalue())

    def test_brand_tracker_example_passes_with_its_lifecycle(self):
        report = run_doctor(EXAMPLES / "brand_tracker", installed_version="1.1.2")
        self.assertTrue(report.ok, report.format())
        self.assertEqual(self._findings(report, "update"), [])
        self.assertEqual(self._findings(report, "lifecycle")[0][0], "pass")


# --------------------------------------------------------------------------- #
# Console
# --------------------------------------------------------------------------- #


class ConsoleTests(_Dir):
    def setUp(self):
        super().setUp()
        for target in (
            "core.services.setup_service.SetupService.is_setup_needed",
            "core.services.setup_service.SetupService.needs_admin_setup",
        ):
            patch = mock.patch(target, return_value=False)
            patch.start()
            self.addCleanup(patch.stop)
        self.client.force_login(self.admin)

    def _upload(self, version):
        buf = demo_zip(version)
        buf.name = f"{SLUG}.zip"
        return self.client.post(reverse("cpanel:plugin_upload"), {"plugin_file": buf}, follow=True)

    def test_upload_updates_and_the_page_shows_history_and_rollback(self):
        self.install("1.0.0")
        resp = self._upload("1.1.0")
        self.assertRedirects(resp, reverse("cpanel:plugin_detail", args=[SLUG]))
        self.assertContains(resp, "updated from 1.0.0 to 1.1.0")
        self.assertContains(resp, "Its update steps run after the next restart while it is active.")
        self.assertContains(resp, "1.0.0 → 1.1.0")
        self.assertContains(resp, "Waiting for a restart")
        self.assertContains(resp, "Roll back to 1.0.0")
        self.assertContains(resp, reverse("cpanel:plugin_rollback", args=[Plugin.objects.get().pk]))

    def test_refused_upload_shows_the_reason_and_history(self):
        self.install("1.0.0")
        resp = self._upload("1.0.0")
        self.assertContains(resp, "Upload failed:")
        self.assertContains(resp, "the same version as")
        page = self.client.get(reverse("cpanel:plugin_detail", args=[SLUG]))
        self.assertContains(page, "Refused")
        self.assertNotContains(page, "Roll back to")

    def test_rollback_view(self):
        self.install("1.0.0")
        self._upload("1.1.0")
        plugin = Plugin.objects.get()
        resp = self.client.post(reverse("cpanel:plugin_rollback", args=[plugin.pk]), follow=True)
        self.assertContains(resp, "rolled back from 1.1.0 to 1.0.0")
        plugin.refresh_from_db()
        self.assertEqual(plugin.version, "1.0.0")
        resp = self.client.post(reverse("cpanel:plugin_rollback", args=[plugin.pk]), follow=True)
        self.assertContains(resp, "Rollback failed: There is no previous version")

    def test_rollback_needs_a_superuser_and_post(self):
        self.install("1.0.0")
        self._upload("1.1.0")
        plugin = Plugin.objects.get()
        url = reverse("cpanel:plugin_rollback", args=[plugin.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        member = User.objects.create(email="member@example.com")
        self.client.force_login(member)
        self.client.post(url)
        plugin.refresh_from_db()
        self.assertEqual(plugin.version, "1.1.0")

    def test_missing_kept_folder_says_why(self):
        self.install("1.0.0")
        self._upload("1.1.0")
        shutil.rmtree(self.root / ".previous" / f"{SLUG}-1.0.0")
        page = self.client.get(reverse("cpanel:plugin_detail", args=[SLUG]))
        self.assertContains(page, "Rollback unavailable:")
        self.assertNotContains(page, "Roll back to")


# --------------------------------------------------------------------------- #
# brand_tracker 1.1.2 -> 1.1.3 (the acceptance proof)
# --------------------------------------------------------------------------- #


class BrandTrackerUpdateTests(TestCase):
    """Install brand_tracker 1.1.2, configure it, update to 1.1.3, roll back."""

    SLUG = "brand_tracker"
    MARKER = b"\n# brand_tracker 1.1.3 worker marker (T42 test)\n"

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="t42-bt-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        override = override_settings(PLUGINS_DIR=self.root)
        override.enable()
        self.addCleanup(override.disable)
        plugins_package.__path__.insert(0, str(self.root))
        self.addCleanup(plugins_package.__path__.remove, str(self.root))
        _forget(self.SLUG)
        self.addCleanup(_forget, self.SLUG)
        self.env = Environment.objects.create(name="t42-env", requirements="requests\n")

    def _provision(self):
        provisioning = __import__(f"plugins.{self.SLUG}.provisioning", fromlist=["provision"])
        provisioning.provision({
            "keywords": "pyrun\nself-hosted",
            "excluded_domains": "example.com",
            "news_enabled": True,
            "hackernews_enabled": True,
            "reddit_enabled": False,
            "num_results": 7,
            "retention_days": 45,
            "monthly_credit_cap": 300,
            "email_enabled": False,
            "enrich_provider": "off",
            "serper_api_key": "serper-secret-value-112",
            "resend_api_key": "resend-secret-value-112",
            "environment": self.env.name,
            "notify_on": "always",
            "notify_email": "ops@example.com",
            "schedule_time": "09:30",
            "schedule_weekday": "2",
            "timezone": "Europe/Berlin",
        })
        return provisioning

    def _state(self):
        from core.plugins.api import DataStoreAPI, ScheduleAPI, ScriptAPI, SecretAPI

        secrets = SecretAPI(self.SLUG)
        schedule = ScheduleAPI(self.SLUG).list()[0]
        script = ScriptAPI(self.SLUG).get("track")
        return {
            "config": DataStoreAPI(self.SLUG).get("state").get("config"),
            "secrets": {k: secrets.get(k).get_decrypted_value()
                        for k in ("SERPER_API_KEY", "RESEND_API_KEY")},
            "schedule": (schedule.run_mode, list(schedule.weekly_times), list(schedule.weekly_days),
                         schedule.timezone),
            "script": (script.pk, script.notify_on, script.notify_email, script.environment_id),
            "code": script.code,
        }

    def test_update_keeps_settings_and_secrets_and_updates_the_code(self):
        folder = EXAMPLES / self.SLUG
        old_zip = folder_zip(folder, version="1.1.2", drop=("lifecycle.py",))
        new_zip = folder_zip(
            folder, replace={"worker_body.py": (folder / "worker_body.py").read_bytes() + self.MARKER},
        )

        plugin, update, _ = PluginService.upload(old_zip)
        self.assertIsNone(update)
        Plugin.objects.filter(pk=plugin.pk).update(status=Plugin.Status.ACTIVE)
        plugin.refresh_from_db()
        self._provision()
        before = self._state()
        self.assertNotIn(self.MARKER.decode().strip(), before["code"])

        with _no_preflight():
            plugin, update, warnings = PluginService.upload(new_zip)
        self.assertEqual((plugin.version, plugin.previous_version), ("1.1.3", "1.1.2"))
        self.assertEqual(update.outcome, PluginUpdate.Outcome.PENDING)
        # The swap itself touches nothing the plugin owns.
        self.assertEqual(self._state(), before)

        # "Restart": the new code is what loads, then the steps run.
        _forget(self.SLUG)
        plugin_updates.run_steps(self.SLUG, update.from_version, write=lambda _: None)
        after = self._state()
        self.assertIn(self.MARKER.decode().strip(), after["code"])
        for key in ("config", "secrets", "schedule", "script"):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(after["schedule"][:4], ("weekly", ["09:30"], [2], "Europe/Berlin"))
        self.assertEqual(Secret.objects.filter(owner_plugin=self.SLUG).count(), 2)

        # Rollback: 1.1.2's folder and row are back; it ships no lifecycle.py,
        # so its steps are a no-op and the settings stay as they were.
        row = PluginService.rollback(plugin)
        plugin.refresh_from_db()
        self.assertEqual((plugin.version, row.to_version), ("1.1.2", "1.1.2"))
        self.assertFalse((self.root / self.SLUG / "lifecycle.py").exists())
        _forget(self.SLUG)
        plugin_updates.run_steps(self.SLUG, "", reprovision_only=True, write=lambda _: None)
        rolled = self._state()
        for key in ("config", "secrets", "schedule", "script"):
            self.assertEqual(rolled[key], before[key], key)


# --------------------------------------------------------------------------- #
# Backup
# --------------------------------------------------------------------------- #


class BackupTests(_Dir):
    def test_restore_leaves_plugins_and_their_history_alone(self):
        self.install("1.0.0")
        plugin, _, _ = PluginService.upload(demo_zip("1.1.0"), user=self.admin)
        data = BackupService.create_backup()
        self.assertNotIn("plugins", data)
        self.assertNotIn("plugin_updates", data)
        BackupService.restore_backup(data, current_user=self.admin)
        plugin.refresh_from_db()
        self.assertEqual((plugin.version, plugin.previous_version), ("1.1.0", "1.0.0"))
        self.assertEqual(plugin.updates.count(), 1)
