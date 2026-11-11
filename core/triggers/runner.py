"""
Checking watchers (2.5 Stage 7, T31).

``tick()`` runs every minute (``core.tasks.watchers_tick_task``, its own
django-q schedule, never the heartbeat) and checks every due watcher within a
time budget; ``check_watcher()`` is one check, also behind the console's
Check now. A check:

1. claims the row with a conditional UPDATE of ``checking_since`` (works on
   both engines without row locks). A claim older than ``CLAIM_SECONDS`` is
   abandoned (a worker died mid-check) and can be taken again; every write
   after the claim is conditional on it, so a worker that lost its claim
   changes nothing;
2. skips, leaving the state untouched, when the script can't run or a
   maintenance window blocks watcher runs; the fire cap's hour is used up
   (the remaining events wait for the next hour);
3. asks the adapter (``check(ctx, config, cursor)``) in a thread it stops
   waiting for after ``ADAPTER_TIMEOUT_SECONDS``;
4. queues one run per new event through ``core.tasks.queue_script_run``
   (inputs from ``input_map``; maintenance and the concurrency policy apply
   per run), skipping event ids already in the seen ring. The ring is saved
   after each run, so a crash mid-check never starts a run twice;
5. only after every run is queued, saves the adapter's new cursor. A failure
   anywhere keeps the old cursor: the next check reports the same events and
   the ring skips the ones that already have runs.

Failures back off (the interval doubles per consecutive failure, up to an
hour) and after ``Watcher.AUTO_DISABLE_AFTER`` in a row the watcher turns
itself off and its creator gets an email.
"""

import datetime
import json
import logging
import threading
import time
from dataclasses import dataclass

from django.db.models import F, Q
from django.utils import timezone

from core.triggers import registry
from core.triggers.adapters import ADAPTER_TIMEOUT_SECONDS, TEST_ITEMS, CheckContext, dig, with_defaults

logger = logging.getLogger(__name__)

# A claim older than this is abandoned (the adapter call is capped well below it).
CLAIM_SECONDS = 300
# A tick stops starting new checks after this many seconds (it runs every minute).
TICK_BUDGET_SECONDS = 45
# At most this many watchers are looked at per tick; the rest stay due.
MAX_CHECKS_PER_TICK = 200
# Longest error text kept on the watcher.
ERROR_MAX_CHARS = 1000
CAP_WINDOW = datetime.timedelta(hours=1)

# Outcomes of check_watcher.
OK = "ok"
BUSY = "busy"
NOT_FOUND = "not_found"
DISABLED = "disabled"
SCRIPT_UNAVAILABLE = "script_unavailable"
MAINTENANCE = "maintenance"
CAPPED = "capped"
FAILED = "failed"
LOST = "lost"


@dataclass
class CheckResult:
    outcome: str
    message: str = ""
    runs: int = 0  # runs queued (skipped/held/maintenance-cancelled rows included)


class _InputMapError(Exception):
    pass


def due(now=None):
    """Watchers whose check is due and not claimed by a live check."""
    from core.models import Watcher

    now = now or timezone.now()
    return Watcher.objects.filter(
        Q(next_check_at__isnull=True) | Q(next_check_at__lte=now),
        Q(checking_since__isnull=True) | Q(checking_since__lt=now - datetime.timedelta(seconds=CLAIM_SECONDS)),
        enabled=True,
    )


def _claim(watcher_id, now):
    from core.models import Watcher

    stale = now - datetime.timedelta(seconds=CLAIM_SECONDS)
    taken = Watcher.objects.filter(
        Q(checking_since__isnull=True) | Q(checking_since__lt=stale), pk=watcher_id,
    ).update(checking_since=now)
    if not taken:
        return None
    return Watcher.objects.select_related("script", "connection", "workspace", "created_by").get(pk=watcher_id)


def _write(watcher, claim, **fields) -> bool:
    """Update the claimed row; False when the claim was lost (taken over)."""
    from core.models import Watcher

    return bool(Watcher.objects.filter(pk=watcher.pk, checking_since=claim).update(**fields))


def _release(watcher, claim, **fields) -> bool:
    return _write(watcher, claim, checking_since=None, **fields)


def _interval(watcher):
    return datetime.timedelta(seconds=max(watcher.interval_seconds, watcher.INTERVAL_MIN_SECONDS))


def backoff_delay(watcher, failures: int) -> datetime.timedelta:
    """The wait after ``failures`` consecutive failed checks: the interval
    doubled per failure, at most an hour (or the interval, if longer)."""
    base = max(watcher.interval_seconds, watcher.INTERVAL_MIN_SECONDS)
    ceiling = max(watcher.BACKOFF_MAX_SECONDS, base)
    return datetime.timedelta(seconds=min(base * (2 ** min(failures, 20)), ceiling))


def _masks(watcher) -> dict:
    if not watcher.connection_id:
        return {}
    from core.services.connections import masks_for

    return masks_for(watcher.connection)


def _error_text(watcher, exc) -> str:
    from core.executor import mask_secrets_in_value

    text = str(exc).strip() or exc.__class__.__name__
    masks = _masks(watcher)
    if masks:
        text = mask_secrets_in_value(text, masks)
    return text[:ERROR_MAX_CHARS]


def _call_adapter(adapter, ctx, config, cursor):
    """``adapter.check(...)`` in a thread; TimeoutError if it outlasts the budget.

    A thread can't be killed: one that overruns keeps going in the
    background, but its result is dropped and nothing it returns is saved.
    """
    box = {}

    def target():
        from django.db import connection

        try:
            box["result"] = adapter.check(ctx, config, cursor)
        except BaseException as e:  # handed to the caller
            box["error"] = e
        finally:
            connection.close()

    thread = threading.Thread(target=target, name=f"watcher-{adapter.key}", daemon=True)
    thread.start()
    thread.join(ctx.timeout + 5)
    if thread.is_alive():
        raise TimeoutError(f"The check didn't finish within {ctx.timeout + 5:g} seconds.")
    if "error" in box:
        raise box["error"]
    return box["result"]


def _checked(result):
    """``(events, new_cursor)`` from an adapter, validated; ValueError if malformed."""
    if not isinstance(result, tuple) or len(result) != 2:
        raise ValueError("The adapter returned something other than (events, state).")
    events, new_cursor = result
    if not isinstance(events, list):
        raise ValueError("The adapter's events aren't a list.")
    clean = []
    for event in events:
        if not isinstance(event, dict) or event.get("id") in (None, ""):
            raise ValueError("An event from the adapter has no id.")
        clean.append({"id": str(event["id"]), "item": event.get("item")})
    try:
        json.dumps(new_cursor)
    except (TypeError, ValueError):
        raise ValueError("The adapter's new state isn't JSON.") from None
    return clean, new_cursor


def map_inputs(input_map, event: dict) -> dict:
    """The run's inputs from ``input_map`` (``{"input": "item.path"}``).

    Paths are dotted, read from ``{"event_id", "item", "watcher"}``; an
    absent value is left out, so the input's default applies.
    """
    inputs = {}
    for name, path in (input_map or {}).items():
        value = dig(event, str(path))
        if value is not None:
            inputs[name] = value
    return inputs


def _cap(watcher, now):
    """``(window_start, count)`` of the fire cap's current hour."""
    start = watcher.cap_window_start
    if start is None or now - start >= CAP_WINDOW:
        return now, 0
    return start, watcher.cap_window_count


def check_watcher(watcher_id) -> CheckResult:
    """Check one watcher now (see the module docstring). Never raises for an adapter failure."""
    from core.models import Watcher

    now = timezone.now()
    try:
        watcher = _claim(watcher_id, now)
    except Watcher.DoesNotExist:
        return CheckResult(NOT_FOUND, "The watcher no longer exists.")
    if watcher is None:
        if not Watcher.objects.filter(pk=watcher_id).exists():
            return CheckResult(NOT_FOUND, "The watcher no longer exists.")
        return CheckResult(BUSY, "A check of this watcher is already going.")
    claim = now
    try:
        return _check_claimed(watcher, claim)
    except Exception:
        # A bug, not an adapter failure: free the claim so the watcher isn't
        # stuck for CLAIM_SECONDS, then let the task log it.
        _release(watcher, claim, next_check_at=timezone.now() + _interval(watcher))
        raise


def _check_claimed(watcher, claim) -> CheckResult:
    from core.services import maintenance

    now = claim
    later = now + _interval(watcher)
    if not watcher.enabled:
        _release(watcher, claim)
        return CheckResult(DISABLED, "The watcher is off.")
    script = watcher.script
    if not script.can_run:
        _release(watcher, claim, next_check_at=later)
        state = "archived" if script.is_archived else "disabled"
        return CheckResult(SCRIPT_UNAVAILABLE, f'Not checked: the script "{script.name}" is {state}.')
    block = maintenance.block_for(watcher.workspace_id, "watcher", now)
    if block is not None:
        _release(watcher, claim, next_check_at=later)
        return CheckResult(
            MAINTENANCE,
            f'Not checked: maintenance window "{block.window.name}" blocks watcher runs until '
            f"{block.window.format_local(block.until)} ({block.window.timezone}).",
        )
    window_start, fired = _cap(watcher, now)
    remaining = watcher.fire_cap_per_hour - fired
    if remaining <= 0:
        resume = window_start + CAP_WINDOW
        _release(watcher, claim, next_check_at=max(resume, later), cap_window_start=window_start,
                 cap_window_count=fired)
        return CheckResult(
            CAPPED,
            f"Not checked: the fire cap ({watcher.fire_cap_per_hour} runs an hour) is reached; "
            "new items wait for the next hour.",
        )

    adapter = registry.get(watcher.adapter)
    try:
        if adapter is None:
            raise LookupError(f'The adapter "{watcher.adapter}" isn\'t available (a plugin removed?).')
        if adapter.requires_connection and watcher.connection_id is None:
            raise LookupError("This watcher needs a connection and has none (it was deleted?).")
        ctx = CheckContext(watcher=watcher, connection=watcher.connection, workspace=watcher.workspace,
                           limit=remaining, timeout=ADAPTER_TIMEOUT_SECONDS)
        config = with_defaults(adapter.config_schema, watcher.config)
        events, new_cursor = _checked(_call_adapter(adapter, ctx, config, watcher.cursor))
    except Exception as e:
        return _failed(watcher, claim, now, _error_text(watcher, e))

    # More than the cap allows: queue up to it, and keep the cursor where it
    # was so the rest are reported again in the next hour.
    advance = len(events) <= remaining
    events = events[:remaining]
    return _fire(watcher, claim, now, events, new_cursor if advance else watcher.cursor,
                 window_start, fired, capped=not advance)


def _fire(watcher, claim, now, events, new_cursor, window_start, fired, capped) -> CheckResult:
    from core.models import Run
    from core.services.run_events import watcher_event
    from core.services.script_inputs import ScriptInputsError
    from core.tasks import queue_script_run

    script = watcher.script
    seen = watcher.seen
    seen_set = set(seen)
    log = list(watcher.recent_events or [])
    ref = {"id": str(watcher.pk), "name": watcher.name, "adapter": watcher.adapter}
    queued = 0
    last_event_at = watcher.last_event_at
    for event in events:
        if event["id"] in seen_set:
            continue
        payload = {"event_id": event["id"], "item": event["item"], "watcher": ref}
        run = Run.objects.create(
            script=script,
            workspace_id=script.workspace_id,
            status=Run.Status.PENDING,
            triggered_by=None,
            trigger_type=Run.TriggerType.WATCHER,
            code_snapshot=script.code,
            inputs=map_inputs(watcher.input_map, payload),
            event=watcher_event(watcher, event["id"], event["item"], now),
        )
        try:
            queue_script_run(run)
        except ScriptInputsError as e:
            # queue_script_run deleted the pending row. This event can't start
            # a run until the input map or the script changes: a failure.
            _write(watcher, claim, state={"cursor": watcher.cursor, "seen": seen[-watcher.SEEN_MAX:]},
                   recent_events=log, cap_window_start=window_start, cap_window_count=fired,
                   last_event_at=last_event_at)
            return _failed(watcher, claim, now, f"Event {event['id']!r}: the script's inputs refused it. {e.message}")
        queued += 1
        fired += 1
        last_event_at = now
        seen.append(event["id"])
        seen_set.add(event["id"])
        log.insert(0, {"id": event["id"], "run": str(run.pk), "at": now.isoformat()})
        del log[watcher.EVENT_LOG_MAX:]
        # Saved per run: a crash between two runs can't start the first one again.
        if not _write(watcher, claim, state={"cursor": watcher.cursor, "seen": seen[-watcher.SEEN_MAX:]},
                      recent_events=log, cap_window_start=window_start, cap_window_count=fired,
                      last_event_at=last_event_at):
            return CheckResult(LOST, "The check was taken over by another worker.", queued)

    fields = {
        "state": {"cursor": new_cursor, "seen": seen[-watcher.SEEN_MAX:]},
        "recent_events": log,
        "cap_window_start": window_start,
        "cap_window_count": fired,
        "last_checked_at": now,
        "last_event_at": last_event_at,
        "error_count": 0,
        "last_error": "",
        "next_check_at": now + _interval(watcher),
    }
    if capped:
        fields["next_check_at"] = max(fields["next_check_at"], window_start + CAP_WINDOW)
    if not _release(watcher, claim, **fields):
        return CheckResult(LOST, "The check was taken over by another worker.", queued)
    if capped:
        logger.warning("Watcher %s reached its fire cap; the remaining events wait", watcher.pk)
        return CheckResult(CAPPED, f"Started {queued} run(s); the fire cap is reached, the rest wait.", queued)
    if queued:
        return CheckResult(OK, f"Started {queued} run{'s' if queued != 1 else ''}.", queued)
    if watcher.cursor is None and new_cursor is not None:
        return CheckResult(OK, "First check: marked where the source is. New items from now on start runs.")
    return CheckResult(OK, "Nothing new.")


def _failed(watcher, claim, now, message) -> CheckResult:
    failures = watcher.error_count + 1
    fields = {
        "last_checked_at": now,
        "last_error": message,
        "error_count": failures,
        "next_check_at": now + backoff_delay(watcher, failures),
    }
    disabled = failures >= watcher.AUTO_DISABLE_AFTER
    if disabled:
        fields.update(enabled=False, auto_disabled_at=now, next_check_at=None)
    if not _release(watcher, claim, **fields):
        return CheckResult(LOST, "The check was taken over by another worker.")
    logger.info("Watcher %s check failed (%s in a row): %s", watcher.pk, failures, message)
    if disabled:
        notify_auto_disabled(watcher, message)
        return CheckResult(FAILED, f"{message} The watcher failed {failures} times in a row and turned itself off.")
    return CheckResult(FAILED, message)


def notify_auto_disabled(watcher, message) -> bool:
    """Email the watcher's creator that it turned itself off. Never raises."""
    from core.services.notification_service import NotificationService

    user = watcher.created_by if watcher.created_by_id else None
    if user is None or not user.email:
        return False
    body = (
        f'The watcher "{watcher.name}" of the script "{watcher.script.name}" in the workspace '
        f'"{watcher.workspace.name}" failed {watcher.AUTO_DISABLE_AFTER} checks in a row, so PyRun '
        "turned it off.\n\n"
        f"The last error: {message}\n\n"
        "Fix the cause, then open the script's Triggers page and turn the watcher back on.\n"
    )
    try:
        NotificationService.send_email(
            NotificationService._subject(f'Watcher "{watcher.name}" was turned off'), body, to=user.email,
        )
    except Exception as e:
        logger.warning("Watcher %s turned off; its creator could not be emailed: %s", watcher.pk, e)
        return False
    return True


def tick(budget: float = TICK_BUDGET_SECONDS) -> dict:
    """Check every due watcher, oldest due first, until the budget is spent."""
    from core.models import GlobalSettings

    if GlobalSettings.get_settings().schedules_paused:
        return {"paused": True, "checked": 0}
    started = time.monotonic()
    ids = list(
        due().order_by(F("next_check_at").asc(nulls_first=True), "created_at")
        .values_list("pk", flat=True)[:MAX_CHECKS_PER_TICK]
    )
    outcomes = {}
    for watcher_id in ids:
        if time.monotonic() - started > budget:
            break
        try:
            result = check_watcher(watcher_id)
        except Exception:
            logger.exception("Watcher %s: check crashed", watcher_id)
            outcomes["crashed"] = outcomes.get("crashed", 0) + 1
            continue
        outcomes[result.outcome] = outcomes.get(result.outcome, 0) + 1
    return {"paused": False, "checked": sum(outcomes.values()), "outcomes": outcomes}


def test_adapter(adapter, config, connection=None, workspace=None) -> tuple[bool, str, list]:
    """The console's Test button: ``adapter.test`` on unsaved settings.

    Returns ``(ok, message, items)`` with the connection's secret values
    masked out of both. It reads the source only: no state, no runs.
    ``workspace`` is the watcher's (what a workspace resource is looked up in).
    """
    from core.executor import mask_secrets_in_value
    from core.models import Watcher

    probe = Watcher(adapter=adapter.key, connection=connection)
    ctx = CheckContext(watcher=None, connection=connection, workspace=workspace, timeout=ADAPTER_TIMEOUT_SECONDS)
    try:
        message, items = adapter.test(ctx, with_defaults(adapter.config_schema, config))
    except Exception as e:
        return False, _error_text(probe, e), []
    masks = _masks(probe)
    items = list(items or [])[:TEST_ITEMS]
    return True, mask_secrets_in_value(str(message), masks), mask_secrets_in_value(items, masks)
