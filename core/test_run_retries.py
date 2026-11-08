"""
Retry on failure with backoff (2.5 Stage 1, ticket T06).

What this pins, roughly in order of how much it would hurt to get wrong:

- Off by default: a failed run of a script with no retries creates nothing
  and notifies exactly as before.
- A failed run (a timed-out one only with ``retry_on_timeout``) creates the
  next attempt as a PENDING run with no task: same inputs, event and webhook
  data, ``attempt`` one higher, ``retry_of`` the first attempt, due after
  ``delay * multiplier ** (attempt - 1)`` (at most a day). Cancelled and
  skipped runs, successes, and the last allowed attempt never retry.
- The heartbeat starts due retries through ``queue_script_run`` once, with
  the same webhook data byte for byte, and they obey the concurrency policy
  (lined up behind runs triggered while they waited). A waiting retry doesn't
  count as in flight, isn't failed as lost, and can be cancelled.
- Only the last failed attempt notifies (or the first success); when a retry
  can't start, the attempt before it notifies instead.
- The editor, the run page, the runs list and the script page show it;
  backups and forks carry it.
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

from core.executor import execute_run, run_file_payload
from core.forms import ScriptForm
from core.models import (
    Environment,
    GlobalSettings,
    Run,
    Script,
    Workspace,
    WorkspaceMembership,
)
from core.services import run_concurrency, run_retries
from core.services.backup_service import BackupService
from core.services.notification_service import NotificationService
from core.services.script_service import fork_script
from core.tasks import execute_run_task, queue_script_run, worker_heartbeat_task

User = get_user_model()

FAILS = "import sys\nsys.exit(3)\n"
SUCCEEDS = "print('ok')\n"
SCHEMA = {
    "type": "object",
    "properties": {"city": {"type": "string"}},
    "required": ["city"],
}
WEBHOOK_DATA = {
    "method": "POST",
    "query": {"z": ["1"], "a": ["2"]},
    "content_type": "application/json",
    "body": '{"b": 1, "a": 2}',
    "body_json": {"b": 1, "a": 2},
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


def _executor():
    """Run scripts for real with this interpreter, without AI credentials."""
    stack = mock.patch("core.executor._validate_environment", return_value=sys.executable)
    ai = mock.patch("core.executor.ClaudeService.get_script_env", return_value={})
    return stack, ai


class _Fixture:
    def setUp(self):
        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(name="renv", path=f"renv{uuid.uuid4().hex[:6]}")

    def _script(self, retries=0, **kwargs):
        kwargs.setdefault("code", FAILS)
        return Script.objects.create(
            name=f"r{uuid.uuid4().hex[:6]}",
            environment=self.env,
            workspace=self.ws,
            retry_max_attempts=retries,
            **kwargs,
        )

    def _run(self, script, status=Run.Status.PENDING, **kwargs):
        return Run.objects.create(script=script, workspace=self.ws, status=status, **kwargs)

    def _execute(self, run, webhook_data=None):
        """Execute ``run`` for real; return it refreshed."""
        stack, ai = _executor()
        with stack, ai, _tasks():
            execute_run(Run.objects.select_related("script").get(pk=run.pk), webhook_data)
        run.refresh_from_db()
        return run

    def _waiting(self, script, due=None, first=None, attempt=2, payload=None, **kwargs):
        """A retry waiting for its delay, as schedule_retry leaves it."""
        first = first or self._run(
            script, status=Run.Status.FAILED, ended_at=timezone.now(), exit_code=3
        )
        return self._run(
            script,
            attempt=attempt,
            retry_of=first,
            next_attempt_at=due or timezone.now() - datetime.timedelta(seconds=1),
            queued_payload=json.dumps(payload) if payload is not None else "",
            code_snapshot=script.code,
            **kwargs,
        )

    def _first(self, retry):
        return Run.objects.get(pk=retry.retry_of_id)


class ScheduleRetryTests(_Fixture, TestCase):
    def test_retries_are_off_by_default(self):
        script = self._script(code=FAILS)
        self.assertEqual(script.retry_max_attempts, 0)
        self.assertEqual(script.retry_delay_seconds, 60)
        self.assertEqual(script.retry_backoff_multiplier, 1.0)
        self.assertFalse(script.retry_on_timeout)
        run = self._execute(self._run(script))
        self.assertEqual(run.status, Run.Status.FAILED)
        self.assertEqual(run.attempt, 1)
        self.assertEqual(Run.objects.filter(script=script).count(), 1)

    def test_a_failed_run_creates_the_next_attempt(self):
        script = self._script(retries=2, inputs_schema=SCHEMA)
        event = {"kind": "webhook", "received_at": "2026-11-08T10:00:00+00:00", "method": "POST"}
        run = self._run(script, inputs={"city": "Paris"}, event=event, trigger_type=Run.TriggerType.API)
        before = timezone.now()
        run = self._execute(run, WEBHOOK_DATA)
        self.assertEqual(run.status, Run.Status.FAILED)

        retry = Run.objects.exclude(pk=run.pk).get(script=script)
        self.assertEqual(retry.status, Run.Status.PENDING)
        self.assertEqual(retry.task_id, "")
        self.assertIsNone(retry.queued_behind)
        self.assertTrue(retry.is_waiting_retry)
        self.assertFalse(retry.is_held)
        self.assertEqual(retry.attempt, 2)
        self.assertEqual(retry.retry_of_id, run.pk)
        self.assertEqual(retry.inputs, {"city": "Paris"})
        self.assertEqual(retry.event, event)
        self.assertEqual(retry.trigger_type, Run.TriggerType.API)
        self.assertEqual(retry.workspace_id, self.ws.pk)
        self.assertEqual(json.loads(retry.queued_payload), WEBHOOK_DATA)
        delay = (retry.next_attempt_at - before).total_seconds()
        self.assertGreaterEqual(delay, 60)
        self.assertLess(delay, 90)

    def test_delays_follow_the_backoff(self):
        script = self._script(retries=5, retry_delay_seconds=60, retry_backoff_multiplier=2.0)
        self.assertEqual([run_retries.retry_delay(script, n) for n in (1, 2, 3)], [60, 120, 240])
        script.retry_backoff_multiplier = 1.0
        self.assertEqual([run_retries.retry_delay(script, n) for n in (1, 2, 3)], [60, 60, 60])
        script.retry_delay_seconds = 0
        self.assertEqual(run_retries.retry_delay(script, 1), 0)

    def test_the_delay_never_exceeds_a_day(self):
        script = self._script(
            retries=10, retry_delay_seconds=Script.MAX_RETRY_DELAY_SECONDS, retry_backoff_multiplier=10.0
        )
        self.assertEqual(run_retries.retry_delay(script, 10), Script.MAX_RETRY_DELAY_SECONDS)

    def test_the_second_retry_waits_longer_and_links_the_first_attempt(self):
        script = self._script(retries=3, retry_delay_seconds=60, retry_backoff_multiplier=2.0)
        first = self._run(script, status=Run.Status.FAILED)
        second = self._run(script, status=Run.Status.FAILED, attempt=2, retry_of=first)
        before = timezone.now()
        third = run_retries.schedule_retry(second)
        self.assertEqual(third.attempt, 3)
        self.assertEqual(third.retry_of_id, first.pk)
        self.assertGreaterEqual((third.next_attempt_at - before).total_seconds(), 120)

    def test_the_last_allowed_attempt_does_not_retry(self):
        script = self._script(retries=2)
        first = self._run(script, status=Run.Status.FAILED)
        last = self._run(script, status=Run.Status.FAILED, attempt=3, retry_of=first)
        self.assertIsNone(run_retries.schedule_retry(last))

    def test_only_failures_retry(self):
        script = self._script(retries=2)
        for status in (Run.Status.SUCCESS, Run.Status.CANCELLED):
            self.assertIsNone(run_retries.schedule_retry(self._run(script, status=status)))
        skipped = self._run(
            script, status=Run.Status.CANCELLED, cancel_reason=Run.CancelReason.CONCURRENCY_SKIP
        )
        self.assertIsNone(run_retries.schedule_retry(skipped))
        self.assertEqual(Run.objects.filter(script=script, attempt=2).count(), 0)

    def test_a_success_creates_no_retry(self):
        script = self._script(retries=2, code=SUCCEEDS)
        run = self._execute(self._run(script))
        self.assertEqual(run.status, Run.Status.SUCCESS, run.stderr)
        self.assertEqual(Run.objects.filter(script=script).count(), 1)

    def test_timeouts_retry_only_when_asked(self):
        script = self._script(retries=2)
        timed_out = self._run(script, status=Run.Status.TIMEOUT)
        self.assertIsNone(run_retries.schedule_retry(timed_out))
        Script.objects.filter(pk=script.pk).update(retry_on_timeout=True)
        self.assertEqual(run_retries.schedule_retry(timed_out).attempt, 2)

    def test_a_disabled_script_does_not_retry(self):
        script = self._script(retries=2, is_enabled=False)
        self.assertIsNone(run_retries.schedule_retry(self._run(script, status=Run.Status.FAILED)))

    def test_settings_are_read_fresh_when_the_run_ends(self):
        script = self._script(retries=0)
        run = self._run(script, status=Run.Status.FAILED)
        self.assertEqual(run.script.retry_max_attempts, 0)  # cached on the run
        Script.objects.filter(pk=script.pk).update(retry_max_attempts=1)
        self.assertIsNotNone(run_retries.schedule_retry(run))

    def test_a_retry_is_scheduled_once(self):
        script = self._script(retries=2)
        run = self._run(script, status=Run.Status.FAILED)
        self.assertIsNotNone(run_retries.schedule_retry(run))
        self.assertIsNone(run_retries.schedule_retry(run))
        self.assertEqual(Run.objects.filter(script=script, attempt=2).count(), 1)

    def test_a_failing_retry_hook_never_breaks_the_finished_run(self):
        script = self._script(retries=2)
        run = self._run(script)
        with mock.patch.object(run_retries, "schedule_retry", side_effect=RuntimeError("boom")), \
                self.assertLogs("core.services.run_retries", "ERROR"):
            run = self._execute(run)
        self.assertEqual(run.status, Run.Status.FAILED)

    def test_a_stopped_run_does_not_retry(self):
        # Stop lands while the script is going: the executor keeps CANCELLED.
        from core.executor_backends.local import LocalSubprocessBackend

        script = self._script(retries=2)
        run = self._run(script)
        real_wait = LocalSubprocessBackend.wait

        def stop_then_wait(backend, handle, timeout):
            Run.objects.filter(pk=run.pk).update(status=Run.Status.CANCELLED)
            return real_wait(backend, handle, timeout)

        with mock.patch.object(LocalSubprocessBackend, "wait", stop_then_wait):
            run = self._execute(run)
        self.assertEqual(run.status, Run.Status.CANCELLED)
        self.assertEqual(Run.objects.filter(script=script).count(), 1)

    def test_run_json_carries_the_attempt(self):
        script = self._script(retries=2)
        first = self._run(script, status=Run.Status.FAILED)
        retry = self._run(script, attempt=2, retry_of=first)
        self.assertEqual(run_file_payload(retry)["run"]["attempt"], 2)
        self.assertEqual(run_file_payload(first)["run"]["attempt"], 1)


class DispatchTests(_Fixture, TestCase):
    def test_a_retry_that_is_not_due_waits(self):
        script = self._script(retries=2)
        retry = self._waiting(script, due=timezone.now() + datetime.timedelta(minutes=5))
        with _tasks() as task:
            self.assertEqual(run_retries.dispatch_due(), 0)
        task.assert_not_called()
        self.assertTrue(Run.objects.get(pk=retry.pk).is_waiting_retry)

    def test_a_due_retry_is_queued_with_its_request_data(self):
        script = self._script(retries=2)
        retry = self._waiting(script, payload=WEBHOOK_DATA)
        Run.objects.filter(pk=retry.pk).update(created_at=timezone.now() - datetime.timedelta(hours=1))
        Script.objects.filter(pk=script.pk).update(code="print('fixed')\n")
        before = timezone.now()
        with _tasks() as task:
            self.assertEqual(run_retries.dispatch_due(), 1)
        task.assert_called_once()
        self.assertEqual(task.call_args.args[1], str(retry.pk))
        self.assertEqual(task.call_args.args[2], WEBHOOK_DATA)
        self.assertEqual(json.dumps(task.call_args.args[2]), json.dumps(WEBHOOK_DATA))
        retry.refresh_from_db()
        self.assertEqual(retry.status, Run.Status.PENDING)
        self.assertEqual(retry.task_id, "task-1")
        self.assertIsNone(retry.next_attempt_at)
        self.assertEqual(retry.queued_payload, "")
        self.assertGreaterEqual(retry.created_at, before)
        self.assertEqual(retry.code_snapshot, "print('fixed')\n")
        self.assertFalse(retry.is_waiting_retry)

    def test_a_retry_starts_once(self):
        script = self._script(retries=2)
        self._waiting(script)
        with _tasks() as task:
            self.assertEqual(run_retries.dispatch_due(), 1)
            self.assertEqual(run_retries.dispatch_due(), 0)
        task.assert_called_once()

    def test_a_dispatch_that_lost_the_claim_starts_nothing(self):
        script = self._script(retries=2)
        retry = self._waiting(script)
        real_filter = Run.objects.filter

        def racing_filter(*args, **kwargs):
            # Another heartbeat claims the run between our read and our UPDATE.
            if "next_attempt_at" in kwargs and kwargs.get("pk") == retry.pk:
                real_filter(pk=retry.pk).update(next_attempt_at=None, task_id="other")
            return real_filter(*args, **kwargs)

        with _tasks() as task, mock.patch.object(Run.objects, "filter", side_effect=racing_filter):
            self.assertEqual(run_retries.dispatch_due(), 0)
        task.assert_not_called()

    def test_a_retry_obeys_skip_behind_a_run_triggered_while_it_waited(self):
        script = self._script(retries=2, concurrency_policy=Script.ConcurrencyPolicy.SKIP)
        retry = self._waiting(script)
        # Triggered after the retry row was created, still going when it's due.
        self._run(script, status=Run.Status.RUNNING, started_at=timezone.now())
        with _tasks() as task, mock.patch.object(NotificationService, "send_notification") as notify:
            self.assertEqual(run_retries.dispatch_due(), 0)
        task.assert_not_called()
        retry.refresh_from_db()
        self.assertEqual(retry.status, Run.Status.CANCELLED)
        self.assertTrue(retry.is_skipped)
        # The failed attempt before it was the last one after all: it reports.
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[0].pk, retry.retry_of_id)

    def test_a_retry_obeys_queue(self):
        script = self._script(retries=2, concurrency_policy=Script.ConcurrencyPolicy.QUEUE)
        retry = self._waiting(script, payload=WEBHOOK_DATA)
        running = self._run(script, status=Run.Status.RUNNING, started_at=timezone.now())
        with _tasks() as task:
            self.assertEqual(run_retries.dispatch_due(), 1)
        task.assert_not_called()
        retry.refresh_from_db()
        self.assertTrue(retry.is_held)
        self.assertEqual(retry.queued_behind, running.pk)
        # It starts when the run ahead ends, with its webhook data.
        Run.objects.filter(pk=running.pk).update(status=Run.Status.FAILED, ended_at=timezone.now())
        with _tasks() as task:
            self.assertEqual(run_concurrency.release(script.pk).pk, retry.pk)
        self.assertEqual(task.call_args.args[2], WEBHOOK_DATA)

    def test_a_waiting_retry_does_not_block_new_runs(self):
        script = self._script(retries=2, concurrency_policy=Script.ConcurrencyPolicy.SKIP)
        self._waiting(script, due=timezone.now() + datetime.timedelta(minutes=5))
        self.assertFalse(run_concurrency.in_flight(script.pk).exists())
        run = self._run(script)
        with _tasks() as task:
            queue_script_run(run)
        task.assert_called_once()
        self.assertFalse(Run.objects.get(pk=run.pk).is_skipped)

    def test_a_retry_of_a_disabled_script_is_cancelled(self):
        script = self._script(retries=2)
        retry = self._waiting(script)
        Script.objects.filter(pk=script.pk).update(is_enabled=False)
        with _tasks() as task, mock.patch.object(NotificationService, "send_notification") as notify:
            self.assertEqual(run_retries.dispatch_due(), 0)
        task.assert_not_called()
        retry.refresh_from_db()
        self.assertEqual(retry.status, Run.Status.CANCELLED)
        self.assertIn("the script is disabled", retry.stderr)
        self.assertEqual(notify.call_args.args[0].pk, retry.retry_of_id)

    def test_a_retry_after_retries_were_turned_off_is_cancelled(self):
        script = self._script(retries=2)
        retry = self._waiting(script)
        Script.objects.filter(pk=script.pk).update(retry_max_attempts=0)
        with _tasks() as task, mock.patch.object(NotificationService, "send_notification"):
            run_retries.dispatch_due()
        task.assert_not_called()
        retry.refresh_from_db()
        self.assertEqual(retry.status, Run.Status.CANCELLED)
        self.assertIn("retries were turned off", retry.stderr)

    def test_a_retry_that_cannot_be_queued_fails_and_the_attempt_before_reports(self):
        script = self._script(retries=2)
        retry = self._waiting(script)
        with mock.patch("core.tasks.async_task", side_effect=RuntimeError("broker down")), \
                mock.patch.object(NotificationService, "send_notification") as notify, \
                self.assertLogs("core.services.run_retries", "ERROR"):
            self.assertEqual(run_retries.dispatch_due(), 0)
        retry.refresh_from_db()
        self.assertEqual(retry.status, Run.Status.FAILED)
        self.assertIn("broker down", retry.stderr)
        self.assertEqual(notify.call_args.args[0].pk, retry.retry_of_id)
        # It never ran, so the failed attempt before it isn't hidden by it.
        self.assertFalse(run_retries.retry_follows(self._first(retry)))

    def test_a_retry_whose_inputs_no_longer_fit_is_dropped(self):
        script = self._script(retries=2)
        retry = self._waiting(script)
        Script.objects.filter(pk=script.pk).update(inputs_schema=SCHEMA)
        with _tasks() as task, mock.patch.object(NotificationService, "send_notification") as notify, \
                self.assertLogs("core.services.run_retries", "WARNING"):
            self.assertEqual(run_retries.dispatch_due(), 0)
        task.assert_not_called()
        self.assertFalse(Run.objects.filter(pk=retry.pk).exists())
        self.assertEqual(notify.call_args.args[0].pk, retry.retry_of_id)

    def test_one_broken_retry_does_not_stop_the_others(self):
        script = self._script(retries=2)
        self._waiting(script)
        self._waiting(script)
        real = run_retries._dispatch
        calls = []

        def flaky(pk):
            calls.append(pk)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return real(pk)

        with _tasks() as task, mock.patch.object(run_retries, "_dispatch", side_effect=flaky), \
                self.assertLogs("core.services.run_retries", "ERROR"):
            self.assertEqual(run_retries.dispatch_due(), 1)
        task.assert_called_once()

    def test_the_heartbeat_starts_due_retries(self):
        script = self._script(retries=2)
        retry = self._waiting(script)
        with _tasks() as task:
            self.assertTrue(worker_heartbeat_task()["success"])
        task.assert_called_once()
        self.assertEqual(task.call_args.args[1], str(retry.pk))

    def test_the_heartbeat_survives_a_failing_dispatch(self):
        with mock.patch.object(run_retries, "dispatch_due", side_effect=RuntimeError("x")), \
                self.assertLogs("core.tasks", "ERROR"):
            self.assertTrue(worker_heartbeat_task()["success"])

    def test_cancelling_a_waiting_retry(self):
        script = self._script(retries=2)
        retry = self._waiting(script, payload=WEBHOOK_DATA)
        self.assertTrue(run_retries.cancel_waiting(retry))
        retry.refresh_from_db()
        self.assertEqual(retry.status, Run.Status.CANCELLED)
        self.assertEqual(retry.queued_payload, "")
        with _tasks() as task:
            run_retries.dispatch_due()
        task.assert_not_called()
        self.assertFalse(run_retries.cancel_waiting(retry))

    def test_cancel_leaves_a_started_retry_alone(self):
        script = self._script(retries=2)
        retry = self._waiting(script)
        with _tasks():
            run_retries.dispatch_due()
        self.assertFalse(run_retries.cancel_waiting(retry))
        self.assertEqual(Run.objects.get(pk=retry.pk).status, Run.Status.PENDING)

    def test_a_waiting_retry_is_not_failed_as_lost(self):
        settings = GlobalSettings.get_settings()
        settings.worker_heartbeat_at = timezone.now()
        settings.save(update_fields=["worker_heartbeat_at"])
        script = self._script(retries=2)
        long_ago = timezone.now() - Run.RECONCILE_PENDING_AFTER - datetime.timedelta(hours=1)
        # Created over a day ago, due soon (a long backoff): still waiting.
        waiting = self._waiting(script, due=timezone.now() + datetime.timedelta(hours=1))
        # Due for over a day with workers alive: the retry was lost.
        lost = self._waiting(script, due=long_ago)
        Run.objects.filter(pk__in=[waiting.pk, lost.pk]).update(created_at=long_ago)
        with mock.patch.object(NotificationService, "send_notification"):
            Run.reconcile_stale()
        self.assertEqual(Run.objects.get(pk=waiting.pk).status, Run.Status.PENDING)
        self.assertEqual(Run.objects.get(pk=lost.pk).status, Run.Status.FAILED)


class NotificationTests(_Fixture, TestCase):
    def _notifying(self, retries, code=FAILS, notify_on=Script.NotifyOn.BOTH):
        return self._script(
            retries=retries,
            code=code,
            notify_on=notify_on,
            notify_webhook_enabled=True,
            notify_webhook_url="https://hooks.example.com/x",
        )

    def _task(self, run):
        stack, ai = _executor()
        with stack, ai, _tasks(), mock.patch.object(
            NotificationService, "_send_webhook_notification"
        ) as sent:
            execute_run_task(str(run.pk))
        return sent

    def test_without_retries_a_failure_notifies_as_before(self):
        script = self._notifying(retries=0)
        self.assertEqual(self._task(self._run(script)).call_count, 1)

    def test_an_attempt_followed_by_a_retry_is_quiet(self):
        script = self._notifying(retries=2)
        run = self._run(script)
        self.assertEqual(self._task(run).call_count, 0)
        self.assertTrue(run_retries.retry_follows(Run.objects.get(pk=run.pk)))

    def test_the_last_failed_attempt_notifies(self):
        script = self._notifying(retries=1)
        first = self._run(script, status=Run.Status.FAILED)
        last = self._run(script, attempt=2, retry_of=first)
        self.assertEqual(self._task(last).call_count, 1)
        self.assertEqual(Run.objects.filter(script=script).count(), 2)

    def test_a_retry_that_succeeds_notifies(self):
        script = self._notifying(retries=2, code=SUCCEEDS)
        first = self._run(script, status=Run.Status.FAILED)
        retry = self._run(script, attempt=2, retry_of=first)
        self.assertEqual(self._task(retry).call_count, 1)

    def test_should_notify_rules(self):
        script = self._notifying(retries=2, notify_on=Script.NotifyOn.FAILURE)
        first = self._run(script, status=Run.Status.FAILED)
        self.assertTrue(NotificationService.should_notify(first))
        retry = self._run(script, attempt=2, retry_of=first, next_attempt_at=timezone.now())
        self.assertFalse(NotificationService.should_notify(first))
        # A retry that ran (and failed) still hides the attempt before it.
        Run.objects.filter(pk=retry.pk).update(status=Run.Status.FAILED, started_at=timezone.now())
        self.assertFalse(NotificationService.should_notify(first))
        self.assertTrue(NotificationService.should_notify(Run.objects.get(pk=retry.pk)))
        # One cancelled before it started doesn't.
        Run.objects.filter(pk=retry.pk).update(status=Run.Status.CANCELLED, started_at=None)
        self.assertTrue(NotificationService.should_notify(first))


class ConsoleTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        _setup_wizard_off(self)
        self.user = User.objects.create(email="retry-ui@example.com", is_superuser=True, is_staff=True)
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

    def _save(self, script, **over):
        form = ScriptForm(data=self._form_data(script, **over), instance=script, workspace=self.ws)
        valid = form.is_valid()
        if valid:
            form.save()
            script.refresh_from_db()
        return form, valid

    def test_the_editor_shows_the_retry_settings(self):
        script = self._script()
        resp = self.client.get(reverse("cpanel:script_edit", args=[script.pk]))
        for name in ("retry_max_attempts", "retry_delay_seconds", "retry_backoff_multiplier", "retry_on_timeout"):
            self.assertContains(resp, f'name="{name}"')
        self.assertContains(resp, "Retries on failure")
        self.assertContains(resp, "once a minute")
        self.assertContains(resp, "Only the last attempt notifies")

    def test_the_form_saves_the_settings_and_keeps_them_when_left_out(self):
        script = self._script()
        form, valid = self._save(
            script,
            retry_max_attempts="3",
            retry_delay_seconds="30",
            retry_backoff_multiplier="2.5",
            retry_on_timeout="on",
        )
        self.assertTrue(valid, form.errors)
        self.assertEqual(
            (script.retry_max_attempts, script.retry_delay_seconds,
             script.retry_backoff_multiplier, script.retry_on_timeout),
            (3, 30, 2.5, True),
        )
        form, valid = self._save(script, retry_max_attempts="", retry_on_timeout="on")
        self.assertTrue(valid, form.errors)
        self.assertEqual((script.retry_max_attempts, script.retry_delay_seconds), (3, 30))
        self.assertEqual(script.retry_backoff_multiplier, 2.5)

    def test_the_form_refuses_values_out_of_range(self):
        script = self._script()
        for field, value in (
            ("retry_max_attempts", "11"),
            ("retry_delay_seconds", str(Script.MAX_RETRY_DELAY_SECONDS + 1)),
            ("retry_backoff_multiplier", "0.5"),
            ("retry_backoff_multiplier", "11"),
            ("retry_max_attempts", "-1"),
        ):
            form, valid = self._save(script, **{field: value})
            self.assertFalse(valid, (field, value))
            self.assertIn(field, form.errors)

    def test_a_new_script_has_no_retries(self):
        script = self._script()
        form = ScriptForm(data=self._form_data(script, name="brand new"), workspace=self.ws)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().retry_max_attempts, 0)

    def test_the_script_page_shows_the_retries(self):
        script = self._script()
        resp = self.client.get(reverse("cpanel:script_detail", args=[script.pk]))
        self.assertContains(resp, "Retries on failure")
        Script.objects.filter(pk=script.pk).update(retry_max_attempts=2, retry_backoff_multiplier=2.0)
        resp = self.client.get(reverse("cpanel:script_detail", args=[script.pk]))
        self.assertContains(resp, "Up to 2")
        self.assertContains(resp, "×2")

    def test_the_run_page_shows_the_attempt_and_links_both_ways(self):
        script = self._script(retries=2)
        first = self._run(script, status=Run.Status.FAILED, ended_at=timezone.now())
        retry = self._run(script, status=Run.Status.SUCCESS, attempt=2, retry_of=first, task_id="t")
        resp = self.client.get(reverse("cpanel:run_detail", args=[retry.pk]))
        self.assertContains(resp, "attempt 2 of 3 · retry of")
        self.assertContains(resp, reverse("cpanel:run_detail", args=[first.pk]))
        resp = self.client.get(reverse("cpanel:run_detail", args=[first.pk]))
        self.assertContains(resp, "attempt 1 of 3")
        self.assertContains(resp, "Retried as")
        self.assertContains(resp, reverse("cpanel:run_detail", args=[retry.pk]))

    def test_a_run_with_no_retries_shows_no_attempt(self):
        script = self._script()
        run = self._run(script, status=Run.Status.FAILED, ended_at=timezone.now())
        resp = self.client.get(reverse("cpanel:run_detail", args=[run.pk]))
        self.assertNotContains(resp, "Retried as")
        self.assertNotContains(resp, "attempt 1 of")

    def test_a_waiting_retry_says_when_it_starts_and_can_be_cancelled(self):
        script = self._script(retries=2)
        retry = self._waiting(script, due=timezone.now() + datetime.timedelta(minutes=5))
        page = self.client.get(reverse("cpanel:run_detail", args=[retry.pk]))
        self.assertContains(page, "Retry waiting: attempt 2 of 3 starts at")
        self.assertContains(page, reverse("cpanel:run_cancel", args=[retry.pk]))
        listing = self.client.get(reverse("cpanel:run_list"))
        self.assertContains(listing, "retry at ")
        resp = self.client.post(reverse("cpanel:run_cancel", args=[retry.pk]), follow=True)
        self.assertContains(resp, "Retry cancelled")
        self.assertEqual(Run.objects.get(pk=retry.pk).status, Run.Status.CANCELLED)
        resp = self.client.post(reverse("cpanel:run_cancel", args=[retry.pk]), follow=True)
        self.assertContains(resp, "nothing to cancel")

    def test_the_dashboard_says_retrying(self):
        script = self._script(retries=2)
        self._waiting(script, due=timezone.now() + datetime.timedelta(minutes=5))
        resp = self.client.get(reverse("cpanel:dashboard"))
        self.assertContains(resp, "retrying")

    def test_cancel_is_scoped_to_the_workspace(self):
        other = Workspace.objects.create(name="Retry elsewhere")
        script = self._script(retries=2)
        Script.objects.filter(pk=script.pk).update(workspace=other)
        retry = self._waiting(script)
        Run.objects.filter(pk__in=[retry.pk, retry.retry_of_id]).update(workspace=other)
        resp = self.client.post(reverse("cpanel:run_cancel", args=[retry.pk]))
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(Run.objects.get(pk=retry.pk).status, Run.Status.PENDING)


class BackupAndForkTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create(email="admin-retry@example.com", is_superuser=True)

    def test_fork_copies_the_retry_settings(self):
        script = self._script(
            retries=3, retry_delay_seconds=10, retry_backoff_multiplier=2.0, retry_on_timeout=True
        )
        fork = fork_script(script, user=self.user)
        self.assertEqual(
            (fork.retry_max_attempts, fork.retry_delay_seconds,
             fork.retry_backoff_multiplier, fork.retry_on_timeout),
            (3, 10, 2.0, True),
        )

    def test_backup_round_trips_the_settings_and_the_attempts(self):
        script = self._script(
            retries=3, retry_delay_seconds=10, retry_backoff_multiplier=2.0, retry_on_timeout=True
        )
        first = self._run(script, status=Run.Status.FAILED, ended_at=timezone.now())
        second = self._run(script, status=Run.Status.FAILED, attempt=2, retry_of=first, ended_at=timezone.now())
        due = timezone.now() + datetime.timedelta(minutes=5)
        cancelled = self._run(
            script, status=Run.Status.CANCELLED, attempt=3, retry_of=first,
            next_attempt_at=due, ended_at=timezone.now(),
        )
        waiting = self._waiting(script, first=first, attempt=3, payload={"body": "secret-ish"})

        data = BackupService.create_backup(include_runs=True)
        by_id = {r["id"]: r for r in data["runs"]}
        exported = next(s for s in data["scripts"] if s["id"] == str(script.pk))
        self.assertEqual(
            (exported["retry_max_attempts"], exported["retry_delay_seconds"],
             exported["retry_backoff_multiplier"], exported["retry_on_timeout"]),
            (3, 10, 2.0, True),
        )
        self.assertEqual(by_id[str(second.pk)]["attempt"], 2)
        self.assertEqual(by_id[str(second.pk)]["retry_of"], str(first.pk))
        self.assertIsNone(by_id[str(first.pk)]["retry_of"])
        self.assertIsNotNone(by_id[str(waiting.pk)]["next_attempt_at"])
        self.assertNotIn("secret-ish", json.dumps(data["runs"]))
        # The newest runs come first: the first attempt is restored last.
        self.assertLess(
            [r["id"] for r in data["runs"]].index(str(second.pk)),
            [r["id"] for r in data["runs"]].index(str(first.pk)),
        )

        result = BackupService.restore_backup(data, restore_runs=True, current_user=self.user)
        self.assertTrue(result["success"], result)
        restored = Script.objects.get(pk=script.pk)
        self.assertEqual(
            (restored.retry_max_attempts, restored.retry_delay_seconds,
             restored.retry_backoff_multiplier, restored.retry_on_timeout),
            (3, 10, 2.0, True),
        )
        back = Run.objects.get(pk=second.pk)
        self.assertEqual((back.attempt, back.retry_of_id), (2, first.pk))
        self.assertIsNotNone(Run.objects.get(pk=cancelled.pk).next_attempt_at)
        # A retry that was waiting comes back without its due time: a restore
        # never starts it.
        back = Run.objects.get(pk=waiting.pk)
        self.assertEqual(back.status, Run.Status.PENDING)
        self.assertIsNone(back.next_attempt_at)
        self.assertFalse(back.is_waiting_retry)
        with _tasks() as task:
            self.assertEqual(run_retries.dispatch_due(), 0)
        task.assert_not_called()

    def test_a_retry_whose_first_attempt_is_not_in_the_backup_restores_unlinked(self):
        script = self._script(retries=2)
        first = self._run(script, status=Run.Status.FAILED)
        second = self._run(script, status=Run.Status.SUCCESS, attempt=2, retry_of=first)
        data = BackupService.create_backup(include_runs=True)
        data["runs"] = [r for r in data["runs"] if r["id"] != str(first.pk)]
        result = BackupService.restore_backup(data, restore_runs=True, current_user=self.user)
        self.assertTrue(result["success"], result)
        back = Run.objects.get(pk=second.pk)
        self.assertEqual(back.attempt, 2)
        self.assertIsNone(back.retry_of_id)

    def test_an_older_backup_restores_the_defaults(self):
        script = self._script(retries=3)
        first = self._run(script, status=Run.Status.FAILED)
        second = self._run(script, status=Run.Status.FAILED, attempt=2, retry_of=first)
        data = BackupService.create_backup(include_runs=True)
        for entry in data["scripts"]:
            for key in ("retry_max_attempts", "retry_delay_seconds", "retry_backoff_multiplier", "retry_on_timeout"):
                entry.pop(key, None)
        for entry in data["runs"]:
            for key in ("attempt", "retry_of", "next_attempt_at"):
                entry.pop(key, None)
        result = BackupService.restore_backup(data, restore_runs=True, current_user=self.user)
        self.assertTrue(result["success"], result)
        restored = Script.objects.get(pk=script.pk)
        self.assertEqual(
            (restored.retry_max_attempts, restored.retry_delay_seconds,
             restored.retry_backoff_multiplier, restored.retry_on_timeout),
            (0, 60, 1.0, False),
        )
        back = Run.objects.get(pk=second.pk)
        self.assertEqual((back.attempt, back.retry_of_id, back.next_attempt_at), (1, None, None))
