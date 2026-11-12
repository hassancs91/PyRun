"""
Internal Channels send API (loopback-only).

The only way an in-run script can deliver a chat message or email: a sandboxed
run has no DB / encryption key / provider access, so ``pyrunner_notify`` POSTs
here and PyRun does the send server-side. Authenticated by the same signed
per-run token the internal datastore API uses (loopback-only), so the run's
workspace is derived server-side and a run cannot send through another
workspace's channel.

POST /internal/channels/send
  body: {
    "target": "<channel name>" | "email",
    "text": "...",
    "subject": "...",       # email only (optional)
    "to": "...",            # email only (optional; defaults to core default)
    "reply_ref": {...},     # chat target override (e.g. {"chat_id": ...})
    "template": {...}       # WhatsApp only: {"name", "language", "components"};
                            # "text" is then optional (the log shows it)
    "media": {...}          # WhatsApp only: {"kind", "url", "caption", "filename"}
    "interactive": {...}    # WhatsApp only: reply buttons or a list
                            # (at most one of template / media / interactive)
  }

A chat send goes through the delivery ledger (``core.services.channels.ledger``)
and is recorded on the run:
  200 {"ok": true, "status": "sent", "id": ..., "result": {...}}
  202 {"ok": true, "status": "retrying", "id": ..., "error": ..., "next_attempt_at": ...}
      a temporary failure: PyRun sends it again later (resending would deliver twice)
  429 SEND_CAP_REACHED   the channel's daily send cap is used up (not sent)
  502 SEND_FAILED        rejected (bad token, unknown chat, ...): not retried
A send with a ``reply_ref`` is a reply (kind "reply": it counts toward the
channel's daily reply cap); one without is a script send (kind "script").
"""

import json
import logging

from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from core.ratelimit import rate_limit_exceeded
from core.views.api.decorators import internal_datastore_token_required

logger = logging.getLogger(__name__)

# Per-run send brake: a runaway script can't spam a provider. Generous enough
# for normal use; the cap is per run, not per channel (the channel's own
# daily_send_cap covers every run and kind).
_RUN_SEND_LIMIT = 30
_RUN_SEND_WINDOW = 60  # seconds
# A template's, file's or interactive message's JSON.
_CONTENT_MAX_BYTES = 16 * 1024


def _bad_request(message: str) -> JsonResponse:
    return JsonResponse(
        {"error": {"code": "BAD_REQUEST", "message": message}}, status=400
    )


def _structured_content(channel, target: str, key: str, value):
    """``({key: cleaned}, None)`` or ``(None, error sentence)``.

    A template needs a provider with ``supports_templates``; a file or an
    interactive message one with ``supports_rich_content``.
    """
    from core.services.channels import get_provider
    from core.services.channels.whatsapp import clean_interactive, clean_media, clean_template

    provider = get_provider(channel.provider)
    flag, what, clean = {
        "template": ("supports_templates", "templates", clean_template),
        "media": ("supports_rich_content", "files", clean_media),
        "interactive": ("supports_rich_content", "interactive messages", clean_interactive),
    }[key]
    if not getattr(provider, flag, False):
        return None, f"Channel '{target}' ({channel.get_provider_display()}) doesn't send {what}"
    try:
        cleaned = clean(value)
    except ValueError as e:
        return None, str(e)
    if len(json.dumps(cleaned)) > _CONTENT_MAX_BYTES:
        return None, f"'{key}' is too large"
    return {key: cleaned}, None


@csrf_exempt
@require_http_methods(["POST"])
@internal_datastore_token_required
def send(request: HttpRequest) -> JsonResponse:
    run_id = (getattr(request, "datastore_run", None) or {}).get("run_id")

    # Per-run rate limit (fixed window, shared helper).
    if rate_limit_exceeded(
        f"channel_send_rate_{run_id}", _RUN_SEND_LIMIT, _RUN_SEND_WINDOW
    ):
        return JsonResponse(
            {"error": {"code": "RATE_LIMITED", "message": "Per-run send rate exceeded."}},
            status=429,
        )

    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, TypeError):
        return _bad_request("Body must be valid JSON")
    if not isinstance(data, dict):
        return _bad_request("Body must be a JSON object")

    target = (data.get("target") or "").strip()
    text = data.get("text")
    structured = [key for key in ("template", "media", "interactive") if data.get(key) is not None]
    if not target:
        return _bad_request("Missing 'target'")
    if len(structured) > 1:
        return _bad_request(f"Send one of {' / '.join(structured)}, not several")
    if structured and target == "email":
        return _bad_request(f"'{structured[0]}' is for a WhatsApp channel, not email")
    if not structured and (not isinstance(text, str) or not text):
        return _bad_request("Missing 'text'")
    if text is not None and not isinstance(text, str):
        return _bad_request("'text' must be a string")

    # --- email target: delegate to PyRun core email ---
    if target == "email":
        from core.services import NotificationService

        html = data.get("html")
        if html is not None and not isinstance(html, str):
            return _bad_request("'html' must be a string")
        try:
            NotificationService.send_email(
                subject=data.get("subject") or "[PyRun] Notification",
                body=text,
                to=data.get("to") or None,
                html=html or None,
            )
        except Exception as e:
            logger.warning("channels send email failed for run %s: %s", run_id, e)
            return JsonResponse(
                {"error": {"code": "SEND_FAILED", "message": str(e)}}, status=502
            )
        return JsonResponse({"ok": True})

    # --- chat channel target: resolve within the run's workspace ---
    from core.models import Channel, ChannelMessage, Run
    from core.services.channels import ledger

    # An unscoped run reaches no channel (fail closed, F129).
    ws_id = getattr(request, "datastore_workspace", None)

    channel = Channel.objects.filter(workspace_id=ws_id, name=target).first()
    if channel is None:
        return JsonResponse(
            {"error": {"code": "CHANNEL_NOT_FOUND", "message": f"Channel '{target}' not found"}},
            status=404,
        )
    if not channel.enabled:
        return _bad_request(f"Channel '{target}' is disabled")

    reply_ref = data.get("reply_ref")
    if reply_ref is not None and not isinstance(reply_ref, dict):
        return _bad_request("'reply_ref' must be an object")

    content = None
    if structured:
        content, error = _structured_content(channel, target, structured[0], data[structured[0]])
        if error:
            return _bad_request(error)
        if not text:
            from core.services.channels.whatsapp import rich_text

            template = content.get("template")
            text = f"Template {template['name']}" if template else rich_text(content)

    # A reply answers an inbound message (pyrunner_notify.reply() passes its
    # reply_ref) and counts toward the channel's daily reply cap.
    kind = ChannelMessage.Kind.REPLY if reply_ref else ChannelMessage.Kind.SCRIPT
    run = Run.objects.filter(pk=run_id).first() if run_id else None
    row = ledger.deliver(channel, text, kind=kind, reply_ref=reply_ref, run=run, content=content)

    if row.status == ChannelMessage.Status.SENT:
        return JsonResponse(
            {"ok": True, "status": "sent", "id": str(row.id), "result": getattr(row, "send_result", None)}
        )
    if row.status == ChannelMessage.Status.RETRYING:
        return JsonResponse(
            {
                "ok": True,
                "status": "retrying",
                "id": str(row.id),
                "error": row.error,
                "next_attempt_at": row.next_attempt_at.isoformat() if row.next_attempt_at else None,
            },
            status=202,
        )
    code, status = "SEND_FAILED", 502
    if row.attempts == 0:  # refused before any attempt: the daily send cap
        code, status = "SEND_CAP_REACHED", 429
    return JsonResponse(
        {"error": {"code": code, "message": row.error, "id": str(row.id)}}, status=status
    )
