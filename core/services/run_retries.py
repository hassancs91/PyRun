"""
Retry on failure with backoff (2.5 Stage 1, ticket T06).

A script with ``retry_max_attempts`` > 0 gets that many more attempts when a
run fails (and when one times out, if ``retry_on_timeout`` is on). The rules:

- ``schedule_retry`` runs in the executor's finalize. For a run that failed,
  it creates the next attempt as a PENDING run with no task: same inputs,
  event and webhook or chat data, ``attempt`` one higher, ``retry_of`` the
  first attempt, and ``next_attempt_at`` = now + the delay. Attempt n + 1
  waits ``retry_delay_seconds * retry_backoff_multiplier ** (n - 1)``, at
  most a day.
- ``dispatch_due`` runs on the worker heartbeat, once a minute: it starts
  every waiting retry that is due, through ``core.tasks.queue_script_run``,
  so a retry obeys the script's concurrency policy like any trigger. The
  heartbeat's cadence is the granularity: a retry starts up to a minute after
  its delay ends.
- Runs that were cancelled (Stop, or skipped by the concurrency policy) never
  retry; neither does a run the stale-run reconciler failed, since it never
  passed through the executor's finalize.
- Notifications: a failed attempt with a retry after it stays quiet
  (``retry_follows``, checked by ``NotificationService.should_notify``). When
  a retry can't start (the script was disabled, retries were turned off, the
  concurrency policy skipped it, it couldn't be queued), the attempt before
  it was the last one after all, and its notification is sent then. A retry
  someone cancels sends nothing.

No row locks: the dispatcher claims a waiting retry with a conditional
UPDATE, so two heartbeats at once start it once.
"""

import datetime
import json
import logging

from django.utils import timezone

logger = logging.getLogger(__name__)

# How many due retries one heartbeat starts. More wait for the next one.
DISPATCH_BATCH = 100


def retry_delay(script, attempt: int) -> float:
    """Seconds to wait before the attempt after ``attempt`` (1-based)."""
    from core.models import Script

    multiplier = max(1.0, float(script.retry_backoff_multiplier or 1.0))
    delay = float(script.retry_delay_seconds or 0) * multiplier ** max(0, attempt - 1)
    return min(delay, float(Script.MAX_RETRY_DELAY_SECONDS))


def _retries(run, script) -> bool:
    """Whether ``run``'s outcome is one ``script`` retries."""
    from core.models import Run

    if run.status == Run.Status.FAILED:
        return True
    return run.status == Run.Status.TIMEOUT and script.retry_on_timeout


def schedule_retry(run, webhook_data=None):
    """Create the next attempt of ``run``, which just ended, if it should retry.

    Returns the waiting retry, or None. Reads the script's settings fresh, so
    a change made while the run was going applies to it.
    """
    from core.models import Run, Script

    if run.status not in (Run.Status.FAILED, Run.Status.TIMEOUT):
        return None
    script = Script.objects.filter(pk=run.script_id).first()
    if script is None or not script.can_run:
        return None
    if not _retries(run, script) or run.attempt > script.retry_max_attempts:
        return None

    root_id = run.retry_of_id or run.id
    if Run.objects.filter(retry_of_id=root_id, attempt=run.attempt + 1).exists():
        return None  # already scheduled

    due = timezone.now() + datetime.timedelta(seconds=retry_delay(script, run.attempt))
    retry = Run.objects.create(
        script=script,
        workspace_id=run.workspace_id,
        status=Run.Status.PENDING,
        triggered_by_id=run.triggered_by_id,
        trigger_type=run.trigger_type,
        code_snapshot=script.code,
        inputs=run.inputs,
        event=run.event,
        attempt=run.attempt + 1,
        retry_of_id=root_id,
        next_attempt_at=due,
        queued_payload=json.dumps(webhook_data) if webhook_data is not None else "",
    )
    logger.info(
        f"Run {run.id} {run.status}: attempt {retry.attempt} ({retry.id}) "
        f"is due at {due.isoformat()}"
    )
    return retry


def try_schedule_retry(run, webhook_data=None):
    """``schedule_retry`` for the executor's finalize: logs, never raises."""
    try:
        return schedule_retry(run, webhook_data)
    except Exception:
        logger.exception(f"Scheduling a retry of run {run.id} failed")
        return None


def next_attempt(run):
    """The attempt that came after ``run``, or None."""
    from core.models import Run

    return Run.objects.filter(
        retry_of_id=run.retry_of_id or run.id, attempt=run.attempt + 1
    ).first()


def previous_attempt(run):
    """The attempt ``run`` retried, or None (a first attempt, or it was deleted)."""
    from core.models import Run

    if run.attempt <= 1 or run.retry_of_id is None:
        return None
    if run.attempt == 2:
        return Run.objects.filter(pk=run.retry_of_id).first()
    return Run.objects.filter(
        retry_of_id=run.retry_of_id, attempt=run.attempt - 1
    ).first()


def retry_follows(run) -> bool:
    """True if a retry of ``run`` is waiting, going, or ran.

    A retry that ended without starting (cancelled or failed before it was
    queued) doesn't count, so the attempt before it reports its failure.
    """
    from core.models import Run

    after = next_attempt(run)
    if after is None:
        return False
    return after.status in (Run.Status.PENDING, Run.Status.RUNNING) or (
        after.started_at is not None
    )


def _notify_previous(run) -> None:
    """``run`` won't start, so the failed attempt before it was the last one:
    send that attempt's notification now. Best effort."""
    from core.services.notification_service import NotificationService

    try:
        previous = previous_attempt(run)
        if previous is not None:
            NotificationService.send_notification(previous)
    except Exception:
        logger.exception(f"Notification for the attempt before run {run.id} failed")


def _not_startable(run) -> str:
    """Why the waiting retry ``run`` must not start now ("" = it may)."""
    script = run.script
    if script.is_archived:
        return "the script is archived"
    if not script.is_enabled:
        return "the script is disabled"
    if run.attempt > 1 + script.retry_max_attempts:
        if script.retry_max_attempts == 0:
            return "retries were turned off for the script"
        return f"the script now retries at most {script.retry_max_attempts} times"
    return ""


def _dispatch(pk) -> bool:
    """Start one due retry. Returns True if it was queued (or is held)."""
    from core.models import Run
    from core.services.script_inputs import ScriptInputsError
    from core.tasks import queue_script_run

    run = Run.objects.select_related("script").filter(pk=pk).first()
    if run is None or run.next_attempt_at is None:
        return False
    payload = run.queued_payload
    now = timezone.now()
    # The claim: one conditional UPDATE, so only one dispatcher gets the run.
    # From here it looks like a run a trigger just created: PENDING, no task,
    # created now, running the script's current code.
    claimed = Run.objects.filter(
        pk=pk,
        status=Run.Status.PENDING,
        task_id="",
        queued_behind__isnull=True,
        next_attempt_at=run.next_attempt_at,
    ).update(next_attempt_at=None, created_at=now, code_snapshot=run.script.code, queued_payload="")
    if not claimed:
        return False
    run.refresh_from_db()

    reason = _not_startable(run)
    if reason:
        Run.objects.filter(pk=pk, status=Run.Status.PENDING, task_id="").update(
            status=Run.Status.CANCELLED,
            ended_at=now,
            stderr=f"[Retry not started: {reason}.]",
        )
        logger.info(f"Retry {run.id} not started: {reason}")
        _notify_previous(run)
        return False

    try:
        webhook_data = json.loads(payload) if payload else None
        queue_script_run(run, webhook_data)
    except ScriptInputsError as e:
        # queue_script_run deleted the row: the inputs no longer fit the schema.
        logger.warning(f"Retry {run.id} not started: {e.message}")
        _notify_previous(run)
        return False
    except Exception as e:
        logger.exception(f"Could not queue retry {run.id}")
        Run.objects.filter(pk=pk, status=Run.Status.PENDING).update(
            status=Run.Status.FAILED,
            stderr=f"Failed to queue the retry: {e}",
            ended_at=timezone.now(),
            task_id="",
            queued_payload="",
        )
        _notify_previous(run)
        return False

    if run.is_skipped:
        _notify_previous(run)
        return False
    logger.info(f"Started retry {run.id} (attempt {run.attempt})")
    return True


def dispatch_due(now=None) -> int:
    """The heartbeat's job: start every waiting retry that is due.

    Returns the number started (held ones included). One retry failing
    doesn't stop the others; the heartbeat must never fail over it.
    """
    from core.models import Run

    now = now or timezone.now()
    due = list(
        Run.objects.filter(
            status=Run.Status.PENDING,
            task_id="",
            queued_behind__isnull=True,
            next_attempt_at__lte=now,
        )
        .order_by("next_attempt_at", "id")
        .values_list("pk", flat=True)[:DISPATCH_BATCH]
    )
    started = 0
    for pk in due:
        try:
            if _dispatch(pk):
                started += 1
        except Exception:
            logger.exception(f"Starting retry {pk} failed")
    return started


def cancel_waiting(run) -> bool:
    """Cancel ``run`` if it is a retry still waiting for its delay.

    One conditional UPDATE, so a retry the heartbeat started in the meantime
    is left alone. Sends no notification: someone chose to drop it.
    """
    from core.models import Run

    return bool(
        Run.objects.filter(
            pk=run.pk,
            status=Run.Status.PENDING,
            task_id="",
            queued_behind__isnull=True,
            next_attempt_at__isnull=False,
        ).update(status=Run.Status.CANCELLED, ended_at=timezone.now(), queued_payload="")
    )


def attempt_info(run):
    """What the run page shows about attempts, or None for a run with no retries.

    ``total`` follows the script's current setting (never below ``attempt``).
    """
    after = next_attempt(run) if run.is_finished else None
    if run.attempt <= 1 and after is None:
        return None
    return {
        "attempt": run.attempt,
        "total": max(run.attempt, 1 + run.script.retry_max_attempts),
        "next": after,
    }
