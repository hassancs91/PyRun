"""Watcher adapters: the base (``base``) and the built-ins."""

from .base import (  # noqa: F401
    ADAPTER_TIMEOUT_SECONDS, TEST_ITEMS, Adapter, AdapterError, CheckContext, dig, validate_config, with_defaults,
)
from .http_poll import HttpPollAdapter
from .rss import RssNewItemAdapter

BUILTIN_ADAPTERS = (HttpPollAdapter(), RssNewItemAdapter())
