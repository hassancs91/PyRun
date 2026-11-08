"""
Inbound handler registry — what an inbound message *does*.

The ``script`` handler (chat → run a script) and ``pyai`` (the read-only Py AI
assistant) are both live; ``agent`` is RESERVED for the future Agents plan. A handler takes
``(channel, InboundMessage)`` and returns an ``OutboundMessage`` to reply
synchronously, or ``None`` when the reply happens asynchronously — the script
handler queues a Run that replies later via ``pyrunner_notify.reply()``.
"""

import logging
import re

from .base import OutboundMessage

logger = logging.getLogger(__name__)

_HANDLERS = {}


def register_handler(key: str, fn):
    _HANDLERS[key] = fn
    return fn


def get_handler(key: str):
    return _HANDLERS.get(key)


def _script_handler(channel, msg):
    """Run the linked script, passing the inbound message as INBOUND_* env.

    The run's event (``channel_event``) carries the same message, plus the
    provider's raw payload, for ``pyrunner_run.event()``. Returns None: the script replies asynchronously via ``pyrunner_notify.reply()``.
    """
    from core.models import Run, Script
    from core.services.run_events import channel_event
    from core.services.script_inputs import ScriptInputsError, prepare_inputs
    from core.tasks import queue_script_run

    if not channel.inbound_target_id:
        return OutboundMessage(
            "This bot isn't linked to a script yet.", reply_ref=msg.reply_ref
        )

    script = Script.objects.filter(
        id=channel.inbound_target_id, workspace_id=channel.workspace_id
    ).first()
    if script is None or not script.can_run:
        return OutboundMessage(
            "The linked script is unavailable right now.", reply_ref=msg.reply_ref
        )

    # A chat message supplies no declared inputs: defaults only, and a script
    # with a required input that has none can't be started from chat.
    try:
        inputs = prepare_inputs(script, None)
    except ScriptInputsError as e:
        return OutboundMessage(
            f"The linked script can't run from chat. {e.message}", reply_ref=msg.reply_ref
        )

    run = Run.objects.create(
        script=script,
        workspace_id=script.workspace_id,
        status=Run.Status.PENDING,
        trigger_type=Run.TriggerType.API,
        code_snapshot=script.code,
        inputs=inputs,
        event=channel_event(channel, msg),
    )
    webhook_data = {
        "method": "INBOUND",
        "inbound": {
            "channel": channel.name,
            "reply_ref": msg.reply_ref,
            "text": msg.text,
            "sender": msg.sender,
        },
    }
    queue_script_run(run, webhook_data=webhook_data)
    logger.info("Inbound message routed to script %s (run %s)", script.id, run.id)
    return None


def _channel_history(channel, msg, limit: int = 6):
    """Recent same-conversation turns as [{role, text}] (excludes the current msg)."""
    from core.models import ChannelMessage

    chat_id = (msg.reply_ref or {}).get("chat_id")
    rows = list(channel.messages.order_by("-created_at")[: limit * 2 + 2])
    # The current inbound was already logged by dispatch_inbound — drop it once.
    for i, r in enumerate(rows):
        if r.direction == ChannelMessage.Direction.IN and r.text == msg.text:
            rows.pop(i)
            break
    history = []
    for r in reversed(rows[: limit * 2]):
        if chat_id is not None and (r.reply_ref_json or {}).get("chat_id") not in (chat_id, None):
            continue
        history.append(
            {"role": "user" if r.direction == ChannelMessage.Direction.IN else "assistant",
             "text": r.text}
        )
    return history


def _pyai_handler(channel, msg):
    """Answer the message with Py AI (synchronous reply, counts against the cap)."""
    from core.services.pyai import PyAIError, PyAIService

    agent = f"{channel.provider}:{channel.pk}"
    if channel.provider == "telegram" and (msg.text or "").split(maxsplit=1)[0:1] == ["/remember"]:
        from core.api.exceptions import ApiError
        from core.api.registry import registry
        from core.brain.access import principal_context

        parts = msg.text.split(maxsplit=2)
        if len(parts) < 3:
            return OutboundMessage("Use /remember <brain-slug> <text to remember>.", reply_ref=msg.reply_ref)
        try:
            result = registry.call("brain.propose-feed", principal_context(channel.workspace, "agent", agent, transport="telegram"),
                                   {"brain": parts[1], "content": parts[2], "source_kind": "thought"})
            text = f"Captured for human review: {result['feed_id']}"
        except ApiError as exc:
            text = exc.message
        return OutboundMessage(text, reply_ref=msg.reply_ref)

    if not PyAIService.is_available():
        return OutboundMessage("Py AI is not available right now.", reply_ref=msg.reply_ref)
    if not (msg.text or "").strip():
        return OutboundMessage("Send me a question and I'll take a look.", reply_ref=msg.reply_ref)

    try:
        result = PyAIService.respond(
            msg.text, workspace=channel.workspace, history=_channel_history(channel, msg),
            agent=agent, plain_text=True,
        )
    except PyAIError as e:
        return OutboundMessage(f"Sorry — {e}", reply_ref=msg.reply_ref)
    return OutboundMessage(markdown_to_plain(result.text) or "(no answer)", reply_ref=msg.reply_ref)


_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*\n?(.*?)```", re.S)
# Only ** for bold: "__" also wraps Python names such as __init__.
_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_ITALIC_RE = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])")
_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+", re.M)
_BULLET_RE = re.compile(r"^(\s*)[*+]\s+", re.M)


def markdown_to_plain(text: str) -> str:
    """Light Markdown -> plain text for chat channels that show text as-is (U55).

    Telegram's sendMessage gets no parse_mode, so ``**name**`` arrived with its
    asterisks. Keeps the words (code, link text + URL), drops the markers. Code
    spans are taken out first, so ``25 * 1024 * 1024`` keeps its asterisks.
    """
    if not text:
        return ""
    text = _FENCE_RE.sub(lambda m: m.group(1).rstrip("\n"), text)
    spans = []

    def keep(match):
        spans.append(match.group(1))
        return f"\x00{len(spans) - 1}\x00"

    text = _CODE_RE.sub(keep, text)
    text = _LINK_RE.sub(r"\1 (\2)", text)
    text = _BOLD_RE.sub(r"\1", text)
    text = _ITALIC_RE.sub(r"\1", text)
    text = _HEADING_RE.sub("", text)
    text = _BULLET_RE.sub(r"\1- ", text)
    text = re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], text)
    return text.strip()


register_handler("script", _script_handler)
register_handler("pyai", _pyai_handler)
