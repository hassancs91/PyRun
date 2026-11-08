"""
Concurrency policy per script (2.5 Stage 1, ticket T05).

What this pins, roughly in order of how much it would hurt to get wrong:

- ``allow`` (the default) changes nothing: every trigger queues its task as
  before, with the same task arguments.
- ``skip``: a run triggered while another is in flight is created, then
  marked CANCELLED with ``cancel_reason = concurrency_skip``, and no task is
  queued. ``queue``: it stays PENDING with no task (held), ``queued_behind``
  set, and starts when the runs ahead of it end, one at a time, oldest first,
  with the webhook / chat data it arrived with, byte for byte.
- A held run is released by the executor's finalize, by Stop, by the
  stale-run reconciler, and by the worker heartbeat's backstop; a release
  never starts two runs, and a failed release doesn't leave the queue stuck.
- Every trigger obeys it: webhook, schedule, Run now, chat channel and the
  plugin SDK's ``queue_run``.
- The editor, the runs list and the run page show it; backups and forks
  carry it.
"""

import datetime
import json
import sys
import uuid
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.executor import execute_run
from core.forms import ScriptForm
from core.models import (
    Channel,
    Environment,
    GlobalSettings,
    Run,
    Script,
    ScriptSchedule,
    Workspace,
    WorkspaceMembership,
)
from core.plugins.api import RunView, ScriptAPI
from core.services import EncryptionService, run_concurrency
from core.services.backup_service import BackupService
from core.services.script_service import fork_script
from core.services.task_service import TaskService
from core.tasks import execute_scheduled_run, queue_script_run, worker_heartbeat_task

User = get_user_model()

ALLOW = Script.ConcurrencyPolicy.ALLOW
SKIP = Script.ConcurrencyPolicy.SKIP
QUEUE = Script.ConcurrencyPolicy.QUEUE
SKIPPED = Run.CancelReason.CONCURRENCY_SKIP

_TG_UPDATE = {
    "update_id": 11,
    "message": {
        "message_id": 2,
        "from": {"id": 8, "username": "bo", "first_name": "Bo"},
        "chat": {"id": 8, "type": "private"},
        "text": "run it",
    },
}


def _setup_wizard_off(test):
    for target in (
        "core.services.setup_service.SetupService.is_setup_needed",
        "core.services.setup_service.SetupService.needs_admin_setup",
    ):
        p = mock.patch(target, return_value=False)
        p.start()
        test.addCleanup(p.stop)


def _tasks():
    """Patch the queue: each call returns a distinct task id."""
    counter = iter(range(1, 10_000))
    return mock.patch("core.tasks.async_task", side_effect=lambda *a, **k: f"task-{next(counter)}")


class _Fixture:
    def setUp(self):
        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(name="cenv", path=f"cenv{uuid.uuid4().hex[:6]}")

    def _script(self, policy=ALLOW, **kwargs):
        kwargs.setdefault("code", "pass")
        return Script.objects.create(
            name=f"s{uuid.uuid4().hex[:6]}",
            environment=self.env,
            workspace=self.ws,
            concurrency_policy=policy,
            **kwargs,
        )

    def _run(self, script, status=Run.Status.PENDING, **kwargs):
        return Run.objects.create(script=script, workspace=self.ws, status=status, **kwargs)

    def _queue(self, script, webhook_data=None):
        run = self._run(script)
        with _tasks() as task:
            queue_script_run(run, webhook_data=webhook_data)
        run.refresh_from_db()
        return run, task

    def _assert_skipped(self, run):
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.CANCELLED)
        self.assertEqual(run.cancel_reason, SKIPPED)
        self.assertIsNotNone(run.ended_at)
        self.assertEqual(run.task_id, "")
        self.assertTrue(run.is_skipped)

    def _assert_held(self, run, behind):
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.PENDING)
        self.assertEqual(run.task_id, "")
        self.assertEqual(run.queued_behind, behind.pk)
        self.assertTrue(run.is_held)


# --------------------------------------------------------------------------- #
# The funnel: allow / skip / queue
# --------------------------------------------------------------------------- #


class FunnelTests(_Fixture, TestCase):
    def test_allow_is_the_default_and_queues_every_run_as_before(self):
        script = Script.objects.create(
            name="plain", environment=self.env, workspace=self.ws, code="pass"
        )
        self.assertEqual(script.concurrency_policy, ALLOW)
        self._run(script, status=Run.Status.RUNNING)
        data = {"method": "POST", "query": {}, "content_type": "", "body": "x"}
        run, task = self._queue(script, webhook_data=data)
        self.assertEqual(run.status, Run.Status.PENDING)
        self.assertEqual(run.task_id, "task-1")
        self.assertEqual(run.cancel_reason, "")
        self.assertIsNone(run.queued_behind)
        self.assertEqual(task.call_args.args, ("core.tasks.execute_run_task", str(run.pk), data))

    def test_skip_cancels_the_new_run_when_one_is_running(self):
        script = self._script(SKIP)
        self._run(script, status=Run.Status.RUNNING)
        run = self._run(script)
        with _tasks() as task:
            self.assertEqual(queue_script_run(run), "")
        task.assert_not_called()
        self.assertTrue(run.is_skipped)  # the caller's instance says so too
        self._assert_skipped(run)

    def test_skip_counts_a_queued_run_that_has_not_started(self):
        script = self._script(SKIP)
        self._run(script, task_id="waiting-for-a-worker")
        run, task = self._queue(script)
        task.assert_not_called()
        self._assert_skipped(run)

    def test_skip_runs_when_nothing_is_in_flight(self):
        script = self._script(SKIP)
        for status in (Run.Status.SUCCESS, Run.Status.FAILED, Run.Status.CANCELLED):
            self._run(script, status=status)
        run, task = self._queue(script)
        task.assert_called_once()
        self.assertEqual(run.status, Run.Status.PENDING)
        self.assertEqual(run.task_id, "task-1")

    def test_skip_ignores_other_scripts(self):
        self._run(self._script(SKIP), status=Run.Status.RUNNING)
        run, task = self._queue(self._script(SKIP))
        task.assert_called_once()
        self.assertEqual(run.cancel_reason, "")

    def test_queue_holds_the_new_run_behind_the_running_one(self):
        script = self._script(QUEUE)
        running = self._run(script, status=Run.Status.RUNNING)
        run = self._run(script)
        with _tasks() as task:
            self.assertEqual(queue_script_run(run), "")
        task.assert_not_called()
        self.assertTrue(run.is_held)
        self._assert_held(run, running)

    def test_queued_runs_form_a_line(self):
        script = self._script(QUEUE)
        running = self._run(script, status=Run.Status.RUNNING)
        first, _ = self._queue(script)
        second, _ = self._queue(script)
        self._assert_held(first, running)
        self._assert_held(second, first)

    def test_a_held_run_keeps_its_stamped_inputs(self):
        script = self._script(
            QUEUE,
            inputs_schema={
                "type": "object",
                "properties": {"n": {"type": "integer", "default": 3}},
            },
        )
        self._run(script, status=Run.Status.RUNNING)
        run, _ = self._queue(script)
        self.assertEqual(run.inputs, {"n": 3})
        self.assertIsNotNone(run.event)

    def test_a_run_only_waits_for_runs_created_before_it(self):
        # Two triggers at once: both rows exist before either is queued. The
        # later one sees the earlier one; the earlier one never waits for it.
        script = self._script(SKIP)
        earlier = self._run(script)
        later = self._run(script)
        with _tasks() as task:
            queue_script_run(later)
            queue_script_run(earlier)
        self._assert_skipped(later)
        earlier.refresh_from_db()
        self.assertEqual(earlier.status, Run.Status.PENDING)
        self.assertEqual(task.call_count, 1)
        self.assertEqual(task.call_args.args[1], str(earlier.pk))

    def test_runs_created_in_the_same_instant_are_ordered_by_id(self):
        script = self._script(SKIP)
        a, b = self._run(script), self._run(script)
        same = timezone.now()
        Run.objects.filter(pk__in=[a.pk, b.pk]).update(created_at=same)
        low, high = sorted([a, b], key=lambda r: str(r.pk))
        with _tasks():
            queue_script_run(Run.objects.get(pk=high.pk))
            queue_script_run(Run.objects.get(pk=low.pk))
        self._assert_skipped(high)
        self.assertEqual(Run.objects.get(pk=low.pk).status, Run.Status.PENDING)

    def test_an_orphaned_unqueued_run_stops_blocking_after_the_grace(self):
        # A row that never got its task (its creator died, or a restore brought
        # it back) blocks for a few minutes at most, not until the 24 h reconcile.
        script = self._script(SKIP)
        orphan = self._run(script)
        Run.objects.filter(pk=orphan.pk).update(
            created_at=timezone.now() - run_concurrency.UNQUEUED_GRACE - datetime.timedelta(seconds=5)
        )
        run, task = self._queue(script)
        task.assert_called_once()
        self.assertEqual(run.cancel_reason, "")

    def test_a_run_that_is_no_longer_pending_is_refused_as_before(self):
        script = self._script(QUEUE)
        run = self._run(script, status=Run.Status.CANCELLED)
        with self.assertRaises(ValueError):
            queue_script_run(run)


# --------------------------------------------------------------------------- #
# Releasing a held run
# --------------------------------------------------------------------------- #


class ReleaseTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.script = self._script(QUEUE)
        self.running = self._run(self.script, status=Run.Status.RUNNING, started_at=timezone.now())
        self.body = '{"b": 1, "a": 2}'
        self.data = {
            "method": "POST",
            "query": {"z": ["1"], "a": ["2"]},
            "content_type": "application/json",
            "body": self.body,
            "body_json": {"b": 1, "a": 2},
        }
        self.held, _ = self._queue(self.script, webhook_data=self.data)
        self.second, _ = self._queue(self.script)

    def _finish_running(self):
        Run.objects.filter(pk=self.running.pk).update(
            status=Run.Status.SUCCESS, ended_at=timezone.now()
        )

    def test_nothing_starts_while_the_run_ahead_is_going(self):
        with _tasks() as task:
            self.assertIsNone(run_concurrency.release(self.script.pk))
        task.assert_not_called()
        self._assert_held(self.held, self.running)

    def test_the_oldest_held_run_starts_with_its_request_data(self):
        self._finish_running()
        with _tasks() as task:
            started = run_concurrency.release(self.script.pk)
        self.assertEqual(started.pk, self.held.pk)
        task.assert_called_once()
        # The task gets the same webhook data a direct queue would have:
        # same values AND the same key order (WEBHOOK_BODY_JSON byte for byte).
        passed = task.call_args.args[2]
        self.assertEqual(json.dumps(passed), json.dumps(self.data))
        self.held.refresh_from_db()
        self.assertEqual(self.held.task_id, "task-1")
        self.assertEqual(self.held.queued_payload, "")
        self.assertFalse(self.held.is_held)
        self.assertEqual(self.held.queued_behind, self.running.pk)  # kept as history
        self._assert_held(self.second, self.held)

    def test_release_starts_one_run_however_often_it_is_called(self):
        self._finish_running()
        with _tasks() as task:
            run_concurrency.release(self.script.pk)
            self.assertIsNone(run_concurrency.release(self.script.pk))
            self.assertEqual(run_concurrency.release_all(), 0)
        self.assertEqual(task.call_count, 1)

    def test_a_release_that_lost_the_claim_starts_nothing(self):
        self._finish_running()
        # Another process claimed the head between this one's read and its claim.
        real_filter = Run.objects.filter

        def racing_filter(*args, **kwargs):
            if kwargs.get("task_id") == "" and kwargs.get("pk") == self.held.pk:
                Run.objects.all().filter(pk=self.held.pk).update(task_id="other-process")
            return real_filter(*args, **kwargs)

        with _tasks() as task, mock.patch.object(Run.objects, "filter", side_effect=racing_filter):
            self.assertIsNone(run_concurrency.release(self.script.pk))
        task.assert_not_called()
        self._assert_held(self.second, self.held)

    def test_a_release_that_cannot_queue_fails_the_run_instead_of_blocking(self):
        self._finish_running()
        with mock.patch("core.tasks.async_task", side_effect=RuntimeError("broker down")),                 self.assertLogs("core.services.run_concurrency", "ERROR"):
            self.assertIsNone(run_concurrency.release(self.script.pk))
        self.held.refresh_from_db()
        self.assertEqual(self.held.status, Run.Status.FAILED)
        self.assertIn("broker down", self.held.stderr)
        self.assertEqual(self.held.task_id, "")
        with _tasks() as task:
            self.assertEqual(run_concurrency.release(self.script.pk).pk, self.second.pk)
        task.assert_called_once()

    def test_the_executor_finalize_starts_the_next_run(self):
        Run.objects.filter(pk=self.running.pk).update(status=Run.Status.PENDING, started_at=None)
        running = Run.objects.get(pk=self.running.pk)
        with mock.patch("core.executor._validate_environment", return_value=sys.executable), \
                mock.patch("core.executor.ClaudeService.get_script_env", return_value={}), \
                _tasks() as task:
            execute_run(running)
        running.refresh_from_db()
        self.assertEqual(running.status, Run.Status.SUCCESS, running.stderr)
        task.assert_called_once()
        self.assertEqual(task.call_args.args[1], str(self.held.pk))

    def test_a_failed_release_never_breaks_the_finished_run(self):
        Run.objects.filter(pk=self.running.pk).update(status=Run.Status.PENDING, started_at=None)
        running = Run.objects.get(pk=self.running.pk)
        with mock.patch("core.executor._validate_environment", return_value=sys.executable), \
                mock.patch("core.executor.ClaudeService.get_script_env", return_value={}), \
                mock.patch.object(run_concurrency, "release", side_effect=RuntimeError("boom")), \
                self.assertLogs("core.services.run_concurrency", "ERROR"):
            execute_run(running)
        running.refresh_from_db()
        self.assertEqual(running.status, Run.Status.SUCCESS)

    def test_stopping_the_running_run_starts_the_next(self):
        with _tasks() as task, mock.patch("core.executor._kill_process_tree"):
            # A graceful stop: the run is still shutting down, so the next
            # one waits (the worker releases it when the process has ended).
            changed, _ = TaskService.force_stop_run(Run.objects.get(pk=self.running.pk))
            self.assertTrue(changed)
            task.assert_not_called()
            # Stopping it again kills it now, and the next run starts.
            changed, _ = TaskService.force_stop_run(Run.objects.get(pk=self.running.pk))
        self.assertTrue(changed)
        self.assertEqual(task.call_args.args[1], str(self.held.pk))

    def test_cancelling_a_held_run_drops_its_data_and_keeps_the_line(self):
        with _tasks() as task:
            changed, _ = TaskService.force_stop_run(Run.objects.get(pk=self.held.pk))
        self.assertTrue(changed)
        task.assert_not_called()  # the running run is still ahead
        self.held.refresh_from_db()
        self.assertEqual(self.held.status, Run.Status.CANCELLED)
        self.assertEqual(self.held.queued_payload, "")
        self._finish_running()
        with _tasks() as task:
            self.assertEqual(run_concurrency.release(self.script.pk).pk, self.second.pk)

    def test_cancel_held_leaves_a_run_that_has_started_alone(self):
        self._finish_running()
        with _tasks():
            run_concurrency.release(self.script.pk)  # held -> has a task now
        self.assertFalse(run_concurrency.cancel_held(Run.objects.get(pk=self.held.pk)))
        self.held.refresh_from_db()
        self.assertEqual(self.held.status, Run.Status.PENDING)
        self.assertEqual(self.held.task_id, "task-1")
        self.assertTrue(run_concurrency.cancel_held(self.second))
        self.assertEqual(Run.objects.get(pk=self.second.pk).status, Run.Status.CANCELLED)

    def test_a_stale_running_run_reconciled_releases_the_queue(self):
        GlobalSettings.get_settings()
        long_ago = timezone.now() - datetime.timedelta(
            seconds=self.script.timeout_seconds + Run.RECONCILE_GRACE_SECONDS + 60
        )
        Run.objects.filter(pk=self.running.pk).update(started_at=long_ago)
        with _tasks() as task, mock.patch(
            "core.services.notification_service.NotificationService.send_notification"
        ):
            self.assertEqual(Run.reconcile_stale(), 1)
        self.assertEqual(Run.objects.get(pk=self.running.pk).status, Run.Status.FAILED)
        task.assert_called_once()
        self.assertEqual(task.call_args.args[1], str(self.held.pk))

    def test_the_heartbeat_backstop_starts_a_run_whose_turn_came(self):
        # The run ahead ended without any release (e.g. its worker died after
        # saving the status): the heartbeat picks it up within a minute.
        self._finish_running()
        with _tasks() as task:
            self.assertTrue(worker_heartbeat_task()["success"])
        task.assert_called_once()
        self.assertEqual(task.call_args.args[1], str(self.held.pk))

    def test_the_heartbeat_survives_a_failing_backstop(self):
        with mock.patch.object(run_concurrency, "release_all", side_effect=RuntimeError("x")), \
                self.assertLogs("core.tasks", "ERROR"):
            self.assertTrue(worker_heartbeat_task()["success"])

    def test_a_held_run_is_not_failed_as_lost_after_a_day(self):
        settings = GlobalSettings.get_settings()
        settings.worker_heartbeat_at = timezone.now()
        settings.save(update_fields=["worker_heartbeat_at"])
        a_day_ago = timezone.now() - Run.RECONCILE_PENDING_AFTER - datetime.timedelta(hours=1)
        Run.objects.filter(pk=self.held.pk).update(created_at=a_day_ago)
        lost = self._run(self.script, task_id="lost-task")
        Run.objects.filter(pk=lost.pk).update(created_at=a_day_ago)
        with mock.patch(
            "core.services.notification_service.NotificationService.send_notification"
        ), _tasks():
            Run.reconcile_stale()
        self.assertEqual(Run.objects.get(pk=self.held.pk).status, Run.Status.PENDING)
        self.assertEqual(Run.objects.get(pk=lost.pk).status, Run.Status.FAILED)

    def test_a_run_that_ends_before_the_hold_is_saved_does_not_strand_it(self):
        # The run ahead ends between the check and the hold: the funnel's own
        # release starts the held run right away.
        script = self._script(QUEUE)
        ahead = self._run(script, status=Run.Status.RUNNING)
        real_ahead_of = run_concurrency._ahead_of

        def ahead_then_finish(run):
            found = real_ahead_of(run)
            Run.objects.filter(pk=ahead.pk).update(status=Run.Status.SUCCESS)
            return found

        run = self._run(script)
        with _tasks() as task, mock.patch.object(run_concurrency, "_ahead_of", ahead_then_finish):
            task_id = queue_script_run(run)
        self.assertEqual(task_id, "task-1")
        task.assert_called_once()
        run.refresh_from_db()
        self.assertEqual(run.task_id, "task-1")


# --------------------------------------------------------------------------- #
# Every trigger obeys the policy
# --------------------------------------------------------------------------- #


class TriggerTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        _setup_wizard_off(self)
        self.user = User.objects.create(email="conc@example.com")
        WorkspaceMembership.objects.update_or_create(
            user=self.user, workspace=self.ws, defaults={"role": WorkspaceMembership.ROLE_OWNER}
        )

    def _busy(self, policy, **kwargs):
        script = self._script(policy, is_enabled=True, **kwargs)
        running = self._run(script, status=Run.Status.RUNNING)
        return script, running

    def _new_run(self, script, running):
        return Run.objects.exclude(pk=running.pk).get(script=script)

    # -- webhook ------------------------------------------------------------- #

    def _webhook(self, script):
        url = reverse("webhook_trigger", args=[script.webhook_token])
        with _tasks() as task:
            resp = self.client.post(url + "?k=v", data='{"x": 1}', content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json(), task

    def test_webhook_skip(self):
        script, running = self._busy(SKIP, webhook_token="tok_" + uuid.uuid4().hex)
        body, task = self._webhook(script)
        run = self._new_run(script, running)
        self.assertEqual(body, {
            "status": "skipped", "reason": "already_running",
            "run_id": str(run.pk), "script": script.name,
        })
        task.assert_not_called()
        self._assert_skipped(run)

    def test_webhook_queue_runs_later_with_the_same_variables(self):
        script, running = self._busy(QUEUE, webhook_token="tok_" + uuid.uuid4().hex)
        body, task = self._webhook(script)
        run = self._new_run(script, running)
        self.assertEqual(body, {"status": "queued", "run_id": str(run.pk), "script": script.name})
        task.assert_not_called()
        self._assert_held(run, running)

        # What the task gets for the same call to a script that is free.
        free = self._script(ALLOW, is_enabled=True, webhook_token="tok_" + uuid.uuid4().hex)
        _, free_task = self._webhook(free)
        direct = free_task.call_args.args[2]
        Run.objects.filter(pk=running.pk).update(status=Run.Status.SUCCESS)
        with _tasks() as released:
            self.assertEqual(run_concurrency.release(script.pk).pk, run.pk)
        self.assertEqual(json.dumps(released.call_args.args[2]), json.dumps(direct))

    def test_webhook_allow_response_is_unchanged(self):
        script, running = self._busy(ALLOW, webhook_token="tok_" + uuid.uuid4().hex)
        body, task = self._webhook(script)
        run = self._new_run(script, running)
        self.assertEqual(body, {"status": "queued", "run_id": str(run.pk), "script": script.name})
        task.assert_called_once()

    # -- schedule ------------------------------------------------------------ #

    def _tick(self, script):
        ScriptSchedule.objects.create(
            script=script, workspace=self.ws, run_mode="interval", interval_minutes=60, is_active=True
        )
        with _tasks() as task:
            result = execute_scheduled_run(str(script.id))
        self.assertTrue(result["success"])
        return result, task

    def test_schedule_skip(self):
        script, running = self._busy(SKIP)
        result, task = self._tick(script)
        task.assert_not_called()
        self.assertEqual(result["status"], Run.Status.CANCELLED)
        self._assert_skipped(Run.objects.get(pk=result["run_id"]))

    def test_schedule_queue(self):
        script, running = self._busy(QUEUE)
        result, task = self._tick(script)
        task.assert_not_called()
        self.assertEqual(result["status"], Run.Status.PENDING)
        self._assert_held(Run.objects.get(pk=result["run_id"]), running)

    # -- Run now / Re-run ---------------------------------------------------- #

    def _run_now(self, script):
        self.client.force_login(self.user)
        with _tasks() as task:
            resp = self.client.post(reverse("cpanel:script_run", args=[script.pk]), follow=True)
        return resp, task

    def test_run_now_skip(self):
        script, running = self._busy(SKIP)
        resp, task = self._run_now(script)
        task.assert_not_called()
        run = self._new_run(script, running)
        self._assert_skipped(run)
        self.assertContains(resp, "concurrency policy is Skip")
        self.assertContains(resp, "skipped: already running")

    def test_run_now_queue(self):
        script, running = self._busy(QUEUE)
        resp, task = self._run_now(script)
        task.assert_not_called()
        run = self._new_run(script, running)
        self._assert_held(run, running)
        self.assertContains(resp, "starts when the run ahead of it ends")
        self.assertContains(resp, "Waiting its turn")

    def test_rerun_skip(self):
        script, running = self._busy(SKIP)
        self.client.force_login(self.user)
        with _tasks() as task:
            self.client.post(reverse("cpanel:run_rerun", args=[running.pk]))
        task.assert_not_called()
        self._assert_skipped(self._new_run(script, running))

    # -- chat channel -------------------------------------------------------- #

    def _channel_for(self, script):
        EncryptionService.reset()
        self.addCleanup(EncryptionService.reset)
        channel = Channel(
            workspace=self.ws, provider="telegram", name="Bot",
            inbound_enabled=True, inbound_handler="script", inbound_target_id=script.id,
        )
        channel.set_credentials({"bot_token": "1:2"}, identity="1:2")
        channel.save()
        return channel

    def test_channel_skip_replies_that_it_was_skipped(self):
        from core.services.channels.handlers import _script_handler
        from core.services.channels.base import InboundMessage

        script, running = self._busy(SKIP)
        channel = self._channel_for(script)
        msg = InboundMessage(
            channel_id=str(channel.id), provider="telegram", text="run it",
            sender={"id": 8}, reply_ref={"chat_id": 8}, raw=_TG_UPDATE,
        )
        with _tasks() as task:
            reply = _script_handler(channel, msg)
        task.assert_not_called()
        self.assertIn("already running", reply.text)
        self.assertEqual(reply.reply_ref, {"chat_id": 8})
        self._assert_skipped(self._new_run(script, running))

    def test_channel_queue_keeps_the_message_for_the_run(self):
        from core.services.channels.handlers import _script_handler
        from core.services.channels.base import InboundMessage

        script, running = self._busy(QUEUE)
        channel = self._channel_for(script)
        msg = InboundMessage(
            channel_id=str(channel.id), provider="telegram", text="run it",
            sender={"id": 8}, reply_ref={"chat_id": 8}, raw=_TG_UPDATE,
        )
        with _tasks() as task:
            self.assertIsNone(_script_handler(channel, msg))
        task.assert_not_called()
        run = self._new_run(script, running)
        self._assert_held(run, running)
        Run.objects.filter(pk=running.pk).update(status=Run.Status.SUCCESS)
        with _tasks() as task:
            run_concurrency.release(script.pk)
        self.assertEqual(task.call_args.args[2], {
            "method": "INBOUND",
            "inbound": {"channel": "Bot", "reply_ref": {"chat_id": 8}, "text": "run it", "sender": {"id": 8}},
        })

    # -- plugin SDK ---------------------------------------------------------- #

    def _sdk(self, policy):
        api = ScriptAPI("concplugin")
        script = api.upsert(
            key="job", name=f"job{uuid.uuid4().hex[:6]}", code="pass",
            environment=self.env, concurrency_policy=policy,
        )
        running = self._run(script, status=Run.Status.RUNNING)
        return api, script, running

    def test_sdk_queue_run_skip(self):
        api, script, running = self._sdk("skip")
        with _tasks() as task:
            run = api.queue_run("job", triggered_by=self.user)
        task.assert_not_called()
        self.assertEqual(run.cancel_reason, "concurrency_skip")
        self._assert_skipped(run)
        # The badge follows the run that is going, not the skipped one...
        self.assertEqual(api.latest_run("job").id, str(running.pk))
        # ...and the history shows both.
        views = api.runs("job")
        self.assertEqual([v.cancel_reason for v in views], ["concurrency_skip", ""])
        self.assertEqual(views[0].as_dict()["cancel_reason"], "concurrency_skip")

    def test_sdk_queue_run_queue(self):
        api, script, running = self._sdk("queue")
        with _tasks() as task:
            run = api.queue_run("job")
        task.assert_not_called()
        self.assertTrue(run.is_held)
        self._assert_held(run, running)
        view = api.latest_run("job")
        self.assertEqual(view.id, str(run.pk))
        self.assertTrue(view.is_held)
        self.assertTrue(view.as_dict()["is_held"])

    def test_sdk_upsert_policy_rules(self):
        api = ScriptAPI("concplugin2")
        script = api.upsert(key="j", name=f"j{uuid.uuid4().hex[:6]}", code="pass", environment=self.env)
        self.assertEqual(script.concurrency_policy, ALLOW)
        api.upsert(key="j", concurrency_policy="queue")
        script.refresh_from_db()
        self.assertEqual(script.concurrency_policy, QUEUE)
        api.upsert(key="j", code="print(1)")  # None leaves it as it is
        script.refresh_from_db()
        self.assertEqual(script.concurrency_policy, QUEUE)
        with self.assertRaisesMessage(ValueError, "concurrency_policy must be one of allow, skip, queue"):
            api.upsert(key="j", concurrency_policy="parallel")
        script.refresh_from_db()
        self.assertEqual(script.concurrency_policy, QUEUE)

    def test_run_view_defaults_for_an_ordinary_run(self):
        view = RunView._from_run(self._run(self._script()))
        self.assertEqual((view.cancel_reason, view.is_held), ("", False))


# --------------------------------------------------------------------------- #
# Console: editor, runs list, run page
# --------------------------------------------------------------------------- #


class ConsoleTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        _setup_wizard_off(self)
        self.user = User.objects.create(email="conc-ui@example.com", is_superuser=True, is_staff=True)
        WorkspaceMembership.objects.update_or_create(
            user=self.user, workspace=self.ws, defaults={"role": WorkspaceMembership.ROLE_OWNER}
        )
        self.client.force_login(self.user)

    def _form_data(self, script, **over):
        data = {
            "name": script.name,
            "description": "",
            "code": script.code,
            "environment": str(self.env.pk),
            "timeout_seconds": "60",
            "is_enabled": "on",
            "isolation_mode": "inherit",
            "injection_mode": "all",
            "notify_on": "never",
        }
        data.update(over)
        return data

    def test_the_editor_explains_the_policy(self):
        script = self._script()
        resp = self.client.get(reverse("cpanel:script_edit", args=[script.pk]))
        self.assertContains(resp, 'name="concurrency_policy"')
        self.assertContains(resp, "If it&#x27;s already running")
        self.assertContains(resp, "Queue starts it when the current run ends")
        self.assertContains(resp, "Allow: start another run alongside it")

    def test_the_form_saves_the_policy_and_keeps_it_when_left_out(self):
        script = self._script()
        form = ScriptForm(data=self._form_data(script, concurrency_policy="queue"), instance=script, workspace=self.ws)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        script.refresh_from_db()
        self.assertEqual(script.concurrency_policy, QUEUE)
        form = ScriptForm(data=self._form_data(script), instance=script, workspace=self.ws)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        script.refresh_from_db()
        self.assertEqual(script.concurrency_policy, QUEUE)
        form = ScriptForm(data=self._form_data(script, concurrency_policy="sometimes"), instance=script, workspace=self.ws)
        self.assertFalse(form.is_valid())
        self.assertIn("concurrency_policy", form.errors)

    def test_a_new_script_defaults_to_allow(self):
        script = self._script()
        form = ScriptForm(data=self._form_data(script, name="brand new"), workspace=self.ws)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().concurrency_policy, ALLOW)

    def test_the_runs_list_and_script_page_show_the_chips(self):
        script = self._script(SKIP)
        running = self._run(script, status=Run.Status.RUNNING)
        skipped, _ = self._queue(script)
        Script.objects.filter(pk=script.pk).update(concurrency_policy=QUEUE)
        script.refresh_from_db()
        held, _ = self._queue(script)
        for url in (reverse("cpanel:run_list"), reverse("cpanel:script_detail", args=[script.pk])):
            resp = self.client.get(url)
            self.assertContains(resp, "skipped: already running", count=1)
            self.assertContains(resp, "waiting its turn", count=1)
        self.assertContains(self.client.get(reverse("cpanel:script_detail", args=[script.pk])), "If already running")
        self.assertIsNotNone(running)
        self.assertTrue(held.is_held)
        self.assertTrue(skipped.is_skipped)

    def test_the_run_page_explains_a_skip(self):
        script = self._script(SKIP)
        self._run(script, status=Run.Status.RUNNING)
        skipped, _ = self._queue(script)
        resp = self.client.get(reverse("cpanel:run_detail", args=[skipped.pk]))
        self.assertContains(resp, "skipped: already running")
        self.assertContains(resp, "its concurrency policy is Skip")

    def test_the_run_page_explains_a_wait_and_links_the_run_ahead(self):
        script = self._script(QUEUE)
        running = self._run(script, status=Run.Status.RUNNING)
        held, _ = self._queue(script)
        resp = self.client.get(reverse("cpanel:run_detail", args=[held.pk]))
        self.assertContains(resp, "Waiting its turn")
        self.assertContains(resp, reverse("cpanel:run_detail", args=[running.pk]))
        # Once it has started, the page says what it waited behind.
        Run.objects.filter(pk=held.pk).update(status=Run.Status.SUCCESS, task_id="t")
        resp = self.client.get(reverse("cpanel:run_detail", args=[held.pk]))
        self.assertContains(resp, "Waited behind")
        self.assertNotContains(resp, "Waiting its turn")

    def test_a_held_run_can_be_cancelled_from_its_page(self):
        script = self._script(QUEUE)
        running = self._run(script, status=Run.Status.RUNNING)
        held, _ = self._queue(script, webhook_data={"method": "POST", "body": "b"})
        page = self.client.get(reverse("cpanel:run_detail", args=[held.pk]))
        self.assertContains(page, reverse("cpanel:run_cancel", args=[held.pk]))
        with _tasks() as task:
            resp = self.client.post(reverse("cpanel:run_cancel", args=[held.pk]), follow=True)
        task.assert_not_called()
        self.assertContains(resp, "Queued run cancelled")
        held.refresh_from_db()
        self.assertEqual(held.status, Run.Status.CANCELLED)
        self.assertEqual(held.queued_payload, "")
        # Only a waiting run: a running one keeps its Stop button.
        resp = self.client.post(reverse("cpanel:run_cancel", args=[running.pk]), follow=True)
        self.assertContains(resp, "nothing to cancel")
        self.assertEqual(Run.objects.get(pk=running.pk).status, Run.Status.RUNNING)
        self.assertEqual(self.client.get(reverse("cpanel:run_cancel", args=[held.pk])).status_code, 405)

    def test_cancel_is_scoped_to_the_workspace(self):
        other = Workspace.objects.create(name="Elsewhere")
        script = Script.objects.create(
            name="theirs", environment=self.env, workspace=other, code="pass",
            concurrency_policy=QUEUE,
        )
        Run.objects.create(script=script, workspace=other, status=Run.Status.RUNNING)
        held = Run.objects.create(script=script, workspace=other, status=Run.Status.PENDING)
        with _tasks():
            queue_script_run(held)
        resp = self.client.post(reverse("cpanel:run_cancel", args=[held.pk]))
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(Run.objects.get(pk=held.pk).is_held)

    def test_the_dashboard_says_skipped(self):
        script = self._script(SKIP)
        self._run(script, status=Run.Status.RUNNING)
        self._queue(script)
        resp = self.client.get(reverse("cpanel:dashboard"))
        self.assertContains(resp, "skipped")


# --------------------------------------------------------------------------- #
# Backups and forks
# --------------------------------------------------------------------------- #


class BackupAndForkTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create(email="admin-conc@example.com", is_superuser=True)

    def test_fork_copies_the_policy(self):
        script = self._script(QUEUE)
        self.assertEqual(fork_script(script, user=self.user).concurrency_policy, QUEUE)

    def test_backup_round_trips_the_policy_and_the_run_outcomes(self):
        script = self._script(QUEUE)
        running = self._run(script, status=Run.Status.RUNNING)
        held, _ = self._queue(script, webhook_data={"method": "POST", "body": "secret-ish"})
        done = self._run(script, status=Run.Status.SUCCESS, queued_behind=running.pk, task_id="t")
        skipped = self._run(
            script, status=Run.Status.CANCELLED, cancel_reason=SKIPPED, ended_at=timezone.now()
        )

        data = BackupService.create_backup(include_runs=True)
        by_id = {r["id"]: r for r in data["runs"]}
        exported_script = next(s for s in data["scripts"] if s["id"] == str(script.pk))
        self.assertEqual(exported_script["concurrency_policy"], "queue")
        self.assertEqual(by_id[str(skipped.pk)]["cancel_reason"], "concurrency_skip")
        self.assertEqual(by_id[str(done.pk)]["queued_behind"], str(running.pk))
        self.assertEqual(by_id[str(held.pk)]["queued_behind"], str(running.pk))
        self.assertNotIn("queued_payload", by_id[str(held.pk)])
        self.assertNotIn("secret-ish", json.dumps(data["runs"]))

        result = BackupService.restore_backup(data, restore_runs=True, current_user=self.user)
        self.assertTrue(result["success"], result)

        restored = Script.objects.get(pk=script.pk)
        self.assertEqual(restored.concurrency_policy, QUEUE)
        self.assertEqual(Run.objects.get(pk=skipped.pk).cancel_reason, SKIPPED)
        self.assertEqual(Run.objects.get(pk=done.pk).queued_behind, running.pk)
        # A run that was waiting comes back unheld: a restore never starts it.
        back = Run.objects.get(pk=held.pk)
        self.assertEqual(back.status, Run.Status.PENDING)
        self.assertIsNone(back.queued_behind)
        self.assertFalse(back.is_held)
        with _tasks() as task:
            run_concurrency.release_all()
        task.assert_not_called()

    def test_an_older_backup_restores_the_defaults(self):
        script = self._script(SKIP)
        run = self._run(script, status=Run.Status.CANCELLED, cancel_reason=SKIPPED)
        data = BackupService.create_backup(include_runs=True)
        for entry in data["scripts"]:
            entry.pop("concurrency_policy", None)
        for entry in data["runs"]:
            entry.pop("cancel_reason", None)
            entry.pop("queued_behind", None)
        result = BackupService.restore_backup(data, restore_runs=True, current_user=self.user)
        self.assertTrue(result["success"], result)
        self.assertEqual(Script.objects.get(pk=script.pk).concurrency_policy, ALLOW)
        back = Run.objects.get(pk=run.pk)
        self.assertEqual(back.cancel_reason, "")
        self.assertIsNone(back.queued_behind)
