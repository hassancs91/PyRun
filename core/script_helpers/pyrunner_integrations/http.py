"""
Call any HTTP API through an HTTP API connection: an API key, a bearer token or a username and password.

Paths are relative to the connection's base URL (a full URL works too; the
credentials only go to the base URL). Each function returns the reply's body:
the parsed JSON for a JSON reply, the text for any other, None when it is empty.
A 4xx or 5xx reply raises ``IntegrationError``.

An OAuth connection (Google, Microsoft 365, GitHub, Slack or Notion) works too
when you pass its name, for an endpoint PyRun has no integration for. For example:

    files = http.get("/drive/v3/files", connection="google")

Its access token goes as a bearer token only to that provider's base URL
(``https://www.googleapis.com`` for Google; another host, even another Google
one, gets the request without it), and PyRun refreshes it when it expires.
"""

from . import _client

PROVIDER = "http"


def client(connection=None, *, timeout=30):
    """The shared client of ``connection``, for anything the other functions don't cover.

    Its ``request(method, path, ...)`` returns the reply itself (``status_code``,
    ``headers``, ``content``, ``text``, ``json()``); ``get``/``post``/``put``/
    ``patch``/``delete`` and ``paginate`` work as the functions of this module.
    """
    return _client.Client(connection, provider=PROVIDER, timeout=timeout)


def request(method, path, *, connection=None, params=None, json=None, data=None, headers=None, timeout=None):
    """Send ``method`` to ``path`` and return the reply's body.

    ``params`` go in the query string, ``json`` is sent as JSON, ``data`` as a
    form (a dict) or as is (text or bytes). ``headers`` are added to the
    connection's own. GET, PUT and DELETE are retried after a 5xx reply or a lost
    connection; POST and PATCH only after a 429 or a 503 with Retry-After (any
    other failure might have happened after the service acted).
    """
    return _client.shared(connection, PROVIDER).call(
        method, path, params=params, json=json, data=data, headers=headers, timeout=timeout)


def get(path, *, connection=None, params=None, headers=None, timeout=None):
    """GET ``path`` and return the reply's body.

    For example:

        user = http.get("/v1/me")
        page = http.get("/v1/items", params={"page": 2})
    """
    return request("GET", path, connection=connection, params=params, headers=headers, timeout=timeout)


def post(path, *, connection=None, json=None, data=None, params=None, headers=None, timeout=None):
    """POST ``json`` (or form ``data``) to ``path`` and return the reply's body.

    For example:

        created = http.post("/v1/items", json={"name": "Ada"})
    """
    return request("POST", path, connection=connection, params=params, json=json, data=data,
                   headers=headers, timeout=timeout)


def put(path, *, connection=None, json=None, data=None, params=None, headers=None, timeout=None):
    """PUT ``json`` (or ``data``) to ``path`` and return the reply's body."""
    return request("PUT", path, connection=connection, params=params, json=json, data=data,
                   headers=headers, timeout=timeout)


def patch(path, *, connection=None, json=None, data=None, params=None, headers=None, timeout=None):
    """PATCH ``path`` with ``json`` (or ``data``) and return the reply's body."""
    return request("PATCH", path, connection=connection, params=params, json=json, data=data,
                   headers=headers, timeout=timeout)


def delete(path, *, connection=None, params=None, headers=None, timeout=None):
    """DELETE ``path`` and return the reply's body (often None)."""
    return request("DELETE", path, connection=connection, params=params, headers=headers, timeout=timeout)


def paginate(path, *, connection=None, items="items", cursor=None, cursor_param=None, link=False,
             page_param=None, params=None, limit=None):
    """Yield every item of a paginated list at ``path``, fetching pages as it goes.

    ``items`` is the key of the list in each reply (dotted for a nested one,
    None when the reply is the list). Pick how the API pages: ``cursor="next"``
    (the reply's key holding the next page's cursor, sent back as
    ``cursor_param``, by default the same name), ``link=True`` (a Link header,
    like GitHub's) or ``page_param="page"`` (page numbers until an empty page).
    ``limit`` stops after that many items. For example:

        for item in http.paginate("/v1/items", items="data", cursor="next_cursor", cursor_param="cursor"):
            print(item["id"])
    """
    return _client.shared(connection, PROVIDER).paginate(
        path, params=params, items=items, cursor=cursor, cursor_param=cursor_param, link=link,
        page_param=page_param, limit=limit)
