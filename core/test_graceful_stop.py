"""
Graceful stop before kill (2.5 Stage 1, ticket T08).

What this pins, roughly in order of how much it would hurt to get wrong:

- Stopping a run (console, SDK) and a timeout send the stop signal (SIGTERM,
  CTRL_BREAK on Windows) to the script's own process group, wait the script's
  ``stop_grace_seconds``, then kill it. The django-q worker's group is never
  signalled, and neither is a pid that no longer leads its own group.
- A cooperative script (``pyrunner_run.on_stop``) flushes and exits within
  the grace period and ends CANCELLED with its output kept; a stubborn one is
  killed when the grace period is up. The stderr note says which.
- A timeout gets the same grace period; with none (0) the TIMEOUT message is
  what it was before 2.5.
- ``Run.stop_requested_at`` is recorded; the run stays RUNNING while its
  script shuts down; stopping it again kills it at once ("Kill now").
- The setting is on the script form, forked, backed up and restored, and the
  task timeout / worker window leave room for it.

Real scripts run with this interpreter (``_validate_environment`` stubbed), so
the signals, the console attach on Windows and the kill are the real thing.
The helper on its own, without Django, is in core/test_pyrunner_run_helper.py.
"""

import os
import subprocess
import sys
import time
import uuid
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.executor import execute_run, remove_script_workdir, script_workdir
from core.executor_backends import local
from core.executor_backends.base import STOP_GRACEFUL, STOP_KILLED, RunSpec
from core.forms import ScriptForm
from core.models import Environment, Run, Script, Workspace, WorkspaceMembership
from core.plugins.api import RunView
from core.services.backup_service import BackupService
from core.services.script_service import fork_script
from core.services.task_service import TaskService
from core.tasks import enqueue_run

User = get_user_model()

HELPERS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "script_helpers")
MARKER = "ready.marker"

# Saves its work in on_stop, then tells the test it is ready (the handler is in
# place). Short sleeps: on Windows a signal handler only runs between bytecodes.
COOPERATIVE = (
    "import time, pyrunner_run\n"
    "@pyrunner_run.on_stop\n"
    "def flush():\n"
    "    print('flushed partial work', flush=True)\n"
    f"open({MARKER!r}, 'w').close()\n"
    "print('working', flush=True)\n"
    "while True:\n"
    "    time.sleep(0.05)\n"
)
# Ignores the stop signal: only the kill ends it.
STUBBORN = (
    "import signal, time\n"
    "for name in ('SIGTERM', 'SIGBREAK'):\n"
    "    if hasattr(signal, name):\n"
    "        signal.signal(getattr(signal, name), signal.SIG_IGN)\n"
    f"open({MARKER!r}, 'w').close()\n"
    "print('ignoring the stop', flush=True)\n"
    "while True:\n"
    "    time.sleep(0.05)\n"
)


def _wait_for(path, timeout=30.0):
    deadline = time.monotonic() + timeout
    while not os.path.exists(path):
        if time.monotonic() > deadline:
            raise AssertionError(f"{path} never appeared")
        time.sleep(0.02)


def _fast_poll():
    """Ask for stop requests every 0.1 s instead of every 2 s."""
    return mock.patch.object(local, "STOP_POLL_SECONDS", 0.1)


class _Fixture:
    def setUp(self):
        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(name="genv", path=f"genv{uuid.uuid4().hex[:6]}")
        for target, value in (
            ("core.executor._validate_environment", sys.executable),
            ("core.executor.ClaudeService.get_script_env", {}),
        ):
            p = mock.patch(target, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def _script(self, code="print('hi')", **fields):
        script = Script.objects.create(
            name=f"g-{uuid.uuid4().hex[:6]}", code=code, environment=self.env,
            workspace=self.ws, **fields,
        )
        self.addCleanup(remove_script_workdir, script.pk)
        return script

    def _run(self, script, **fields):
        fields.setdefault("status", Run.Status.PENDING)
        return Run.objects.create(script=script, workspace=self.ws, **fields)

    def _marker(self, script):
        return os.path.join(script_workdir(script.pk), MARKER)


class BackendTests(TestCase):
    """LocalSubprocessBackend on its own: real processes, no Run rows."""

    def _start(self, code, **spec):
        workdir = self.enterContext(_TempDir())
        env = {**os.environ, "PYTHONPATH": HELPERS}
        backend = local.LocalSubprocessBackend()
        handle = backend.start(RunSpec(cmd=[sys.executable, "-c", code], env=env, cwd=workdir, **spec))
        self.addCleanup(local.kill_process_tree, handle.pid)
        _wait_for(os.path.join(workdir, MARKER))
        return backend, handle

    def test_a_timeout_gives_a_cooperative_script_its_grace_period(self):
        backend, handle = self._start(COOPERATIVE, timeout=1, stop_grace=20)
        result = backend.wait(handle, 1)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.stop, STOP_GRACEFUL)
        self.assertIn("flushed partial work", result.stdout)

    def test_a_stubborn_script_is_killed_when_the_grace_period_is_up(self):
        backend, handle = self._start(STUBBORN, timeout=1, stop_grace=1)
        started = time.monotonic()
        result = backend.wait(handle, 1)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.stop, STOP_KILLED)
        self.assertGreaterEqual(time.monotonic() - started, 1.9)  # timeout + grace
        self.assertIn("ignoring the stop", result.stdout)

    def test_without_a_grace_period_it_is_killed_at_once(self):
        backend, handle = self._start(COOPERATIVE, timeout=1, stop_grace=0)
        result = backend.wait(handle, 1)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.stop, STOP_KILLED)
        self.assertNotIn("flushed", result.stdout)  # no stop signal, no callback

    def test_a_stop_request_waits_the_grace_period_then_kills(self):
        asked = []
        backend, handle = self._start(
            STUBBORN, timeout=600, stop_grace=1, stop_requested=lambda: bool(asked.append(1)) or True
        )
        started = time.monotonic()
        with _fast_poll():
            result = backend.wait(handle, 600)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.stop, STOP_KILLED)
        self.assertGreaterEqual(time.monotonic() - started, 1.0)
        self.assertTrue(asked)

    def test_a_command_that_ends_on_its_own_is_not_a_stop(self):
        backend = local.LocalSubprocessBackend()
        handle = backend.start(RunSpec(
            cmd=[sys.executable, "-c", "print('done')"], env=dict(os.environ), cwd=os.getcwd(),
            timeout=60, stop_grace=10, stop_requested=lambda: False,
        ))
        with _fast_poll():
            result = backend.wait(handle, 60)
        self.assertEqual((result.exit_code, result.stdout.strip(), result.stop), (0, "done", ""))

    @mock.patch.object(local.subprocess, "run")
    def test_the_stop_signal_never_reaches_this_process(self, run):
        if os.name == "nt":
            self.assertFalse(local.signal_process_tree(os.getpid()))
            run.assert_not_called()
            return
        with mock.patch.object(local.os, "killpg") as killpg:
            # Our own pid (or our own group), and a child in our group (it
            # doesn't lead its own group, like a reused pid): never signalled.
            self.assertFalse(local.signal_process_tree(os.getpid()))
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            self.addCleanup(child.kill)
            self.assertFalse(local.signal_process_tree(child.pid))
            local.kill_process_tree(child.pid)
        killpg.assert_not_called()

    def test_a_gone_process_is_reported_not_raised(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait(30)
        self.assertFalse(local.signal_process_tree(proc.pid))


class _TempDir:
    def __enter__(self):
        import tempfile

        self._dir = tempfile.TemporaryDirectory()
        return self._dir.name

    def __exit__(self, *exc):
        self._dir.cleanup()


class StopARunTests(_Fixture, TestCase):
    """execute_run with a stop requested through the real TaskService path."""

    def _stop_when_ready(self, run):
        """A stop poll that presses Stop once the script is ready, like a user."""
        marker = self._marker(run.script)

        def factory(run_pk):
            def poll():
                if not os.path.exists(marker):
                    return False
                current = Run.objects.get(pk=run_pk)
                if current.stop_requested_at is None:
                    self.stop_result = TaskService.force_stop_run(current)
                return True

            return poll

        return mock.patch("core.executor._stop_poll", side_effect=factory)

    def test_a_cooperative_script_flushes_and_ends_cancelled(self):
        run = self._run(self._script(COOPERATIVE, stop_grace_seconds=20))
        with _fast_poll(), self._stop_when_ready(run):
            execute_run(run)
        run.refresh_from_db()
        self.assertEqual(self.stop_result[0], True)
        self.assertIn("20 s to shut down", self.stop_result[1])
        self.assertEqual(run.status, Run.Status.CANCELLED)
        self.assertIn("flushed partial work", run.stdout)  # what it printed on its way out
        self.assertTrue(run.stderr.endswith("[Stopped by user after graceful shutdown]"), run.stderr)
        self.assertIsNotNone(run.stop_requested_at)
        self.assertGreaterEqual(run.ended_at, run.stop_requested_at)
        self.assertIsNone(run.pid)
        self.assertEqual(run.cancel_reason, "")

    def test_a_stubborn_script_is_killed_after_the_grace_period(self):
        run = self._run(self._script(STUBBORN, stop_grace_seconds=1))
        with _fast_poll(), self._stop_when_ready(run):
            execute_run(run)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.CANCELLED)
        self.assertIn("ignoring the stop", run.stdout)
        self.assertTrue(run.stderr.endswith("[Killed by user after the 1 s grace period]"), run.stderr)
        self.assertGreaterEqual(run.ended_at - run.stop_requested_at, timedelta(seconds=1))

    def test_without_a_grace_period_the_stop_kills_at_once(self):
        run = self._run(self._script(COOPERATIVE, stop_grace_seconds=0))
        with _fast_poll(), self._stop_when_ready(run):
            execute_run(run)
        run.refresh_from_db()
        self.assertEqual(self.stop_result, (True, "Run stopped — the script process was killed."))
        self.assertEqual(run.status, Run.Status.CANCELLED)
        self.assertNotIn("flushed", run.stdout)
        self.assertTrue(run.stderr.endswith("[Killed by user]"), run.stderr)

    def test_a_stop_requested_before_the_pid_was_recorded_still_reaches_the_script(self):
        # The web stamped the request while the worker was still preparing the
        # run, so it had no pid to signal: the worker sends it after the spawn.
        # (The signal may land before the script ignores it, so either note.)
        run = self._run(self._script(STUBBORN, stop_grace_seconds=1), stop_requested_at=timezone.now())
        with _fast_poll(), mock.patch.object(
            local.LocalSubprocessBackend, "signal_stop", autospec=True,
            side_effect=local.LocalSubprocessBackend.signal_stop,
        ) as signal_stop:
            execute_run(run)
        signal_stop.assert_called_once()
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.CANCELLED)
        self.assertRegex(
            run.stderr,
            r"\[(Stopped by user after graceful shutdown|Killed by user after the 1 s grace period)\]$",
        )

    def test_a_run_that_ended_before_the_stop_keeps_its_outcome(self):
        run = self._run(self._script("print('done')"))
        later = timezone.now() + timedelta(minutes=1)
        with mock.patch("core.executor._stop_requested_at", side_effect=[None, later]):
            execute_run(run)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.SUCCESS)
        self.assertNotIn("Stopped", run.stderr)


class StopPollTests(_Fixture, TestCase):
    def test_it_sees_a_stop_request(self):
        from core.executor import _stop_poll

        run = self._run(self._script(), status=Run.Status.RUNNING)
        poll = _stop_poll(run.pk)
        self.assertFalse(poll())
        Run.objects.filter(pk=run.pk).update(stop_requested_at=timezone.now())
        self.assertTrue(poll())

    def test_a_failing_read_is_not_a_stop_and_is_logged_once(self):
        from core.executor import _stop_poll

        poll = _stop_poll(uuid.uuid4())
        with mock.patch("core.executor.Run.objects.filter", side_effect=RuntimeError("db down")), \
                self.assertLogs("core.executor", "WARNING") as logs:
            self.assertFalse(poll())
            self.assertFalse(poll())
        self.assertEqual(len(logs.records), 1)


class TimeoutTests(_Fixture, TestCase):
    def test_a_timeout_gives_the_script_its_grace_period(self):
        run = self._run(self._script(COOPERATIVE, timeout_seconds=3, stop_grace_seconds=20))
        execute_run(run)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.TIMEOUT)
        self.assertEqual(run.exit_code, -1)
        self.assertIn("flushed partial work", run.stdout)
        self.assertEqual(run.stderr, "[TIMEOUT: Script exceeded 3 seconds]\n[Stopped after graceful shutdown]")

    def test_a_stubborn_script_is_killed_after_the_grace_period(self):
        run = self._run(self._script(STUBBORN, timeout_seconds=2, stop_grace_seconds=1))
        execute_run(run)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.TIMEOUT)
        self.assertEqual(run.stderr, "[TIMEOUT: Script exceeded 2 seconds]\n[Killed after the 1 s grace period]")
        self.assertGreaterEqual(run.ended_at - run.started_at, timedelta(seconds=3))

    def test_without_a_grace_period_the_message_is_unchanged(self):
        run = self._run(self._script(COOPERATIVE, timeout_seconds=1, stop_grace_seconds=0))
        execute_run(run)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.TIMEOUT)
        self.assertEqual(run.stderr, "[TIMEOUT: Script exceeded 1 seconds]")


class ForceStopTests(_Fixture, TestCase):
    """TaskService.force_stop_run, the web side, with the signals mocked."""

    def setUp(self):
        super().setUp()
        self.signal = self.enterContext(mock.patch.object(local, "signal_process_tree"))
        self.kill = self.enterContext(mock.patch("core.executor._kill_process_tree"))

    def _running(self, **script_fields):
        return self._run(
            self._script(**script_fields), status=Run.Status.RUNNING,
            started_at=timezone.now(), pid=4242, task_id="t-1",
        )

    def test_a_stop_is_requested_and_the_script_signalled(self):
        run = self._running(stop_grace_seconds=15)
        changed, message = TaskService.force_stop_run(run)
        self.assertTrue(changed)
        self.assertEqual(message, "Stopping the run: the script has 15 s to shut down, then it is killed.")
        self.signal.assert_called_once_with(4242)
        self.kill.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.RUNNING)  # the worker finishes it
        self.assertIsNotNone(run.stop_requested_at)
        self.assertTrue(run.is_stopping)
        self.assertEqual(run.stop_deadline, run.stop_requested_at + timedelta(seconds=15))
        self.assertIsNotNone(RunView._from_run(run).as_dict()["stop_requested_at"])

    def test_with_no_grace_period_the_script_is_killed_at_once(self):
        run = self._running(stop_grace_seconds=0)
        self.assertTrue(TaskService.force_stop_run(run)[0])
        self.kill.assert_called_once_with(4242)
        self.signal.assert_not_called()

    def test_stopping_a_stopping_run_kills_it_now(self):
        run = self._running()
        TaskService.force_stop_run(run)
        changed, message = TaskService.force_stop_run(Run.objects.get(pk=run.pk))
        self.assertEqual((changed, message), (True, "Run stopped — the script process was killed."))
        self.kill.assert_called_once_with(4242)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.CANCELLED)
        self.assertIn("[Killed by user]", run.stderr)
        self.assertIsNone(run.pid)
        self.assertIsNotNone(run.ended_at)

    def test_a_run_that_finished_meanwhile_is_left_alone(self):
        run = self._running()
        stale = Run.objects.get(pk=run.pk)
        Run.objects.filter(pk=run.pk).update(status=Run.Status.SUCCESS, pid=None)
        self.assertFalse(TaskService.force_stop_run(stale)[0])
        self.signal.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.SUCCESS)
        self.assertIsNone(run.stop_requested_at)

    def test_the_console_stop_view_requests_the_stop(self):
        user = User.objects.create(email="stop@example.com", is_superuser=True, is_staff=True)
        WorkspaceMembership.objects.update_or_create(
            user=user, workspace=self.ws, defaults={"role": WorkspaceMembership.ROLE_OWNER}
        )
        self.client.force_login(user)
        run = self._running()
        with mock.patch("core.services.setup_service.SetupService.is_setup_needed", return_value=False), \
                mock.patch("core.services.setup_service.SetupService.needs_admin_setup", return_value=False):
            resp = self.client.post(reverse("cpanel:task_force_stop", args=["t-1"]))
            self.assertEqual(resp.status_code, 200, resp.content)
            self.assertTrue(resp.json()["success"])
            page = self.client.get(reverse("cpanel:run_detail", args=[run.pk]))
            runs = self.client.get(reverse("cpanel:run_list"))
        self.assertContains(page, "Stopping: the script was asked to shut down")
        self.assertContains(page, "Kill now")
        self.assertContains(runs, ">stopping</span>")


class SettingTests(_Fixture, TestCase):
    def _form(self, script, **over):
        data = {
            "name": script.name, "description": "", "code": script.code,
            "environment": str(self.env.pk), "timeout_seconds": "60", "is_enabled": "on",
            "notify_on": "never", "isolation_mode": "inherit",
            **over,
        }
        return ScriptForm(data=data, instance=script, workspace=self.ws)

    def test_the_default_is_ten_seconds(self):
        self.assertEqual(self._script().stop_grace_seconds, 10)

    def test_the_form_saves_and_caps_it(self):
        script = self._script()
        form = self._form(script, stop_grace_seconds="45")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().stop_grace_seconds, 45)
        self.assertFalse(self._form(script, stop_grace_seconds="61").is_valid())
        form = self._form(Script.objects.get(pk=script.pk))  # left out: keeps the saved value
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().stop_grace_seconds, 45)

    def test_the_task_timeout_leaves_room_for_the_grace_period(self):
        script = self._script(timeout_seconds=100, stop_grace_seconds=30)
        self.assertEqual(script.max_run_seconds, 130)
        with mock.patch("core.tasks.async_task", return_value="t-9") as task:
            enqueue_run(self._run(script))
        self.assertEqual(task.call_args.kwargs["timeout"], 190)

    def test_a_fork_copies_it(self):
        user = User.objects.create(email="fork-grace@example.com", is_superuser=True)
        self.assertEqual(fork_script(self._script(stop_grace_seconds=33), user=user).stop_grace_seconds, 33)

    def test_backup_round_trips_it_and_the_stop_time(self):
        user = User.objects.create(email="backup-grace@example.com", is_superuser=True)
        script = self._script(stop_grace_seconds=25)
        stopped = self._run(
            script, status=Run.Status.CANCELLED, stop_requested_at=timezone.now(), ended_at=timezone.now()
        )
        stopping = self._run(script, status=Run.Status.RUNNING, stop_requested_at=timezone.now())
        data = BackupService.create_backup(include_runs=True)
        exported = next(s for s in data["scripts"] if s["id"] == str(script.pk))
        self.assertEqual(exported["stop_grace_seconds"], 25)
        self.assertIsNotNone(next(r for r in data["runs"] if r["id"] == str(stopped.pk))["stop_requested_at"])

        result = BackupService.restore_backup(data, restore_runs=True, current_user=user)
        self.assertTrue(result["success"], result)
        self.assertEqual(Script.objects.get(pk=script.pk).stop_grace_seconds, 25)
        self.assertIsNotNone(Run.objects.get(pk=stopped.pk).stop_requested_at)
        # An unfinished run never comes back half-stopped.
        self.assertIsNone(Run.objects.get(pk=stopping.pk).stop_requested_at)

    def test_an_older_backup_restores_the_defaults(self):
        user = User.objects.create(email="old-grace@example.com", is_superuser=True)
        script = self._script(stop_grace_seconds=25)
        run = self._run(script, status=Run.Status.CANCELLED, stop_requested_at=timezone.now())
        data = BackupService.create_backup(include_runs=True)
        for entry in data["scripts"]:
            entry.pop("stop_grace_seconds", None)
        for entry in data["runs"]:
            entry.pop("stop_requested_at", None)
        result = BackupService.restore_backup(data, restore_runs=True, current_user=user)
        self.assertTrue(result["success"], result)
        self.assertEqual(Script.objects.get(pk=script.pk).stop_grace_seconds, 10)
        self.assertIsNone(Run.objects.get(pk=run.pk).stop_requested_at)
