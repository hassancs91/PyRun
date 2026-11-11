"""
ChannelService — high-level operations over Channel rows.

Thin orchestration on top of the provider seam (``core.services.channels``):
resolve the provider, perform the action, and record status (last_tested_at /
last_error) on the row. Used by the Channels UI and by the delivery ledger
(``core.services.channels.ledger``), through which every outbound message goes.
"""

import logging

from django.utils import timezone

logger = logging.getLogger(__name__)


class ChannelServiceError(Exception):
    """Raised for channel-level operational failures."""


class ChannelService:
    @staticmethod
    def provider_for(channel):
        from core.services.channels import get_provider

        return get_provider(channel.provider)

    @classmethod
    def test(cls, channel) -> tuple[bool, str]:
        """Test connectivity; persist last_tested_at / last_error. Returns (ok, msg)."""
        provider = cls.provider_for(channel)
        ok, message = provider.test_connection(channel)
        channel.last_tested_at = timezone.now()
        channel.last_error = "" if ok else message
        channel.save(update_fields=["last_tested_at", "last_error", "updated_at"])
        return ok, message

    @classmethod
    def send(cls, channel, text: str, reply_ref: dict | None = None) -> dict:
        """Send a message through the channel. Raises ChannelError on failure.

        Persists last_error on failure (and clears a stale one on success) so the
        UI surfaces delivery problems without an extra write on every send.
        """
        from core.services.channels import ChannelError, OutboundMessage

        provider = cls.provider_for(channel)
        try:
            result = provider.send(channel, OutboundMessage(text=text, reply_ref=reply_ref))
        except ChannelError as e:
            channel.last_error = str(e)
            channel.save(update_fields=["last_error", "updated_at"])
            raise
        if channel.last_error:
            channel.last_error = ""
            channel.save(update_fields=["last_error", "updated_at"])
        return result

    @classmethod
    def discover_chat_ids(cls, channel) -> list[dict]:
        """Telegram-style chat_id discovery (getUpdates). Raises if unsupported."""
        provider = cls.provider_for(channel)
        discover = getattr(provider, "discover_chat_ids", None)
        if discover is None:
            raise ChannelServiceError(
                f"{channel.get_provider_display()} does not support chat-ID discovery."
            )
        return discover(channel)
