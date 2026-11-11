"""
Maintenance windows (2.5 Stage 1, ticket T07).

What this pins, roughly in order of how much it would hurt to get wrong:

- When a window is on: one-off between its two moments, weekly on its days
  at its times in its own time zone (across midnight, across DST changes,
  a whole day when the times are equal), the end never included.
- Enforcement in the run funnel: a run of a blocked trigger class is marked
  CANCELLED with ``cancel_reason = maintenance`` and a note naming the
  window, before the concurrency policy, with no task queued. Only enabled
  windows of the run's workspace and global ones count. A retry counts as
  the trigger of its first attempt and, when dropped, the attempt before it
  notifies. Running and held runs are never touched.
- What each trigger answers: the tick is skipped, a webhook gets 503 with
  ``Retry-After``, Run now / chat / plugins get a message.
- The console (global under Settings for superusers, per workspace for its
  Owners and Admins), the dashboard banner and backups.
"""

import datetime
import uuid
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import (
    Channel,
    Environment,
    MaintenanceWindow,
    Run,
    Script,
    ScriptSchedule,
    Workspace,
    WorkspaceMembership,
)
from core.plugins.api import ScriptAPI
from core.services import EncryptionService, maintenance, run_concurrency, run_retries
from core.services.backup_service import BackupService
from core.services.run_events import manual_event, plugin_event, webhook_event
from core.tasks import execute_scheduled_run, queue_script_run

User = get_user_model()

UTC = datetime.timezone.utc
W = MaintenanceWindow
MAINTENANCE = Run.CancelReason.MAINTENANCE


def _utc(*args):
    return datetime.datetime(*args, tzinfo=UTC)


def _weekly(days, start, end, tz="UTC", **kwargs):
    return W(
        name="w", kind=W.Kind.WEEKLY, weekdays=days, timezone=tz,
        start_time=datetime.time(*start), end_time=datetime.time(*end), **kwargs,
    )


def _tasks():
    counter = iter(range(1, 10_000))
    return mock.patch("core.tasks.async_task", side_effect=lambda *a, **k: f"task-{next(counter)}")


def _setup_wizard_off(test):
    for target in (
        "core.services.setup_service.SetupService.is_setup_needed",
        "core.services.setup_service.SetupService.needs_admin_setup",
    ):
        p = mock.patch(target, return_value=False)
        p.start()
        test.addCleanup(p.stop)


# --------------------------------------------------------------------------- #
# When a window is on
# --------------------------------------------------------------------------- #


class OccurrenceTests(TestCase):
    # 2026-11-09 is a Monday.
    def test_weekly_window_is_on_between_its_times_end_excluded(self):
        w = _weekly([0], (2, 0), (4, 0))
        self.assertIsNone(w.occurrence_at(_utc(2026, 11, 9, 1, 59)))
        self.assertEqual(
            w.occurrence_at(_utc(2026, 11, 9, 2, 0)), (_utc(2026, 11, 9, 2), _utc(2026, 11, 9, 4))
        )
        self.assertIsNotNone(w.occurrence_at(_utc(2026, 11, 9, 3, 59, 59)))
        self.assertIsNone(w.occurrence_at(_utc(2026, 11, 9, 4, 0)))
        # Not on the other days.
        self.assertIsNone(w.occurrence_at(_utc(2026, 11, 10, 3, 0)))

    def test_next_occurrence_is_the_current_one_then_the_next_week(self):
        w = _weekly([0], (2, 0), (4, 0))
        self.assertEqual(w.next_occurrence(_utc(2026, 11, 9, 3)), (_utc(2026, 11, 9, 2), _utc(2026, 11, 9, 4)))
        self.assertEqual(w.next_occurrence(_utc(2026, 11, 9, 4)), (_utc(2026, 11, 16, 2), _utc(2026, 11, 16, 4)))
        self.assertEqual(w.next_occurrence(_utc(2026, 11, 8, 23)), (_utc(2026, 11, 9, 2), _utc(2026, 11, 9, 4)))

    def test_several_weekdays_pick_the_nearest(self):
        w = _weekly([2, 5], (10, 0), (11, 0))  # Wed, Sat
        self.assertEqual(w.next_occurrence(_utc(2026, 11, 9, 12))[0], _utc(2026, 11, 11, 10))
        self.assertEqual(w.next_occurrence(_utc(2026, 11, 11, 11))[0], _utc(2026, 11, 14, 10))

    def test_an_end_before_the_start_crosses_midnight(self):
        w = _weekly([4], (23, 0), (1, 0))  # Friday 23:00 to Saturday 01:00
        self.assertIsNone(w.occurrence_at(_utc(2026, 11, 13, 22, 59)))
        self.assertEqual(
            w.occurrence_at(_utc(2026, 11, 14, 0, 30)), (_utc(2026, 11, 13, 23), _utc(2026, 11, 14, 1))
        )
        self.assertIsNone(w.occurrence_at(_utc(2026, 11, 14, 1, 0)))
        # Saturday isn't a start day: Saturday 23:30 is outside.
        self.assertIsNone(w.occurrence_at(_utc(2026, 11, 14, 23, 30)))

    def test_equal_times_are_a_whole_day(self):
        w = _weekly([5], (0, 0), (0, 0))  # all of Saturday
        self.assertIsNotNone(w.occurrence_at(_utc(2026, 11, 14, 0, 0)))
        self.assertIsNotNone(w.occurrence_at(_utc(2026, 11, 14, 23, 59)))
        self.assertIsNone(w.occurrence_at(_utc(2026, 11, 15, 0, 0)))

    def test_times_are_in_the_windows_time_zone(self):
        # Monday 00:00-02:00 in Tokyo (UTC+9) is Sunday 15:00-17:00 UTC.
        w = _weekly([0], (0, 0), (2, 0), tz="Asia/Tokyo")
        self.assertEqual(
            w.occurrence_at(_utc(2026, 11, 8, 15, 30)), (_utc(2026, 11, 8, 15), _utc(2026, 11, 8, 17))
        )
        # Monday 00:30 UTC is Monday 09:30 in Tokyo: outside.
        self.assertIsNone(w.occurrence_at(_utc(2026, 11, 9, 0, 30)))
        # And the reverse: Sunday 22:00-23:00 in New York (UTC-5 in November)
        # is Monday 03:00-04:00 UTC.
        w = _weekly([6], (22, 0), (23, 0), tz="America/New_York")
        self.assertIsNotNone(w.occurrence_at(_utc(2026, 11, 9, 3, 30)))
        self.assertIsNone(w.occurrence_at(_utc(2026, 11, 8, 22, 30)))

    def test_daylight_saving_changes_keep_the_local_times(self):
        w = _weekly([6], (1, 0), (5, 0), tz="America/New_York")
        # 2026-03-08: clocks go forward at 02:00, so 01:00 EST to 05:00 EDT is 3 hours.
        start, end = w.occurrence_at(_utc(2026, 3, 8, 7))
        self.assertEqual((start, end), (_utc(2026, 3, 8, 6), _utc(2026, 3, 8, 9)))
        # 2026-11-01: clocks go back at 02:00, so 01:00 EDT to 05:00 EST is 5 hours.
        start, end = w.occurrence_at(_utc(2026, 11, 1, 6))
        self.assertEqual((start, end), (_utc(2026, 11, 1, 5), _utc(2026, 11, 1, 10)))
        # The week after, a plain 4 hours at UTC-5.
        self.assertEqual(w.next_occurrence(_utc(2026, 11, 1, 10)), (_utc(2026, 11, 8, 6), _utc(2026, 11, 8, 10)))

    def test_a_late_window_across_the_dst_change_still_matches_the_day_before(self):
        # Saturday 23:00 to Sunday 03:00, Europe/Berlin; DST ends 2026-10-25 at 03:00.
        w = _weekly([5], (23, 0), (3, 0), tz="Europe/Berlin")
        start, end = w.occurrence_at(_utc(2026, 10, 25, 1, 30))
        self.assertEqual(start, _utc(2026, 10, 24, 21))  # 23:00 CEST
        self.assertEqual(end, _utc(2026, 10, 25, 2))  # 03:00 CET
        self.assertEqual(end - start, datetime.timedelta(hours=5))

    def test_one_off_window(self):
        w = W(name="o", kind=W.Kind.ONE_OFF, starts_at=_utc(2026, 12, 1, 2), ends_at=_utc(2026, 12, 1, 6))
        self.assertIsNone(w.occurrence_at(_utc(2026, 12, 1, 1, 59)))
        self.assertIsNotNone(w.occurrence_at(_utc(2026, 12, 1, 2)))
        self.assertIsNone(w.occurrence_at(_utc(2026, 12, 1, 6)))
        self.assertEqual(w.next_occurrence(_utc(2026, 11, 1)), (_utc(2026, 12, 1, 2), _utc(2026, 12, 1, 6)))
        self.assertIsNone(w.next_occurrence(_utc(2026, 12, 1, 6)))

    def test_no_days_or_no_times_never_match(self):
        self.assertIsNone(_weekly([], (2, 0), (4, 0)).next_occurrence(_utc(2026, 11, 9)))
        w = W(name="x", kind=W.Kind.WEEKLY, weekdays=[0])
        self.assertIsNone(w.next_occurrence(_utc(2026, 11, 9)))
        self.assertIsNone(W(name="x", kind=W.Kind.ONE_OFF).occurrence_at(_utc(2026, 11, 9)))

    def test_an_unknown_time_zone_falls_back_to_utc(self):
        w = _weekly([0], (2, 0), (4, 0), tz="Mars/Olympus")
        self.assertIsNotNone(w.occurrence_at(_utc(2026, 11, 9, 3)))

    def test_descriptions(self):
        w = _weekly([5, 0], (23, 0), (1, 0), tz="Europe/Paris", blocks=["webhook", "scheduled"])
        self.assertEqual(w.schedule_display, "Weekly on Mon, Sat, 23:00 to 01:00 (next day) (Europe/Paris)")
        self.assertEqual(w.blocks_display, "scheduled runs and webhook calls")
        w.blocks = ["api", "manual", "channel"]
        self.assertEqual(w.blocks_display, "chat messages, manual runs and API runs")
        o = W(name="o", kind=W.Kind.ONE_OFF, timezone="Asia/Tokyo",
              starts_at=_utc(2026, 11, 30, 17), ends_at=_utc(2026, 11, 30, 19))
        self.assertEqual(o.schedule_display, "Once: Tue 1 Dec 2026, 02:00 to Tue 1 Dec 2026, 04:00 (Asia/Tokyo)")

    def test_default_blocks(self):
        self.assertEqual(W(name="d").blocks, ["scheduled", "webhook", "channel", "watcher"])


# --------------------------------------------------------------------------- #
# The funnel
# --------------------------------------------------------------------------- #


class _Fixture:
    def setUp(self):
        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(name="menv", path=f"menv{uuid.uuid4().hex[:6]}")

    def _script(self, workspace=None, **kwargs):
        kwargs.setdefault("code", "pass")
        return Script.objects.create(
            name=f"m{uuid.uuid4().hex[:6]}", environment=self.env,
            workspace=workspace or self.ws, is_enabled=True, **kwargs,
        )

    def _run(self, script, event=None, trigger_type=Run.TriggerType.MANUAL, **kwargs):
        return Run.objects.create(
            script=script, workspace=script.workspace, status=Run.Status.PENDING,
            trigger_type=trigger_type, event=event, **kwargs,
        )

    def _window(self, blocks=None, workspace=None, minutes_left=60, **kwargs):
        """A one-off window that is on now."""
        now = timezone.now()
        kwargs.setdefault("starts_at", now - datetime.timedelta(minutes=5))
        kwargs.setdefault("ends_at", now + datetime.timedelta(minutes=minutes_left))
        return MaintenanceWindow.objects.create(
            name=kwargs.pop("name", "Upgrade"), kind=W.Kind.ONE_OFF, workspace=workspace,
            blocks=blocks if blocks is not None else list(W.DEFAULT_BLOCKS), **kwargs,
        )

    def _assert_blocked(self, run, window_name="Upgrade"):
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.CANCELLED)
        self.assertEqual(run.cancel_reason, MAINTENANCE)
        self.assertTrue(run.is_maintenance_skipped)
        self.assertFalse(run.is_skipped)
        self.assertIsNotNone(run.ended_at)
        self.assertEqual(run.task_id, "")
        self.assertIn(f'maintenance window "{window_name}"', run.stderr)


class FunnelTests(_Fixture, TestCase):
    def test_no_window_queues_as_before(self):
        run = self._run(self._script(), event={"kind": "schedule"})
        with _tasks() as task:
            self.assertEqual(queue_script_run(run), "task-1")
        task.assert_called_once()

    def test_a_blocked_class_is_cancelled_with_the_reason_and_no_task(self):
        self._window()
        run = self._run(self._script(), event={"kind": "schedule", "received_at": "x"},
                        trigger_type=Run.TriggerType.SCHEDULED)
        with _tasks() as task:
            self.assertEqual(queue_script_run(run), "")
        task.assert_not_called()
        self._assert_blocked(run)
        self.assertIn("blocks scheduled runs until", run.stderr)
        self.assertEqual(run.maintenance_block.window.name, "Upgrade")

    def test_a_class_the_window_does_not_block_runs(self):
        self._window()  # default: scheduled, webhook, channel
        run = self._run(self._script(), event={"kind": "manual", "received_at": "x"})
        with _tasks() as task:
            queue_script_run(run)
        task.assert_called_once()
        run.refresh_from_db()
        self.assertEqual(run.cancel_reason, "")

    def test_disabled_and_ended_windows_and_other_workspaces_do_not_block(self):
        other = Workspace.objects.create(name="Other")
        self._window(name="off", enabled=False)
        self._window(name="ended", starts_at=timezone.now() - datetime.timedelta(hours=2),
                     ends_at=timezone.now() - datetime.timedelta(minutes=1))
        self._window(name="later", starts_at=timezone.now() + datetime.timedelta(minutes=5))
        self._window(name="elsewhere", workspace=other)
        run = self._run(self._script(), event={"kind": "schedule"})
        with _tasks() as task:
            queue_script_run(run)
        task.assert_called_once()

    def test_a_workspace_window_blocks_its_workspace_and_a_global_one_every_workspace(self):
        other = Workspace.objects.create(name="Other")
        self._window(name="Mine", workspace=other)
        mine = self._run(self._script(workspace=other), event={"kind": "webhook"})
        default = self._run(self._script(), event={"kind": "webhook"})
        with _tasks():
            queue_script_run(mine)
            queue_script_run(default)
        self._assert_blocked(mine, "Mine")
        default.refresh_from_db()
        self.assertEqual(default.status, Run.Status.PENDING)

        self._window(name="Everywhere")
        run = self._run(self._script(workspace=other), event={"kind": "webhook"})
        run2 = self._run(self._script(), event={"kind": "webhook"})
        with _tasks():
            queue_script_run(run)
            queue_script_run(run2)
        self.assertEqual(Run.objects.get(pk=run.pk).cancel_reason, MAINTENANCE)
        self._assert_blocked(run2, "Everywhere")

    def test_maintenance_comes_before_the_concurrency_policy(self):
        script = self._script(concurrency_policy=Script.ConcurrencyPolicy.QUEUE)
        Run.objects.create(script=script, workspace=self.ws, status=Run.Status.RUNNING)
        self._window()
        run = self._run(script, event={"kind": "schedule"})
        with _tasks():
            queue_script_run(run)
        self._assert_blocked(run)
        self.assertIsNone(run.queued_behind)

    def test_running_and_held_runs_are_not_touched(self):
        script = self._script(concurrency_policy=Script.ConcurrencyPolicy.QUEUE)
        running = Run.objects.create(script=script, workspace=self.ws, status=Run.Status.RUNNING)
        held = self._run(script, event={"kind": "schedule"})
        with _tasks():
            queue_script_run(held)
        held.refresh_from_db()
        self.assertTrue(held.is_held)

        self._window()
        running.refresh_from_db()
        self.assertEqual(running.status, Run.Status.RUNNING)
        # The held run was admitted before the window: it starts on its turn.
        Run.objects.filter(pk=running.pk).update(status=Run.Status.SUCCESS)
        with _tasks() as task:
            run_concurrency.release(script.pk)
        task.assert_called_once()

    def test_back_to_back_windows_block_until_the_last_one_ends(self):
        now = timezone.now()
        self._window(name="A", ends_at=now + datetime.timedelta(minutes=30))
        self._window(name="B", starts_at=now + datetime.timedelta(minutes=20),
                     ends_at=now + datetime.timedelta(minutes=90))
        block = maintenance.block_for(self.ws.id, "scheduled")
        self.assertEqual(block.window.name, "A")
        self.assertEqual(block.until, now + datetime.timedelta(minutes=90))
        self.assertGreater(block.retry_after(), 89 * 60)

    def test_retry_after_is_at_least_one_second(self):
        block = maintenance.Block(window=None, until=timezone.now() - datetime.timedelta(seconds=5))
        self.assertEqual(block.retry_after(), 1)

    def test_trigger_classes(self):
        script = self._script()
        cases = [
            (manual_event(None), Run.TriggerType.MANUAL, "manual"),
            (plugin_event(None, "job"), Run.TriggerType.MANUAL, "manual"),
            ({"kind": "schedule"}, Run.TriggerType.SCHEDULED, "scheduled"),
            (webhook_event({"method": "GET", "query": {}}), Run.TriggerType.API, "webhook"),
            ({"kind": "channel"}, Run.TriggerType.API, "channel"),
            ({"kind": "api"}, Run.TriggerType.API, "api"),
            (None, Run.TriggerType.API, "api"),
            (None, Run.TriggerType.SCHEDULED, "scheduled"),
        ]
        for event, trigger_type, expected in cases:
            run = Run(script=script, event=event, trigger_type=trigger_type)
            self.assertEqual(maintenance.trigger_class(run), expected, (event, trigger_type))

    def test_a_run_with_no_event_is_classed_by_its_trigger_type(self):
        # queue_script_run stamps the default event first.
        self._window(blocks=["api"])
        run = self._run(self._script(), trigger_type=Run.TriggerType.API)
        with _tasks():
            queue_script_run(run)
        self._assert_blocked(run)


class TriggerTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        _setup_wizard_off(self)
        self.user = User.objects.create(email="maint@example.com")
        WorkspaceMembership.objects.update_or_create(
            user=self.user, workspace=self.ws, defaults={"role": WorkspaceMembership.ROLE_OWNER}
        )

    def test_schedule_tick_inside_a_window_is_skipped_and_logged(self):
        script = self._script()
        ScriptSchedule.objects.create(
            script=script, workspace=self.ws, run_mode="interval", interval_minutes=60, is_active=True
        )
        self._window(name="Nightly")
        with _tasks() as task, self.assertLogs("core.tasks", "INFO") as logs:
            result = execute_scheduled_run(str(script.id))
        task.assert_not_called()
        self.assertTrue(result["success"])
        self.assertEqual(result["status"], Run.Status.CANCELLED)
        self._assert_blocked(Run.objects.get(pk=result["run_id"]), "Nightly")
        self.assertTrue(any("skipped - maintenance window" in line for line in logs.output))

    def test_webhook_gets_503_with_retry_after(self):
        script = self._script(webhook_token="tok_" + uuid.uuid4().hex)
        window = self._window(minutes_left=30)
        with _tasks() as task:
            resp = self.client.post(
                reverse("webhook_trigger", args=[script.webhook_token]),
                data='{"x": 1}', content_type="application/json",
            )
        task.assert_not_called()
        self.assertEqual(resp.status_code, 503)
        retry_after = int(resp["Retry-After"])
        self.assertTrue(29 * 60 < retry_after <= 30 * 60, retry_after)
        body = resp.json()
        run = Run.objects.get(script=script)
        self.assertEqual(body["status"], "maintenance")
        self.assertEqual(body["run_id"], str(run.pk))
        self.assertEqual(datetime.datetime.fromisoformat(body["until"]), window.ends_at)
        self._assert_blocked(run)

    def test_webhook_outside_a_window_is_unchanged(self):
        script = self._script(webhook_token="tok_" + uuid.uuid4().hex)
        self._window(blocks=["scheduled"])
        with _tasks():
            resp = self.client.post(reverse("webhook_trigger", args=[script.webhook_token]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "queued")
        self.assertNotIn("Retry-After", resp)

    def test_run_now_shows_a_message_when_manual_runs_are_blocked(self):
        script = self._script()
        self._window(name="Freeze", blocks=["manual"])
        self.client.force_login(self.user)
        with _tasks() as task:
            resp = self.client.post(reverse("cpanel:script_run", args=[script.pk]), follow=True)
        task.assert_not_called()
        # Messages render as a Toast call: the quotes come out JSON-escaped.
        self.assertContains(resp, r"Not started: the maintenance window \u0022Freeze\u0022 blocks manual runs until")
        self.assertContains(resp, "skipped: maintenance window")
        self.assertContains(resp, "a maintenance window blocked this kind of trigger")
        self._assert_blocked(Run.objects.get(script=script), "Freeze")

    def test_run_now_is_not_blocked_by_default(self):
        script = self._script()
        self._window()
        self.client.force_login(self.user)
        with _tasks() as task:
            self.client.post(reverse("cpanel:script_run", args=[script.pk]))
        task.assert_called_once()

    def test_chat_message_gets_a_reply(self):
        from core.services.channels.base import InboundMessage
        from core.services.channels.handlers import _script_handler

        EncryptionService.reset()
        self.addCleanup(EncryptionService.reset)
        script = self._script()
        channel = Channel(
            workspace=self.ws, provider="telegram", name="Bot",
            inbound_enabled=True, inbound_handler="script", inbound_target_id=script.id,
        )
        channel.set_credentials({"bot_token": "1:2"}, identity="1:2")
        channel.save()
        self._window(name="Upgrade")
        msg = InboundMessage(
            channel_id=str(channel.id), provider="telegram", text="hi",
            sender={"id": 8}, reply_ref={"chat_id": 8}, raw={},
        )
        with _tasks() as task:
            reply = _script_handler(channel, msg)
        task.assert_not_called()
        self.assertIn('maintenance window "Upgrade" blocks chat messages until', reply.text)
        self.assertEqual(reply.reply_ref, {"chat_id": 8})
        self._assert_blocked(Run.objects.get(script=script))

    def test_plugin_queue_run_returns_the_run_cancelled_and_latest_run_passes_over_it(self):
        api = ScriptAPI("maintplugin")
        api.upsert(key="job", name=f"job{uuid.uuid4().hex[:6]}", code="pass", environment=self.env)
        with _tasks():
            first = api.queue_run("job")
        self._window(blocks=["manual"])
        with _tasks() as task:
            run = api.queue_run("job")
        task.assert_not_called()
        self.assertEqual(run.cancel_reason, "maintenance")
        self.assertEqual(api.latest_run("job").id, str(first.pk))

    def test_a_due_retry_inside_a_window_is_dropped_and_the_failed_attempt_notifies(self):
        script = self._script(retry_max_attempts=2)
        first = Run.objects.create(
            script=script, workspace=self.ws, status=Run.Status.FAILED,
            trigger_type=Run.TriggerType.SCHEDULED, event={"kind": "schedule", "received_at": "x"},
            ended_at=timezone.now(),
        )
        retry = Run.objects.create(
            script=script, workspace=self.ws, status=Run.Status.PENDING,
            trigger_type=Run.TriggerType.SCHEDULED, event=first.event, attempt=2,
            retry_of=first, next_attempt_at=timezone.now() - datetime.timedelta(seconds=1),
            code_snapshot=script.code,
        )
        self._window()
        with _tasks() as task, mock.patch(
            "core.services.notification_service.NotificationService.send_notification"
        ) as notify:
            started = run_retries.dispatch_due()
        self.assertEqual(started, 0)
        task.assert_not_called()
        self._assert_blocked(retry)
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[0].pk, first.pk)


# --------------------------------------------------------------------------- #
# Console
# --------------------------------------------------------------------------- #


class ConsoleTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        _setup_wizard_off(self)
        self.admin = User.objects.create(email="maint-admin@example.com", is_superuser=True, is_staff=True)
        WorkspaceMembership.objects.update_or_create(
            user=self.admin, workspace=self.ws, defaults={"role": WorkspaceMembership.ROLE_OWNER}
        )
        self.team = Workspace.objects.create(name="Team")
        self.owner = User.objects.create(email="maint-owner@example.com")
        self.member = User.objects.create(email="maint-member@example.com")
        self.outsider = User.objects.create(email="maint-out@example.com")
        WorkspaceMembership.objects.create(user=self.owner, workspace=self.team, role=WorkspaceMembership.ROLE_ADMIN)
        WorkspaceMembership.objects.create(user=self.member, workspace=self.team, role=WorkspaceMembership.ROLE_MEMBER)
        WorkspaceMembership.objects.update_or_create(
            user=self.outsider, workspace=self.ws, defaults={"role": WorkspaceMembership.ROLE_MEMBER}
        )

    def _weekly_post(self, **overrides):
        data = {
            "name": "Sunday upgrade", "kind": "weekly", "timezone": "Europe/Paris",
            "weekdays": ["6"], "start_time": "02:00", "end_time": "04:00",
            "blocks": ["scheduled", "webhook"], "enabled": "on",
        }
        data.update(overrides)
        return data

    def test_global_list_is_for_superusers(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("cpanel:maintenance_list")).status_code, 403)
        self.assertEqual(self.client.post(reverse("cpanel:maintenance_create"), self._weekly_post()).status_code, 403)
        self.assertFalse(MaintenanceWindow.objects.exists())

    def test_superuser_creates_a_global_weekly_window_and_sees_the_next_occurrence(self):
        self.client.force_login(self.admin)
        resp = self.client.post(reverse("cpanel:maintenance_create"), self._weekly_post())
        self.assertRedirects(resp, reverse("cpanel:maintenance_list"), fetch_redirect_response=False)
        w = MaintenanceWindow.objects.get()
        self.assertIsNone(w.workspace)
        self.assertEqual((w.kind, w.weekdays, w.timezone), ("weekly", [6], "Europe/Paris"))
        self.assertEqual((w.start_time, w.end_time), (datetime.time(2), datetime.time(4)))
        self.assertEqual(w.blocks, ["scheduled", "webhook"])
        self.assertIsNone(w.starts_at)

        page = self.client.get(reverse("cpanel:maintenance_list"))
        self.assertContains(page, "Sunday upgrade")
        self.assertContains(page, "Weekly on Sun, 02:00 to 04:00 (Europe/Paris)")
        self.assertContains(page, "Blocks scheduled runs and webhook calls")
        start, end = w.next_occurrence()
        self.assertContains(page, f"{w.format_local(start)} to {w.format_local(end)} (Europe/Paris)")
        edit = self.client.get(reverse("cpanel:maintenance_edit", args=[w.pk]))
        self.assertContains(edit, "Next occurrence:")
        self.assertContains(edit, w.format_local(start))
        self.assertContains(edit, "Delete window")

    def test_new_window_page_is_a_create_page(self):
        # Found in the 2.5 test pass: the UUID pk default made the unsaved
        # window look saved (blank title, "Edit", a Delete card to a 404).
        self.client.force_login(self.admin)
        for url in (
            reverse("cpanel:maintenance_create"),
            reverse("cpanel:workspace_maintenance_create", args=[self.team.pk]),
        ):
            page = self.client.get(url)
            self.assertContains(page, "<title>New maintenance window - ", html=False)
            self.assertNotContains(page, "Next occurrence:")
            self.assertNotContains(page, "Delete window")
            self.assertNotContains(page, "/delete/")

    def test_one_off_times_are_read_in_the_chosen_time_zone(self):
        self.client.force_login(self.admin)
        year = timezone.now().year + 1
        resp = self.client.post(reverse("cpanel:maintenance_create"), {
            "name": "Migration", "kind": "one_off", "timezone": "Asia/Tokyo",
            "starts_at_local": f"{year}-12-01T02:00", "ends_at_local": f"{year}-12-01T04:30",
            "blocks": ["manual"], "enabled": "on",
            # Fields of the other kind are ignored and cleared.
            "weekdays": ["1"], "start_time": "01:00", "end_time": "02:00",
        })
        self.assertEqual(resp.status_code, 302, resp.content)
        w = MaintenanceWindow.objects.get()
        self.assertEqual(w.starts_at, datetime.datetime(year, 11, 30, 17, tzinfo=UTC))
        self.assertEqual(w.ends_at, datetime.datetime(year, 11, 30, 19, 30, tzinfo=UTC))
        self.assertEqual((w.weekdays, w.start_time, w.end_time), ([], None, None))
        # Editing shows the local times back.
        edit = self.client.get(reverse("cpanel:maintenance_edit", args=[w.pk]))
        self.assertContains(edit, f'value="{year}-12-01T02:00"')

    def test_one_off_times_ignore_the_consoles_own_time_zone(self):
        # 02:30 on 2030-03-31 doesn't exist in Berlin (clocks jump 02:00 to
        # 03:00), but it does in Tokyo, the window's zone.
        from core.models import GlobalSettings

        settings = GlobalSettings.get_settings()
        settings.timezone = "Europe/Berlin"
        settings.save(update_fields=["timezone"])
        self.client.force_login(self.admin)
        resp = self.client.post(reverse("cpanel:maintenance_create"), {
            "name": "Gap", "kind": "one_off", "timezone": "Asia/Tokyo",
            "starts_at_local": "2030-03-31T02:30", "ends_at_local": "2030-03-31T03:30",
            "blocks": ["manual"], "enabled": "on",
        })
        self.assertEqual(resp.status_code, 302, resp.content)
        self.assertEqual(MaintenanceWindow.objects.get().starts_at, _utc(2030, 3, 30, 17, 30))

    def test_invalid_windows_are_refused_on_the_field(self):
        self.client.force_login(self.admin)
        url = reverse("cpanel:maintenance_create")
        resp = self.client.post(url, self._weekly_post(weekdays=[], blocks=[]))
        self.assertEqual(resp.status_code, 200)
        self.assertFormError(resp.context["form"], "weekdays", "Choose at least one day.")
        self.assertFormError(resp.context["form"], "blocks", "Choose at least one kind of trigger to block.")
        resp = self.client.post(url, {
            "name": "x", "kind": "one_off", "timezone": "UTC", "blocks": ["manual"],
            "starts_at_local": "2030-01-02T05:00", "ends_at_local": "2030-01-02T04:00",
        })
        self.assertFormError(resp.context["form"], "ends_at_local", "The end must be after the start.")
        resp = self.client.post(url, {
            "name": "x", "kind": "one_off", "timezone": "UTC", "blocks": ["manual"],
            "starts_at_local": "2020-01-02T03:00", "ends_at_local": "2020-01-02T04:00",
        })
        self.assertFormError(resp.context["form"], "ends_at_local", "This window would already be over.")
        resp = self.client.post(url, self._weekly_post(start_time="", end_time=""))
        self.assertFormError(resp.context["form"], "start_time", "Enter the time the window starts.")
        self.assertFalse(MaintenanceWindow.objects.exists())

    def test_edit_disable_enable_and_delete(self):
        w = self._window(name="Old")
        self.client.force_login(self.admin)
        resp = self.client.post(reverse("cpanel:maintenance_edit", args=[w.pk]), self._weekly_post(name="New"))
        self.assertEqual(resp.status_code, 302)
        w.refresh_from_db()
        self.assertEqual((w.name, w.kind, w.starts_at, w.ends_at), ("New", "weekly", None, None))

        self.client.post(reverse("cpanel:maintenance_toggle", args=[w.pk]))
        w.refresh_from_db()
        self.assertFalse(w.enabled)
        self.assertContains(self.client.get(reverse("cpanel:maintenance_list")), "Disabled")
        self.client.post(reverse("cpanel:maintenance_toggle", args=[w.pk]))
        w.refresh_from_db()
        self.assertTrue(w.enabled)
        self.assertEqual(self.client.get(reverse("cpanel:maintenance_toggle", args=[w.pk])).status_code, 405)

        self.client.post(reverse("cpanel:maintenance_delete", args=[w.pk]))
        self.assertFalse(MaintenanceWindow.objects.exists())

    def test_workspace_admins_manage_their_workspace_windows(self):
        self.client.force_login(self.owner)
        url = reverse("cpanel:workspace_maintenance_create", args=[self.team.pk])
        resp = self.client.post(url, self._weekly_post(name="Team freeze"))
        self.assertRedirects(
            resp, reverse("cpanel:workspace_maintenance_list", args=[self.team.pk]),
            fetch_redirect_response=False,
        )
        w = MaintenanceWindow.objects.get()
        self.assertEqual(w.workspace, self.team)
        self._window(name="Instance-wide")
        page = self.client.get(reverse("cpanel:workspace_maintenance_list", args=[self.team.pk]))
        self.assertContains(page, "Team freeze")
        # Global windows are listed read-only.
        self.assertContains(page, "Instance-wide")
        self.assertContains(page, "An instance admin manages them.")
        self.client.post(reverse("cpanel:workspace_maintenance_toggle", args=[self.team.pk, w.pk]))
        w.refresh_from_db()
        self.assertFalse(w.enabled)

    def test_workspace_pages_gate_like_the_other_workspace_pages(self):
        url = reverse("cpanel:workspace_maintenance_list", args=[self.team.pk])
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_a_workspace_url_reaches_only_that_workspaces_windows(self):
        global_window = self._window(name="Global")
        default_window = self._window(name="Default", workspace=self.ws)
        self.client.force_login(self.owner)
        for w in (global_window, default_window):
            for name in ("workspace_maintenance_edit", "workspace_maintenance_toggle", "workspace_maintenance_delete"):
                resp = self.client.post(reverse(f"cpanel:{name}", args=[self.team.pk, w.pk]), self._weekly_post())
                self.assertEqual(resp.status_code, 404, (name, w.name))
        self.assertEqual(MaintenanceWindow.objects.count(), 2)
        self.assertTrue(MaintenanceWindow.objects.get(pk=global_window.pk).enabled)

    def test_dashboard_banner_while_a_window_is_on(self):
        self.client.force_login(self.admin)
        resp = self.client.get(reverse("cpanel:dashboard"))
        self.assertNotContains(resp, "Maintenance until")
        w = self._window(name="Upgrade", blocks=["scheduled", "webhook"])
        resp = self.client.get(reverse("cpanel:dashboard"))
        self.assertContains(resp, "Maintenance until")
        self.assertContains(resp, "Scheduled runs and webhook calls don't start")
        self.assertContains(resp, 'window "Upgrade"')
        self.assertContains(resp, "Runs already going continue.")
        self.assertContains(resp, reverse("cpanel:workspace_maintenance_list", args=[self.ws.pk]))
        # A plain member sees the banner, without the link.
        self.client.force_login(self.outsider)
        resp = self.client.get(reverse("cpanel:dashboard"))
        self.assertContains(resp, "Maintenance until")
        self.assertNotContains(resp, reverse("cpanel:workspace_maintenance_list", args=[self.ws.pk]))
        # Disabled: gone.
        W.objects.filter(pk=w.pk).update(enabled=False)
        self.assertNotContains(self.client.get(reverse("cpanel:dashboard")), "Maintenance until")

    def test_banner_until_follows_back_to_back_windows(self):
        now = timezone.now()
        self._window(name="A", ends_at=now + datetime.timedelta(minutes=30))
        self._window(name="B", blocks=["manual"], starts_at=now + datetime.timedelta(minutes=10),
                     ends_at=now + datetime.timedelta(hours=2))
        banner = maintenance.banner(self.ws.id)
        self.assertEqual([w.name for w in banner.windows], ["A"])
        self.assertEqual(banner.until, now + datetime.timedelta(hours=2))
        self.assertEqual(banner.blocks, "scheduled runs, webhook calls, chat messages and watcher runs")

    def test_settings_and_workspace_list_link_to_the_pages(self):
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("cpanel:settings")), reverse("cpanel:maintenance_list"))
        self.assertContains(
            self.client.get(reverse("cpanel:workspace_list")),
            reverse("cpanel:workspace_maintenance_list", args=[self.team.pk]),
        )


# --------------------------------------------------------------------------- #
# Backups and workspaces
# --------------------------------------------------------------------------- #


class BackupTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create(email="maint-backup@example.com", is_superuser=True)

    def test_backup_round_trips_global_and_workspace_windows(self):
        team = Workspace.objects.create(name="Team")
        weekly = MaintenanceWindow.objects.create(
            name="Weekly", kind=W.Kind.WEEKLY, weekdays=[5, 6], timezone="Europe/Paris",
            start_time=datetime.time(23, 30), end_time=datetime.time(1, 0),
            blocks=["scheduled", "api"], enabled=False,
        )
        one_off = MaintenanceWindow.objects.create(
            name="Once", kind=W.Kind.ONE_OFF, workspace=team, timezone="Asia/Tokyo",
            starts_at=_utc(2027, 1, 1, 2), ends_at=_utc(2027, 1, 1, 4), blocks=["manual"],
        )
        data = BackupService.create_backup(include_runs=False)
        rows = {r["name"]: r for r in data["maintenance_windows"]}
        self.assertIsNone(rows["Weekly"]["workspace_id"])
        self.assertEqual(rows["Once"]["workspace_id"], str(team.pk))

        MaintenanceWindow.objects.all().delete()
        MaintenanceWindow.objects.create(name="Stray")
        result = BackupService.restore_backup(data, restore_runs=False, current_user=self.user)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["counts"]["maintenance_windows"], 2)
        self.assertFalse(MaintenanceWindow.objects.filter(name="Stray").exists())

        w = MaintenanceWindow.objects.get(pk=weekly.pk)
        self.assertIsNone(w.workspace)
        self.assertEqual(
            (w.kind, w.weekdays, w.timezone, w.start_time, w.end_time, w.blocks, w.enabled),
            ("weekly", [5, 6], "Europe/Paris", datetime.time(23, 30), datetime.time(1, 0), ["scheduled", "api"], False),
        )
        self.assertEqual(w.created_at, weekly.created_at)
        o = MaintenanceWindow.objects.get(pk=one_off.pk)
        self.assertEqual(o.workspace.name, "Team")
        self.assertEqual((o.starts_at, o.ends_at, o.blocks), (_utc(2027, 1, 1, 2), _utc(2027, 1, 1, 4), ["manual"]))

    def test_an_unknown_time_zone_is_restored_as_utc_with_a_warning(self):
        MaintenanceWindow.objects.create(name="Far", timezone="Asia/Tokyo")
        data = BackupService.create_backup(include_runs=False)
        data["maintenance_windows"][0]["timezone"] = "Mars/Olympus"
        result = BackupService.restore_backup(data, restore_runs=False, current_user=self.user)
        self.assertTrue(result["success"], result)
        self.assertEqual(MaintenanceWindow.objects.get().timezone, "UTC")
        self.assertTrue(any('"Far"' in w and "Mars/Olympus" in w for w in result["warnings"]), result["warnings"])

    def test_an_older_backup_leaves_the_windows_alone(self):
        MaintenanceWindow.objects.create(name="Keep")
        data = BackupService.create_backup(include_runs=False)
        data.pop("maintenance_windows")
        result = BackupService.restore_backup(data, restore_runs=False, current_user=self.user)
        self.assertTrue(result["success"], result)
        self.assertNotIn("maintenance_windows", result["counts"])
        self.assertTrue(MaintenanceWindow.objects.filter(name="Keep").exists())

    def test_the_cancel_reason_round_trips(self):
        script = self._script()
        run = Run.objects.create(
            script=script, workspace=self.ws, status=Run.Status.CANCELLED, cancel_reason=MAINTENANCE,
        )
        data = BackupService.create_backup(include_runs=True)
        BackupService.restore_backup(data, restore_runs=True, current_user=self.user)
        self.assertEqual(Run.objects.get(pk=run.pk).cancel_reason, MAINTENANCE)

    def test_deleting_a_workspace_deletes_its_windows_never_makes_them_global(self):
        team = Workspace.objects.create(name="Gone")
        MaintenanceWindow.objects.create(name="Team's", workspace=team)
        MaintenanceWindow.objects.create(name="Global")
        team.delete()
        self.assertEqual(list(MaintenanceWindow.objects.values_list("name", flat=True)), ["Global"])
