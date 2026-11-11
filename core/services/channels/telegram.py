"""
Telegram provider — Bot API outbound + the getUpdates chat_id helper.

The ``discover_chat_ids`` helper solves the bootstrap problem (you only learn a
chat_id once someone messages the bot) — it works *because* no webhook is
registered (getUpdates and webhooks are mutually exclusive). Inbound is wired:
the webhook registration and parse/verify are implemented below.
"""

import hmac
import logging

import requests

from core.net_errors import describe_request_error

from .base import ChannelError, ChannelProvider, InboundMessage, OutboundMessage, register

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"
_TIMEOUT = 10  # seconds
BAD_TOKEN_MESSAGE = (
    "Telegram rejected this bot token (Unauthorized) — copy it again from @BotFather."
)


def _is_id(value) -> bool:
    """A Telegram chat/user id: an int (not a bool) or a non-empty string."""
    if isinstance(value, bool):
        return False
    return isinstance(value, int) or (isinstance(value, str) and value != "")


def _text(value) -> str:
    return value if isinstance(value, str) else ""


class TelegramProvider(ChannelProvider):
    provider_key = "telegram"
    supports_inbound = True

    def identity_for_fingerprint(self, creds: dict) -> str:
        return (creds.get("bot_token") or "").strip()

    # --- internals -----------------------------------------------------------

    def _token(self, channel) -> str:
        token = (channel.get_credentials().get("bot_token") or "").strip()
        if not token:
            raise ChannelError("This Telegram channel has no bot token configured.")
        return token

    def _call(self, token: str, method: str, payload: dict | None = None):
        """Call a Telegram Bot API method; return ``result`` or raise ChannelError."""
        url = _API.format(token=token, method=method)
        try:
            resp = requests.post(url, json=payload or {}, timeout=_TIMEOUT)
        except requests.RequestException as e:
            # requests puts the URL — bot token included — into its message.
            text = describe_request_error(e, "Telegram", _TIMEOUT).replace(token, "<bot token>")
            raise ChannelError(text, retryable=True)
        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise ChannelError(
                f"Unexpected Telegram response (HTTP {resp.status_code}).",
                retryable=resp.status_code >= 500,
            )
        if not data.get("ok"):
            code = data.get("error_code") if isinstance(data.get("error_code"), int) else resp.status_code
            # 401 means a bad/revoked bot token; Telegram only says "Unauthorized" (U50).
            if resp.status_code == 401 or code == 401:
                raise ChannelError(BAD_TOKEN_MESSAGE, auth=True)
            description = data.get("description") or "Telegram API error."
            if code == 429 or resp.status_code == 429:
                # Flood control: Telegram says how long to wait.
                params = data.get("parameters") if isinstance(data.get("parameters"), dict) else {}
                wait = params.get("retry_after")
                raise ChannelError(
                    description, retryable=True,
                    retry_after=wait if isinstance(wait, int) and not isinstance(wait, bool) else None,
                )
            raise ChannelError(description, retryable=code >= 500 or resp.status_code >= 500)
        return data.get("result")

    # --- outbound ------------------------------------------------------------

    def test_connection(self, channel) -> tuple[bool, str]:
        try:
            me = self._call(self._token(channel), "getMe")
        except ChannelError as e:
            return False, str(e)
        username = (me or {}).get("username", "?")
        return True, f"Connected as @{username}."

    def send(self, channel, msg: OutboundMessage) -> dict:
        token = self._token(channel)
        chat_id = (msg.reply_ref or {}).get("chat_id") or channel.default_target
        if not chat_id:
            raise ChannelError(
                "No chat to send to — set a default chat for this channel "
                "(use the 'Find chat ID' helper) or send as a reply."
            )
        result = self._call(
            token, "sendMessage", {"chat_id": chat_id, "text": msg.text}
        )
        return {"message_id": (result or {}).get("message_id"), "chat_id": chat_id}

    # --- chat_id discovery (Phase 1 bootstrap helper) ------------------------

    def discover_chat_ids(self, channel) -> list[dict]:
        """Recent chats that have messaged the bot, via getUpdates.

        Only works while no webhook is registered (Phase 1). Returns a
        de-duplicated list of ``{chat_id, name, type, last_text}``.
        """
        updates = self._call(self._token(channel), "getUpdates") or []
        seen: dict = {}
        for update in updates:
            message = (
                update.get("message")
                or update.get("channel_post")
                or update.get("edited_message")
                or {}
            )
            chat = message.get("chat") or {}
            cid = chat.get("id")
            if cid is None:
                continue
            name = (
                chat.get("title")
                or " ".join(
                    p for p in (chat.get("first_name"), chat.get("last_name")) if p
                )
                or chat.get("username")
                or str(cid)
            )
            seen[cid] = {
                "chat_id": cid,
                "name": name,
                "type": chat.get("type", ""),
                "last_text": (message.get("text") or "")[:80],
            }
        return list(seen.values())

    # --- inbound (Phase 2) ---------------------------------------------------

    def verify_inbound(self, channel, request) -> bool:
        """Telegram echoes the configured secret in this header on every update."""
        expected = channel.get_inbound_secret()
        if not expected:
            return False
        provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        return hmac.compare_digest(provided, expected)

    def dedup_id(self, payload: dict):
        update_id = payload.get("update_id")
        return str(update_id) if update_id is not None else None

    def parse_inbound(self, channel, payload: dict):
        # Accept only the shape Telegram documents. Anything else (an update type
        # we don't handle, or a caller who knows the secret sending junk) is
        # ignored instead of crashing the public webhook with a 500.
        message = payload.get("message") or payload.get("edited_message")
        if not isinstance(message, dict):
            return None
        chat = message.get("chat")
        chat_id = chat.get("id") if isinstance(chat, dict) else None
        if not _is_id(chat_id):
            return None
        frm = message.get("from")
        if not isinstance(frm, dict):
            frm = {}
        display = (
            " ".join(p for p in (_text(frm.get("first_name")), _text(frm.get("last_name"))) if p)
            or _text(chat.get("title"))
            or _text(frm.get("username"))
            or str(chat_id)
        )
        sender_id = frm.get("id") if _is_id(frm.get("id")) else chat_id
        return InboundMessage(
            channel_id=str(channel.id),
            provider=self.provider_key,
            text=_text(message.get("text")),
            sender={
                "id": str(sender_id),
                "username": _text(frm.get("username")),
                "display_name": display,
            },
            reply_ref={"chat_id": chat_id},
            raw=payload,
        )

    def set_inbound_webhook(self, channel, public_url: str) -> None:
        self._call(
            self._token(channel),
            "setWebhook",
            {
                "url": public_url,
                "secret_token": channel.ensure_inbound_secret(),
                "allowed_updates": ["message"],
            },
        )

    def clear_inbound_webhook(self, channel) -> None:
        self._call(self._token(channel), "deleteWebhook", {})


register(TelegramProvider())
