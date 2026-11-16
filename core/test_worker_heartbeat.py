"""
The worker heartbeat written by the cluster process (2.5 run 7, O10-2).

What this pins:

- ``ClusterHeartbeat.tick`` moves ``worker_heartbeat_at`` while no task runs at
  all (no queued task, no task ending), once per interval (a fake clock, no
  real sleeps), so a cluster whose workers are all busy with long runs still
  reads as alive.
- It writes nothing when the cluster isn't working: sentinel gone, not started
  yet, stopping, no status report from the sentinel, or every worker process
  dead. The real django-q status report (``Stat``) round-trips through the cache.
- ``run`` ends when the cluster stops, and the heartbeat then ages out in the
  usual 3-minute window (``worker_is_alive`` is unchanged).
- SQLite's "database is locked" is quiet and retried in a few seconds; another
  error is logged once per streak.
- ``pyrunner_qcluster`` runs the loop after starting the cluster (not with
  ``--run-once``); nothing else starts it.
- The dashboard card: a fresh beat outweighs a backlog of queued tasks; a
  stale heartbeat with stuck tasks still reads stopped.
"""

import os
import subprocess
import sys
import threading
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.db import OperationalError
from django.test import TestCase, override_settings
from django.utils import timezone
from django_q.conf import Conf
from django_q.models import OrmQ
from django_q.status import Stat

from core.management.commands.pyrunner_qcluster import Command as QClusterCommand
from core.models import GlobalSettings
from core.services import worker_heartbeat
from core.services.system_info_service import SystemInfoService
from core.services.worker_heartbeat import ClusterHeartbeat, cluster_is_working

INTERVAL = GlobalSettings.WORKER_HEARTBEAT_INTERVAL_SECONDS
STAT_GET = "django_q.status.Stat.get"


class FakeSentinel:
    def __init__(self, alive=True):
        self.alive = alive

    def is_alive(self):
        return self.alive


def fake_cluster(started=True, stopping=False, alive=True):
    start, stop = threading.Event(), threading.Event()
    if started:
        start.set()
    if stopping:
        stop.set()
    return SimpleNamespace(sentinel=FakeSentinel(alive), start_event=start, stop_event=stop,
                           pid=os.getpid(), cluster_id=uuid.uuid4())


def report(status=None, workers=None):
    """What the sentinel saves every guard cycle (the parts the heartbeat reads)."""
    return SimpleNamespace(status=status or Conf.WORKING, workers=[os.getpid()] if workers is None else workers)


def dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def heartbeat_at():
    return GlobalSettings.get_settings().worker_heartbeat_at


class TickTests(TestCase):
    def setUp(self):
        gs = GlobalSettings.get_settings()
        gs.worker_heartbeat_at = timezone.now() - timedelta(hours=1)
        gs.save(update_fields=["worker_heartbeat_at"])
        self.clock = Clock()

    def test_the_heartbeat_moves_with_no_task_at_all(self):
        before = heartbeat_at()
        self.assertFalse(GlobalSettings.get_settings().worker_is_alive())
        beat = ClusterHeartbeat(fake_cluster(), clock=self.clock)
        with mock.patch(STAT_GET, return_value=report()):
            self.assertTrue(beat.tick())
        self.assertGreater(heartbeat_at(), before)
        self.assertTrue(GlobalSettings.get_settings().worker_is_alive())
        self.assertEqual(OrmQ.objects.count(), 0)

    def test_one_write_per_interval(self):
        beat = ClusterHeartbeat(fake_cluster(), clock=self.clock)
        with mock.patch(STAT_GET, return_value=report()), \
                mock.patch.object(worker_heartbeat, "write_heartbeat") as write:
            self.assertTrue(beat.tick())
            self.clock.now += INTERVAL - 1
            self.assertFalse(beat.tick())
            self.clock.now += 1
            self.assertTrue(beat.tick())
        self.assertEqual(write.call_count, 2)

    def test_an_idle_cluster_beats_too(self):
        with mock.patch(STAT_GET, return_value=report(status=Conf.IDLE)):
            self.assertTrue(ClusterHeartbeat(fake_cluster(), clock=self.clock).tick())

    def test_long_runs_on_every_worker_keep_it_alive(self):
        # The O10-2 case: 10 minutes of busy workers, no task ends.
        beat = ClusterHeartbeat(fake_cluster(), clock=self.clock)
        start = timezone.now()
        with mock.patch(STAT_GET, return_value=report(status=Conf.WORKING)), \
                mock.patch("django.utils.timezone.now") as now:
            for step in range(0, 600, INTERVAL):
                now.return_value = start + timedelta(seconds=step)
                self.assertTrue(beat.tick())
                # Just before the next beat is due, it still reads alive.
                now.return_value += timedelta(seconds=INTERVAL - 1)
                self.assertTrue(GlobalSettings.get_settings().worker_is_alive())
                self.clock.now += INTERVAL
        self.assertEqual(heartbeat_at(), start + timedelta(seconds=600 - INTERVAL))

    def test_nothing_is_written_when_the_cluster_isnt_working(self):
        cases = {
            "sentinel gone": (fake_cluster(alive=False), report()),
            "not started yet": (fake_cluster(started=False), report()),
            "stopping": (fake_cluster(stopping=True), report()),
            "no status report": (fake_cluster(), None),
            "sentinel says stopped": (fake_cluster(), report(status=Conf.STOPPED)),
            "every worker dead": (fake_cluster(), report(workers=[dead_pid()])),
            "no workers": (fake_cluster(), report(workers=[])),
        }
        before = heartbeat_at()
        for name, (cluster, stat) in cases.items():
            with self.subTest(name), mock.patch(STAT_GET, return_value=stat):
                self.assertFalse(cluster_is_working(cluster))
                beat = ClusterHeartbeat(cluster, clock=self.clock)
                self.assertFalse(beat.tick())
                # Tried again soon, not a whole interval later.
                self.assertEqual(beat.next_at, self.clock.now + worker_heartbeat.RETRY_SECONDS)
        self.assertEqual(heartbeat_at(), before)

    def test_one_live_worker_is_enough(self):
        with mock.patch(STAT_GET, return_value=report(workers=[dead_pid(), os.getpid()])):
            self.assertTrue(cluster_is_working(fake_cluster()))

    def test_a_cleared_cluster_isnt_working(self):
        # Cluster.stop() sets its events to None once the sentinel has joined.
        cluster = fake_cluster()
        cluster.start_event = cluster.stop_event = None
        self.assertFalse(cluster_is_working(cluster))

    def test_the_sentinels_real_status_report(self):
        """A Stat saved like the sentinel's guard loop saves it, read back through the cache."""
        cluster = fake_cluster()
        self.assertFalse(cluster_is_working(cluster))  # nothing saved yet
        sentinel = SimpleNamespace(
            parent_pid=cluster.pid, pid=os.getpid(), cluster_id=cluster.cluster_id, broker=None,
            tob=timezone.now(), reincarnations=0, status=lambda: Conf.WORKING, monitor=None,
            pusher=None, pool=[SimpleNamespace(pid=os.getpid())],
            result_queue=SimpleNamespace(qsize=lambda: 0), task_queue=SimpleNamespace(qsize=lambda: 0),
        )
        Stat(sentinel).save()
        self.assertTrue(cluster_is_working(cluster))
        self.assertFalse(cluster_is_working(fake_cluster()))  # another cluster's report


class FailureTests(TestCase):
    def setUp(self):
        self.clock = Clock()
        patch = mock.patch(STAT_GET, return_value=report())
        patch.start()
        self.addCleanup(patch.stop)

    def test_database_locked_is_quiet_and_retried_soon(self):
        beat = ClusterHeartbeat(fake_cluster(), clock=self.clock)
        locked = OperationalError("database is locked")
        with mock.patch.object(worker_heartbeat, "write_heartbeat", side_effect=locked), \
                self.assertNoLogs("core.services.worker_heartbeat", "WARNING"):
            for _ in range(3):
                self.assertFalse(beat.tick())
                self.clock.now += worker_heartbeat.RETRY_SECONDS
        self.assertTrue(beat.tick())

    def test_another_error_is_logged_once_per_streak(self):
        beat = ClusterHeartbeat(fake_cluster(), clock=self.clock)
        with mock.patch.object(worker_heartbeat, "write_heartbeat", side_effect=OperationalError("gone")), \
                self.assertLogs("core.services.worker_heartbeat", "INFO") as logs:
            for _ in range(4):
                self.assertFalse(beat.tick())
                self.clock.now += worker_heartbeat.RETRY_SECONDS
        self.assertEqual(len([line for line in logs.output if line.startswith("WARNING")]), 1)
        with self.assertLogs("core.services.worker_heartbeat", "INFO") as logs:
            self.assertTrue(beat.tick())
        self.assertIn("written again", logs.output[0])


class RunLoopTests(TestCase):
    def test_the_loop_ends_when_the_cluster_stops(self):
        cluster = fake_cluster()
        clock = Clock()
        beat = ClusterHeartbeat(cluster, clock=clock)
        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            clock.now += seconds
            if len(sleeps) == 200:  # 100 s in: SIGTERM → Cluster.stop()
                cluster.stop_event.set()

        with mock.patch(STAT_GET, return_value=report()), \
                mock.patch.object(worker_heartbeat, "write_heartbeat") as write:
            beat.run(sleep=fake_sleep)
        self.assertEqual(len(sleeps), 200)
        self.assertEqual(set(sleeps), {worker_heartbeat.POLL_SECONDS})
        self.assertEqual(write.call_count, 4)  # 0, 30, 60, 90 s

    def test_the_loop_ends_when_the_sentinel_dies(self):
        cluster = fake_cluster()
        calls = []

        def fake_sleep(seconds):
            calls.append(seconds)
            cluster.sentinel.alive = False

        with mock.patch(STAT_GET, return_value=report()), \
                mock.patch.object(worker_heartbeat, "write_heartbeat"):
            ClusterHeartbeat(cluster, clock=Clock()).run(sleep=fake_sleep)
        self.assertEqual(len(calls), 1)

    def test_a_cluster_stopped_while_starting_has_no_loop(self):
        # django-q2 1.11: a SIGTERM during start() stops the cluster and clears its events.
        cluster = fake_cluster()
        cluster.sentinel.alive = False
        cluster.start_event = cluster.stop_event = None
        sleep = mock.Mock()
        with mock.patch.object(worker_heartbeat, "write_heartbeat") as write:
            ClusterHeartbeat(cluster, clock=Clock()).run(sleep=sleep)
        sleep.assert_not_called()
        write.assert_not_called()

    def test_a_stopped_cluster_ages_out_in_the_usual_window(self):
        gs = GlobalSettings.get_settings()
        self.assertEqual(GlobalSettings.WORKER_HEARTBEAT_TIMEOUT_SECONDS, 180)
        self.assertLess(INTERVAL * 3, GlobalSettings.WORKER_HEARTBEAT_TIMEOUT_SECONDS)
        gs.worker_heartbeat_at = timezone.now() - timedelta(seconds=179)
        self.assertTrue(gs.worker_is_alive())
        gs.worker_heartbeat_at = timezone.now() - timedelta(seconds=181)
        self.assertFalse(gs.worker_is_alive())


class CommandTests(TestCase):
    def test_the_cluster_runs_with_the_heartbeat(self):
        cluster = mock.Mock()
        with mock.patch("core.management.commands.pyrunner_qcluster.Cluster", return_value=cluster), \
                mock.patch("core.management.commands.pyrunner_qcluster.ClusterHeartbeat") as heartbeat:
            QClusterCommand._run_cluster({"run_once": False, "cluster_name": None})
        cluster.start.assert_called_once_with()
        heartbeat.assert_called_once_with(cluster)
        heartbeat.return_value.run.assert_called_once_with()
        cluster.stop.assert_not_called()

    def test_run_once_has_no_heartbeat(self):
        cluster = mock.Mock()
        with mock.patch("core.management.commands.pyrunner_qcluster.Cluster", return_value=cluster), \
                mock.patch("core.management.commands.pyrunner_qcluster.ClusterHeartbeat") as heartbeat:
            QClusterCommand._run_cluster({"run_once": True, "cluster_name": None})
        cluster.start.assert_called_once_with()
        cluster.stop.assert_called_once_with()
        heartbeat.assert_not_called()

    def test_no_cluster_starts_without_a_secret_key(self):
        # As django-q2 1.11's own qcluster handle: tasks are signed with it.
        with override_settings(SECRET_KEY=""), \
                mock.patch("core.management.commands.pyrunner_qcluster.Cluster") as cluster, \
                self.assertRaises(ImproperlyConfigured):
            QClusterCommand._run_cluster({"run_once": False, "cluster_name": None})
        cluster.assert_not_called()

    def test_handle_starts_the_cluster_this_way(self):
        command = QClusterCommand()
        with mock.patch("core.management.commands.pyrunner_qcluster.apply_db_worker_settings",
                        return_value={"workers": 2, "timeout": 600, "retry": 660, "queue_limit": 20}), \
                mock.patch.object(QClusterCommand, "_run_cluster") as run, \
                mock.patch.object(QClusterCommand, "_lead_process_group"):
            command.handle(run_once=False, cluster_name=None)
        run.assert_called_once()


class DashboardCardTests(TestCase):
    def _beat(self, seconds_ago):
        gs = GlobalSettings.get_settings()
        gs.worker_heartbeat_at = timezone.now() - timedelta(seconds=seconds_ago)
        gs.save(update_fields=["worker_heartbeat_at"])

    def _stuck_task(self):
        OrmQ.objects.create(key=Conf.PREFIX, payload="x", lock=timezone.now() - timedelta(minutes=5))

    def test_a_fresh_beat_outweighs_a_backlog(self):
        self._beat(10)
        self._stuck_task()
        self.assertEqual(SystemInfoService.get_worker_status()["status"], "running")

    def test_stuck_tasks_without_a_recent_beat_read_stopped(self):
        self._beat(2 * INTERVAL + 30)
        self._stuck_task()
        self.assertEqual(SystemInfoService.get_worker_status()["status"], "stopped")

    def test_the_heartbeat_window_alone(self):
        self._beat(120)
        self.assertEqual(SystemInfoService.get_worker_status()["status"], "running")
        self._beat(200)
        self.assertEqual(SystemInfoService.get_worker_status()["status"], "stopped")
