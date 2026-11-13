"""
Restoring a real PyRun 2.0.0 backup into 2.5 (2.5 T47).

The two files in core/test_backups/ were exported by the released
hasanaboulhasan/pyrun:2.0.0 image, one on SQLite and one on Postgres 16, with
the CI ENCRYPTION_KEY (the only key material in them; the secret values are
fixture strings). Each instance had three scripts with interval, daily and
weekly schedules, two secrets, a data store with three entries, two runs and a
Brain with a GitHub sync set up. core/test_backups/make_v2_0_0.py made them
(its docstring has the docker command).

What this pins:

- Both files validate (with only the version warning), preview and restore
  with every row, secret value, schedule and the Brain sync intact.
- After the restore every 2.5 feature is off: the script, run and global
  settings fields 2.5 added hold their model defaults (even when this server
  had them on), and there are no maintenance windows, watchers, tables,
  connections, OAuth apps, two-factor enrolments or plugins.
- A 2.0.0 restore followed by a 1.14.0 backup and restore keeps everything.
- A fresh 1.14.0 backup round-trips the 2.5 settings that the restore
  carries.
"""

from pathlib import Path

from django.conf import settings
from django.test import TestCase, override_settings

from core.models import (
    Brain,
    Connection,
    DataStore,
    GlobalSettings,
    MaintenanceWindow,
    OAuthClient,
    Plugin,
    Run,
    Script,
    ScriptSchedule,
    Secret,
    Table,
    User,
    UserTOTP,
    Watcher,
)
from core.services.backup_service import BackupService
from core.services.encryption_service import EncryptionService

# The CI ENCRYPTION_KEY the fixtures were made with (not a real key: 32 "A"s).
CI_KEY = "QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE="
FIXTURES = Path(settings.BASE_DIR) / "core" / "test_backups"

SCRIPT_25 = (
    "inputs_schema", "env_vars", "stop_grace_seconds", "concurrency_policy", "retry_max_attempts",
    "retry_delay_seconds", "retry_backoff_multiplier", "retry_on_timeout", "webhook_signature",
    "webhook_signature_secret_encrypted", "webhook_signature_header", "webhook_rate_per_minute",
    "webhook_rate_burst", "webhook_mode", "expose_as_tool", "tool_name",
)
RUN_25 = (
    "inputs", "result", "progress", "event", "cancel_reason", "queued_behind", "attempt", "retry_of",
    "next_attempt_at", "stop_requested_at", "response_code",
)
SETTINGS_25 = ("api_writes_enabled", "require_2fa", "plugin_trust_policy", "plugin_store_url")


def _use_ci_key(test):
    """Run ``test`` with the fixtures' key (the host may have its own)."""
    override = override_settings(ENCRYPTION_KEY=CI_KEY)
    override.enable()
    EncryptionService.reset()
    test.addCleanup(EncryptionService.reset)
    test.addCleanup(override.disable)


def _default(model, name):
    return model._meta.get_field(name).get_default()


class _From200:
    """Shared tests; subclasses name the fixture file."""

    fixture = ""

    def setUp(self):
        _use_ci_key(self)
        self.owner = User.objects.create_user(
            username="owner@example.com", email="owner@example.com", password="x", is_superuser=True,
            is_staff=True,
        )

    def load(self):
        path = FIXTURES / self.fixture
        return BackupService.deserialize_backup(path.read_bytes(), path.name)

    def restore(self, data):
        result = BackupService.restore_backup(data, current_user=self.owner)
        self.assertTrue(result["success"], result)
        return result

    def test_it_is_a_real_2_0_0_backup(self):
        meta = self.load()["backup_metadata"]
        self.assertEqual((meta["version"], meta["pyrunner_version"]), ("1.13.0", "2.0.0"))

    def test_it_validates_and_previews(self):
        data = self.load()
        check = BackupService.validate_backup(data)
        self.assertTrue(check["valid"], check)
        self.assertEqual(check["warnings"], [
            f"Backup version 1.13.0 differs from current version {BackupService.BACKUP_VERSION}",
        ])
        counts = BackupService.get_backup_preview(data)["counts"]
        self.assertEqual(
            {k: counts[k] for k in ("scripts", "schedules", "secrets", "runs", "datastores", "datastore_entries",
                                    "brains", "plugins", "tables", "watchers", "connections")},
            {"scripts": 3, "schedules": 3, "secrets": 2, "runs": 2, "datastores": 1, "datastore_entries": 3,
             "brains": 1, "plugins": 0, "tables": 0, "watchers": 0, "connections": 0},
        )

    def test_everything_comes_back(self):
        result = self.restore(self.load())
        self.assertEqual(
            {k: result["counts"][k] for k in ("scripts", "secrets", "runs", "datastores", "datastore_entries",
                                              "brains")},
            {"scripts": 3, "secrets": 2, "runs": 2, "datastores": 1, "datastore_entries": 3, "brains": 1},
        )
        self.assertNotIn("plugins", result["counts"])
        values = {s.key: s.get_decrypted_value() for s in Secret.objects.all()}
        self.assertEqual(values, {"API_KEY": "fixture-not-a-real-key", "GITHUB_TOKEN": "fixture-not-a-real-token"})
        store = DataStore.objects.get(name="settings")
        self.assertEqual(
            {e.key: e.get_value() for e in store.entries.all()},
            {"greeting": "hi", "count": 3, "flags": {"beta": True, "list": [1, 2]}},
        )
        modes = {s.script.name: s for s in ScriptSchedule.objects.select_related("script")}
        self.assertEqual((modes["Hello"].run_mode, modes["Hello"].interval_minutes), ("interval", 15))
        self.assertEqual(modes["Reads a secret"].daily_times, ["09:00", "17:30"])
        self.assertEqual((modes["Weekly report"].weekly_days, modes["Weekly report"].weekly_times),
                         ([0, 3], ["08:15"]))
        self.assertTrue(all(s.is_active for s in modes.values()))
        self.assertEqual(sorted(Run.objects.values_list("stdout", flat=True)), ["run 0\n", "run 1\n"])
        brain = Brain.objects.get(slug="notes")
        self.assertEqual((brain.github_sync.repository, brain.github_sync.poll_enabled), ("example/brain-notes", False))
        self.assertEqual(brain.github_sync.secret.key, "GITHUB_TOKEN")

    def test_every_2_5_feature_is_off_after_the_restore(self):
        gs = GlobalSettings.get_settings()
        gs.api_writes_enabled = True
        gs.require_2fa = True
        gs.plugin_trust_policy = GlobalSettings.PluginTrustPolicy.SIGNED_ONLY
        gs.save()
        plugin = Plugin.objects.create(slug="kept", name="Kept", version="1.0.0")

        self.restore(self.load())

        gs = GlobalSettings.get_settings()
        for name in SETTINGS_25:
            self.assertEqual(getattr(gs, name), _default(GlobalSettings, name), name)
        self.assertEqual(Script.objects.count(), 3)
        for script in Script.objects.all():
            for name in SCRIPT_25:
                self.assertEqual(getattr(script, name), _default(Script, name), f"{script.name}: {name}")
        for run in Run.objects.all():
            for name in RUN_25:
                value = getattr(run, name)
                self.assertEqual(value, None if name == "retry_of" else _default(Run, name), name)
        for model in (MaintenanceWindow, Watcher, Table, Connection, OAuthClient, UserTOTP):
            self.assertFalse(model.objects.exists(), model.__name__)
        self.assertEqual(list(Plugin.objects.values_list("pk", flat=True)), [plugin.pk])
        # A restore never creates accounts (the 2.0.0 admin is mapped to the restoring one).
        self.assertFalse(User.objects.filter(email="admin@example.com").exists())
        self.assertFalse(User.objects.get(pk=self.owner.pk).notify_on_login)

    def test_a_1_14_backup_of_the_restored_server_round_trips(self):
        self.restore(self.load())
        again = BackupService.create_backup(created_by_user=self.owner)
        self.assertEqual(again["backup_metadata"]["version"], "1.14.0")
        raw, _ = BackupService.serialize_backup(again, BackupService.FORMAT_GZIP)
        result = self.restore(BackupService.deserialize_backup(raw, "again.json.gz"))
        self.assertEqual(
            {k: result["counts"][k] for k in ("scripts", "secrets", "runs", "datastore_entries", "brains", "tables",
                                              "watchers", "connections", "maintenance_windows")},
            {"scripts": 3, "secrets": 2, "runs": 2, "datastore_entries": 3, "brains": 1, "tables": 0,
             "watchers": 0, "connections": 0, "maintenance_windows": 0},
        )
        self.assertEqual(Secret.objects.get(key="API_KEY").get_decrypted_value(), "fixture-not-a-real-key")


class From200SQLiteTests(_From200, TestCase):
    fixture = "v2_0_0_sqlite.json"


class From200PostgresTests(_From200, TestCase):
    fixture = "v2_0_0_postgres.json"


class Fresh114RoundTripTests(TestCase):
    """The 2.5 settings a restore carries, set away from their defaults, come back."""

    def setUp(self):
        _use_ci_key(self)
        self.owner = User.objects.create_user(
            username="owner@example.com", email="owner@example.com", password="x", is_superuser=True,
            is_staff=True,
        )

    def test_the_2_5_settings_come_back(self):
        from core.models import Environment

        env = Environment.objects.create(name="default", is_default=True, python_version="3.12")
        script = Script.objects.create(
            name="Tool", code="print(1)\n", environment=env, created_by=self.owner,
            concurrency_policy=Script.ConcurrencyPolicy.SKIP, retry_max_attempts=2, retry_delay_seconds=30,
            stop_grace_seconds=25, env_vars={"MODE": "prod"}, expose_as_tool=True, tool_name="tool_one",
            webhook_mode=Script.WebhookMode.SYNC, inputs_schema=[{"name": "q", "type": "text"}],
        )
        gs = GlobalSettings.get_settings()
        gs.api_writes_enabled = True
        gs.require_2fa = True
        gs.save()
        self.owner.notify_on_login = True
        self.owner.save()

        data = BackupService.create_backup(created_by_user=self.owner)
        raw, _ = BackupService.serialize_backup(data, BackupService.FORMAT_JSON)
        self.assertTrue(BackupService.restore_backup(
            BackupService.deserialize_backup(raw, "fresh.json"), current_user=self.owner)["success"])

        back = Script.objects.get(pk=script.pk)
        self.assertEqual(
            (back.concurrency_policy, back.retry_max_attempts, back.retry_delay_seconds, back.stop_grace_seconds,
             back.env_vars, back.expose_as_tool, back.tool_name, back.webhook_mode),
            ("skip", 2, 30, 25, {"MODE": "prod"}, True, "tool_one", "sync"),
        )
        self.assertEqual(back.inputs_schema, [{"name": "q", "type": "text"}])
        gs = GlobalSettings.get_settings()
        self.assertEqual((gs.api_writes_enabled, gs.require_2fa), (True, True))
        self.assertTrue(User.objects.get(pk=self.owner.pk).notify_on_login)
