"""A hard-killed worker must not leave a second cluster behind (F111).

Release test P6.04 (rc25): `kill -9` of the pyrunner_qcluster main process
left its django-q children (sentinel, pusher, monitor, workers) alive,
reparented to PID 1, still pulling tasks — and entrypoint.sh's monitor started
a replacement next to them: two clusters, twice the configured workers, two
schedulers, and "Restart workers" could only ever stop the new one.

The fix: pyrunner_qcluster leads its own process group, and the monitor kills
whatever is left of a dead worker's group before starting a new one.
"""

import os
import re
import shutil
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from core.management.commands import pyrunner_qcluster

ENTRYPOINT = Path(__file__).resolve().parent.parent / "entrypoint.sh"


def _shell_function(name):
    """The text of a bash function defined in entrypoint.sh."""
    text = ENTRYPOINT.read_text(encoding="utf-8")
    m = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.M | re.S)
    if not m:
        raise AssertionError(f"{name}() not found in entrypoint.sh")
    return m.group(0)


class QClusterLeadsProcessGroupTests(SimpleTestCase):
    @mock.patch.object(pyrunner_qcluster.Command, "_run_cluster")
    @mock.patch.object(pyrunner_qcluster, "apply_db_worker_settings")
    def test_handle_makes_the_cluster_a_process_group_leader(self, apply, _super):
        apply.return_value = {"workers": 2, "timeout": 60, "retry": 120, "queue_limit": 4}
        with (
            mock.patch.object(os, "getpgid", create=True, return_value=1),
            mock.patch.object(os, "getpid", return_value=4242),
            mock.patch.object(os, "setpgid", create=True) as setpgid,
        ):
            pyrunner_qcluster.Command().handle(reconcile_orphans=False)
        setpgid.assert_called_once_with(0, 0)

    def test_already_a_group_leader_is_left_alone(self):
        with (
            mock.patch.object(os, "getpgid", create=True, return_value=4242),
            mock.patch.object(os, "getpid", return_value=4242),
            mock.patch.object(os, "setpgid", create=True) as setpgid,
        ):
            pyrunner_qcluster.Command._lead_process_group()
        setpgid.assert_not_called()

    def test_session_leader_error_is_not_fatal(self):
        with (
            mock.patch.object(os, "getpgid", create=True, return_value=1),
            mock.patch.object(os, "getpid", return_value=4242),
            mock.patch.object(os, "setpgid", create=True, side_effect=PermissionError),
        ):
            pyrunner_qcluster.Command._lead_process_group()  # no exception


class EntrypointMonitorKillsLeftoversTests(SimpleTestCase):
    def test_monitor_stops_the_dead_workers_group_before_restarting(self):
        monitor = _shell_function("monitor_workers")
        self.assertIn('stop_leftover_workers "$pid"', monitor)
        self.assertLess(
            monitor.index("stop_leftover_workers"),
            monitor.index("start_qcluster"),
            "leftovers must be stopped before the replacement starts",
        )
        self.assertIn('kill -KILL -- "-$pid"', _shell_function("stop_leftover_workers"))

    @unittest.skipUnless(hasattr(os, "setpgid") and shutil.which("bash"), "POSIX + bash only")
    def test_leftover_children_of_a_killed_leader_are_killed(self):
        """Real processes: a group leader with a child; kill -9 the leader;
        the entrypoint function kills the reparented child."""
        code = (
            "import os, subprocess, sys, time\n"
            "os.setpgid(0, 0)\n"
            "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "print(c.pid, flush=True)\n"
            "time.sleep(60)\n"
        )
        leader = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        child = int(leader.stdout.readline())
        self.addCleanup(lambda: _kill_quietly(child))
        os.kill(leader.pid, signal.SIGKILL)
        leader.wait()
        os.kill(child, 0)  # the child survived its leader — the bug's precondition

        script = _shell_function("stop_leftover_workers") + f"stop_leftover_workers {leader.pid}\n"
        subprocess.run(["bash", "-c", script], check=True, timeout=30)

        deadline = time.time() + 10
        while time.time() < deadline and _alive(child):
            time.sleep(0.1)
        self.assertFalse(_alive(child), "the leftover child of the killed worker is still running")


def _alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # A zombie (killed, not yet reaped by its new parent) counts as gone.
    try:
        with open(f"/proc/{pid}/status") as f:
            return "\nState:\tZ" not in f.read()
    except OSError:
        return True


def _kill_quietly(pid):
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
