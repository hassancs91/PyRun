"""Worker-start reconcile of orphaned runs (F32, 2.0).

After a container restart a run that was executing stayed "running" until the
heartbeat's deadline rule fired (started + timeout + 5 min — 65 min for the
default timeout). The all-in-one container's worker now fails, at start, the
RUNNING runs whose script process is gone; a worker started without the flag
(split worker containers) keeps the deadline rule.
"""

import datetime
import io
import os
import subprocess
import sys
import time
from unittest import mock

import psutil
from django.core.management import call_command
from django.test import TestCase

from core.models import Environment, Run, Script, Workspace


def _dead_pid() -> int:
    """A pid that existed a moment ago and is gone now."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


class ReconcileAtWorkerStartTests(TestCase):
    def setUp(self):
        self.ws = Workspace.get_default()
        env = Environment.objects.create(name="rc-env", path="rc-env")
        self.script = Script.objects.create(
            name="long", code="x", environment=env, workspace=self.ws,
            timeout_seconds=3600,
        )
        # The "worker" in these tests is this test process (the command reads its
        # own start time), so orphaned runs must predate it — the suite may have
        # been running for many minutes.
        self.proc_start = psutil.Process(os.getpid()).create_time()

    def _running(self, pid, started_at=None):
        ts = self.proc_start - 60 if started_at is None else started_at
        return Run.objects.create(
            script=self.script,
            workspace=self.ws,
            status=Run.Status.RUNNING,
            started_at=datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc),
            pid=pid,
        )

    def test_run_whose_process_is_gone_is_failed_at_once(self):
        run = self._running(_dead_pid())
        # The old deadline rule leaves it alone for another ~64 minutes.
        self.assertEqual(Run.reconcile_stale(), 0)

        n = Run.reconcile_orphans_at_worker_start(time.time())
        self.assertEqual(n, 1)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.FAILED)
        self.assertEqual(run.exit_code, -1)
        self.assertIsNone(run.pid)
        self.assertIsNotNone(run.ended_at)
        self.assertIn("worker restarted while this run was executing", run.stderr)

    def test_live_process_is_left_alone(self):
        # This test process is alive and older than the "worker".
        run = self._running(os.getpid())
        later = self.proc_start + 5
        self.assertEqual(Run.reconcile_orphans_at_worker_start(later), 0)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.RUNNING)

    def test_pid_reused_after_the_worker_started_counts_as_gone(self):
        # The pid exists, but its process was created after the worker started:
        # it is not the run's script.
        run = self._running(os.getpid())
        before = self.proc_start - 5
        self.assertEqual(Run.reconcile_orphans_at_worker_start(before), 1)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.FAILED)

    def test_runs_without_pid_or_started_after_the_worker_are_untouched(self):
        no_pid = self._running(None)
        newer = self._running(_dead_pid(), started_at=time.time() + 30)  # after the worker
        self.assertEqual(Run.reconcile_orphans_at_worker_start(time.time()), 0)
        for run in (no_pid, newer):
            run.refresh_from_db()
            self.assertEqual(run.status, Run.Status.RUNNING)

    def test_qcluster_reconciles_only_with_the_flag(self):
        run = self._running(_dead_pid())
        with mock.patch(
            "core.management.commands.pyrunner_qcluster.Command._run_cluster", return_value=None
        ), mock.patch(
            "core.management.commands.pyrunner_qcluster.apply_db_worker_settings",
            return_value={"workers": 1, "timeout": 1, "retry": 2, "queue_limit": 1},
        ):
            call_command("pyrunner_qcluster", stdout=io.StringIO())
            run.refresh_from_db()
            self.assertEqual(run.status, Run.Status.RUNNING)  # split worker: deadline rule

            call_command(
                "pyrunner_qcluster", "--reconcile-orphans", stdout=io.StringIO()
            )
            run.refresh_from_db()
            self.assertEqual(run.status, Run.Status.FAILED)
