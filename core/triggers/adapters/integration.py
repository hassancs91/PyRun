"""
Integration triggers as watcher adapters (2.5 Stage 7, T32).

An integration may ship ``core/integrations/<key>/triggers.py`` with
``TRIGGERS = (framework.Trigger(...),)``: ``gmail_new_message``,
``sheets_new_row``, ``github_new_issue``. ``IntegrationAdapter`` wraps one so
the watcher runner treats it like any adapter: it needs a Connection of the
integration's provider, and a check calls the trigger through the
integration's script-facing module (``pyrunner_integrations.<key>``), the
same code a script runs.

A script gets its credentials from PyRun's loopback; here the module runs
inside PyRun, so ``pyrunner_connections.credentials`` is pointed at the
watcher's Connection for the length of the call (``credentials_for``: the
same checks and OAuth refresh a run gets, no run masks recorded). That is a
module-wide function, so the calls of one process take turns (``_LOCK``),
and the module's cached clients and credentials are dropped before and after
each one: nothing of one connection is left for the next.
"""

import hashlib
import threading

from .base import Adapter, AdapterError

# One integration call at a time per process (see the module docstring).
_LOCK = threading.Lock()


class IntegrationAdapter(Adapter):
    """A ``framework.Trigger`` of integration ``integration`` as a watcher adapter."""

    requires_connection = True

    def __init__(self, integration, trigger):
        self.integration = integration
        self.trigger = trigger
        self.key = trigger.key
        self.title = trigger.title
        self.description = (
            f"Through the {integration.title} integration, as one of your {integration.provider} connections."
        )
        self.config_schema = trigger.config_schema
        self.providers = (integration.provider,)

    def _call(self, ctx, config, state):
        from core.integrations import framework

        if ctx.connection is None:
            raise AdapterError("This watcher needs a connection.")
        # The runner gives up waiting after timeout + 5 s; a call that started
        # later than that would only run for nobody.
        if not _LOCK.acquire(timeout=ctx.timeout):
            raise AdapterError(
                "Another integration check in this worker is still going; this one is tried again later."
            )
        try:
            pyrunner_connections, client = framework.script_helpers()
            module = framework.script_module(self.integration)
            original = pyrunner_connections.credentials
            pyrunner_connections.credentials = _credentials_of(ctx.connection, pyrunner_connections)
            client.reset()
            try:
                return self.trigger.check(module, ctx.connection.name, config, state, limit=ctx.limit)
            except pyrunner_connections.ConnectionsError as e:
                raise AdapterError(f"{getattr(e, 'code', '') or 'ERROR'}: {e}") from None
            finally:
                pyrunner_connections.credentials = original
                client.reset()
        finally:
            _LOCK.release()

    def check(self, ctx, config, state):
        return self._call(ctx, config, state)

    def test(self, ctx, config):
        # The first check only marks where the service is: it reads, reports
        # nothing and its state is thrown away.
        self._call(ctx, config, None)
        return "Connected; new items from now on start runs.", []


def _credentials_of(connection, pyrunner_connections):
    """A stand-in for ``pyrunner_connections.credentials`` that answers with ``connection``'s.

    The module asks by the connection's name; whatever name it passes, only
    this connection's credentials come back. Fetched once per call (an OAuth
    token is refreshed first when it is close to expiring); ``refused=True``
    (the service answered 401) fetches them again from the stored row, which
    refreshes a token that is still the refused one.
    """
    from core.models import Connection
    from core.services.connections import ConnectionLookupError, credentials_for

    cache = {}

    def credentials(name, *, refused=False):
        if "material" in cache and not refused:
            return cache["material"]
        digest = None
        row = connection
        if refused and cache.get("material", {}).get("token"):
            digest = hashlib.sha256(cache["material"]["token"].encode("utf-8")).hexdigest()
            row = Connection.objects.get(pk=connection.pk)
        try:
            cache["material"] = credentials_for(row, refused=digest)
        except ConnectionLookupError as e:
            raise pyrunner_connections.ConnectionsError(e.message, e.code, e.status) from None
        return cache["material"]

    return credentials


def integration_adapters() -> tuple:
    """An adapter per trigger of every shipped integration, in ``INTEGRATION_MODULES`` order."""
    from core.integrations import framework

    found = []
    for key in framework.INTEGRATION_MODULES:
        integration = framework.load(key)
        for trigger in framework.load_triggers(key):
            found.append(IntegrationAdapter(integration, trigger))
    return tuple(found)
