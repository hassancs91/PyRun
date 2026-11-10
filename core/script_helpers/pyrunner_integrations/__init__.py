"""
PyRun integrations for scripts: typed functions for outside services, on top of connections.

Each service is a module with the handful of calls most scripts need, using a
connection (sidebar -> Connections) granted to the script:

    from pyrunner_integrations import http

    items = http.get("/v1/items", params={"page": 1})          # the script's one HTTP API connection
    http.post("/v1/items", json={"name": "Ada"}, connection="crm")

Every function takes ``connection=``: a connection's name, or nothing for the
script's one connection of that service. Functions return plain data (parsed
JSON, text, lists, dicts) and raise one exception, ``IntegrationError``, with a
``code``, the HTTP ``status`` and ``retry_after``. Underneath, one shared client
puts the credentials only on requests to the connection's base URL, retries 429
and 5xx replies with backoff, and asks PyRun once for new credentials after a
401 (an OAuth token is refreshed then). ``<module>.client()`` gives that client
for anything a module has no function for.

Standard library only; ``requests`` is used when it is installed. Function
signatures are public API: they only grow (new functions, new keyword
arguments with defaults), and anything deprecated is announced in the changelog.
"""

from ._client import VERSION as __version__  # noqa: F401
from ._client import Client, IntegrationError, Response, paginate

__all__ = ["Client", "IntegrationError", "Response", "paginate", "__version__"]
