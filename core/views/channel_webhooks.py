"""
Public inbound webhook for Channels (Phase 2).

Mounted at /channels/<token>/ (no auth — external chat services POST here). The
flow is deliberately front-loaded into the web process so we can fast-ack:

  IP rate-limit → resolve channel → provider URL-verification challenge →
  verify signature → delivery callbacks → then for each message: dedup →
  parse → per-sender rate-limit → approval inbox → worker-heartbeat
  preflight → enqueue → 200

A provider that batches (WhatsApp) splits one POST into one payload per
message (``split_inbound``) and reports delivery statuses in it
(``apply_status_updates``); Telegram sends one update per POST and no
statuses, so its responses are the same as before.

GET is accepted only for a provider's challenge (WhatsApp's ``hub.challenge``);
any other GET gets 405, as before.

All real handler work happens in the django-q worker (dispatch_inbound). Requires
a publicly reachable HTTPS instance — inbound does not work on localhost.
"""

import json
import logging

from django.core.cache import cache
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django_q.tasks import async_task

from core.models import Channel, ChannelMember, GlobalSettings
from core.ratelimit import client_ip, rate_limit_exceeded
from core.services.channels import get_provider

logger = logging.getLogger(__name__)

_IP_RATE_LIMIT = 60       # per IP per window
_SENDER_RATE_LIMIT = 20   # per (channel, sender) per window
_RATE_WINDOW = 60         # seconds
_DEDUP_TTL = 300
_REPLY_THROTTLE_TTL = 300


@csrf_exempt
@require_http_methods(["GET", "POST"])
def channel_webhook_view(request: HttpRequest, token: str) -> HttpResponse:
    # Coarse per-IP rate limit (cheap abuse brake before any work). client_ip honors
    # RATELIMIT_TRUSTED_PROXY_DEPTH so a proxy deploy doesn't collapse every caller
    # onto the proxy IP.
    ip = client_ip(request)
    if rate_limit_exceeded(f"channel_ip_rate_{ip}", _IP_RATE_LIMIT, _RATE_WINDOW):
        return JsonResponse({"error": "rate limited"}, status=429)

    channel = Channel.objects.filter(
        inbound_token=token, enabled=True, inbound_enabled=True
    ).first()
    if channel is None:
        return JsonResponse({"error": "not found"}, status=404)

    provider = get_provider(channel.provider)

    # URL verification handshake (WhatsApp GET hub.challenge, Slack
    # url_verification): answered before the signature check, as the
    # provider proves itself with the handshake's own token.
    try:
        challenge = provider.handle_challenge(channel, request)
    except Exception:
        logger.exception("Inbound challenge failed for channel %s", channel.id)
        return JsonResponse({"error": "bad request"}, status=400)
    if challenge is not None:
        return challenge
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    # Signature verification is the real authenticity gate.
    if not provider.verify_inbound(channel, request):
        logger.warning("Inbound signature rejected for channel %s", channel.id)
        return JsonResponse({"error": "unauthorized"}, status=401)

    try:
        payload = json.loads(request.body or b"{}")
    except (ValueError, TypeError):
        return JsonResponse({"error": "bad request"}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"error": "bad request"}, status=400)

    # Delivery callbacks (WhatsApp: sent / delivered / read / failed) update
    # the ledger. A failure here is logged; the messages are still handled.
    statuses = 0
    try:
        statuses = provider.apply_status_updates(channel, payload)
    except Exception:
        logger.exception("Applying delivery statuses failed for channel %s", channel.id)

    pieces = provider.split_inbound(payload)
    if not pieces:
        return JsonResponse({"status": "ok" if statuses else "ignored"})
    if len(pieces) == 1:
        body, status = _accept(channel, provider, pieces[0])
        return JsonResponse(body, status=status)
    results = [_accept(channel, provider, piece)[0].get("status", "error") for piece in pieces]
    return JsonResponse({"status": "batch", "results": results})


def _accept(channel, provider, payload):
    """Handle one inbound message's payload: ``(body, http status)``."""
    # Drop retried/duplicate deliveries (providers retry on slow acks).
    dedup = provider.dedup_id(payload)
    if dedup:
        dkey = f"channel_dedup_{channel.id}_{dedup}"
        if cache.get(dkey):
            return {"status": "duplicate"}, 200
        cache.set(dkey, 1, _DEDUP_TTL)

    try:
        msg = provider.parse_inbound(channel, payload)
    except (AttributeError, KeyError, TypeError, ValueError):
        # A payload with the right secret but an unexpected shape is the
        # caller's error, not ours: answer 400 instead of a 500.
        logger.warning("Unparseable inbound payload for channel %s", channel.id, exc_info=True)
        return {"error": "bad request"}, 400
    if msg is None:
        return {"status": "ignored"}, 200
    sender_id = msg.sender.get("id")

    # Per-sender rate limit (applies even to unapproved senders).
    if rate_limit_exceeded(
        f"channel_sender_rate_{channel.id}_{sender_id}",
        _SENDER_RATE_LIMIT,
        _RATE_WINDOW,
    ):
        return {"status": "rate_limited"}, 200

    channel.last_inbound_at = timezone.now()
    channel.save(update_fields=["last_inbound_at", "updated_at"])

    # Approval inbox — deny by default. The owner approves from the dashboard.
    if channel.inbound_access == Channel.InboundAccess.APPROVAL:
        decision = _approval_gate(channel, provider, msg, sender_id)
        if decision is not None:
            return decision, 200  # not approved → short-circuit

    # Worker-down preflight: a friendly reply beats a silent black hole.
    if not GlobalSettings.get_settings().worker_is_alive():
        _safe_send(channel, provider, msg, "💤 I'm asleep right now — please try again shortly.")
        return {"status": "worker_unavailable"}, 200

    async_task(
        "core.services.channels.inbound.dispatch_inbound",
        str(channel.id),
        payload,
        task_name=f"inbound-{channel.id}",
    )
    return {"status": "queued"}, 200


def _approval_gate(channel, provider, msg, sender_id):
    """Return a response body to short-circuit, or None if the sender is approved."""
    member = ChannelMember.objects.filter(channel=channel, sender_id=sender_id).first()
    if member is None:
        ChannelMember.objects.create(
            channel=channel,
            sender_id=sender_id,
            display_name=msg.sender.get("display_name", "") or "",
            status=ChannelMember.Status.PENDING,
            last_seen_at=timezone.now(),
        )
        _reply_once(
            channel, provider, msg, sender_id,
            "👋 You're not approved to use this bot yet. Ask the owner to grant you access.",
        )
        return {"status": "pending"}

    ChannelMember.objects.filter(pk=member.pk).update(last_seen_at=timezone.now())
    if member.status == ChannelMember.Status.BLOCKED:
        return {"status": "blocked"}
    if member.status == ChannelMember.Status.PENDING:
        _reply_once(channel, provider, msg, sender_id, "Still pending approval — hang tight.")
        return {"status": "pending"}
    return None  # approved


def _reply_once(channel, provider, msg, sender_id, text):
    """Reply at most once per sender per window (no amplification)."""
    key = f"channel_notapproved_{channel.id}_{sender_id}"
    if cache.get(key):
        return
    cache.set(key, 1, _REPLY_THROTTLE_TTL)
    _safe_send(channel, provider, msg, text)


def _safe_send(channel, provider, msg, text):
    """Send one of PyRun's own notices through the delivery ledger (never raises).

    A ``system`` send: logged like any message, outside the daily caps (its
    own throttles bound it), so strangers can't use up the channel's send cap.
    """
    from core.models import ChannelMessage
    from core.services.channels import ledger

    try:
        row = ledger.deliver(channel, text, kind=ChannelMessage.Kind.SYSTEM, reply_ref=msg.reply_ref)
    except Exception:
        logger.exception("Inbound auto-reply failed on channel %s", channel.id)
        return
    if row.status != ChannelMessage.Status.SENT:
        logger.warning("Inbound auto-reply on channel %s not sent (%s): %s", channel.id, row.status, row.error)
