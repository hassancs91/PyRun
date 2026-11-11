"""
The shared client every ``pyrunner_integrations`` module is built on.

One authenticated HTTP client per connection. It reads the connection's
credentials from ``pyrunner_connections.credentials()`` before every request
(so an OAuth token PyRun refreshed is used at once), puts them only on requests
to the connection's base URL, follows redirects itself (a redirect that leaves
the base URL goes on without them), retries 429 and 5xx replies with backoff,
asks PyRun once for new credentials after a 401, and turns every failure into
one exception: ``IntegrationError(code, status, retry_after)``.

Standard library only; when ``requests`` is installed in the script's
environment it carries the requests (connection reuse, its proxy and TLS
handling), with the same behaviour.
"""

import email.utils
import hashlib
import http.client
import json as _json
import logging
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import pyrunner_connections as _connections
except ImportError:  # imported as core.script_helpers.pyrunner_integrations (PyRun's own process)
    from core.script_helpers import pyrunner_connections as _connections

logger = logging.getLogger("pyrunner_integrations")

VERSION = "1.0.0"
USER_AGENT = f"pyrunner-integrations/{VERSION}"

MAX_RETRIES = 3  # retries after the first attempt
BACKOFF = 1.0  # seconds before the first retry without Retry-After; doubles each time
MAX_BACKOFF = 30.0
MAX_WAIT = 60.0  # a Retry-After longer than this isn't waited for: the error says it
MAX_REDIRECTS = 5
MAX_PAGES = 1000
# Methods a 5xx or a lost connection may repeat: doing them twice does no harm.
IDEMPOTENT = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
_REDIRECTS = (301, 302, 303, 307, 308)
_DETAIL_CHARS = 300
_MASK = "[MASKED]"

# Every code an IntegrationError can carry besides the connection codes of
# pyrunner_connections (CONNECTION_NOT_FOUND, CONNECTION_EXPIRED...). The docs
# generator prints this table.
ERROR_CODES = {
    "INVALID_REQUEST": "The service refused the request as malformed (HTTP 400, 405 or 422).",
    "UNAUTHORIZED": "The service refused the credentials (HTTP 401), also after PyRun was asked for new ones.",
    "FORBIDDEN": "The account may not do this (HTTP 403): a missing scope or permission.",
    "NOT_FOUND": "No such thing (HTTP 404 or 410).",
    "CONFLICT": "It clashes with the current state (HTTP 409 or 412).",
    "RATE_LIMITED": "Too many requests (HTTP 429), still after the retries; `retry_after` says when to try again.",
    "HTTP_ERROR": "Any other 4xx reply.",
    "SERVER_ERROR": "The service failed (HTTP 5xx), still after the retries.",
    "UNREACHABLE": "The service couldn't be reached (DNS, refused connection, TLS).",
    "TIMEOUT": "No reply in time.",
    "BAD_RESPONSE": "A reply that can't be used: broken JSON, too many redirects or pages, a repeated page cursor.",
    "NOT_HTTP": "The connection isn't an HTTP connection (an SMTP login).",
}
_STATUS_CODES = {
    400: "INVALID_REQUEST", 405: "INVALID_REQUEST", 422: "INVALID_REQUEST",
    401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND", 410: "NOT_FOUND",
    409: "CONFLICT", 412: "CONFLICT", 429: "RATE_LIMITED",
}

# The testing kit's seam (core/integrations/testing.py): a callable with
# ``_send``'s signature that replaces the network (cassettes, a fake service).
# Always None in a real run.
TRANSPORT = None
_sleep = time.sleep

_clients = {}  # (connection, provider) -> Client, so a module's calls share one session


class IntegrationError(_connections.ConnectionsError):
    """Any failure of an integration call.

    ``code`` is one of ``ERROR_CODES`` or a connection code (``CONNECTION_NOT_FOUND``...),
    ``status`` the HTTP status (None when there was no reply), ``retry_after``
    the seconds the service asked to wait (429 or 503), else None. The message
    never holds the connection's credentials. ``response`` is the reply, when
    there was one.
    """

    def __init__(self, message, code, status=None, retry_after=None, response=None):
        super().__init__(message, code, status)
        self.retry_after = retry_after
        self.response = response

    def __repr__(self):
        return f"IntegrationError(code={self.code!r}, status={self.status!r}, retry_after={self.retry_after!r})"


class Headers(dict):
    """Reply headers with lower-case names; lookups ignore case. Repeated headers are joined with ", "."""

    def __init__(self, items=()):
        super().__init__()
        for name, value in items:
            name = name.lower()
            super().__setitem__(name, f"{super().__getitem__(name)}, {value}" if name in self else value)

    def __getitem__(self, name):
        return super().__getitem__(name.lower())

    def __contains__(self, name):
        return super().__contains__(str(name).lower())

    def get(self, name, default=None):
        return super().get(str(name).lower(), default)


class Response:
    """A reply: ``status_code``, ``headers`` (a ``Headers``), ``content`` (bytes), ``url``."""

    def __init__(self, status_code, headers, content, url):
        self.status_code = status_code
        self.headers = headers if isinstance(headers, Headers) else Headers(dict(headers or {}).items())
        self.content = content or b""
        self.url = url

    @property
    def ok(self):
        return self.status_code < 400

    @property
    def text(self):
        """The body decoded with the reply's charset (UTF-8 when it names none or one Python doesn't know)."""
        match = re.search(r"charset=[\"']?([\w.:-]+)", self.headers.get("content-type") or "", re.I)
        try:
            return self.content.decode(match.group(1) if match else "utf-8", "replace")
        except LookupError:
            return self.content.decode("utf-8", "replace")

    def json(self):
        return _json.loads(self.text)

    def __repr__(self):
        return f"<Response [{self.status_code}]>"


def safe_url(url) -> str:
    """``url`` as it may be shown: scheme, host and path (no query string, no user info)."""
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{_host(url)}{parts.path}" if parts.scheme else parts.path


def _secret_values(material) -> list:
    """Every credential value in ``material`` (the token, header and query values, a password)."""
    values = []
    for value in list((material.get("headers") or {}).values()) + list((material.get("params") or {}).values()):
        value = str(value or "")
        values.append(value)
        if " " in value:
            values.append(value.split(" ", 1)[1])  # "Bearer <token>", "Basic <pair>"
    values.append(str(material.get("token") or ""))
    basic = material.get("basic") or {}
    values.append(str(basic.get("password") or ""))
    # Longest first, so a header value is masked whole before its token part.
    return sorted({v for v in values if len(v) >= 4}, key=len, reverse=True)


def mask_in(text, values) -> str:
    """``text`` with each of ``values`` (4 characters or more; longest first) replaced by ``[MASKED]``."""
    text = str(text)
    for value in sorted({str(v) for v in values if v and len(str(v)) >= 4}, key=len, reverse=True):
        text = text.replace(value, _MASK)
    return text


def scrub(text, material) -> str:
    """``text`` with every credential value of ``material`` replaced by ``[MASKED]``."""
    return mask_in(text, _secret_values(material or {}))


def _retry_after(headers):
    """Seconds from a Retry-After header (seconds or an HTTP date), or None."""
    value = (headers.get("retry-after") or "").strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    return max(0.0, when.timestamp() - time.time())


def _detail(response) -> str:
    """The service's own error text from a reply, if it has one (JSON or plain text)."""
    content_type = (response.headers.get("content-type") or "").lower()
    text = response.text.strip()
    if not text:
        return ""
    if "json" in content_type or text[:1] in "{[":
        try:
            body = _json.loads(text)
        except ValueError:
            body = None
        if isinstance(body, dict):
            error = body.get("error")
            candidates = [
                error.get("message") if isinstance(error, dict) else error,
                body.get("message"),
                body.get("error_description"),
            ]
            errors = body.get("errors")
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                candidates.append(errors[0].get("message"))
            for candidate in candidates:
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
        return ""
    if content_type.startswith("text/plain"):
        return text
    return ""


def error_for(method, response, material=None) -> IntegrationError:
    """The IntegrationError for a 4xx/5xx ``response``, its credentials masked."""
    status = response.status_code
    code = _STATUS_CODES.get(status) or ("SERVER_ERROR" if status >= 500 else "HTTP_ERROR")
    message = f"{method} {safe_url(response.url)}: HTTP {status}"
    # Masked before it is cut short, so no part of a credential is left.
    detail = scrub(_detail(response), material)[:_DETAIL_CHARS]
    if detail:
        message += f": {detail}"
    retry_after = _retry_after(response.headers) if status in (429, 503) else None
    return IntegrationError(message, code, status, retry_after, response)


def decode(response, method="GET"):
    """A reply's body: parsed JSON for a JSON reply, the text otherwise, None when empty."""
    if not response.content.strip():
        return None
    content_type = (response.headers.get("content-type") or "").lower()
    if "json" in content_type:
        try:
            return response.json()
        except ValueError:
            raise IntegrationError(
                f"{method} {safe_url(response.url)}: the reply isn't valid JSON", "BAD_RESPONSE",
                response.status_code, response=response,
            ) from None
    return response.text


def quote(value) -> str:
    """``value`` safe as one URL path segment (``/`` included): ``f"/items/{quote(item_id)}"``."""
    return urllib.parse.quote(str(value), safe="")


def dig(data, dotted):
    """``data["a"]["b"]`` for ``"a.b"``; None when a part is missing."""
    for part in str(dotted).split("."):
        if not isinstance(data, dict):
            return None
        data = data.get(part)
    return data


def multipart_related(metadata, data, mime_type):
    """``(body, content type)`` of a multipart/related upload (Google's): ``metadata`` as JSON, then ``data``.

    The boundary is made from the content, so the same upload gives the same
    body (a cassette replays it).
    """
    boundary = "pyrunner-" + hashlib.sha256(data).hexdigest()[:32]
    while boundary.encode("ascii") in data:
        boundary += "x"
    head = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{_json.dumps(metadata)}\r\n--{boundary}\r\nContent-Type: {mime_type}\r\n\r\n")
    body = head.encode("utf-8") + data + f"\r\n--{boundary}--".encode("ascii")
    return body, f"multipart/related; boundary={boundary}"


def next_link(header):
    """The ``rel="next"`` URL of a Link header (GitHub's pagination), or None."""
    for part in (header or "").split(","):
        if ";" not in part:
            continue
        url, _sep, params = part.partition(";")
        rels = [p.strip() for p in params.split(";")]
        if any(r.replace(" ", "") in ('rel="next"', "rel=next") for r in rels):
            return url.strip().strip("<>")
    return None


def paginate(fetch, *, limit=None, max_pages=MAX_PAGES):
    """Yield every item of a paginated API, page by page.

    ``fetch(cursor)`` returns ``(items, next_cursor)`` for one page; the first
    call gets None. It stops at a falsy next cursor, after ``limit`` items
    (without asking for the next page) or raises BAD_RESPONSE on a cursor seen
    before or after ``max_pages`` pages.
    """
    cursor, seen, pages, count = None, set(), 0, 0
    if limit is not None and limit <= 0:
        return
    while True:
        items, cursor = fetch(cursor)
        pages += 1
        for item in items:
            yield item
            count += 1
            if limit is not None and count >= limit:
                return
        if not cursor:
            return
        key = _json.dumps(cursor, sort_keys=True, default=str)
        if key in seen:
            raise IntegrationError("The service returned the same page cursor twice.", "BAD_RESPONSE")
        if pages >= max_pages:
            raise IntegrationError(
                f"More than {max_pages} pages: pass a limit, or narrow the query.", "BAD_RESPONSE")
        seen.add(key)


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """The client follows redirects itself (to decide which hop gets the credentials)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirects)


def _host(url) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.hostname}:{parts.port}" if parts.port else (parts.hostname or "")


def _send_urllib(method, url, headers, body, timeout) -> Response:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    host = _host(url)
    try:
        with _OPENER.open(request, timeout=timeout) as reply:
            return Response(reply.status, Headers(reply.headers.items()), reply.read(), url)
    except urllib.error.HTTPError as e:
        try:
            content = e.read()
        except (OSError, http.client.HTTPException):
            content = b""  # the status says what happened; a body cut off adds nothing
        return Response(e.code, Headers(e.headers.items() if e.headers else ()), content, url)
    except urllib.error.URLError as e:
        if isinstance(e.reason, (TimeoutError, socket.timeout)):
            raise IntegrationError(f"{method} {safe_url(url)}: no reply in {timeout} s", "TIMEOUT") from None
        raise IntegrationError(f"Could not reach {host}: {e.reason}", "UNREACHABLE") from None
    except (TimeoutError, socket.timeout):
        raise IntegrationError(f"{method} {safe_url(url)}: no reply in {timeout} s", "TIMEOUT") from None
    except (OSError, http.client.HTTPException) as e:
        raise IntegrationError(f"Could not reach {host}: {type(e).__name__}", "UNREACHABLE") from None


def _send_requests(session, requests, method, url, headers, body, timeout) -> Response:
    host = _host(url)
    try:
        reply = session.request(method, url, headers=headers, data=body, timeout=timeout, allow_redirects=False)
    except requests.exceptions.Timeout:
        raise IntegrationError(f"{method} {safe_url(url)}: no reply in {timeout} s", "TIMEOUT") from None
    except requests.exceptions.RequestException as e:
        # Not str(e): requests puts the full URL (an API key in the query) in it.
        raise IntegrationError(f"Could not reach {host}: {type(e).__name__}", "UNREACHABLE") from None
    return Response(reply.status_code, Headers(reply.headers.items()), reply.content, url)


def _encode(json, data):
    """(body bytes or None, its Content-Type or None)."""
    if json is not None:
        return _json.dumps(json).encode("utf-8"), "application/json"
    if isinstance(data, dict):
        return urllib.parse.urlencode(data, doseq=True).encode("utf-8"), "application/x-www-form-urlencoded"
    if isinstance(data, str):
        return data.encode("utf-8"), None
    if data is not None:
        return bytes(data), None
    return None, None


def _with_query(url, params):
    if not params:
        return url
    joiner = "&" if urllib.parse.urlsplit(url).query else "?"
    return url + joiner + urllib.parse.urlencode(params, doseq=True)


class Client:
    """An authenticated HTTP client for one connection.

    ``connection`` is a connection name, or None for the script's one connection
    of ``provider``. ``check(response, body)`` may raise an IntegrationError for a
    2xx reply that is really a failure (Slack's ``"ok": false``).
    ``rate_limit_wait`` is the seconds to wait after a 429 without Retry-After,
    for a service that documents a fixed pause (Airtable's 30 s); otherwise
    the usual backoff applies.
    ``base_url`` sends the requests (and the credentials) to another API host of
    the same service instead of the connection's base URL: an https URL whose
    host is a sibling of the base URL's (``https://sheets.googleapis.com`` for a
    Google connection at ``https://www.googleapis.com``). Anything else raises
    ValueError.
    """

    def __init__(self, connection=None, *, provider="http", timeout=30, max_retries=MAX_RETRIES,
                 max_wait=MAX_WAIT, check=None, rate_limit_wait=None, base_url=None):
        self.connection = connection or provider
        self.provider = provider
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_wait = max_wait
        self.check = check
        self.rate_limit_wait = rate_limit_wait
        self.base_url = base_url
        self._session = None

    def __repr__(self):
        return f"<pyrunner_integrations.Client connection={self.connection!r}>"

    # -- credentials ----------------------------------------------------------

    def _material(self, refused=False) -> dict:
        try:
            material = _connections.credentials(self.connection, refused=refused)
        except IntegrationError:
            raise
        except _connections.ConnectionsError as e:
            raise IntegrationError(str(e), e.code or "CONNECTION_ERROR", e.status) from None
        if not material.get("base_url"):
            raise IntegrationError(
                f"Connection '{self.connection}' is not an HTTP connection.", "NOT_HTTP")
        if self.base_url:
            material = dict(material, base_url=sibling_base(material["base_url"], self.base_url))
        return material

    # -- sending --------------------------------------------------------------

    def _transport(self, method, url, headers, body, timeout) -> Response:
        if TRANSPORT is not None:
            return TRANSPORT(method, url, headers, body, timeout)
        try:
            import requests
        except ImportError:
            return _send_urllib(method, url, headers, body, timeout)
        if self._session is None:
            self._session = requests.Session()
        return _send_requests(self._session, requests, method, url, headers, body, timeout)

    def _send(self, material, method, path, params, body, content_type, headers, timeout) -> Response:
        """One request, its redirects followed: credentials only on hops to the base URL."""
        base = material["base_url"]
        origin = _connections._origin(base)
        secret_names = {k.lower() for k in material.get("headers") or {}}
        url = _connections._join(base, path)
        query = params
        for hop in range(MAX_REDIRECTS + 1):
            same = _connections._origin(url) == origin
            sent = {"User-Agent": USER_AGENT}
            if same:
                sent.update(material.get("headers") or {})
            if body is not None and content_type:
                sent["Content-Type"] = content_type
            for name, value in (headers or {}).items():
                if same or name.lower() not in secret_names:
                    sent[name] = value
            if hop:
                # A redirect's Location: the query key once on the origin, never elsewhere.
                full = _connections._redirect_url(url, material.get("params"), same)
            else:
                full = _with_query(url, _connections._with_params(material.get("params") if same else {}, query))
            logger.debug("%s %s", method, safe_url(full))
            response = self._transport(method, full, sent, body, timeout or self.timeout)
            location = response.headers.get("location")
            if response.status_code not in _REDIRECTS or not location:
                return response
            url, query = urllib.parse.urljoin(full, location), None
            # 303 means "GET it there"; a POST answered 301/302 goes on as a GET too.
            if (response.status_code == 303 and method != "HEAD") or (
                    response.status_code in (301, 302) and method == "POST"):
                method, body, content_type = "GET", None, None
        raise IntegrationError(f"{method} {safe_url(url)}: more than {MAX_REDIRECTS} redirects", "BAD_RESPONSE")

    def _wait(self, method, url, why, attempt, retry_after):
        wait = retry_after if retry_after is not None else min(MAX_BACKOFF, BACKOFF * 2 ** (attempt - 1))
        logger.warning("%s %s: %s, retrying in %.1f s (retry %d of %d)",
                    method, safe_url(url), why, wait, attempt, self.max_retries)
        _sleep(wait)

    def request(self, method, path, *, params=None, json=None, data=None, headers=None, timeout=None,
                idempotent=None) -> Response:
        """Send one request and return its 2xx/3xx ``Response``; raise IntegrationError otherwise.

        ``path`` is relative to the connection's base URL (or a full URL).
        ``json`` is sent as JSON, ``data`` as a form (a dict) or as is. A 429
        (or a 503 with Retry-After) is retried for any method; other 5xx
        replies and lost connections only for idempotent methods, unless
        ``idempotent=True`` says this one is safe to repeat.
        """
        method = method.upper()
        repeatable = method in IDEMPOTENT if idempotent is None else bool(idempotent)
        body, content_type = _encode(json, data)
        material = self._material()
        attempt, reauthed = 0, False
        url = _connections._join(material["base_url"], path)
        while True:
            try:
                response = self._send(material, method, path, params, body, content_type, headers, timeout)
            except IntegrationError as e:
                if e.code in ("UNREACHABLE", "TIMEOUT") and repeatable and attempt < self.max_retries:
                    attempt += 1
                    self._wait(method, url, e.code.lower(), attempt, None)
                    continue
                e.args = (scrub(e.args[0], material),) if e.args else e.args
                raise
            status = response.status_code
            if status == 401 and not reauthed:
                reauthed = True
                fresh = self._material(refused=True)
                if _secret_values(fresh) != _secret_values(material):
                    logger.warning("%s %s: HTTP 401, retrying once with new credentials", method, safe_url(url))
                    material = fresh
                    continue
            if status in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                retry_after = _retry_after(response.headers)
                if status == 429 and retry_after is None:
                    retry_after = self.rate_limit_wait
                if status == 429 or (status == 503 and retry_after is not None) or repeatable:
                    if retry_after is None or retry_after <= self.max_wait:
                        attempt += 1
                        self._wait(method, url, f"HTTP {status}", attempt, retry_after)
                        continue
            if status >= 400:
                raise error_for(method, response, material)
            return response

    def call(self, method, path, **kwargs):
        """``request()``, then the body: parsed JSON for a JSON reply, the text otherwise, None when empty."""
        response = self.request(method, path, **kwargs)
        body = decode(response, method.upper())
        if self.check is not None:
            self.check(response, body)
        return body

    def get(self, path, **kwargs):
        return self.call("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.call("POST", path, **kwargs)

    def put(self, path, **kwargs):
        return self.call("PUT", path, **kwargs)

    def patch(self, path, **kwargs):
        return self.call("PATCH", path, **kwargs)

    def delete(self, path, **kwargs):
        return self.call("DELETE", path, **kwargs)

    def paginate(self, path, *, method="GET", params=None, json=None, items="items", cursor=None,
                 cursor_param=None, link=False, page_param=None, page_start=1, limit=None,
                 max_pages=MAX_PAGES, **kwargs):
        """Yield every item of a paginated list, asking for pages as it goes.

        ``items`` is the (dotted) key of the list in each reply, or None when the
        reply is the list. Say how the next page is asked for with one of:

        - ``cursor``: the (dotted) key of the next page's cursor in the reply
          (Airtable ``"offset"``, Notion ``"next_cursor"``, Slack
          ``"response_metadata.next_cursor"``), sent back as ``cursor_param``
          (default: the cursor key's last part) in the query, or in the JSON
          body when ``json`` is given;
        - ``link=True``: the Link header's ``rel="next"`` URL (GitHub);
        - ``page_param``: page numbers from ``page_start``, until a page has no items.

        ``limit`` stops after that many items.
        """
        if sum(bool(x) for x in (cursor, link, page_param)) != 1:
            raise ValueError("paginate() needs exactly one of cursor=, link=True or page_param=")
        send_as = cursor_param or (str(cursor).rsplit(".", 1)[-1] if cursor else None)

        def page_items(body):
            found = body if items is None else dig(body, items)
            if not isinstance(found, list):
                raise IntegrationError(
                    f"{method} {path}: the reply has no {items or 'list'!r} list", "BAD_RESPONSE")
            return found

        def fetch(token):
            query = dict(params or {})
            payload = dict(json) if json is not None else None
            if link:
                response = self.request(method, token or path, params=None if token else params,
                                        json=payload, **kwargs)
                body = decode(response, method)
                if self.check is not None:
                    self.check(response, body)
                return page_items(body), next_link(response.headers.get("link"))
            if page_param:
                query[page_param] = token or page_start
            elif token is not None:
                if payload is not None:
                    payload[send_as] = token
                else:
                    query[send_as] = token
            body = self.call(method, path, params=query or None, json=payload, **kwargs)
            found = page_items(body)
            if page_param:
                return found, ((token or page_start) + 1 if found else None)
            return found, dig(body, cursor)

        return paginate(fetch, limit=limit, max_pages=max_pages)


def sibling_base(connection_base, base_url) -> str:
    """``base_url`` when it may stand in for ``connection_base`` (``Client(base_url=...)``), else ValueError.

    It must be https, with no path, user info or query, on a host that shares
    the connection host's parent domain: ``sheets.googleapis.com`` for
    ``www.googleapis.com``. It keeps a module from sending a connection's
    credentials to an unrelated host by mistake; it is not a boundary against
    the script itself, which can read its token anyway (``pyrunner_connections.token()``).
    """
    parts, base = urllib.parse.urlsplit(base_url), urllib.parse.urlsplit(connection_base)
    host, base_host = (parts.hostname or "").lower(), (base.hostname or "").lower()
    parent = base_host.partition(".")[2]
    if (parts.scheme != "https" or parts.username or parts.password or parts.query or parts.fragment
            or parts.path.strip("/") or "." not in parent or not host.endswith("." + parent)):
        raise ValueError(
            f"base_url {safe_url(base_url)!r} isn't another https host of the connection's service "
            f"(a sibling of {base_host or 'its host'})."
        )
    return f"https://{_host(base_url)}"


def shared(connection=None, provider="http", **options) -> Client:
    """The module-wide Client for ``connection`` (one session reused across calls).

    ``options`` are Client keyword arguments, used when the client is made: a
    module passes the same ones on every call (two modules of one provider
    with different options, such as another ``base_url``, get their own client).
    """
    key = (connection or provider, provider, tuple(sorted(options.items())))
    client = _clients.get(key)
    if client is None:
        client = _clients[key] = Client(connection, provider=provider, **options)
    return client


def reset():
    """Forget cached clients and credentials (the testing kit calls it between tests)."""
    _clients.clear()
    _connections._cache.clear()
