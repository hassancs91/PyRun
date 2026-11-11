"""Watcher adapters: the base (``base``), the built-ins, and ``integration`` (integration triggers as adapters)."""

from .base import (  # noqa: F401
    ADAPTER_TIMEOUT_SECONDS, TEST_ITEMS, Adapter, AdapterError, CheckContext, dig, validate_config, with_defaults,
)
from .db_poll import DbPollAdapter
from .http_poll import HttpPollAdapter
from .imap import ImapNewMessageAdapter
from .rss import RssNewItemAdapter

BUILTIN_ADAPTERS = (HttpPollAdapter(), RssNewItemAdapter(), ImapNewMessageAdapter(), DbPollAdapter())
