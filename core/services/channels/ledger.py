"""
Delivery ledger — the one funnel every outbound channel message goes through.

``deliver()`` records a ``ChannelMessage`` row (kind, run, attempts, status) and
sends it at once. A temporary failure (network, timeout, rate limit, a
provider-side 5xx) becomes ``retrying`` with ``next_attempt_at`` set by
``BACKOFF``; the worker heartbeat calls ``drain_due()`` to try again. Rejected
credentials and every other provider error fail at once: an auth error is
never retried. A channel's ``daily_send_cap`` covers notifications, replies
and script sends; a send over it is recorded as ``failed`` and not attempted.
PyRun's own notices (kind ``system``: approval pending, asleep, daily limit)
are recorded but sit outside the caps, so strangers messaging a bot can't use
the cap up; their own throttles bound them. The cap is checked before each
send without a lock, so sends at the same moment can pass it by a few.

Claims are conditional UPDATEs (no row locks, same on SQLite and Postgres). A
row being sent carries a lease in ``next_attempt_at``: if the process dies
mid-send, the heartbeat picks the row up again once the lease runs out.
Delivery is at-least-once.

Callers: run notifications (``NotificationService``), inbound replies (Py AI
and the script handler's synchronous answers, ``inbound.dispatch_inbound``) and
``pyrunner_notify`` (``core.views.api.channels_internal``).
"""

import logging
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

# Attempts per message, the first send included.
MAX_ATTEMPTS = 5
# Wait before attempt 2, 3, 4, 5 (seconds). The heartbeat runs once a minute,
# so a retry starts up to a minute after it is due.
BACKOFF = (60, 300, 900, 3600)
# How long a send in progress holds its row before the heartbeat may take it.
LEASE = timedelta(minutes=5)
# Rows the heartbeat tries per tick.
DRAIN_BATCH = 50
# Ledger rows (both directions) older than this are deleted daily.
RETENTION_DAYS = 30

SEND_CAP_MESSAGE = "Daily send cap reached ({cap} messages today); not sent."


def _model():
    from core.models import ChannelMessage

    return ChannelMessage


def backoff_seconds(attempts: int, retry_after=None) -> int:
    """The wait after failed attempt number ``attempts`` (1-based)."""
    delay = BACKOFF[min(max(attempts, 1), len(BACKOFF)) - 1]
    if retry_after:
        delay = max(delay, int(retry_after))
    return delay


def deliver(channel, text: str, *, kind: str, reply_ref: dict | None = None,
            run=None, run_id=None, note: str = ""):
    """Record and send one outbound message; returns its ``ChannelMessage``.

    Never raises for a provider failure: the outcome is on the row (``status``
    sent | retrying | failed, ``error``). ``note`` is kept in ``error`` on a
    successful send (e.g. why PyRun answered with an apology).
    """
    ChannelMessage = _model()
    now = timezone.now()
    row = ChannelMessage(
        channel=channel,
        direction=ChannelMessage.Direction.OUT,
        kind=kind,
        text=text,
        reply_ref_json=reply_ref or {},
        handler=channel.inbound_handler,
        run=run,
    )
    if run is None and run_id is not None:
        row.run_id = run_id
    if kind != ChannelMessage.Kind.SYSTEM and channel.send_cap_reached():
        row.status = ChannelMessage.Status.FAILED
        row.error = SEND_CAP_MESSAGE.format(cap=channel.daily_send_cap)
        row.save()
        logger.warning("Channel %s: daily send cap reached, %s message not sent", channel.id, kind)
        return row
    row.status = ChannelMessage.Status.PENDING
    row.attempts = 1
    row.next_attempt_at = now + LEASE
    row.save()
    _attempt(row, channel, note=note)
    return row


def _attempt(row, channel, *, note: str = "", now=None) -> None:
    """Send ``row`` (already claimed: status pending, attempts counted) and record the outcome.

    A retry's wait counts from ``now`` (the drain's tick), else from the send.
    """
    from core.services import ChannelService
    from core.services.channels import ChannelError

    ChannelMessage = _model()
    try:
        result = ChannelService.send(channel, row.text, reply_ref=row.reply_ref_json or None)
    except Exception as e:  # noqa: BLE001 — the outcome goes on the row, never up
        if not isinstance(e, ChannelError):
            logger.exception("Channel %s: unexpected send failure", channel.id)
        retryable = isinstance(e, ChannelError) and e.retryable
        error = str(e) or type(e).__name__
        now = now or timezone.now()
        if retryable and row.attempts < MAX_ATTEMPTS:
            delay = backoff_seconds(row.attempts, getattr(e, "retry_after", None))
            _record(row, status=ChannelMessage.Status.RETRYING, error=error,
                    next_attempt_at=now + timedelta(seconds=delay))
        else:
            _record(row, status=ChannelMessage.Status.FAILED, error=error, next_attempt_at=None)
        return
    # The provider's answer, for a caller that returns it (not stored).
    row.send_result = result
    message_id = (result or {}).get("message_id") if isinstance(result, dict) else None
    _record(
        row,
        status=ChannelMessage.Status.SENT,
        error=note,
        next_attempt_at=None,
        provider_message_id="" if message_id is None else str(message_id)[:128],
        delivery_state=ChannelMessage.DeliveryState.SENT,
    )


def _record(row, **fields) -> None:
    """Write the outcome of the attempt that claimed ``row`` (only if no one claimed it since)."""
    fields["updated_at"] = timezone.now()
    _model().objects.filter(pk=row.pk, attempts=row.attempts).update(**fields)
    for name, value in fields.items():
        setattr(row, name, value)


def drain_due(now=None) -> dict:
    """The heartbeat's job: retry every due delivery. Returns counts by outcome.

    One row failing doesn't stop the others; the heartbeat never fails over it.
    """
    ChannelMessage = _model()
    now = now or timezone.now()
    due = list(
        ChannelMessage.objects.filter(
            direction=ChannelMessage.Direction.OUT,
            status__in=(ChannelMessage.Status.PENDING, ChannelMessage.Status.RETRYING),
            next_attempt_at__lte=now,
        )
        .order_by("next_attempt_at", "id")
        .values_list("pk", flat=True)[:DRAIN_BATCH]
    )
    counts = {"sent": 0, "retrying": 0, "failed": 0}
    for pk in due:
        try:
            outcome = _retry(pk, now)
        except Exception:
            logger.exception("Retrying channel message %s failed", pk)
            continue
        if outcome in counts:
            counts[outcome] += 1
    return counts


def _retry(pk, now):
    ChannelMessage = _model()
    row = ChannelMessage.objects.select_related("channel").filter(pk=pk).first()
    if row is None or row.status not in (ChannelMessage.Status.PENDING, ChannelMessage.Status.RETRYING):
        return None
    if row.next_attempt_at is None or row.next_attempt_at > now:
        return None
    channel = row.channel
    capped = row.kind != ChannelMessage.Kind.SYSTEM and channel.send_cap_reached()
    if row.attempts >= MAX_ATTEMPTS or not channel.enabled or capped:
        if row.attempts >= MAX_ATTEMPTS:
            # Its last attempt was cut off (the sending process died).
            reason = row.error or "The last attempt was interrupted."
        elif not channel.enabled:
            reason = "The channel was disabled before the retry."
        else:
            reason = SEND_CAP_MESSAGE.format(cap=channel.daily_send_cap)
        claimed = ChannelMessage.objects.filter(
            pk=row.pk, status=row.status, attempts=row.attempts, next_attempt_at=row.next_attempt_at,
        ).update(status=ChannelMessage.Status.FAILED, next_attempt_at=None, error=reason, updated_at=now)
        return "failed" if claimed else None
    claimed = ChannelMessage.objects.filter(
        pk=row.pk, status=row.status, attempts=row.attempts, next_attempt_at=row.next_attempt_at,
    ).update(
        status=ChannelMessage.Status.PENDING,
        attempts=row.attempts + 1,
        next_attempt_at=now + LEASE,
        updated_at=now,
    )
    if not claimed:
        return None  # someone else took it
    row.status, row.attempts = ChannelMessage.Status.PENDING, row.attempts + 1
    _attempt(row, channel, now=now)
    return row.status


def display_rows(rows, mask_sets: dict | None = None) -> list[dict]:
    """Ledger rows for the console: ``[{"msg": row, "text": ..., "hidden": bool}]``.

    The text of a row a run sent is masked with that run's secrets (the
    stored text is raw, so a retry sends what the script sent); when a secret
    can't be resolved the text is withheld. ``mask_sets`` is the per-(workspace,
    script) cache ``core.executor.mask_run_value`` shares across one page.
    Inbound rows and Py AI replies have no run and are shown as stored.
    """
    from core.executor import mask_run_value
    from core.services.secret_backends import SecretResolutionError

    mask_sets = {} if mask_sets is None else mask_sets
    out = []
    for row in rows:
        text, hidden = row.text, False
        if row.run_id and row.run is not None:
            try:
                text = mask_run_value(row.run, row.text, mask_sets)
            except SecretResolutionError:
                text, hidden = "", True
        out.append({"msg": row, "text": text, "hidden": hidden})
    return out


def purge(now=None) -> int:
    """Delete ledger rows older than ``RETENTION_DAYS``; returns how many."""
    cutoff = (now or timezone.now()) - timedelta(days=RETENTION_DAYS)
    deleted, _ = _model().objects.filter(created_at__lt=cutoff).delete()
    return deleted
