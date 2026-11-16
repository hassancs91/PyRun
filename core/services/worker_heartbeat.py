"""The worker heartbeat written by the cluster process itself (2.5 run 7, O10-2).

Worker liveness (``GlobalSettings.worker_is_alive``: the dashboard card,
``system.health``, the 503 ``workers_unavailable`` of synchronous waits and
webhooks, the Telegram "asleep" reply) reads ``worker_heartbeat_at``. It used
to be written only by queued tasks: the minute heartbeat task and the end of
every task. When every worker was busy with a run longer than the staleness
window, no task ended and the heartbeat task waited behind the runs, so a
working cluster read as stopped.

``ClusterHeartbeat`` runs in the ``pyrunner_qcluster`` process, the parent of
django-q's sentinel, after the cluster has started: it writes the timestamp
every ``GlobalSettings.WORKER_HEARTBEAT_INTERVAL_SECONDS`` while the cluster
works, whatever the workers are doing. "Works" means the sentinel process is
alive, the cluster is started and not stopping, the sentinel's own status
report (django-q's ``Stat``, saved every guard cycle with a 3-second expiry)
says idle or working, and at least one of the worker processes it lists is
running. So nothing else writes it: not a web process, a shell or the test
runner (they never start a cluster), and not a cluster whose sentinel is
stuck or whose workers are all dead. The loop ends when the cluster stops,
and the heartbeat then ages out in the same window as before.

It is the main thread of that process, not a thread: the sentinel is forked
before the loop starts and nothing forks from this process afterwards.
"""

import logging
import time

from django.db import OperationalError, connection
from django.utils import timezone

logger = logging.getLogger(__name__)

# A tick that wrote nothing (cluster not working yet, database locked) is
# tried again this soon instead of a whole interval later.
RETRY_SECONDS = 5
# How often the loop looks at the stop signal and the clock.
POLL_SECONDS = 0.5


def _process_running(pid) -> bool:
    import psutil

    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except (psutil.Error, ValueError, TypeError):
        return False


def cluster_is_working(cluster) -> bool:
    """Whether ``cluster`` (a started ``django_q.cluster.Cluster``) is up with a live worker."""
    from django_q.conf import Conf
    from django_q.status import Stat

    sentinel, start_event, stop_event = cluster.sentinel, cluster.start_event, cluster.stop_event
    if sentinel is None or start_event is None or stop_event is None:
        return False
    if not sentinel.is_alive() or not start_event.is_set() or stop_event.is_set():
        return False
    stat = Stat.get(pid=cluster.pid, cluster_id=cluster.cluster_id)
    # Conf's states are lazy translations: compare them as text.
    if stat is None or str(stat.status) not in (str(Conf.IDLE), str(Conf.WORKING)):
        return False
    return any(_process_running(pid) for pid in stat.workers)


def write_heartbeat(now=None) -> None:
    """Set ``worker_heartbeat_at``: one small UPDATE (the row is created if missing)."""
    from core.models import GlobalSettings

    now = now or timezone.now()
    if not GlobalSettings.objects.filter(pk=1).update(worker_heartbeat_at=now):
        GlobalSettings.get_settings()
        GlobalSettings.objects.filter(pk=1).update(worker_heartbeat_at=now)


def _is_lock_error(exc) -> bool:
    return isinstance(exc, OperationalError) and "locked" in str(exc).lower()


class ClusterHeartbeat:
    """Writes the heartbeat for one running cluster (see the module docstring)."""

    def __init__(self, cluster, *, interval=None, clock=time.monotonic):
        from core.models import GlobalSettings

        self.cluster = cluster
        self.interval = interval or GlobalSettings.WORKER_HEARTBEAT_INTERVAL_SECONDS
        self.clock = clock
        self.next_at = 0.0
        self._warned = False

    def tick(self) -> bool:
        """Write the heartbeat when one is due and the cluster works; True when written."""
        now = self.clock()
        if now < self.next_at:
            return False
        written = False
        try:
            if cluster_is_working(self.cluster):
                write_heartbeat()
                written = True
        except Exception as exc:
            # SQLite's "database is locked" is expected now and then: quiet,
            # tried again in a few seconds. Anything else is logged once per
            # streak of failures, not every few seconds.
            if _is_lock_error(exc) or self._warned:
                logger.debug("Worker heartbeat not written: %s", exc)
            else:
                logger.warning("Worker heartbeat not written (%s); trying again every %s s.",
                               exc, RETRY_SECONDS)
                self._warned = True
        finally:
            # A fresh connection each tick: nothing held open between beats
            # (a database restart or an idle timeout can't break the next one).
            # Never inside a transaction (only a test calls tick() there).
            if not connection.in_atomic_block:
                connection.close()
        if written and self._warned:
            logger.info("Worker heartbeat written again.")
            self._warned = False
        self.next_at = now + (self.interval if written else RETRY_SECONDS)
        return written

    def run(self, sleep=time.sleep) -> None:
        """Tick until the cluster stops (its sentinel ends or a stop is signalled)."""
        sentinel, stop_event = self.cluster.sentinel, self.cluster.stop_event
        if sentinel is None or stop_event is None:
            return  # stopped while starting (django-q2 1.11 clears the events)
        while sentinel.is_alive() and not stop_event.is_set():
            self.tick()
            sleep(POLL_SECONDS)
