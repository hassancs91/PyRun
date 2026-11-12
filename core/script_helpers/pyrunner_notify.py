"""
PyRun notifications API for scripts.

Send a message to a configured Channel, reply to an inbound chat message, or send
an email through PyRun's core email config — all server-side, so your script
never handles credentials. Mirrors ``pyrunner_datastore``: stdlib-only, talking
to PyRun's internal loopback API authenticated by a signed per-run token.

Usage:
    import pyrunner_notify

    pyrunner_notify.send("Ops Alerts", "Backup finished ✅")
    pyrunner_notify.email("Report ready", "See the dashboard.", to="me@example.com")
    pyrunner_notify.reply("On it!")   # only inside an inbound-triggered run (Phase 2)
    # WhatsApp: an approved template (needed outside the 24-hour window)
    pyrunner_notify.send("Support WA", reply_ref={"chat_id": "15551234567"},
                         template={"name": "order_update", "language": "en_US",
                                   "components": [{"type": "body", "parameters": [{"type": "text", "text": "1042"}]}]})
    # WhatsApp, inside the 24-hour window: a file by URL, reply buttons, a list
    pyrunner_notify.reply(media={"kind": "document", "url": "https://example.com/inv.pdf", "filename": "inv.pdf"})
    pyrunner_notify.reply(interactive={"type": "buttons", "body": "Anything else?",
                                       "buttons": [{"id": "yes", "title": "Yes"}, {"id": "no", "title": "No"}]})
"""

import json
import os
import urllib.error
import urllib.request


class NotifyError(Exception):
    """Raised when a notification cannot be delivered."""


def _post(payload: dict) -> dict:
    base = os.environ.get("PYRUNNER_INTERNAL_URL")
    token = os.environ.get("PYRUNNER_INTERNAL_TOKEN")
    if not base or not token:
        raise NotifyError(
            "PyRun notifications are not available in this run context."
        )
    url = f"{base.rstrip('/')}/internal/channels/send"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            body = json.loads(raw) if raw else {}
            message = body.get("error", {}).get("message") or raw.decode("utf-8", "ignore")
        except ValueError:
            message = raw.decode("utf-8", "ignore")
        raise NotifyError(f"Send failed (HTTP {e.code}): {message}")
    except urllib.error.URLError as e:
        raise NotifyError(f"Could not reach PyRun: {e}")


def send(
    channel: str,
    text: str = "",
    reply_ref: dict | None = None,
    template: dict | None = None,
    media: dict | None = None,
    interactive: dict | None = None,
) -> dict:
    """Send ``text`` to the named Channel (resolved in this run's workspace).

    Returns ``{"ok": True, "status": "sent", ...}``, or ``"status": "retrying"``
    when the provider failed for a temporary reason and PyRun will send it
    again later (don't resend, or it may arrive twice). Raises NotifyError when
    the send was refused or the channel's daily send cap is used up.

    ``template`` (WhatsApp channels only) sends an approved message template
    instead of text: ``{"name": ..., "language": "en_US", "components": [...]}``
    as in WhatsApp's Cloud API. ``text`` is then optional: it is what the
    channel's message log shows.

    ``media`` (WhatsApp) sends a file WhatsApp downloads from a URL:
    ``{"kind": "image" | "document" | "audio" | "video", "url": ...,
    "caption": ..., "filename": ...}`` (no caption for audio, a filename for a
    document only). ``interactive`` (WhatsApp) sends reply buttons,
    ``{"type": "buttons", "body": ..., "buttons": [{"id", "title"}, ...]}``
    (1-3), or a list, ``{"type": "list", "body": ..., "button": "Choose",
    "sections": [{"title": ..., "rows": [{"id", "title", "description"}]}]}``
    (up to 10 rows); both take an optional ``header`` and ``footer``. The
    person's choice arrives as a new inbound message whose event has
    ``details["reply"]["id"]``. Files and buttons only go to a number that
    messaged the channel in the last 24 hours. Send one of ``template``,
    ``media`` or ``interactive`` at a time.
    """
    payload = {"target": channel, "text": text}
    if reply_ref:
        payload["reply_ref"] = reply_ref
    for key, value in (("template", template), ("media", media), ("interactive", interactive)):
        if value is not None:
            payload[key] = value
    return _post(payload)


def email(
    subject: str, body: str, to: str | None = None, html: str | None = None
) -> dict:
    """Send an email via PyRun's configured core email backend.

    ``to`` defaults to the instance's default notification email when omitted.
    ``html`` optionally adds an HTML alternative; ``body`` stays the plain-text
    fallback for clients that don't render HTML.
    """
    payload = {"target": "email", "subject": subject, "text": body, "to": to}
    if html:
        payload["html"] = html
    return _post(payload)


def reply(
    text: str = "",
    *,
    template: dict | None = None,
    media: dict | None = None,
    interactive: dict | None = None,
) -> dict:
    """Reply to the inbound chat message that triggered this run.

    Available only when the run was triggered by an inbound channel message
    (Phase 2); PyRun injects ``INBOUND_CHANNEL`` + ``INBOUND_REPLY_REF``.
    ``template``, ``media`` and ``interactive`` are as in ``send()``.
    """
    channel = os.environ.get("INBOUND_CHANNEL")
    if not channel:
        raise NotifyError("reply() is only available inside an inbound-triggered run.")
    reply_ref = None
    raw = os.environ.get("INBOUND_REPLY_REF")
    if raw:
        try:
            reply_ref = json.loads(raw)
        except ValueError:
            reply_ref = None
    return send(channel, text, reply_ref=reply_ref, template=template, media=media, interactive=interactive)
