"""
What happens when a script is triggered while it already has a run in flight:
its concurrency policy (2.5 Stage 1, ticket T05).

``Script.concurrency_policy`` is one of:

- ``allow`` (the default, and the only behaviour before 2.5): the new run
  starts alongside the others.
- ``skip``: the new run is created, then marked CANCELLED with
  ``cancel_reason = concurrency_skip``, so the runs list shows it was skipped.
- ``queue``: the new run stays PENDING with no task (it is "held"), its
  ``queued_behind`` set to the run ahead of it. Held runs start one at a time,
  oldest first, each when nothing older is in flight.

``admit`` applies the policy. ``core.tasks.queue_script_run`` calls it for
every run, after the inputs and libraries are stamped and before the task is
queued, so every trigger (Run now, Re-run, schedule, webhook, chat channel,
plugin SDK) gets the same rules. ``release`` starts the next held run of a
script. It runs when a run ends (the executor's finalize), when a run is
stopped or reconciled (each through ``try_release``, which never raises), and
once a minute on the worker heartbeat (``release_all``), the backstop for
anything those paths missed. ``cancel_held`` drops a run that is waiting.

No row locks (SQLite has none). Two rules keep it race-free on both engines:

- A run only counts the runs created BEFORE it (``created_at``, then ``id``).
  Of two runs triggered together, the later one sees the earlier one, and the
  earlier one never waits for the later.
- ``release`` only starts the OLDEST in-flight run of the script, and only
  if that run is held. It claims the row with a conditional UPDATE, so two
  releases at once start it once.
"""

import datetime
import json
import logging

from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

# A PENDING run that has no task yet and isn't held is either being queued
# right now (the trigger created the row and hasn't called queue_script_run
# yet) or orphaned (the process creating it died, or a backup restored it).
# It counts as in flight for this long, then stops blocking the script; the
# stale-run reconciler fails it after a day.
UNQUEUED_GRACE = datetime.timedelta(minutes=5)

# The task_id a run carries while ``release`` is queueing it, so it counts
# as started (and can't be claimed twice) before the real task id exists.
RELEASING = "releasing"

DISPATCH = "dispatch"
SKIP = "skip"
HOLD = "hold"


def in_flight(script_id, now=None):
    """The script's runs that count as in flight: running or about to run."""
    from core.models import Run

    now = now or timezone.now()
    # A retry waiting for its delay (core.services.run_retries) isn't about
    # to run: it comes back through queue_script_run when it is due.
    pending = Q(status=Run.Status.PENDING) & (
        ~Q(task_id="")
        | Q(queued_behind__isnull=False)
        | Q(created_at__gte=now - UNQUEUED_GRACE, next_attempt_at__isnull=True)
    )
    return Run.objects.filter(script_id=script_id).filter(
        Q(status=Run.Status.RUNNING) | pending
    )


def _ahead_of(run):
    """The newest in-flight run of the same script created before ``run``."""
    earlier = Q(created_at__lt=run.created_at) | Q(
        created_at=run.created_at, id__lt=run.id
    )
    return (
        in_flight(run.script_id)
        .filter(earlier)
        .exclude(pk=run.pk)
        .order_by("-created_at", "-id")
        .first()
    )


def admit(run, webhook_data=None) -> str:
    """Apply the script's concurrency policy to a new PENDING run.

    Returns ``DISPATCH`` (queue the task now), ``SKIP`` (the run was marked
    CANCELLED, skipped) or ``HOLD`` (the run waits for the runs ahead of it;
    ``webhook_data`` is kept on the row until it starts). The in-memory
    ``run`` is updated to match the row.
    """
    from core.models import Run, Script

    policy = run.script.concurrency_policy
    if policy == Script.ConcurrencyPolicy.ALLOW:
        return DISPATCH
    ahead = _ahead_of(run)
    if ahead is None:
        return DISPATCH

    now = timezone.now()
    if policy == Script.ConcurrencyPolicy.SKIP:
        Run.objects.filter(pk=run.pk, status=Run.Status.PENDING).update(
            status=Run.Status.CANCELLED,
            cancel_reason=Run.CancelReason.CONCURRENCY_SKIP,
            ended_at=now,
        )
        run.refresh_from_db(fields=["status", "cancel_reason", "ended_at"])
        logger.info(f"Run {run.id} skipped: run {ahead.id} of the script is in flight")
        return SKIP

    payload = json.dumps(webhook_data) if webhook_data is not None else ""
    Run.objects.filter(pk=run.pk, status=Run.Status.PENDING, task_id="").update(
        queued_behind=ahead.id, queued_payload=payload
    )
    logger.info(f"Run {run.id} queued behind run {ahead.id}")
    # The run ahead may have ended between the check above and the UPDATE,
    # before this run was marked held, so its release found nothing to start.
    release(run.script_id)
    run.refresh_from_db(fields=["status", "task_id", "queued_behind", "queued_payload"])
    return HOLD


def release(script_id):
    """Start the script's oldest held run if nothing older is in flight.

    Returns the run it started, or None. Safe to call any time, from any
    process, as often as you like: it starts at most one run, and only one
    of several concurrent calls gets to start it.
    """
    from core.models import Run
    from core.tasks import enqueue_run

    head = in_flight(script_id).order_by("created_at", "id").first()
    if head is None or not head.is_held:
        return None
    claimed = Run.objects.filter(
        pk=head.pk, status=Run.Status.PENDING, task_id=""
    ).update(task_id=RELEASING)
    if not claimed:
        return None  # another release got there first

    head.refresh_from_db()
    try:
        webhook_data = json.loads(head.queued_payload) if head.queued_payload else None
        task_id = enqueue_run(head, webhook_data)
    except Exception:
        # Fail it rather than leave it claimed: a claimed run counts as in
        # flight and would block the script until the stale-run reconciler.
        logger.exception(f"Could not start held run {head.id}")
        Run.objects.filter(pk=head.pk, status=Run.Status.PENDING).update(
            status=Run.Status.FAILED,
            stderr=Run.QUEUE_FAILED_TEXT,
            ended_at=timezone.now(),
            task_id="",
            queued_payload="",
        )
        return None
    Run.objects.filter(pk=head.pk).update(queued_payload="")
    head.task_id = task_id
    head.queued_payload = ""
    logger.info(f"Released held run {head.id} as task {task_id}")
    return head


def try_release(script_id):
    """``release``, for callers whose own work already succeeded (a run that
    finished, a Stop, the reconciler): a failure is logged, never raised. The
    heartbeat's ``release_all`` retries it within a minute."""
    try:
        return release(script_id)
    except Exception:
        logger.exception(f"Starting the next queued run of script {script_id} failed")
        return None


def release_all() -> int:
    """The heartbeat backstop: try ``release`` for every script with a held run.

    Returns the number of runs started. One script failing doesn't stop the
    others; the heartbeat must never fail over it.
    """
    from core.models import Run

    script_ids = set(
        Run.objects.filter(
            status=Run.Status.PENDING, task_id="", queued_behind__isnull=False
        ).values_list("script_id", flat=True)
    )
    return sum(1 for script_id in script_ids if try_release(script_id) is not None)


def cancel_held(run) -> bool:
    """Cancel ``run`` if it is still waiting its turn. Returns True if it was.

    One conditional UPDATE, so a run that ``release`` started in the meantime
    is left alone (it has a task by then): use Stop for a run that is going.
    """
    from core.models import Run

    cancelled = Run.objects.filter(
        pk=run.pk, status=Run.Status.PENDING, task_id="", queued_behind__isnull=False
    ).update(status=Run.Status.CANCELLED, ended_at=timezone.now(), queued_payload="")
    if cancelled:
        # If it was next in line (the run ahead had ended and its release
        # hadn't happened yet), the run behind it is next now.
        try_release(run.script_id)
    return bool(cancelled)
