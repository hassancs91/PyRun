"""
Restart-workers command tests.

Regression for review 1.1: the command used to send SIGUSR1 to PID 1 — which,
after entrypoint.sh's `exec gunicorn`, IS gunicorn ("reopen log files", a
no-op) — and then declared "Workers restarted successfully!" from the OLD
still-running worker's heartbeat. The restart mechanism is now SIGTERM to the
PID-file worker + the entrypoint monitor loop starting a replacement, and
success is confirmed ONLY by the PID file changing to a new, live PID.
"""

import os
import signal
import tempfile
import threading
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

# SimpleTestCase on purpose: the command must not need the database — the old
# heartbeat-based success check (the false-success vector) required it.


class RestartWorkersCommandTests(SimpleTestCase):
    def setUp(self):
        fd, self.pid_file = tempfile.mkstemp()
        os.close(fd)
        self.addCleanup(self._cleanup_pid_file)
        patcher = mock.patch(
            "core.management.commands.restart_workers.PID_FILE", self.pid_file
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _cleanup_pid_file(self):
        if os.path.exists(self.pid_file):
            os.unlink(self.pid_file)

    def _write_pid(self, pid):
        with open(self.pid_file, "w") as f:
            f.write(str(pid))

    def _monitor_rewrites_pid(self, pid, delay=0.5):
        """Simulate the entrypoint monitor loop starting a fresh worker."""
        t = threading.Timer(delay, self._write_pid, args=(pid,))
        t.start()
        self.addCleanup(t.cancel)

    @mock.patch("core.management.commands.restart_workers.os.kill")
    def test_sigterms_worker_and_never_signals_pid_1(self, kill):
        self._write_pid(4242)
        self._monitor_rewrites_pid(4243)
        out = StringIO()

        call_command("restart_workers", timeout=10, stdout=out)

        signalled = [c.args for c in kill.call_args_list]
        self.assertIn((4242, signal.SIGTERM), signalled)
        # The 1.1 regression: PID 1 is gunicorn, not the entrypoint — the
        # command must never signal it.
        self.assertNotIn(1, [args[0] for args in signalled])
        self.assertIn("New worker started with PID 4243", out.getvalue())

    @mock.patch("core.management.commands.restart_workers.os.kill")
    def test_unchanged_pid_file_is_failure_not_success(self, _kill):
        """No new worker => CommandError (exit 1), never a success message —
        regardless of anything else (heartbeats used to fake success here)."""
        self._write_pid(4242)

        with self.assertRaises(CommandError):
            call_command("restart_workers", timeout=2)

    def test_missing_pid_file_errors(self):
        os.unlink(self.pid_file)

        with self.assertRaises(CommandError):
            call_command("restart_workers", timeout=1)

    @mock.patch("core.management.commands.restart_workers.os.kill")
    def test_already_dead_worker_still_waits_for_monitor(self, kill):
        self._write_pid(4242)

        def kill_side_effect(pid, sig):
            if pid == 4242:
                raise OSError("No such process")
            return None  # sig-0 liveness probes on the new PID succeed

        kill.side_effect = kill_side_effect
        self._monitor_rewrites_pid(5555)
        out = StringIO()

        call_command("restart_workers", timeout=10, stdout=out)

        self.assertIn("New worker started with PID 5555", out.getvalue())


class TaskEndHeartbeatTests(TestCase):
    """A finished task moves the worker heartbeat (2.5 rc, F10-2).

    The post_execute receiver is a closure made in CoreConfig.ready(); connected
    weakly it was garbage-collected at once, so only the queued heartbeat task
    ever moved the heartbeat, and a cluster whose workers were all busy with
    long runs looked stopped after three minutes: "Workers stopped" on the
    dashboard, and 503 for synchronous webhook and API waits.
    """

    def test_post_execute_updates_the_heartbeat(self):
        import gc
        from datetime import timedelta

        from django.utils import timezone
        from django_q.signals import post_execute

        from core.models import GlobalSettings

        settings = GlobalSettings.get_settings()
        settings.worker_heartbeat_at = timezone.now() - timedelta(minutes=10)
        settings.save(update_fields=["worker_heartbeat_at"])
        gc.collect()  # a weakly connected closure would be gone now

        responses = post_execute.send(sender="django_q", task={"success": True})

        self.assertTrue(responses, "no live post_execute receiver")
        settings.refresh_from_db()
        self.assertTrue(settings.worker_is_alive())
