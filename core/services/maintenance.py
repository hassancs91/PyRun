"""
Maintenance windows in the run funnel (2.5 Stage 1, ticket T07).

``core.tasks.queue_script_run`` calls ``admit`` for every run, before the
concurrency policy. When an enabled window that applies to the run's
workspace (its own windows plus the global ones) is on and blocks the run's
trigger class, the run is marked CANCELLED with ``cancel_reason =
maintenance`` and a note in its stderr naming the window: it is listed as
skipped, it never retries, and it sends no notification. What each trigger
does with that outcome:

- a schedule tick: nothing more; the tick is skipped (no catch-up after the
  window);
- a webhook call: 503 with ``Retry-After`` (seconds until the window ends);
- Run now / Re-run, a chat message, a plugin: a message saying so;
- a retry coming due: dropped; the failed attempt before it notifies.

Runs already going are never touched, and a run held by the Queue
concurrency policy was admitted before the window, so it still starts when
its turn comes. The ``Pause all`` switch on Settings stays the instant control
for schedules.
"""

import datetime
import logging
import math
from dataclasses import dataclass

from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

# The trigger class of each event kind (core.services.run_events). A plugin's
# queue_run is a manual run: it is what a plugin page's buttons call.
_CLASS_FOR_KIND = {
    "manual": "manual",
    "plugin": "manual",
    "schedule": "scheduled",
    "webhook": "webhook",
    "channel": "channel",
    "api": "api",
}
_CLASS_FOR_TRIGGER = {"manual": "manual", "scheduled": "scheduled", "api": "api"}

# Back-to-back windows: "until" follows the chain at most this many times.
_MAX_CHAIN = 10


@dataclass(frozen=True)
class Block:
    """A window blocking a run: the first one found, and when the block ends
    (later than ``window``'s own end when another window takes over)."""

    window: object
    until: datetime.datetime

    def retry_after(self, now=None) -> int:
        """Whole seconds until the block ends, at least 1 (for ``Retry-After``)."""
        now = now or timezone.now()
        return max(1, math.ceil((self.until - now).total_seconds()))


def trigger_class(run) -> str:
    """The class a run's trigger belongs to: what a window's ``blocks`` names.

    Read from the run's event, so a webhook or chat run (trigger type API)
    counts as ``webhook`` / ``channel``, and a retry counts as the trigger
    that started its first attempt.
    """
    kind = run.event.get("kind") if isinstance(run.event, dict) else None
    if kind in _CLASS_FOR_KIND:
        return _CLASS_FOR_KIND[kind]
    return _CLASS_FOR_TRIGGER.get(str(run.trigger_type), "manual")


def windows_for(workspace_id, now=None):
    """The enabled windows that apply to a workspace: its own and the global ones."""
    from core.models import MaintenanceWindow

    scope = Q(workspace__isnull=True)
    if workspace_id:
        scope |= Q(workspace_id=workspace_id)
    now = now or timezone.now()
    # A one-off window that has ended can't apply any more.
    over = Q(kind=MaintenanceWindow.Kind.ONE_OFF, ends_at__lte=now)
    return MaintenanceWindow.objects.filter(scope, enabled=True).exclude(over)


def _until(windows, end):
    """Follow ``end`` through the windows that are on at it (back-to-back)."""
    for _ in range(_MAX_CHAIN):
        later = [occ[1] for w in windows if (occ := w.occurrence_at(end)) and occ[1] > end]
        if not later:
            break
        end = max(later)
    return end


def block_for(workspace_id, klass: str, now=None) -> Block | None:
    """The block on ``klass`` runs in a workspace at ``now``, or None."""
    now = now or timezone.now()
    windows = [w for w in windows_for(workspace_id, now) if w.blocks_class(klass)]
    active = [(w, occ) for w in windows if (occ := w.occurrence_at(now))]
    if not active:
        return None
    window, occ = max(active, key=lambda pair: pair[1][1])
    return Block(window=window, until=_until(windows, occ[1]))


def admit(run) -> Block | None:
    """Apply the maintenance windows to a new PENDING run.

    Returns None when no window blocks it (go on queueing it), else the
    ``Block``, after marking the run CANCELLED (``cancel_reason =
    maintenance``) with a stderr note naming the window. The in-memory ``run``
    is updated to match the row, and gets the block as ``run.maintenance_block``.
    """
    from core.models import Run

    klass = trigger_class(run)
    block = block_for(run.workspace_id or run.script.workspace_id, klass)
    if block is None:
        return None
    window = block.window
    note = (
        f'[Not started: maintenance window "{window.name}" blocks '
        f"{_class_label(klass)} until {window.format_local(block.until)} ({window.timezone})]"
    )
    Run.objects.filter(pk=run.pk, status=Run.Status.PENDING).update(
        status=Run.Status.CANCELLED,
        cancel_reason=Run.CancelReason.MAINTENANCE,
        ended_at=timezone.now(),
        stderr=note,
    )
    run.refresh_from_db(fields=["status", "cancel_reason", "ended_at", "stderr"])
    # For the caller's reply (a webhook's Retry-After, Run now's message).
    run.maintenance_block = block
    logger.info(f'Run {run.id} not started: maintenance window "{window.name}" ({klass})')
    return block


def _class_label(klass: str) -> str:
    from core.models import MaintenanceWindow

    return MaintenanceWindow.BLOCK_PHRASES.get(klass, klass)


def blocked_message(run, block: Block) -> str:
    """One sentence for the person (or chat) whose run a window blocked."""
    window = block.window
    return (
        f'Not started: the maintenance window "{window.name}" blocks '
        f"{_class_label(trigger_class(run))} until "
        f"{window.format_local(block.until)} ({window.timezone})."
    )


@dataclass(frozen=True)
class Banner:
    """What the dashboard says while windows are on."""

    windows: list
    until: datetime.datetime
    blocks: str


def banner(workspace_id, now=None) -> Banner | None:
    """The windows on right now for a workspace, for the dashboard; None if none."""
    from core.models.maintenance import describe_blocks

    now = now or timezone.now()
    windows = [w for w in windows_for(workspace_id, now) if w.blocks]
    active = [(w, occ) for w in windows if (occ := w.occurrence_at(now))]
    if not active:
        return None
    until = _until(windows, max(occ[1] for _, occ in active))
    blocked = {b for w, _ in active for b in (w.blocks or [])}
    return Banner(windows=[w for w, _ in active], until=until, blocks=describe_blocks(blocked))
