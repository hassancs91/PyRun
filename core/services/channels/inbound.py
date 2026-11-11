"""
Inbound dispatch — the worker side of the inbound pipeline.

Runs in a django-q worker (fast-ack already happened in the webhook view). It
parses the payload, logs the inbound message, runs the channel's handler, and
delivers any synchronous reply through the delivery ledger. The ``script``
handler returns None and replies asynchronously from its own run via
``pyrunner_notify.reply()``.
"""

import logging

logger = logging.getLogger(__name__)

CAP_NOTICE = "Daily limit reached — please try again tomorrow."


def dispatch_inbound(channel_id: str, payload: dict) -> None:
    from core.models import Channel, ChannelMessage
    from core.services.channels import get_handler, get_provider

    channel = Channel.objects.filter(id=channel_id).first()
    if channel is None:
        return
    provider = get_provider(channel.provider)
    msg = provider.parse_inbound(channel, payload)
    if msg is None:
        return

    ChannelMessage.objects.create(
        channel=channel,
        direction=ChannelMessage.Direction.IN,
        text=msg.text,
        sender_json=msg.sender,
        reply_ref_json=msg.reply_ref,
        handler=channel.inbound_handler,
        status=ChannelMessage.Status.RECEIVED,
    )

    handler = get_handler(channel.inbound_handler)
    if handler is None:
        return  # notify-only / unknown handler

    # Per-channel daily caps (the cost fuses), DB-counted so they hold across
    # processes. Over either one, the handler doesn't run: its reply couldn't
    # be sent, and a Py AI answer costs tokens.
    if channel.daily_cap_reached() or channel.send_cap_reached():
        _cap_notice(channel, msg)
        return

    try:
        out = handler(channel, msg)
    except Exception as e:  # noqa: BLE001 — any handler failure must still reply
        logger.exception("Inbound handler failed for channel %s", channel.id)
        _reply(
            channel, msg, "Sorry — something went wrong handling that.",
            note=f"The handler failed: {e}",
        )
        return

    if out is not None:
        _reply(channel, msg, out.text, reply_ref=out.reply_ref)


def _reply(channel, msg, text, *, reply_ref=None, note=""):
    """Deliver a synchronous reply through the ledger (never raises)."""
    from core.models import ChannelMessage
    from core.services.channels import ledger

    ref = reply_ref if reply_ref is not None else msg.reply_ref
    try:
        row = ledger.deliver(channel, text, kind=ChannelMessage.Kind.REPLY, reply_ref=ref, note=note)
    except Exception:
        logger.exception("Recording the inbound reply on channel %s failed", channel.id)
        return
    if row.status != ChannelMessage.Status.SENT:
        logger.warning("Inbound reply on channel %s not sent yet (%s): %s", channel.id, row.status, row.error)


def _cap_notice(channel, msg):
    """Tell the sender a cap is reached, once per conversation per day (never raises).

    The notice is a ``system`` send, outside both caps; sending it at most once
    a day to each chat keeps a chatty sender from getting one per message.
    """
    from core.models import ChannelMessage
    from core.services.channels import ledger

    ref = msg.reply_ref or {}
    already = channel.messages.filter(
        direction=ChannelMessage.Direction.OUT,
        kind=ChannelMessage.Kind.SYSTEM,
        created_at__gte=channel.day_start(),
    )
    if any((row.reply_ref_json or {}) == ref for row in already.only("reply_ref_json")):
        return
    try:
        ledger.deliver(channel, CAP_NOTICE, kind=ChannelMessage.Kind.SYSTEM, reply_ref=ref)
    except Exception:
        logger.exception("Recording the daily-limit notice on channel %s failed", channel.id)
