"""
Run retention vs unfinished and refused runs (2.5, ticket T51).

What this pins, roughly in order of how much it would hurt to get wrong:

- No cleanup path (the daily task, **Run cleanup now**, the service itself)
  deletes a pending or running run, whatever the days/count settings: a run
  held by the Queue policy, a retry waiting for its time, a running run, all
  older than the newest N or past the retention days.
- Count-based retention keeps the newest N runs that actually ran; runs
  refused before they started (concurrency skip, maintenance window) keep
  their own newest N, so a burst of refused webhook calls can't push real
  runs out, and refused runs stay capped under a count-only policy.
- The preview (``get_cleanup_stats`` and the console's JSON) counts exactly
  what the cleanup deletes, and the cleanup reports runs only (not the rows
  deleted with them).
"""

import datetime
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import (
    Environment,
    GlobalSettings,
    MaintenanceWindow,
    Run,
    RunMask,
    Script,
    Workspace,
)
from core.services.retention_service import RetentionService
from core.tasks import cleanup_old_runs_task
from core.test_maintenance_windows import _setup_wizard_off, _tasks

User = get_user_model()

SKIP = Script.ConcurrencyPolicy.SKIP
QUEUE = Script.ConcurrencyPolicy.QUEUE


class _Fixture:
    def setUp(self):
        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(name="renv", path=f"renv{uuid.uuid4().hex[:6]}")
        self.now = timezone.now()

    def _script(self, **kwargs):
        kwargs.setdefault("code", "pass")
        return Script.objects.create(
            name=f"r{uuid.uuid4().hex[:6]}", environment=self.env,
            workspace=self.ws, is_enabled=True, **kwargs,
        )

    def _run(self, script, age, status=Run.Status.SUCCESS, **kwargs):
        """A run created ``age`` (a timedelta) ago."""
        if status == Run.Status.RUNNING:
            kwargs.setdefault("started_at", self.now - age)
        run = Run.objects.create(
            script=script, workspace=script.workspace, status=status,
            trigger_type=kwargs.pop("trigger_type", Run.TriggerType.MANUAL), **kwargs,
        )
        self._age(run, age)
        return run

    def _age(self, run, age):
        # created_at is auto_now_add: set it afterwards.
        Run.objects.filter(pk=run.pk).update(created_at=self.now - age)

    def _refused(self, script, age, reason):
        return self._run(
            script, age, status=Run.Status.CANCELLED, cancel_reason=reason,
            ended_at=self.now - age,
        )

    def _unfinished(self, script, age):
        """A held run, a waiting retry and a running run, all ``age`` old."""
        running = self._run(script, age, status=Run.Status.RUNNING)
        held = self._run(script, age, status=Run.Status.PENDING, queued_behind=running.pk)
        first = self._run(script, age, status=Run.Status.FAILED)
        retry = self._run(
            script, age, status=Run.Status.PENDING, attempt=2, retry_of=first,
            next_attempt_at=self.now + datetime.timedelta(minutes=10),
        )
        self.assertTrue(held.is_held)
        self.assertTrue(retry.is_waiting_retry)
        return [running, held, retry]

    def _ids(self, script):
        return set(Run.objects.filter(script=script).values_list("pk", flat=True))

    def _pks(self, runs):
        return {r.pk for r in runs}


DAY = datetime.timedelta(days=1)
HOUR = datetime.timedelta(hours=1)


# --------------------------------------------------------------------------- #
# Unfinished runs are never deleted
# --------------------------------------------------------------------------- #

class UnfinishedRunTests(_Fixture, TestCase):
    def test_count_only_keeps_unfinished_runs_older_than_the_newest_n(self):
        script = self._script()
        unfinished = self._unfinished(script, 10 * DAY)
        old = [self._run(script, (5 + i) * DAY, status=s) for i, s in enumerate(Run.FINISHED_STATUSES)]
        newest = [self._run(script, i * HOUR) for i in range(1, 4)]

        deleted = RetentionService.cleanup_runs_for_script(script, days=0, count=3)

        # The FAILED first attempt of the retry is old and finished: it goes.
        self.assertEqual(deleted, len(old) + 1)
        self.assertEqual(self._ids(script), self._pks(unfinished + newest))

    def test_days_only_keeps_unfinished_runs_past_the_retention_days(self):
        script = self._script()
        unfinished = self._unfinished(script, 60 * DAY)
        recent = self._run(script, DAY)
        self._run(script, 40 * DAY)

        RetentionService.cleanup_runs_for_script(script, days=30, count=0)

        self.assertEqual(self._ids(script), self._pks(unfinished + [recent]))

    def test_days_and_count_keep_unfinished_runs_outside_both(self):
        script = self._script()
        unfinished = self._unfinished(script, 90 * DAY)
        newest = self._run(script, 40 * DAY)
        recent = self._run(script, HOUR)
        old = self._run(script, 50 * DAY)

        RetentionService.cleanup_runs_for_script(script, days=30, count=2)

        self.assertEqual(self._ids(script), self._pks(unfinished + [newest, recent]))
        self.assertFalse(Run.objects.filter(pk=old.pk).exists())

    def test_unfinished_runs_dont_take_the_n_places(self):
        """Count 2 keeps 2 finished runs on top of the pending/running ones."""
        script = self._script()
        older = [self._run(script, (3 + i) * DAY) for i in range(3)]
        unfinished = self._unfinished(script, HOUR)  # newer than every finished run

        RetentionService.cleanup_runs_for_script(script, days=0, count=2)

        # The retry's FAILED first attempt is the newest finished run.
        kept = self._ids(script) - self._pks(unfinished)
        self.assertEqual(len(kept), 2)
        self.assertIn(older[0].pk, kept)

    def test_both_rules_off_keeps_everything(self):
        script = self._script()
        self._run(script, 400 * DAY)
        self._refused(script, 400 * DAY, Run.CancelReason.MAINTENANCE)
        self.assertEqual(RetentionService.cleanup_runs_for_script(script, days=0, count=0), 0)
        self.assertEqual(Run.objects.filter(script=script).count(), 2)


# --------------------------------------------------------------------------- #
# Refused runs don't crowd out real ones
# --------------------------------------------------------------------------- #

class RefusedRunTests(_Fixture, TestCase):
    def test_refused_runs_keep_their_own_newest_n(self):
        script = self._script()
        real = [self._run(script, (10 + i) * DAY) for i in range(3)]
        self._run(script, 20 * DAY)  # the 4th real run: goes
        refused = [
            self._refused(script, i * HOUR, reason)
            for i, reason in enumerate(
                [Run.CancelReason.CONCURRENCY_SKIP, Run.CancelReason.MAINTENANCE] * 5, start=1
            )
        ]
        # An ordinary cancel (someone pressed Cancel) counts as a real run.
        user_cancelled = self._run(script, 5 * DAY, status=Run.Status.CANCELLED)

        deleted = RetentionService.cleanup_runs_for_script(script, days=0, count=4)

        self.assertEqual(deleted, 1 + (len(refused) - 4))
        self.assertEqual(
            self._ids(script), self._pks(real + [user_cancelled] + refused[:4])
        )

    def test_days_only_ages_refused_runs_out_like_any_other(self):
        script = self._script()
        recent = self._refused(script, DAY, Run.CancelReason.MAINTENANCE)
        self._refused(script, 40 * DAY, Run.CancelReason.MAINTENANCE)
        RetentionService.cleanup_runs_for_script(script, days=30, count=0)
        self.assertEqual(self._ids(script), {recent.pk})

    def test_script_overrides_apply(self):
        script = self._script(retention_days_override=0, retention_count_override=1)
        gs = GlobalSettings.get_settings()
        gs.retention_days, gs.retention_count = 0, 50
        gs.save()
        newest_real = self._run(script, 2 * DAY)
        self._run(script, 3 * DAY)
        newest_refused = self._refused(script, DAY, Run.CancelReason.CONCURRENCY_SKIP)
        self._refused(script, 4 * DAY, Run.CancelReason.CONCURRENCY_SKIP)
        self.assertEqual(RetentionService.cleanup_all_runs(), 2)
        self.assertEqual(self._ids(script), {newest_real.pk, newest_refused.pk})


class RefusedWebhookBurstTests(_Fixture, TestCase):
    """Refused runs made through the real webhook endpoint."""

    def setUp(self):
        super().setUp()
        _setup_wizard_off(self)
        gs = GlobalSettings.get_settings()
        gs.retention_days, gs.retention_count = 0, 3
        gs.save()

    def _burst(self, script, calls, expect_status):
        url = reverse("webhook_trigger", args=[script.webhook_token])
        ids = []
        with _tasks() as task:
            for _ in range(calls):
                resp = self.client.post(url, data='{"x": 1}', content_type="application/json")
                self.assertEqual(resp.status_code, expect_status, resp.content)
                ids.append(resp.json()["run_id"])
        task.assert_not_called()
        made = [Run.objects.get(pk=pk) for pk in ids]
        self.assertTrue(all(r.cancel_reason in RetentionService.refused_reasons() for r in made))
        # One call after another, a minute apart (auto_now_add can tie on a
        # coarse clock): the last ones are the newest.
        for i, run in enumerate(made):
            self._age(run, (calls - i) * datetime.timedelta(minutes=1))
        return made

    def test_a_maintenance_burst_does_not_push_real_runs_out(self):
        script = self._script(webhook_token="tok_" + uuid.uuid4().hex)
        real = [self._run(script, (1 + i) * DAY, trigger_type=Run.TriggerType.API) for i in range(3)]
        now = timezone.now()
        MaintenanceWindow.objects.create(
            name="Upgrade", kind=MaintenanceWindow.Kind.ONE_OFF,
            starts_at=now - datetime.timedelta(minutes=5), ends_at=now + HOUR,
            blocks=["webhook"],
        )
        refused = self._burst(script, 12, 503)

        preview = RetentionService.get_cleanup_stats()
        self.assertEqual(preview["runs_to_delete"], 9)
        self.assertEqual(cleanup_old_runs_task(), {"success": True, "deleted_count": 9})
        self.assertEqual(self._ids(script), self._pks(real + refused[-3:]))

    def test_a_skip_burst_keeps_the_running_run_and_real_ones(self):
        script = self._script(webhook_token="tok_" + uuid.uuid4().hex, concurrency_policy=SKIP)
        real = [self._run(script, (1 + i) * DAY) for i in range(3)]
        running = self._run(script, 30 * DAY, status=Run.Status.RUNNING)
        refused = self._burst(script, 8, 200)

        RetentionService.cleanup_all_runs()

        self.assertEqual(self._ids(script), self._pks(real + [running] + refused[-3:]))

    def test_held_webhook_runs_are_never_deleted(self):
        script = self._script(webhook_token="tok_" + uuid.uuid4().hex, concurrency_policy=QUEUE)
        running = self._run(script, 30 * DAY, status=Run.Status.RUNNING)
        url = reverse("webhook_trigger", args=[script.webhook_token])
        with _tasks():
            for _ in range(5):
                self.assertEqual(self.client.post(url, data="{}", content_type="application/json").status_code, 200)
        held = list(Run.objects.filter(script=script).exclude(pk=running.pk))
        self.assertTrue(all(r.is_held for r in held))
        for run in held:
            self._age(run, 20 * DAY)
        real = [self._run(script, (1 + i) * HOUR) for i in range(3)]

        self.assertEqual(RetentionService.cleanup_all_runs(), 0)
        self.assertEqual(self._ids(script), self._pks(held + real + [running]))


# --------------------------------------------------------------------------- #
# The preview matches what is deleted
# --------------------------------------------------------------------------- #

class PreviewTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        _setup_wizard_off(self)
        gs = GlobalSettings.get_settings()
        gs.retention_days, gs.retention_count = 0, 2
        gs.save()
        self.admin = User.objects.create(email="root@example.com", is_superuser=True)
        self.client.force_login(self.admin)
        self.a = self._script()
        self.b = self._script()
        self.unfinished = self._unfinished(self.a, 50 * DAY)  # 3 in flight + 1 FAILED
        for i in range(4):
            self._run(self.a, (1 + i) * DAY)
            self._refused(self.a, (1 + i) * HOUR, Run.CancelReason.MAINTENANCE)
        self._run(self.b, DAY)

    def test_preview_counts_what_the_cleanup_deletes(self):
        resp = self.client.get(reverse("cpanel:cleanup_preview"))
        stats = resp.json()["stats"]
        total = Run.objects.count()
        self.assertEqual(stats["total_runs"], total)
        # a: the FAILED first attempt + 2 real + 2 refused; b: nothing.
        self.assertEqual(stats["runs_to_delete"], 5)
        self.assertEqual(stats["runs_to_keep"], total - 5)
        self.assertEqual(stats["runs_in_progress"], 3)
        rows = {row["script_id"]: row for row in stats["per_script"]}
        self.assertEqual(rows[str(self.a.id)]["to_delete"], 5)
        self.assertEqual(rows[str(self.b.id)]["to_delete"], 0)

        resp = self.client.post(reverse("cpanel:manual_cleanup"))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Run.objects.count(), total - 5)
        self.assertTrue(set(Run.objects.values_list("pk", flat=True)) >= self._pks(self.unfinished))
        self.assertEqual(RetentionService.get_cleanup_stats()["runs_to_delete"], 0)

    def test_cleanup_reports_runs_not_the_rows_deleted_with_them(self):
        doomed = RetentionService.get_runs_to_delete_for_script(self.a).first()
        RunMask.objects.create(run=doomed, label="KEY", digest="0" * 64, value_encrypted="x")
        self.assertEqual(cleanup_old_runs_task(), {"success": True, "deleted_count": 5})
        self.assertFalse(RunMask.objects.exists())
        self.assertIsNotNone(GlobalSettings.get_settings().last_cleanup_at)

    def test_manual_cleanup_message_counts_runs(self):
        resp = self.client.post(reverse("cpanel:manual_cleanup"), follow=True)
        self.assertContains(resp, "Cleanup completed. 5 runs deleted.")
        resp = self.client.get(reverse("cpanel:settings"))
        self.assertContains(resp, "Pending and running runs are never deleted")


class RetentionDocsTests(TestCase):
    def test_the_docs_promise_no_per_script_setting_the_console_lacks(self):
        # Test pass 7 F5: docs/scheduling.md said "a script can override both",
        # but no form or API sets Script.retention_*_override.
        from pathlib import Path

        from django.conf import settings

        from core.forms import ScriptForm

        guide = " ".join((Path(settings.BASE_DIR) / "docs" / "scheduling.md").read_text(encoding="utf-8").split())
        exposed = {"retention_days_override", "retention_count_override"} & set(ScriptForm.base_fields)
        if not exposed:
            self.assertNotIn("a script can override", guide)
            self.assertIn("no per-script retention setting", guide)
