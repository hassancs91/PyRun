"""
PyRun connections for scripts: call an outside service as a connected account.

A connection (sidebar → Connections) holds an account's credentials: an Airtable
token, an API key, bearer token or username and password for any HTTP API, or an
SMTP login. Grant it to a script (the script's Connections box), then:

    import pyrunner_connections

    api = pyrunner_connections.client("airtable")       # credentials attached
    bases = api.get("/v0/meta/bases").json()

    with pyrunner_connections.smtp("mail") as server:   # connected and logged in
        server.sendmail(server.from_address, ["you@example.com"], message)

    key = pyrunner_connections.token("airtable")        # the bare key, for an SDK
    pyrunner_connections.info("airtable")               # name, provider, account: no secrets
    pyrunner_connections.list()                         # the connections this script has

Name a connection by its name, or by its provider ("airtable") when the script
has exactly one of that provider. Credentials never reach the script's
environment: this helper asks PyRun's loopback API for them, which checks the
grant, and PyRun masks every value it hands out in the run's output, result and
progress. ``client()`` sends them only to the connection's base URL (its scheme,
host and port); a request or a redirect anywhere else goes without them.

``client()`` returns a ``requests.Session`` when ``requests`` is installed in the
script's environment, otherwise a small standard-library client with the same
basics (``get/post/put/patch/delete/request``; responses with ``status_code``,
``headers``, ``content``, ``text``, ``json()``, ``ok`` and ``raise_for_status()``).
Relative URLs are joined to the base URL. Standard library only.
"""

import json as _json
import os
import smtplib
import ssl
import urllib.error
import urllib.parse
import urllib.request

_cache = {}


class ConnectionsError(Exception):
    """A connection that can't be used: not granted, revoked, unreachable..."""

    def __init__(self, message, code="", status=None):
        super().__init__(message)
        self.code = code
        self.status = status


class HTTPError(ConnectionsError):
    """``raise_for_status()`` on a 4xx/5xx reply of the standard-library client."""

    def __init__(self, response):
        super().__init__(f"HTTP {response.status_code} for {response.url}", "HTTP_ERROR",
                         response.status_code)
        self.response = response
        self.status_code = response.status_code


def _post(endpoint, payload):
    base = os.environ.get("PYRUNNER_INTERNAL_URL")
    run_token = os.environ.get("PYRUNNER_INTERNAL_TOKEN")
    if not base or not run_token:
        raise ConnectionsError("Connections are not available in this run context.", "NO_CONTEXT")
    req = urllib.request.Request(
        f"{base.rstrip('/')}/internal/connections/{endpoint}",
        data=_json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {run_token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return _json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            error = (_json.loads(raw) if raw else {}).get("error") or {}
        except ValueError:
            error = {}
        message = error.get("message") or raw.decode("utf-8", "replace") or f"HTTP {e.code}"
        raise ConnectionsError(message, error.get("code", ""), e.code) from None
    except urllib.error.URLError as e:
        raise ConnectionsError(f"Could not reach PyRun: {e.reason}", "UNREACHABLE") from None


def list():  # noqa: A001 - the natural name here
    """The connections granted to this script: name, provider, status... (no secrets)."""
    return _post("list", {}).get("connections", [])


def info(name):
    """What PyRun knows about connection ``name``, without its secrets."""
    data = _post("info", {"connection": name})
    data.pop("ok", None)
    return data


def credentials(name):
    """The raw auth material of ``name`` (headers, params, token, basic, smtp).

    For a library that wants the pieces itself; ``client()`` and ``smtp()`` use
    them for you. Cached for the rest of the run.
    """
    if name not in _cache:
        data = _post("token", {"connection": name})
        data.pop("ok", None)
        _cache[name] = data
    return _cache[name]


def token(name):
    """The bare API key or bearer token of ``name``, e.g. for a service's own SDK."""
    material = credentials(name)
    if not material.get("token"):
        raise ConnectionsError(
            f"Connection '{name}' ({material.get('auth_kind')}) has no single token: "
            "use client() or smtp().", "NO_TOKEN")
    return material["token"]


def _origin(url):
    parts = urllib.parse.urlsplit(url)
    return (parts.scheme.lower(), parts.netloc.lower())


def _join(base, url):
    if urllib.parse.urlsplit(url).scheme:
        return url
    return base.rstrip("/") + "/" + url.lstrip("/") if url else base


def client(name, timeout=30):
    """An HTTP client for ``name``: its credentials go on requests to its base URL."""
    material = credentials(name)
    if not material.get("base_url"):
        raise ConnectionsError(
            f"Connection '{name}' is not an HTTP connection: use smtp() or info().", "NOT_HTTP")
    try:
        import requests
    except ImportError:
        return Client(material, timeout)
    return _requests_session(requests, material, timeout)


def _with_params(extra, params):
    if not extra:
        return params
    if params is None:
        return dict(extra)
    if isinstance(params, dict):
        return {**extra, **params}
    return [*extra.items(), *params]


def _requests_session(requests, material, timeout):
    base = material["base_url"]
    origin = _origin(base)
    secret_headers = {k.lower() for k in material["headers"]}

    def strip(prepared):
        if _origin(prepared.url) != origin:
            for header in [h for h in prepared.headers if h.lower() in secret_headers]:
                del prepared.headers[header]

    class ConnectionSession(requests.Session):
        base_url = base

        def request(self, method, url, *args, **kwargs):
            full = _join(base, url)
            if _origin(full) == origin:
                kwargs["params"] = _with_params(material["params"], kwargs.get("params"))
            kwargs.setdefault("timeout", timeout)
            return super().request(method, full, *args, **kwargs)

        def prepare_request(self, request):
            prepared = super().prepare_request(request)
            strip(prepared)
            return prepared

        def rebuild_auth(self, prepared_request, response):
            super().rebuild_auth(prepared_request, response)
            strip(prepared_request)

    session = ConnectionSession()
    session.headers.update(material["headers"])
    return session


class Response:
    """A reply of the standard-library client (the requests.Response basics)."""

    def __init__(self, status_code, headers, content, url):
        self.status_code = status_code
        self.headers = headers
        self.content = content
        self.url = url

    @property
    def ok(self):
        return self.status_code < 400

    @property
    def text(self):
        charset = self.headers.get_content_charset() if hasattr(self.headers, "get_content_charset") else None
        return self.content.decode(charset or "utf-8", "replace")

    def json(self):
        return _json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise HTTPError(self)

    def __repr__(self):
        return f"<Response [{self.status_code}]>"


class _Redirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects, dropping the connection's headers when one leaves the origin."""

    def __init__(self, origin, secret_headers):
        self.origin = origin
        self.secret_headers = secret_headers

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and _origin(new.full_url) != self.origin:
            for store in (new.headers, new.unredirected_hdrs):
                for header in [h for h in store if h.lower() in self.secret_headers]:
                    del store[header]
        return new


class Client:
    """The standard-library HTTP client ``client()`` returns without requests."""

    def __init__(self, material, timeout=30):
        self.base_url = material["base_url"]
        self.headers = dict(material["headers"])
        self.timeout = timeout
        self._params = dict(material["params"])
        self._origin = _origin(self.base_url)
        self._secret_headers = {k.lower() for k in material["headers"]}

    def request(self, method, url, *, params=None, json=None, data=None, headers=None, timeout=None):
        full = _join(self.base_url, url)
        same = _origin(full) == self._origin
        sent = {k: v for k, v in self.headers.items() if same or k.lower() not in self._secret_headers}
        query = _with_params(self._params if same else {}, params)
        if query:
            joiner = "&" if urllib.parse.urlsplit(full).query else "?"
            full += joiner + urllib.parse.urlencode(query, doseq=True)
        body = None
        if json is not None:
            body = _json.dumps(json).encode("utf-8")
            sent.setdefault("Content-Type", "application/json")
        elif isinstance(data, dict):
            body = urllib.parse.urlencode(data, doseq=True).encode("utf-8")
            sent.setdefault("Content-Type", "application/x-www-form-urlencoded")
        elif isinstance(data, str):
            body = data.encode("utf-8")
        elif data is not None:
            body = bytes(data)
        sent.update(headers or {})
        opener = urllib.request.build_opener(_Redirects(self._origin, self._secret_headers))
        req = urllib.request.Request(full, data=body, headers=sent, method=method.upper())
        try:
            with opener.open(req, timeout=timeout or self.timeout) as resp:
                return Response(resp.status, resp.headers, resp.read(), resp.url)
        except urllib.error.HTTPError as e:
            return Response(e.code, e.headers, e.read(), e.url)
        except urllib.error.URLError as e:
            host = urllib.parse.urlsplit(full).netloc
            raise ConnectionsError(f"Could not reach {host}: {e.reason}", "UNREACHABLE") from None

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def put(self, url, **kwargs):
        return self.request("PUT", url, **kwargs)

    def patch(self, url, **kwargs):
        return self.request("PATCH", url, **kwargs)

    def delete(self, url, **kwargs):
        return self.request("DELETE", url, **kwargs)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def smtp(name, timeout=30):
    """An ``smtplib.SMTP`` connected and logged in as ``name`` (STARTTLS/SSL as set).

    Use it in a ``with`` block (it quits at the end). ``server.from_address`` is
    the connection's From address ("" when it has none).
    """
    material = credentials(name).get("smtp")
    if not material:
        raise ConnectionsError(f"Connection '{name}' is not an SMTP connection.", "NOT_SMTP")
    context = ssl.create_default_context()
    if material["security"] == "ssl":
        server = smtplib.SMTP_SSL(material["host"], material["port"], timeout=timeout, context=context)
    else:
        server = smtplib.SMTP(material["host"], material["port"], timeout=timeout)
    try:
        server.ehlo()
        if material["security"] == "starttls":
            server.starttls(context=context)
            server.ehlo()
        if material["username"]:
            server.login(material["username"], material["password"])
    except BaseException:
        server.close()
        raise
    server.from_address = material.get("from_address", "")
    return server
