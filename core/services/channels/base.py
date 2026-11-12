"""
Channel provider seam — a registry of ChannelProvider classes (mirrors the
RunBackend seam). Adding a provider is additive: define a class, register it.

The contracts ``InboundMessage`` / ``OutboundMessage`` are ORM-free dataclasses
(same discipline as the SDK's ``RunView``) — the seam every surface produces or
consumes: run notifications, script replies, Py AI, and the future Agents plan.
Providers implement the outbound methods (``send``/``test_connection``) and the
inbound methods (webhook parse + signature verify); both directions are live.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class OutboundMessage:
    """A message to deliver via a provider."""

    text: str
    # Echo back to the same conversation. None ⇒ the channel's default target.
    reply_ref: Optional[dict] = None
    attachments: list = field(default_factory=list)
    # Structured content a provider sends instead of plain text, e.g.
    # {"template": {"name", "language", "components"}} for WhatsApp. ``text``
    # stays the readable version (what the ledger shows). Empty ⇒ plain text.
    content: dict = field(default_factory=dict)


@dataclass(frozen=True)
class InboundMessage:
    """A message received from a provider (produced by ``parse_inbound``)."""

    channel_id: str
    provider: str
    text: str
    sender: dict          # {id, username, display_name}
    reply_ref: dict       # chat_id / channel_id / thread_ts — where a reply goes
    raw: dict             # full provider payload (advanced handlers)
    received_at: Optional[datetime] = None
    # Optional, for providers with typed messages (WhatsApp): the message's
    # type ("text", "interactive", "location", ...), its structured parts
    # (``details``), any attachments as metadata (``media``; the script
    # handler may download them, see ``media.py``) and the
    # provider's own message id. Empty for Telegram.
    message_type: str = ""
    details: dict = field(default_factory=dict)
    media: list = field(default_factory=list)
    message_id: str = ""


class ChannelError(Exception):
    """Raised when a provider operation fails (network, auth, API error).

    ``retryable`` marks a temporary failure (network, timeout, rate limit, a
    provider-side 5xx) that the delivery ledger tries again later;
    ``retry_after`` is the provider's own wait in seconds, when it gives one.
    ``auth`` marks rejected credentials: never retried. The defaults (not
    retryable) suit any error a provider doesn't classify.
    """

    def __init__(self, message="", *, retryable=False, auth=False, retry_after=None):
        super().__init__(message)
        self.retryable = bool(retryable) and not auth
        self.auth = bool(auth)
        self.retry_after = retry_after


@dataclass(frozen=True)
class CredentialField:
    """One credential a provider needs, as the channel form asks for it.

    Every credential field is stored in the channel's encrypted credential blob
    under ``name``. ``secret`` fields render as password inputs, are never shown
    again, and keep their stored value when left blank on edit; non-secret ones
    (an account or phone-number id) render as text and show the stored value.
    ``required`` fields must be filled when the channel is created.
    """

    name: str
    label: str
    secret: bool = True
    help: str = ""
    required: bool = True
    placeholder: str = ""
    # The form error when a required field is left empty on create.
    required_error: str = ""

    @property
    def missing_error(self) -> str:
        return self.required_error or f"{self.label} is required."


# Form fields every channel has; a provider's credential field can't reuse them.
RESERVED_FIELD_NAMES = frozenset({"name", "provider", "default_target", "daily_send_cap", "enabled"})
_FIELD_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")


class ChannelProvider:
    """Base class for a chat provider. Subclasses set ``provider_key``.

    The channel form is driven by the provider: ``credential_fields`` lists
    what to ask for (stored encrypted), the ``default_target_*`` texts label the
    default-recipient field, ``identity_field`` names the credential whose
    duplicate is reported by ``duplicate_identity_error``, and
    ``setup_template`` (optional) is the setup help shown next to the Add form.
    """

    provider_key: str = ""

    credential_fields: tuple[CredentialField, ...] = ()
    identity_field: str = ""
    duplicate_identity_error: str = (
        "These credentials are already connected as another channel "
        "(one account = one channel)."
    )
    default_target_label: str = "Default recipient"
    default_target_help: str = "Where notifications are sent."
    default_target_placeholder: str = ""
    setup_template: str = ""
    # False when the user registers the webhook URL in the provider's own
    # dashboard (WhatsApp) instead of PyRun registering it through the API.
    registers_webhook: bool = True
    # True when ``send`` takes ``OutboundMessage.content["template"]``.
    supports_templates: bool = False
    # True when ``send`` takes ``content["media"]`` (a file by URL) and
    # ``content["interactive"]`` (reply buttons, a list).
    supports_rich_content: bool = False
    # True when ``download_media`` can fetch an inbound attachment.
    supports_media_download: bool = False

    def identity_for_fingerprint(self, creds: dict) -> str:
        """The stable credential identity (e.g. bot token) used for the
        one-bot-one-channel fingerprint. Empty ⇒ no fingerprint."""
        return ""

    # --- inbound URL verification -------------------------------------------

    def handle_challenge(self, channel, request):
        """Answer a provider's URL-verification handshake, or return None.

        The inbound webhook calls this for every request (GET or POST) BEFORE
        the signature check, so a provider that verifies its webhook URL with a
        challenge (WhatsApp's GET ``hub.challenge``, Slack's
        ``url_verification`` POST) can answer it. Return a Django
        ``HttpResponse`` to end the request with it; return None to carry on
        (a GET that no provider answers gets 405). A provider must check the
        handshake's own proof (e.g. the verify token) before echoing anything.
        """
        return None

    # --- outbound (Phase 1) ---
    def test_connection(self, channel) -> tuple[bool, str]:
        raise NotImplementedError

    def send(self, channel, msg: OutboundMessage) -> dict:
        raise NotImplementedError

    # --- inbound (Phase 2) ---
    supports_inbound: bool = False

    def verify_inbound(self, channel, request) -> bool:
        """Validate the request signature (provider-specific). Loopback only on
        the auth; this is the real authenticity gate."""
        return False

    def dedup_id(self, payload: dict) -> Optional[str]:
        """A stable per-message id for de-duplicating provider retries."""
        return None

    def parse_inbound(self, channel, payload: dict) -> Optional[InboundMessage]:
        """Turn a decoded webhook payload into an InboundMessage (or None)."""
        return None

    def split_inbound(self, payload: dict) -> list:
        """The payloads of the messages in one webhook delivery.

        A provider that can batch several messages into one POST (WhatsApp)
        returns one payload per message, each in its own documented shape, so
        de-duplication, the approval inbox and the handler see one message at
        a time. The default is the payload itself.
        """
        return [payload]

    def apply_status_updates(self, channel, payload: dict) -> int:
        """Apply the delivery statuses a webhook delivery carries; returns how many.

        For providers that report delivered/read/failed by callback
        (WhatsApp); the ledger row is found by ``provider_message_id``. The
        default reports none.
        """
        return 0

    def acknowledge_inbound(self, channel, msg: InboundMessage) -> None:
        """Tell the provider an inbound message was seen (WhatsApp's mark-as-read).

        Called by the worker for every message it dispatches (after logging
        it, before the handler); may raise ChannelError (the caller logs it
        and carries on). Default: nothing.
        """
        return None

    def download_media(self, channel, item: dict, max_bytes: int) -> tuple[bytes, str]:
        """Fetch one inbound attachment (an ``InboundMessage.media`` item).

        Returns ``(data, mime_type)``; raises ChannelError with a sentence for
        the run (too large, expired, refused). Only called when
        ``supports_media_download`` is True.
        """
        raise ChannelError("This provider can't download attachments.")

    def set_inbound_webhook(self, channel, public_url: str) -> Optional[str]:
        """Register ``public_url`` as the provider's webhook for this channel.

        May return a sentence for the user (shown instead of "webhook
        registered"), e.g. where to paste the URL when ``registers_webhook``
        is False.
        """
        raise NotImplementedError

    def clear_inbound_webhook(self, channel) -> None:
        """Remove the provider's webhook registration for this channel."""
        raise NotImplementedError


_REGISTRY: dict[str, ChannelProvider] = {}


def _check_fields(provider: ChannelProvider) -> None:
    """Refuse a provider whose credential fields can't drive the channel form."""
    names = [f.name for f in provider.credential_fields]
    for name in names:
        if not name or not set(name) <= _FIELD_NAME_CHARS or name[0].isdigit():
            raise ValueError(f"{provider.provider_key}: bad credential field name {name!r}")
        if name in RESERVED_FIELD_NAMES:
            raise ValueError(f"{provider.provider_key}: credential field {name!r} clashes with a channel field")
    if len(set(names)) != len(names):
        raise ValueError(f"{provider.provider_key}: duplicate credential field names")
    if provider.identity_field and provider.identity_field not in names:
        raise ValueError(f"{provider.provider_key}: identity_field {provider.identity_field!r} is not a credential field")


def register(provider: ChannelProvider) -> ChannelProvider:
    """Register a provider instance under its ``provider_key``."""
    _check_fields(provider)
    _REGISTRY[provider.provider_key] = provider
    return provider


def get_provider(provider_key: str) -> ChannelProvider:
    try:
        return _REGISTRY[provider_key]
    except KeyError:
        raise ChannelError(f"Unknown channel provider: {provider_key!r}")


def list_providers() -> list[str]:
    return sorted(_REGISTRY)
