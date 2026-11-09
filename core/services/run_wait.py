"""
Waiting for a run's result inside the request that started it (2.5 Stage 2, T13).

``scripts.run`` with ``wait`` uses it; it is the one wait path for anything
else that answers with a run's result (the plan's MCP script tools and
webhook ``?sync=1``). A wait holds a web thread (a gunicorn gthread slot) for
up to ``MAX_WAIT_SECONDS``, so it is protected three ways:

1. **Workers first.** ``reserve`` refuses with ``WorkersUnavailable`` when no
   worker has sent a heartbeat recently (``GlobalSettings.worker_is_alive``,
   the window the dashboard uses). Nothing would ever finish the run, so the
   caller answers 503 at once instead of waiting out the budget. Call it
   before creating the run: a refused call leaves no run behind.
2. **A cap on waits in flight.** ``settings.SYNC_WAIT_SLOTS`` (env
   ``PYRUNNER_SYNC_WAIT_SLOTS``, default 4) slots, shared by every web
   process through the cache. A slot is a cache key taken with ``cache.add``
   (the database cache's INSERT, Redis ``SET NX``, LocMem's lock), not a
   read-and-increment counter, which loses counts on the database cache. The
   holder releases it on every exit path (``finally``); a process that dies
   mid-wait leaves a key that expires a little after the longest wait. On the
   database cache, two callers retaking the same expired key at once, or the
   cache culling a key, can let one wait more through, briefly. With every
   slot taken the run is still queued, and the caller answers with the run id
   at once (202).
3. **A budget.** At most ``MAX_WAIT_SECONDS``, and never past
   ``settings.SYNC_WAIT_MAX_SECONDS`` (10 s inside ``GUNICORN_TIMEOUT``, which
   would kill the thread), polling the run's status every 0.5 s at first,
   backing off to every 2 s. Past the budget the caller answers with the run
   id (202) and the run keeps going.

Usage::

    with run_wait.reserve(budget) as waiter:   # WorkersUnavailable -> 503
        run = ...create and queue the run...
        outcome = waiter.wait(run)              # FINISHED, TIMEOUT or BUSY
"""

import logging
import time
import uuid
from contextlib import contextmanager

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

MAX_WAIT_SECONDS = 55
# Left of a caller's own deadline (an MCP call's budget) to build the reply.
REPLY_MARGIN_SECONDS = 3
POLL_FIRST_SECONDS = 0.5
POLL_MAX_SECONDS = 2.0
POLL_BACKOFF = 1.5
# A slot outlives the longest wait by this much, so only a dead holder's slot
# ever expires on its own.
SLOT_SLACK_SECONDS = 30
SLOT_KEY = "pyrunner_sync_wait_slot_{}"

# What ``Waiter.wait`` reports.
FINISHED = "finished"  # the run ended within the budget
TIMEOUT = "timeout"  # the budget ran out; the run keeps going
BUSY = "busy"  # every wait slot was taken; the run was queued, not waited for

# Patched by tests; the loop never sleeps past the budget.
_sleep = time.sleep
_clock = time.monotonic


class WorkersUnavailable(Exception):
    """No worker is alive, so a waited-for run would never finish."""

    message = (
        "No worker is running, so the run would not start. Check Settings → "
        "Workers, or call again without wait to queue it."
    )
    # One heartbeat: a restarted worker reports in within a minute.
    retry_after = 60


def slot_count() -> int:
    return max(0, int(getattr(settings, "SYNC_WAIT_SLOTS", 4)))


def budget(requested=None, remaining=None) -> float:
    """Seconds to wait: ``requested`` (default and cap ``MAX_WAIT_SECONDS``, and
    ``settings.SYNC_WAIT_MAX_SECONDS``), leaving ``REPLY_MARGIN_SECONDS`` of the
    caller's ``remaining`` time."""
    cap = min(MAX_WAIT_SECONDS, getattr(settings, "SYNC_WAIT_MAX_SECONDS", MAX_WAIT_SECONDS))
    seconds = cap if requested is None else min(requested, cap)
    if remaining is not None:
        seconds = min(seconds, remaining - REPLY_MARGIN_SECONDS)
    return max(0, seconds)


def workers_alive() -> bool:
    from core.models import GlobalSettings

    return GlobalSettings.get_settings().worker_is_alive()


def _take_slot(ttl):
    """``(key, owner)`` of a free slot, or ``(None, None)`` when all are taken."""
    owner = uuid.uuid4().hex
    for index in range(slot_count()):
        key = SLOT_KEY.format(index)
        try:
            if cache.add(key, owner, ttl):
                return key, owner
        except Exception:
            # The cache is unreachable: treat it as full, the caller answers 202.
            logger.exception("sync_wait.slot_failed")
            break
    return None, None


def _release_slot(key, owner):
    try:
        # Only our own: an expired slot may already belong to someone else.
        if cache.get(key) == owner:
            cache.delete(key)
    except Exception:
        logger.exception("sync_wait.release_failed")


class Waiter:
    def __init__(self, seconds, held):
        self.seconds, self.held = seconds, held

    def wait(self, run) -> str:
        """Poll ``run`` until it ends or the budget is up; refreshes ``run``."""
        from core.models import Run

        if run.is_finished:
            return FINISHED
        if not self.held:
            return BUSY
        deadline = _clock() + self.seconds
        delay = POLL_FIRST_SECONDS
        while True:
            status = Run.objects.filter(pk=run.pk).values_list("status", flat=True).first()
            if status is None or status in Run.FINISHED_STATUSES:
                break
            left = deadline - _clock()
            if left <= 0:
                break
            _sleep(min(delay, left))
            delay = min(delay * POLL_BACKOFF, POLL_MAX_SECONDS)
        run.refresh_from_db()
        return FINISHED if run.is_finished else TIMEOUT


@contextmanager
def reserve(seconds):
    """Check the workers, then hold a wait slot for up to ``seconds``.

    Raises ``WorkersUnavailable`` before anything else. Yields a ``Waiter``;
    when no slot was free its ``wait`` returns ``BUSY`` at once. The slot is
    released when the block exits, however it exits.
    """
    if not workers_alive():
        raise WorkersUnavailable(WorkersUnavailable.message)
    key, owner = _take_slot(int(seconds) + SLOT_SLACK_SECONDS)
    try:
        yield Waiter(seconds, key is not None)
    finally:
        if key is not None:
            _release_slot(key, owner)
