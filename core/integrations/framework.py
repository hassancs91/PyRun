"""
The integration framework (2.5 Stage 6, T26): what PyRun knows about each integration.

An integration is one service's typed functions for scripts. Its files:

- ``core/integrations/<key>/preset.py``: the connection preset it calls (shared
  with Connections; several integrations may use one preset, e.g. Google's).
- ``core/integrations/<key>/api.py``: ``INTEGRATION = Integration(...)``, its
  public API as PyRun documents, tests and smoke-tests it: the script-facing
  module, the functions in it (the stable signatures), an example script and
  the live smoke checks.
- ``core/integrations/<key>/triggers.py`` (optional): reserved for watcher
  adapters; nothing reads it yet.
- ``core/integrations/<key>/tests/``: ``test_api.py`` (the conformance suite and
  cassette tests) and ``cassettes/`` (recorded exchanges, replayed offline).
- ``core/script_helpers/pyrunner_integrations/<key>.py``: the module scripts
  import (standard library only, on the shared ``_client``).

``INTEGRATION_MODULES`` lists every integration this version ships; the docs
generator and ``manage.py integration_smoke`` read it. ``manage.py
new_integration <key>`` scaffolds the files above.
"""

import importlib
import inspect
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Every integration with a module. Explicit (not a directory scan), like
# presets.PRESET_MODULES.
INTEGRATION_MODULES = ("http",)

KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
HELPERS_DIR = Path(__file__).resolve().parent.parent / "script_helpers"


@dataclass(frozen=True)
class Smoke:
    """One live, read-only check for ``manage.py integration_smoke``.

    ``run(module, connection, config)`` calls the integration's module as the
    connection named ``connection`` (``config`` is its non-secret settings)
    and returns a short line saying what it saw (never a secret); an
    IntegrationError is a failure.
    """

    name: str
    run: object


@dataclass(frozen=True)
class Integration:
    key: str
    # The connection preset the module calls; its default connection is the
    # script's one connection of this provider.
    provider: str
    title: str
    # The public functions of the script-facing module, in the docs' order.
    # Their signatures are public API: additive only.
    functions: tuple
    example: str = ""
    smoke: tuple = ()  # Smoke checks
    # Import path of the script-facing module; default pyrunner_integrations.<key>.
    module: str = ""

    @property
    def module_name(self) -> str:
        return self.module or f"pyrunner_integrations.{self.key}"


def helpers_on_path() -> None:
    """Make ``core/script_helpers`` importable by the names scripts use (``pyrunner_integrations``).

    For PyRun's own process (tests, the docs generator, the smoke command): a
    run gets it on PYTHONPATH from the executor.
    """
    path = str(HELPERS_DIR)
    if path not in sys.path:
        sys.path.append(path)


def load(key) -> Integration:
    """The ``Integration`` in ``core/integrations/<key>/api.py`` (registered or not)."""
    if not KEY_RE.match(str(key)):
        raise ValueError(f"{key!r} is not an integration key (lower-case letters, digits and '_').")
    try:
        api = importlib.import_module(f"core.integrations.{key}.api")
    except ModuleNotFoundError as e:
        if e.name in (f"core.integrations.{key}", f"core.integrations.{key}.api"):
            raise LookupError(f"No integration {key!r}: core/integrations/{key}/api.py doesn't exist.") from None
        raise
    integration = api.INTEGRATION
    if integration.key != key:
        raise ValueError(f"core/integrations/{key}/api.py names {integration.key!r}")
    return integration


def integrations() -> list:
    """Every shipped integration, in ``INTEGRATION_MODULES`` order."""
    return [load(key) for key in INTEGRATION_MODULES]


def script_module(integration):
    """The script-facing module of ``integration`` (an Integration or a key), imported as scripts import it."""
    if isinstance(integration, str):
        integration = load(integration)
    helpers_on_path()
    return importlib.import_module(integration.module_name)


def script_helpers():
    """``(pyrunner_connections, pyrunner_integrations._client)``, imported as scripts import them."""
    helpers_on_path()
    import pyrunner_connections
    import pyrunner_integrations._client as client

    return pyrunner_connections, client


def load_preset(provider):
    """The connection preset in ``core/integrations/<provider>/preset.py`` (registered or not)."""
    try:
        return importlib.import_module(f"core.integrations.{provider}.preset").PRESET
    except ModuleNotFoundError as e:
        if e.name in (f"core.integrations.{provider}", f"core.integrations.{provider}.preset"):
            raise LookupError(f"No connection preset {provider!r} (core/integrations/{provider}/preset.py).") from None
        raise


def public_functions(integration) -> list:
    """``(name, function)`` of every function ``integration`` declares, in order."""
    module = script_module(integration)
    out = []
    for name in integration.functions:
        fn = getattr(module, name, None)
        if not callable(fn):
            raise AttributeError(f"{integration.module_name} has no function {name!r}")
        out.append((name, fn))
    return out


def signature(fn) -> str:
    return f"{fn.__name__}{inspect.signature(fn)}"
