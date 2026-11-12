"""
What started a run: one event envelope for every trigger (2.5 Stage 0, T03).

Every place that starts a run builds ``Run.event`` with one of the builders
below and stamps it on the row when it creates the run.
``core.tasks.queue_script_run`` fills in a minimal one (``default_event``) for
a caller that didn't, so every run queued from 2.5 on has one. The script
reads it with ``pyrunner_run.event()`` (it is the ``event`` key of
``run.json``); the run page shows it in its Event card, secrets masked.

Every event has the same two keys, and the rest depends on the kind::

    {"kind": "webhook", "received_at": "2026-11-08T10:00:00.123456+00:00", ...}

========== =================================================================
kind       other keys
========== =================================================================
manual     ``user`` {id, email} when a user started it; ``rerun_of`` (the
           source run's id) for Re-run with same inputs
schedule   ``schedule_id``, ``run_mode``, ``fired_at``
webhook    ``method``, ``path``, ``remote_ip``, ``query``, ``content_type``,
           ``headers``; for a POST ``body_size``, ``body`` and (JSON bodies
           that parse) ``body_json``, ``body_binary`` for a body that isn't
           UTF-8 text. The values behind the ``WEBHOOK_*`` variables, plus
           the request details (core.services.webhook_request)
channel    ``channel`` (name), ``channel_id``, ``provider``, ``text``,
           ``sender``, ``reply_ref``, ``raw`` (the provider's payload): the
           message behind the ``INBOUND_*`` variables
plugin     ``plugin`` (its slug), ``script_key``, ``user`` when given
api        ``scripts.run`` over REST or MCP: ``token_id`` (the API token that
           started it) and ``via`` (``rest`` or ``mcp``); none for a run
           created with trigger type API and no event (``default_event``)
========== =================================================================

The ``WEBHOOK_*`` / ``INBOUND_*`` variables are not built from the event: they
still come from the task's ``webhook_data``, exactly as before 2.5.

An event is at most ``EVENT_MAX_BYTES`` as compact JSON (``cap_event``).
Stored raw, like ``Run.inputs``, because the script needs the real values;
anything that shows it masks secrets first.
"""

import logging
import math

from django.utils import timezone

from core.services.run_reports import json_size, result_text
from core.services.webhook_request import BODY_BASE64, raw_body

logger = logging.getLogger(__name__)

# The event is the trigger's payload, not a data transfer: bigger data belongs
# in a DataStore or Storage.
EVENT_MAX_BYTES = 1024 * 1024
# The run page shows at most this much of the event's JSON; the rest is in the
# download.
EVENT_PREVIEW_CHARS = 20_000

KIND_MANUAL = "manual"
KIND_SCHEDULE = "schedule"
KIND_WEBHOOK = "webhook"
KIND_CHANNEL = "channel"
KIND_PLUGIN = "plugin"
KIND_API = "api"
KIND_WATCHER = "watcher"

KIND_LABELS = {
    KIND_MANUAL: "Manual",
    KIND_SCHEDULE: "Schedule",
    KIND_WEBHOOK: "Webhook",
    KIND_CHANNEL: "Chat channel",
    KIND_PLUGIN: "Plugin",
    KIND_API: "API",
    KIND_WATCHER: "Watcher",
}

def _iso(when) -> str:
    return (when or timezone.now()).isoformat()


def _clean_str(text: str) -> str:
    # PostgreSQL can't hold U+0000 or an unpaired surrogate (which a JSON body
    # can spell as "\ud800") in jsonb, or in text taken out of a json column
    # (Run.event is json since T50); SQLite would keep them. Replace both
    # so an event is stored the same way on either engine and never fails the
    # INSERT.
    if "\x00" in text:
        text = text.replace("\x00", "�")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = text.encode("utf-8", "replace").decode("utf-8")
    return text


def _clean(value):
    """``value`` as JSON both engines store alike (see ``_clean_str``).

    NaN and Infinity (which Python's JSON parser accepts in a body) become
    None: neither engine's JSON can hold them.
    """
    if isinstance(value, str):
        return _clean_str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {_clean_str(str(k)): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    return value


def _cut_utf8(text: str, max_bytes: int) -> str:
    """``text`` cut to at most ``max_bytes`` of UTF-8, at a character boundary."""
    if max_bytes <= 0:
        return ""
    return text.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")


def cap_event(event: dict) -> dict:
    """``event`` made storable on both engines, at most ``EVENT_MAX_BYTES``.

    An event over the cap gets ``truncated: true`` and loses ``body_json``
    (the script can parse ``body``). If it is still over, ``body`` is cut to
    fit and ``body_truncated: true`` is set. If that isn't enough (or there is
    no body to cut), only ``kind``, ``received_at`` and ``truncated`` are kept.
    It never raises for a payload JSON can't encode (too deeply nested, say):
    that payload is left out the same way. The run always starts.

    A ``body_json`` nested too deep to store (Python's parser takes a few
    thousand levels, storing stops near 1,000) is not a size cut: the event
    keeps ``body`` as text without ``body_json`` and isn't marked
    ``truncated``, like a body that isn't JSON at all.
    """
    try:
        cleaned = _clean(event)
        if json_size(cleaned) <= EVENT_MAX_BYTES:
            return cleaned
    except (RecursionError, TypeError, ValueError) as e:
        if "body_json" in event:
            logger.warning(
                "Run event (%s) keeps the body as text only: its parsed JSON can't be stored (%s)",
                event.get("kind"), e,
            )
            return cap_event({k: v for k, v in event.items() if k != "body_json"})
        # Expected for a hostile body (nested too deep); a caller passing a
        # non-JSON value is a bug, so say what was dropped either way.
        logger.warning("Run event (%s) can't be stored as is (%s); reducing it", event.get("kind"), e)

    head = {"kind": event.get("kind"), "received_at": event.get("received_at"), "truncated": True}
    try:
        reduced = _clean({**{k: v for k, v in event.items() if k != "body_json"}, **head})
        size = json_size(reduced)
        if size <= EVENT_MAX_BYTES:
            return reduced
        body = reduced.get("body")
        if isinstance(body, str):
            reduced["body_truncated"] = True
            over = json_size(reduced) - EVENT_MAX_BYTES
            # Every character takes at least its UTF-8 length in the JSON, so
            # cutting ``over`` bytes of the body brings the event under the cap.
            reduced["body"] = _cut_utf8(body, len(body.encode("utf-8")) - over)
            if json_size(reduced) <= EVENT_MAX_BYTES:
                return reduced
    except (RecursionError, TypeError, ValueError) as e:
        logger.warning("Run event (%s) payload left out: it can't be stored (%s)", event.get("kind"), e)
    return head


def build_event(kind: str, received_at=None, **fields) -> dict:
    """An event of ``kind`` with ``fields``, capped (``cap_event``)."""
    return cap_event({"kind": kind, "received_at": _iso(received_at), **fields})


def _user(user) -> dict:
    return {"id": user.pk, "email": user.email}


def manual_event(user=None, *, rerun_of=None, received_at=None) -> dict:
    """Run now from the console (``rerun_of``: the run a re-run copies)."""
    fields = {}
    if user is not None:
        fields["user"] = _user(user)
    if rerun_of is not None:
        fields["rerun_of"] = rerun_of.hex
    return build_event(KIND_MANUAL, received_at, **fields)


def schedule_event(schedule, fired_at=None) -> dict:
    """A schedule tick: which schedule fired, and when (``fired_at`` = ``received_at``)."""
    fired_at = fired_at or timezone.now()
    return build_event(
        KIND_SCHEDULE,
        fired_at,
        schedule_id=schedule.id.hex,
        run_mode=str(schedule.run_mode),
        fired_at=fired_at.isoformat(),
    )


def webhook_event(webhook_data: dict, received_at=None, *, path=None, headers=None, remote_ip=None) -> dict:
    """A webhook call: the request data the ``WEBHOOK_*`` variables carry, plus
    the request's ``path``, ``headers`` and ``remote_ip`` when given.

    A call with a body also gets ``body_size`` (bytes, as received) and, when
    the body isn't UTF-8 text, ``body_binary: true`` (its ``body`` is then "";
    the bytes are in the run's body file).
    """
    # Request line first, the (possibly long) body last: the order the run
    # page's Event card shows them in.
    request = {"method": webhook_data.get("method"), "path": path, "remote_ip": remote_ip}
    fields = {key: value for key, value in request.items() if value is not None}
    for key in ("query", "content_type"):
        if key in webhook_data:
            fields[key] = webhook_data[key]
    if headers is not None:
        fields["headers"] = headers
    body = raw_body(webhook_data)
    if body is not None:
        fields["body_size"] = len(body)
        if BODY_BASE64 in webhook_data:
            fields["body_binary"] = True
    for key in ("body", "body_json"):
        if key in webhook_data:
            fields[key] = webhook_data[key]
    return build_event(KIND_WEBHOOK, received_at, **fields)


def channel_event(channel, msg) -> dict:
    """An inbound chat message (``core.services.channels.base.InboundMessage``).

    A typed message (WhatsApp) adds ``message_type``, ``message_id`` and, when
    it has them, ``details`` (a button reply, location, contacts, reaction)
    and ``media`` (attachments: metadata, plus ``size`` and a ``path`` or
    ``url`` when the channel downloads them, see
    ``core.services.channels.media``); a Telegram event is unchanged.
    """
    typed = {}
    if getattr(msg, "message_type", ""):
        typed["message_type"] = msg.message_type
        typed["message_id"] = msg.message_id or ""
        if msg.details:
            typed["details"] = msg.details
        if msg.media:
            typed["media"] = msg.media
    return build_event(
        KIND_CHANNEL,
        msg.received_at,
        channel=channel.name,
        channel_id=channel.id.hex,
        provider=msg.provider or channel.provider,
        text=msg.text or "",
        sender=msg.sender or {},
        reply_ref=msg.reply_ref or {},
        **typed,
        raw=msg.raw or {},
    )


def plugin_event(plugin, script_key, user=None) -> dict:
    """``ScriptAPI.queue_run`` from a plugin (``plugin`` is None in the SDK's owner=None lane)."""
    fields = {"plugin": plugin, "script_key": script_key}
    if user is not None:
        fields["user"] = _user(user)
    return build_event(KIND_PLUGIN, None, **fields)


def api_event(token_id, via) -> dict:
    """``scripts.run`` from an API token (``via``: ``rest`` or ``mcp``)."""
    return build_event(KIND_API, None, token_id=str(token_id), via=str(via))


def watcher_event(watcher, event_id, item, received_at=None) -> dict:
    """A watcher saw a new item: which watcher (id, name, adapter), the item's
    ``event_id`` (the de-duplication key) and the ``item`` itself.

    An item too big for the event cap is left out (``truncated: true``); the
    watcher and the event id always stay, so the run knows what it was for.
    """
    ref = {"id": str(watcher.pk), "name": watcher.name, "adapter": watcher.adapter}
    event = build_event(KIND_WATCHER, received_at, watcher=ref, event_id=str(event_id), item=item)
    if "item" not in event:
        event = build_event(KIND_WATCHER, received_at, watcher=ref, event_id=str(event_id), truncated=True)
    return event


_KIND_FOR_TRIGGER = {"manual": KIND_MANUAL, "scheduled": KIND_SCHEDULE, "api": KIND_API, "watcher": KIND_WATCHER}


def default_event(run) -> dict:
    """The minimal event for a run created without one: kind from its trigger type."""
    kind = _KIND_FOR_TRIGGER.get(str(run.trigger_type), str(run.trigger_type))
    return build_event(kind, run.created_at)


def kind_label(kind) -> str:
    return KIND_LABELS.get(kind, str(kind or "Unknown").capitalize())


def event_text(event) -> str:
    """The event as indented JSON, keys in stored order: the result's display form."""
    return result_text(event)
