"""
Channels provider package — outbound chat integrations (Phase 1).

Importing this package registers all built-in providers, so callers only need
``from core.services.channels import get_provider, OutboundMessage``.
"""

from .base import (
    ChannelError,
    ChannelProvider,
    CredentialField,
    InboundMessage,
    OutboundMessage,
    get_provider,
    list_providers,
    register,
)
from .handlers import get_handler, register_handler

# Side-effect imports: register providers and inbound handlers on import.
from . import telegram  # noqa: F401  (provider registration)
from . import whatsapp  # noqa: F401  (provider registration)
from . import handlers  # noqa: F401  (handler registration)

__all__ = [
    "ChannelError",
    "ChannelProvider",
    "CredentialField",
    "InboundMessage",
    "OutboundMessage",
    "get_provider",
    "list_providers",
    "register",
    "get_handler",
    "register_handler",
]
