"""Every adapter a watcher can use, by key (2.5 Stage 7).

Today: the built-ins (``core.triggers.adapters.BUILTIN_ADAPTERS``). The
integrations' triggers (T32) and plugin ``triggers.py`` adapters (T33) join
this same table; the runner and the console only ever look adapters up here.
"""

from core.triggers.adapters import BUILTIN_ADAPTERS


def adapters() -> dict:
    """``{key: adapter}`` in the picker's order."""
    return {adapter.key: adapter for adapter in BUILTIN_ADAPTERS}


def get(key):
    """The adapter registered as ``key``, or None."""
    return adapters().get(key)


def title(key) -> str:
    """The adapter's title, or its key when it isn't registered (any more)."""
    adapter = get(key)
    return adapter.title if adapter else key
