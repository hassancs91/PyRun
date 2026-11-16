"""
Plugin adapters (2.5 Stage 7, T33).

A plugin may ship ``triggers.py`` next to its ``api.py``: every class there
that subclasses ``core.plugins.api.TriggerAdapter`` is a watcher adapter,
offered as ``"<slug>:<key>"`` in every workspace's picker. They are found
like the plugin's ``api.py`` handlers: only for plugins loaded in this
process (``settings.INSTALLED_PLUGINS``), imported lazily on first use and
cached per process, a failed import included (plugins change only across a
restart). Before the import, the doctor's ``triggers`` rule runs on the file
(it parses, it never executes), so a dev-mode plugin that was never activated
is held to it as well; each adapter is then checked again as an object
(``adapter_problem``). A plugin whose triggers.py fails offers no adapters,
and its watchers fail with "isn't available" until it is fixed.

``PluginAdapter`` wraps one so the runner treats it like a built-in: the
plugin's ``check``/``test`` get a ``TriggerContext`` (SDK types, never a
model row except the workspace), and a ``TriggerError`` or
``ConnectionUnavailable`` it raises becomes an ``AdapterError`` whose text
the console shows.
"""

import importlib
import inspect
import logging
import traceback
from pathlib import Path

from django.conf import settings

from .base import Adapter, AdapterError, schema_problem

logger = logging.getLogger(__name__)

# {slug: (PluginAdapter, ...)}: per process, failures included (an empty tuple).
_loaded: dict = {}
# {slug: detail}: why a plugin's triggers.py offered nothing (the doctor's
# failures or the import error), for the plugin page (2.5 T52).
_errors: dict = {}


class PluginAdapter(Adapter):
    """A plugin's ``TriggerAdapter`` as a watcher adapter."""

    def __init__(self, slug, adapter):
        self.plugin_adapter = adapter
        self.owner_plugin = slug
        self.key = f"{slug}:{adapter.key}"
        self.title = adapter.title
        self.description = adapter.description or f'From the "{slug}" plugin.'
        self.config_schema = adapter.config_schema
        self.requires_connection = bool(adapter.requires_connection)
        self.providers = tuple(adapter.providers or ())

    @staticmethod
    def _context(ctx):
        from core.plugins.api import ConnectionView, TriggerContext

        return TriggerContext(
            workspace=ctx.workspace,
            watcher_id=str(ctx.watcher.pk) if ctx.watcher is not None else None,
            connection=ConnectionView._from(ctx.connection) if ctx.connection is not None else None,
            limit=ctx.limit,
            timeout=ctx.timeout,
            _connection_row=ctx.connection,
        )

    def _call(self, method, *args):
        from core.plugins.api import ConnectionUnavailable, TriggerError

        try:
            return method(*args)
        except TriggerError as e:
            raise AdapterError(e.message) from None
        except ConnectionUnavailable as e:
            raise AdapterError(f"{e.code}: {e.message}") from None
        except Exception:
            # Not an error the plugin meant to show: its author wants the traceback.
            logger.warning("Watcher adapter %s raised", self.key, exc_info=True)
            raise

    def check(self, ctx, config, state):
        return self._call(self.plugin_adapter.check, self._context(ctx), config, state)

    def test(self, ctx, config):
        return self._call(self.plugin_adapter.test, self._context(ctx), config)


def adapter_problem(slug, adapter) -> str:
    """Why a plugin's adapter object can't be offered, or "" when it can."""
    from core.integrations.presets import get_preset
    from core.plugins.api import TriggerAdapter
    from core.services.plugin_doctor import TRIGGER_KEY_MAX, TRIGGER_KEY_RE

    key = adapter.key
    if not isinstance(key, str) or not TRIGGER_KEY_RE.match(key):
        return f"key {key!r} must be a lowercase letter, then letters, digits or underscores."
    if len(slug) + 1 + len(key) > TRIGGER_KEY_MAX:
        return f"'{slug}:{key}' is over {TRIGGER_KEY_MAX} characters."
    if not isinstance(adapter.title, str) or not adapter.title.strip():
        return "it needs a title."
    if not isinstance(adapter.description, str):
        return "its description must be text."
    problem = schema_problem(adapter.config_schema)
    if problem:
        return problem
    providers = adapter.providers
    if not isinstance(providers, (list, tuple)) or not all(isinstance(p, str) for p in providers):
        return "providers must be a tuple of connection preset keys."
    unknown = [p for p in providers if get_preset(p) is None]
    if unknown:
        return f"unknown connection provider(s): {', '.join(unknown)}."
    for method in ("check", "test"):
        if getattr(type(adapter), method) is getattr(TriggerAdapter, method):
            return f"it must define {method}()."
    return ""


def _load(slug) -> tuple:
    """The adapters of plugin ``slug``'s triggers.py (cached; () when it has none or fails)."""
    if slug in _loaded:
        return _loaded[slug]
    from core.plugins.api import TriggerAdapter
    from core.services.plugin_doctor import triggers_problems

    found = []
    try:
        package = importlib.import_module(f"plugins.{slug}")
        folder = Path(package.__file__).parent
        failures, _, keys = triggers_problems(folder)
        if failures:
            logger.warning("Plugin %r: triggers.py fails the doctor, no adapters offered: %s",
                           slug, " ".join(failures))
            _errors[slug] = " ".join(failures)
        elif keys:
            module = importlib.import_module(f"plugins.{slug}.triggers")
            for _, cls in inspect.getmembers(module, inspect.isclass):
                if cls is TriggerAdapter or not issubclass(cls, TriggerAdapter) or cls.__module__ != module.__name__:
                    continue
                adapter = cls()
                problem = adapter_problem(slug, adapter)
                if problem:
                    logger.warning("Plugin %r: adapter %s left out: %s", slug, cls.__name__, problem)
                    continue
                found.append(PluginAdapter(slug, adapter))
    except Exception as exc:
        _log_import_failure(slug, exc)
        _errors[slug] = f"{type(exc).__name__}: {exc}"
        found = []
    # The picker's order: by title, then key.
    _loaded[slug] = tuple(sorted(found, key=lambda a: (a.title.lower(), a.key)))
    return _loaded[slug]


def _log_import_failure(slug, exc) -> None:
    """Log a failed triggers.py import with its traceback, the plugin's secrets masked.

    The message can hold a value the plugin read (``RuntimeError(f"token={token}")``):
    masked like the plugin page's notice (``mask_plugin_output``). If the
    secrets can't be read to mask it, only the exception type is logged.
    """
    trace = "".join(traceback.format_exception(exc)).rstrip()
    try:
        from core.services.plugin_updates import mask_plugin_output

        trace = mask_plugin_output(slug, trace)
    except Exception:
        trace = (f"{type(exc).__name__} (the message is not logged: the plugin's "
                 "secrets could not be read to mask it)")
    logger.error("Plugin %r: loading triggers.py failed\n%s", slug, trace)


def load_problem(slug) -> str:
    """Why plugin ``slug``'s triggers.py offers no adapters, or ''.

    Loads it first when the plugin is loaded in this process (as the watcher
    picker would); a plugin that isn't loaded here has nothing to report.
    """
    apps = getattr(settings, "INSTALLED_PLUGINS", [])
    if not any(app.rsplit(".", 1)[-1] == slug for app in apps):
        return ""
    _load(slug)
    return _errors.get(slug, "")


def plugin_adapters() -> tuple:
    """Every loaded plugin's adapters, plugin by plugin (by slug)."""
    slugs = sorted(app.rsplit(".", 1)[-1] for app in getattr(settings, "INSTALLED_PLUGINS", []))
    return tuple(adapter for slug in slugs for adapter in _load(slug))


def reset() -> None:
    """Forget what was loaded (tests; the next lookup loads again)."""
    _loaded.clear()
    _errors.clear()
