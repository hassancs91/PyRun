"""Every adapter a watcher can use, by key (2.5 Stage 7).

The built-ins (``core.triggers.adapters.BUILTIN_ADAPTERS``), then the
integrations' triggers (``core/integrations/<key>/triggers.py``, wrapped by
``IntegrationAdapter``, T32), then the loaded plugins' ``triggers.py`` adapters
(``"<slug>:<key>"``, wrapped by ``PluginAdapter``, T33). The runner and the
console only ever look adapters up here.
"""

import functools

from core.triggers.adapters import BUILTIN_ADAPTERS
from core.triggers.adapters.integration import integration_adapters
from core.triggers.adapters.plugin import plugin_adapters


@functools.lru_cache(maxsize=1)
def _integration_adapters() -> tuple:
    # The shipped integrations are fixed for the process (INTEGRATION_MODULES).
    return integration_adapters()


def adapters() -> dict:
    """``{key: adapter}`` in the picker's order.

    Plugin adapters aren't cached here like the integrations': which plugins
    are loaded is read on every call (each plugin's triggers.py is imported
    once, in ``core.triggers.adapters.plugin``).
    """
    return {adapter.key: adapter for adapter in (*BUILTIN_ADAPTERS, *_integration_adapters(), *plugin_adapters())}


def get(key):
    """The adapter registered as ``key``, or None."""
    return adapters().get(key)


def title(key) -> str:
    """The adapter's title, or its key when it isn't registered (any more)."""
    adapter = get(key)
    return adapter.title if adapter else key
