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


class ChannelProvider:
    """Base class for a chat provider. Subclasses set ``provider_key``."""

    provider_key: str = ""

    def identity_for_fingerprint(self, creds: dict) -> str:
        """The stable credential identity (e.g. bot token) used for the
        one-bot-one-channel fingerprint. Empty ⇒ no fingerprint."""
        return ""

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

    def set_inbound_webhook(self, channel, public_url: str) -> None:
        """Register ``public_url`` as the provider's webhook for this channel."""
        raise NotImplementedError

    def clear_inbound_webhook(self, channel) -> None:
        """Remove the provider's webhook registration for this channel."""
        raise NotImplementedError


_REGISTRY: dict[str, ChannelProvider] = {}


def register(provider: ChannelProvider) -> ChannelProvider:
    """Register a provider instance under its ``provider_key``."""
    _REGISTRY[provider.provider_key] = provider
    return provider


def get_provider(provider_key: str) -> ChannelProvider:
    try:
        return _REGISTRY[provider_key]
    except KeyError:
        raise ChannelError(f"Unknown channel provider: {provider_key!r}")


def list_providers() -> list[str]:
    return sorted(_REGISTRY)
