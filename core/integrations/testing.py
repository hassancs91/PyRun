"""
The integrations testing kit (2.5 Stage 6, T26): test code that calls outside services, with no credentials and no network.

For PyRun's integration modules and for plugin authors whose code (a worker
script's functions, a plugin's own client) uses ``pyrunner_integrations``. Tests
run in PyRun's test process, so a plugin's tests use it as they are:

    from core.integrations import testing

    class FetchItemsTests(testing.IntegrationTestCase):
        def test_fetch(self):
            crm = self.use_connection("crm", provider="http", auth_kind="bearer",
                                      config={"base_url": "https://crm.example.com"})
            fake = self.fake_http()
            fake.add("GET", "/v1/items", json={"items": [{"id": 1}]})
            self.assertEqual(my_worker.fetch_items(), [1])
            self.assertEqual(fake.requests[0].headers["Authorization"], f"Bearer {crm.credentials['token']}")

        def test_against_a_recording(self):
            self.use_connection("crm", provider="http", auth_kind="bearer",
                                config={"base_url": "https://crm.example.com"})
            with self.cassette("list_items"):        # cassettes/list_items.json beside the test file
                self.assertEqual(len(my_worker.fetch_items()), 3)

What it gives:

- ``use_connection()``: a connection the code under test gets from
  ``pyrunner_connections.credentials()`` (and ``token()``, ``info()``,
  ``list()``), with made-up credentials unless you pass some. No database, no
  run, no loopback.
- ``fake_http()``: a scripted stand-in for the service. Replies in the order
  added; every request recorded (method, URL, headers, body).
- ``cassette(name)``: replays a recorded exchange, offline. With
  ``PYRUNNER_RECORD_CASSETTES=1`` and real credentials in the JSON file
  ``PYRUNNER_RECORD_CREDENTIALS`` names, it calls the real service and writes the
  file instead, every credential value scrubbed.
- No request reaches the network otherwise: an unanswered request fails the test.
- Waits (backoff, Retry-After) don't sleep: they are collected in ``self.sleeps``.
- ``ConformanceSuite``: the checks every integration module must pass.
"""

import base64
import inspect
import json
import logging
import os
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from core.integrations import framework
from core.integrations.framework import load_preset
from core.integrations.presets import (
    API_KEY,
    BASIC,
    BEARER,
    OAUTH2,
    SMTP,
    auth_material,
    mask_values,
)
from core.integrations.presets import base_url as preset_base_url

# Importing the kit makes pyrunner_connections / pyrunner_integrations importable
# by the names scripts use, so a test module can import the code under test
# (a worker module that does ``from pyrunner_integrations import http``) after it.
framework.helpers_on_path()

RECORD_ENV = "PYRUNNER_RECORD_CASSETTES"
CREDENTIALS_ENV = "PYRUNNER_RECORD_CREDENTIALS"
CASSETTE_FORMAT = 1
# Reply headers a cassette keeps (the rest is noise: dates, request ids, cookies).
_KEEP_HEADERS = ("content-type", "link", "location", "retry-after")
_KEEP_PREFIXES = ("x-ratelimit", "ratelimit")
_AUTH_KINDS = (OAUTH2, API_KEY, BEARER, BASIC, SMTP)


def load_tests(loader, tests, pattern):
    """This module's name matches test discovery's ``test*.py``: it holds no tests of its own."""
    return unittest.TestSuite()


def recording() -> bool:
    """True when ``PYRUNNER_RECORD_CASSETTES`` asks cassettes to record."""
    return os.environ.get(RECORD_ENV, "").strip().lower() in ("1", "true", "yes", "on")


# (pyrunner_connections, pyrunner_integrations._client), as scripts import them.
helpers = framework.script_helpers


def _real_credentials(name, provider):
    """Real ``{auth_kind, config, credentials}`` for recording, from the PYRUNNER_RECORD_CREDENTIALS file."""
    path = os.environ.get(CREDENTIALS_ENV, "").strip()
    if not path:
        return None
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get(name) or data.get(provider)


# --------------------------------------------------------------------------- #
# Connections
# --------------------------------------------------------------------------- #


class FakeConnection:
    """A connection as the code under test sees it: what PyRun would hand a run.

    ``credentials`` and ``config`` are the stored fields; ``material()`` is what
    ``pyrunner_connections.credentials()`` returns; ``secrets()`` every value
    PyRun would mask. ``real`` is True when the credentials came from the
    recording file.
    """

    def __init__(self, name, provider, auth_kind=None, config=None, credentials=None, base_url=None,
                 rotate=True, error=None, real=False):
        self.name = name
        self.provider = provider
        self.preset = load_preset(provider)
        self.auth_kind = auth_kind or self.preset.auth_kinds[0]
        if self.auth_kind not in self.preset.auth_kinds:
            raise ValueError(f"{provider} connections take {', '.join(self.preset.auth_kinds)}, not {self.auth_kind}")
        self.config = self._default_config(config)
        self.credentials = dict(credentials) if credentials is not None else self._fake_credentials()
        self.base_url = (base_url or preset_base_url(self.preset, self.config)) if self.preset.is_http else None
        self.rotate = rotate
        self.rotations = 0
        self.error = error  # (code, status) the lookup fails with, e.g. ("CONNECTION_EXPIRED", 409)
        self.real = real
        self._old_secrets = set()

    def _default_config(self, given):
        config = {}
        for field in self.preset.config_fields(self.auth_kind):
            default = field.default
            if field.name == "base_url":
                default = f"https://{self.provider.replace('_', '-')}.example.test"
            config[field.name] = list(default) if isinstance(default, tuple) else default
        config.update(given or {})
        return config

    def _fake_credentials(self):
        return {f.name: f"fake-{self.provider}-{f.name}-0123456789" for f in self.preset.secret_fields(self.auth_kind)}

    def material(self) -> dict:
        out = {
            "name": self.name,
            "provider": self.provider,
            "provider_label": self.preset.label,
            "auth_kind": self.auth_kind,
            "status": "ok",
            "account_label": "",
            "base_url": self.base_url,
            **auth_material(self.preset, self.auth_kind, self.config, self.credentials),
        }
        if self.auth_kind == OAUTH2:
            out["expires_in"] = None
        return out

    def info(self) -> dict:
        keys = ("name", "provider", "provider_label", "auth_kind", "status", "account_label", "base_url")
        return {k: v for k, v in self.material().items() if k in keys}

    def secrets(self) -> set:
        """Every value PyRun would mask for this connection, now or before a rotation."""
        current = mask_values(self.name, self.preset, self.auth_kind, self.config, self.credentials)
        return {v for v in current.values() if v} | self._old_secrets

    def rotate_credentials(self):
        """New values for every secret field (what a refresh, or someone editing it, does)."""
        self._old_secrets |= self.secrets()
        self.rotations += 1
        self.credentials = {k: f"{v}-rotated{self.rotations}" if isinstance(v, str) and v else v
                            for k, v in self.credentials.items()}


class Connections:
    """The connections a test has, standing in for PyRun's loopback."""

    def __init__(self):
        self.by_name = {}
        self.calls = []  # (name, refused) of every credentials() call

    def add(self, connection):
        self.by_name[connection.name] = connection
        return connection

    def find(self, name):
        _pc, _client = helpers()
        found = self.by_name.get(name)
        if found is None:
            same = [c for c in self.by_name.values() if c.provider == name]
            if len(same) > 1:
                raise _pc.ConnectionsError(
                    f"Several {name} connections are granted to this script: name one.", "AMBIGUOUS", 409)
            found = same[0] if same else None
        if found is None:
            raise _pc.ConnectionsError(
                f"Connection '{name}' doesn't exist or isn't granted to this script.", "CONNECTION_NOT_FOUND", 404)
        if found.error:
            code, status = found.error
            raise _pc.ConnectionsError(f"Connection '{found.name}': {code}", code, status)
        return found

    def credentials(self, name, *, refused=False):
        self.calls.append((name, refused))
        connection = self.find(name)
        if refused and connection.rotate:
            connection.rotate_credentials()
        return connection.material()

    def info(self, name):
        return self.find(name).info()

    def list(self):
        return [c.info() for c in self.by_name.values() if not c.error]

    def secrets(self) -> set:
        out = set()
        for connection in self.by_name.values():
            out |= connection.secrets()
        return out

    def patch(self):
        """Patchers putting these connections behind ``pyrunner_connections``."""
        pc, _client = helpers()
        return [
            mock.patch.object(pc, "credentials", self.credentials),
            mock.patch.object(pc, "info", self.info),
            mock.patch.object(pc, "list", self.list),
        ]


# --------------------------------------------------------------------------- #
# The fake service
# --------------------------------------------------------------------------- #


def _response(status, json_body, body, headers, url):
    _pc, client = helpers()
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    if json_body is not None:
        content = json.dumps(json_body).encode("utf-8")
        headers.setdefault("content-type", "application/json")
    elif isinstance(body, str):
        content = body.encode("utf-8")
        headers.setdefault("content-type", "text/plain; charset=utf-8")
    else:
        content = bytes(body or b"")
    return client.Response(status, client.Headers(headers.items()), content, url)


class SentRequest:
    """One request the code under test sent: ``method``, ``url``, ``headers`` (any case), ``body`` (bytes or None)."""

    def __init__(self, method, url, headers, body):
        _pc, client = helpers()
        self.method = method
        self.url = url
        self.headers = client.Headers((headers or {}).items())
        self.body = body

    @property
    def path(self):
        return urllib.parse.urlsplit(self.url).path

    @property
    def params(self) -> dict:
        """The query string, one value per name (the last)."""
        return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.url).query, keep_blank_values=True))

    def json(self):
        return json.loads(self.body) if self.body else None

    def __repr__(self):
        return f"<SentRequest {self.method} {self.url}>"


class _Reply:
    def __init__(self, method, url, status, json_body, body, headers, times, error):
        self.method = method.upper() if method else None
        self.url = url
        self.status, self.json, self.body, self.headers = status, json_body, body, headers
        self.times = times
        self.error = error

    def matches(self, request, base_url):
        if self.method and self.method != request.method:
            return False
        if self.url is None:
            return True
        pattern = self.url
        if not urllib.parse.urlsplit(pattern).scheme and base_url:
            pattern = base_url.rstrip("/") + "/" + pattern.lstrip("/")
        want, got = urllib.parse.urlsplit(pattern), urllib.parse.urlsplit(request.url)
        if want.scheme and (want.scheme, want.netloc) != (got.scheme, got.netloc):
            return False
        if want.path != got.path:
            return False
        if want.query:
            return sorted(urllib.parse.parse_qsl(want.query)) == sorted(urllib.parse.parse_qsl(got.query))
        return True

    def reply(self, request):
        if self.error:
            _pc, client = helpers()
            raise client.IntegrationError(f"(fake) {self.error}", self.error)
        json_body = self.json(request) if callable(self.json) else self.json
        return _response(self.status, json_body, self.body, self.headers, request.url)


class FakeTransport:
    """A scripted stand-in for a service: replies in the order added, every request kept in ``requests``.

    ``add(method, url, ...)`` answers the next matching request once (``times``
    for more). ``url`` is a path relative to the connection's base URL, matched
    without the query string unless it has one, or a full URL; None matches any
    URL, as a None ``method`` matches any method. ``default(...)`` answers what
    nothing else does. ``json`` may be a function of the ``SentRequest``.
    ``error="UNREACHABLE"`` (or ``"TIMEOUT"``) fails the request as the network
    would. An unanswered request fails the test.
    """

    def __init__(self, base_url=None):
        self.base_url = base_url
        self.requests = []
        self._replies = []
        self._default = None

    def add(self, method, url, *, status=200, json=None, body=None, headers=None, times=1, error=None):
        self._replies.append(_Reply(method, url, status, json, body, headers, times, error))
        return self

    def default(self, *, status=200, json=None, body=None, headers=None, error=None):
        self._default = _Reply(None, None, status, json, body, headers, 1, error)
        return self

    def __call__(self, method, url, headers, body, timeout):
        request = SentRequest(method, url, headers, body)
        self.requests.append(request)
        for reply in self._replies:
            if reply.times > 0 and reply.matches(request, self.base_url):
                reply.times -= 1
                return reply.reply(request)
        if self._default is not None:
            return self._default.reply(request)
        raise AssertionError(f"Unexpected request {method} {url}: no fake reply matches it (add one with add()).")


def echo(request) -> dict:
    """A reply body quoting back every header and the query of ``request`` (a careless service)."""
    quoted = " ".join(f"{name}={value}" for name, value in request.headers.items())
    return {"error": {"message": f"boom: {quoted} {urllib.parse.urlsplit(request.url).query}"}}


def _no_network(method, url, headers, body, timeout):
    raise AssertionError(
        f"The code under test made a real request ({method} {url}): answer it with "
        "self.fake_http() or self.cassette()."
    )


# --------------------------------------------------------------------------- #
# Cassettes
# --------------------------------------------------------------------------- #


def _body_entry(content, content_type, clean) -> dict:
    """A body as a cassette keeps it (``clean`` masks credentials in its text)."""
    if content is None or content == b"":
        return {}
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return {"base64": base64.b64encode(content).decode("ascii")}
    text = clean(text)
    if "json" in (content_type or "").lower() or text[:1] in "{[":
        try:
            return {"json": json.loads(text)}
        except ValueError:
            pass
    return {"text": text}


def _body_bytes(entry, restore):
    if "json" in entry:
        return restore(json.dumps(entry["json"])).encode("utf-8")
    if "text" in entry:
        return restore(entry["text"]).encode("utf-8")
    if "base64" in entry:
        return base64.b64decode(entry["base64"])
    return b""


def _same_body(recorded, live):
    if "json" in recorded:
        return "json" in live and recorded["json"] == live["json"]
    return recorded == live


class Cassette:
    """Replays (or, while recording, writes) the exchanges in one cassette file.

    Replay: each request must be the next recorded one (method, URL relative to
    the connection's base URL, query and body; credentials compared scrubbed),
    and every recorded request must be made. Recording: the real service is
    called, and the file is written at the end with every credential value of
    ``connections`` (and any ``scrub`` value) replaced by ``[MASKED]``, and a
    connection's base URL in a reply (a Link header, a next-page URL) written
    as ``{base_url:<name>}``, which replay turns back into the test's own.
    """

    def __init__(self, path, connections, *, scrub=(), allow_unused=False):
        self.path = Path(path)
        self.connections = connections
        self.record = recording()
        self.extra = {str(s) for s in scrub if s}
        self.allow_unused = allow_unused
        self.interactions = []
        self._next = 0
        self._previous = None

    def _secrets(self):
        return self.connections.secrets() | self.extra

    def _relative(self, url):
        """``url`` relative to the base URL of the connection it belongs to, else as it is."""
        bases = sorted((c.base_url for c in self.connections.by_name.values() if c.base_url), key=len, reverse=True)
        for base in bases:
            base = base.rstrip("/")
            if url == base or url.startswith(base + "/") or url.startswith(base + "?"):
                return url[len(base):] or "/"
        return url

    def _bases(self):
        return sorted(((c.name, c.base_url.rstrip("/")) for c in self.connections.by_name.values() if c.base_url),
                      key=lambda pair: len(pair[1]), reverse=True)

    def _clean(self, text):
        """Credentials masked, base URLs as placeholders: what the file holds."""
        text = helpers()[1].mask_in(text, self._secrets())
        for name, base in self._bases():
            text = text.replace(base, "{base_url:%s}" % name)
        return text

    def _restore(self, text):
        for name, base in self._bases():
            text = text.replace("{base_url:%s}" % name, base)
        return text

    def _request_key(self, method, url, headers, body):
        content_type = {k.lower(): v for k, v in (headers or {}).items()}.get("content-type", "")
        return {"method": method, "url": helpers()[1].mask_in(self._relative(url), self._secrets()),
                **_body_entry(body, content_type, self._clean)}

    def __enter__(self):
        _pc, client = helpers()
        if self.record:
            missing = [c.name for c in self.connections.by_name.values() if not c.real]
            if missing:
                raise AssertionError(
                    f"Recording {self.path.name} needs real credentials for {', '.join(missing)}: put them in the "
                    f"JSON file {CREDENTIALS_ENV} names ({{\"<name or provider>\": {{\"auth_kind\": ..., "
                    "\"config\": {...}, \"credentials\": {...}}})."
                )
        else:
            if not self.path.is_file():
                raise AssertionError(
                    f"No cassette at {self.path}: record it with {RECORD_ENV}=1 and real credentials "
                    f"in {CREDENTIALS_ENV} (docs/integrations/framework.md)."
                )
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("pyrunner_cassette") != CASSETTE_FORMAT:
                raise AssertionError(f"{self.path} is not a cassette of format {CASSETTE_FORMAT}.")
            self.interactions = data.get("interactions") or []
        self._previous = client.TRANSPORT
        client.TRANSPORT = self
        return self

    def __call__(self, method, url, headers, body, timeout):
        _pc, client = helpers()
        key = self._request_key(method, url, headers, body)
        if self.record:
            response = client._send_urllib(method, url, headers, body, timeout)
            kept = {
                name: self._clean(value) for name, value in response.headers.items()
                if name in _KEEP_HEADERS or name.startswith(_KEEP_PREFIXES)
            }
            self.interactions.append({
                "request": key,
                "response": {"status": response.status_code, "headers": kept,
                             **_body_entry(response.content, response.headers.get("content-type"), self._clean)},
            })
            return response
        if self._next >= len(self.interactions):
            raise AssertionError(f"Cassette {self.path.name}: request {self._next + 1} ({method} {key['url']}) "
                                 f"was not recorded (it has {len(self.interactions)}).")
        recorded = self.interactions[self._next]
        self._next += 1
        want = recorded["request"]
        if (want["method"], _split(want["url"])) != (key["method"], _split(key["url"])) or not _same_body(
                {k: v for k, v in want.items() if k in ("json", "text", "base64")},
                {k: v for k, v in key.items() if k in ("json", "text", "base64")}):
            raise AssertionError(
                f"Cassette {self.path.name}: request {self._next} was {method} {key['url']} "
                f"{_body_repr(key)}, recorded {want['method']} {want['url']} {_body_repr(want)}."
            )
        answer = recorded["response"]
        headers = {name: self._restore(value) for name, value in (answer.get("headers") or {}).items()}
        return client.Response(answer["status"], client.Headers(headers.items()),
                               _body_bytes(answer, self._restore), url)

    def __exit__(self, exc_type, exc, tb):
        _pc, client = helpers()
        client.TRANSPORT = self._previous
        if exc_type is not None:
            return False
        if self.record:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "pyrunner_cassette": CASSETTE_FORMAT,
                "note": f"Recorded with {RECORD_ENV}=1; credentials scrubbed. Check it for personal data before "
                        "committing it.",
                "interactions": self.interactions,
            }
            self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        elif not self.allow_unused and self._next < len(self.interactions):
            left = self.interactions[self._next]["request"]
            raise AssertionError(
                f"Cassette {self.path.name}: {len(self.interactions) - self._next} recorded request(s) were not "
                f"made, starting with {left['method']} {left['url']}."
            )
        return False


def _split(url):
    parts = urllib.parse.urlsplit(url)
    return (parts.scheme, parts.netloc, parts.path, tuple(sorted(urllib.parse.parse_qsl(parts.query))))


def _body_repr(entry):
    for key in ("json", "text", "base64"):
        if key in entry:
            return f"with {key} {json.dumps(entry[key])[:200]}"
    return "with no body"


# --------------------------------------------------------------------------- #
# Test cases
# --------------------------------------------------------------------------- #


class IntegrationTestMixin:
    """The kit for any Django test case (``SimpleTestCase``, ``TestCase``...): see the module docstring."""

    # Where cassette(name) looks: <this dir>/<name>.json. Default: "cassettes"
    # beside the file of the test class.
    cassette_dir = None

    def setUp(self):
        super().setUp()
        self.pyrunner_connections, self.integration_client = helpers()
        self.connections = Connections()
        self.sleeps = []
        self.integration_client.reset()
        patches = self.connections.patch() + [
            mock.patch.object(self.integration_client, "_sleep", self.sleeps.append),
            mock.patch.object(self.integration_client, "TRANSPORT", _no_network),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.integration_client.reset)
        # Retries log a warning each (a run shows them in its stderr); tests would be noisy.
        logger = logging.getLogger("pyrunner_integrations")
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(logging.ERROR)

    def use_connection(self, name, provider="http", auth_kind=None, config=None, credentials=None,
                       base_url=None, rotate=True, error=None) -> FakeConnection:
        """Give the code under test a connection named ``name`` (made-up credentials unless given).

        ``error=("CONNECTION_EXPIRED", 409)`` makes asking for it fail as PyRun
        would. ``rotate=False`` keeps the credentials the same when the code
        reports a 401 (``credentials(name, refused=True)``). While recording
        cassettes, real credentials from the recording file replace made-up ones.
        """
        real = _real_credentials(name, provider) if recording() else None
        if real:
            connection = FakeConnection(
                name, provider, real.get("auth_kind"), real.get("config"), real.get("credentials") or {},
                rotate=False, real=True,
            )
        else:
            connection = FakeConnection(name, provider, auth_kind, config, credentials, base_url, rotate, error)
        return self.connections.add(connection)

    def fake_http(self, base_url=None) -> FakeTransport:
        """A fake service for the rest of the test (paths relative to the last connection's base URL)."""
        if base_url is None:
            bases = [c.base_url for c in self.connections.by_name.values() if c.base_url]
            base_url = bases[-1] if bases else None
        fake = FakeTransport(base_url)
        self.integration_client.TRANSPORT = fake
        return fake

    def cassette(self, name, *, scrub=(), allow_unused=False) -> Cassette:
        """A context manager replaying ``<cassette_dir>/<name>.json`` (recording it when asked)."""
        folder = Path(self.cassette_dir) if self.cassette_dir else Path(inspect.getfile(type(self))).parent / "cassettes"
        return Cassette(folder / f"{name}.json", self.connections, scrub=scrub, allow_unused=allow_unused)

    def integration(self, key):
        """The script-facing module of integration ``key`` (``pyrunner_integrations.<key>``)."""
        return framework.script_module(key)

    def assertNoSecrets(self, text):  # noqa: N802 - unittest style
        """Fail if ``text`` holds any credential value of the test's connections."""
        for value in self.connections.secrets():
            if len(value) >= 4 and value in str(text):
                self.fail(f"A credential value leaked: ...{value[-6:]} in {str(text)[:300]!r}")


class IntegrationTestCase(IntegrationTestMixin, SimpleTestCase):
    """A SimpleTestCase with the kit (no database). Use the mixin for a TestCase."""


# --------------------------------------------------------------------------- #
# Conformance
# --------------------------------------------------------------------------- #


def preset_problems(preset, provider=None) -> list:
    """What is wrong with a connection preset (an empty list when nothing is)."""
    problems = []
    if provider is not None and preset.key != provider:
        problems.append(f"preset key {preset.key!r} isn't {provider!r}")
    if not preset.label or not preset.description:
        problems.append("a preset needs a label and a description")
    if not preset.auth_kinds:
        problems.append("a preset needs at least one auth kind")
    for kind in preset.auth_kinds:
        if kind not in _AUTH_KINDS:
            problems.append(f"unknown auth kind {kind!r}")
        names = [f.name for f in preset.fields_for(kind)]
        if len(names) != len(set(names)):
            problems.append(f"{kind}: field names repeat")
        for field in preset.fields_for(kind):
            if field.kind in ("choice", "multichoice") and not field.choices:
                problems.append(f"{kind}.{field.name}: a {field.kind} field needs choices")
        if kind in (API_KEY, BEARER, BASIC) and not preset.secret_fields(kind):
            problems.append(f"{kind}: no secret field")
        if kind != SMTP and not preset.base_url and "base_url" not in names:
            problems.append(f"{kind}: no fixed base URL and no base_url field")
    if preset.base_url and not preset.base_url.startswith("https://"):
        problems.append(f"base URL {preset.base_url!r} isn't https")
    if preset.whoami is not None and not preset.whoami.path.startswith("/"):
        problems.append("whoami path must start with '/' (relative to the base URL)")
    if len(preset.api_key_header) != 2:
        problems.append("api_key_header is (header name, value prefix)")
    if OAUTH2 in preset.auth_kinds:
        oauth = preset.oauth
        if oauth is None:
            problems.append("an oauth2 preset needs oauth=OAuth(...)")
        else:
            for label, url in (("authorize", oauth.authorize_url), ("token", oauth.token_url)):
                if not url.startswith("https://"):
                    problems.append(f"{label} URL isn't https")
            keys = [s.key for s in oauth.scope_sets]
            if len(keys) != len(set(keys)):
                problems.append("scope set keys repeat")
            if set(oauth.default_scope_sets) - set(keys):
                problems.append("default scope sets name unknown sets")
            if oauth.token_format not in ("form", "json") or oauth.client_auth not in ("body", "basic"):
                problems.append("token_format is form|json and client_auth body|basic")
    return problems


def api_problems(integration, module) -> list:
    """What is wrong with an integration's script-facing module and its declared functions."""
    problems = []
    if getattr(module, "PROVIDER", None) != integration.provider:
        problems.append(f"{module.__name__}.PROVIDER isn't {integration.provider!r}")
    if not (module.__doc__ or "").strip():
        problems.append(f"{module.__name__} has no docstring (it opens the generated docs)")
    if not integration.functions:
        problems.append("the integration declares no functions")
    for name in integration.functions:
        fn = getattr(module, name, None)
        if not inspect.isfunction(fn):
            problems.append(f"declared function {name!r} isn't a function of {module.__name__}")
            continue
        if not (fn.__doc__ or "").strip():
            problems.append(f"{name}() has no docstring")
        param = inspect.signature(fn).parameters.get("connection")
        if param is None or param.default is not None:
            problems.append(f"{name}() needs a connection=None parameter")
    for name, fn in vars(module).items():
        if inspect.isfunction(fn) and fn.__module__ == module.__name__ and not name.startswith("_") \
                and name not in integration.functions:
            problems.append(f"{name}() is public but not in the integration's functions (declare it, or name it _{name})")
    return problems


class ConformanceSuite(IntegrationTestCase):
    """The checks every integration module passes: subclass it in the integration's tests/test_api.py.

        class HttpConformance(testing.ConformanceSuite):
            integration = "http"

            def call(self, module):
                return module.get("/conformance")

    ``call(module)`` makes one request through the module; ``ok_reply`` is what
    the fake service answers it with when all goes well. A module with a
    paginated function also sets ``pages`` (the fake replies, in order),
    ``page_items`` (everything they hold) and ``paginate_call(module, limit)``.
    """

    integration = None  # a key or an Integration
    ok_reply = {"json": {}}
    pages = None
    page_items = None

    @classmethod
    def setUpClass(cls):
        if cls.integration is None:
            raise unittest.SkipTest("ConformanceSuite runs in a subclass that names its integration.")
        super().setUpClass()

    def setUp(self):
        super().setUp()
        spec = self.integration
        self.spec = framework.load(spec) if isinstance(spec, str) else spec
        self.preset = load_preset(self.spec.provider)
        self.module = framework.script_module(self.spec)

    # -- what a subclass provides --------------------------------------------

    def call(self, module):
        raise NotImplementedError("A ConformanceSuite subclass defines call(module): one request through it.")

    def paginate_call(self, module, limit=None):
        raise NotImplementedError("Set pages and page_items, and define paginate_call(module, limit).")

    # -- helpers --------------------------------------------------------------

    def _http_kinds(self):
        kinds = [k for k in self.preset.auth_kinds if k != SMTP]
        if not kinds:
            self.skipTest("not an HTTP integration")
        return kinds

    def _connect(self, auth_kind=None, **kwargs):
        self.connections.by_name.clear()
        return self.use_connection(self.spec.provider, provider=self.spec.provider,
                                   auth_kind=auth_kind or self._http_kinds()[0], **kwargs)

    def _fake(self, *replies):
        fake = self.fake_http()
        for reply in replies:
            fake.add(None, None, **reply)
        return fake

    def _ok(self):
        return dict(self.ok_reply)

    def _raises(self, code):
        with self.assertRaises(self.integration_client.IntegrationError) as ctx:
            self.call(self.module)
        self.assertEqual(ctx.exception.code, code, str(ctx.exception))
        return ctx.exception

    def _assert_auth(self, request, material):
        for name, value in material["headers"].items():
            self.assertEqual(request.headers.get(name), value, f"header {name} not sent to the base URL")
        for name, value in material["params"].items():
            self.assertEqual(request.params.get(name), value, f"query parameter {name} not sent")

    # -- the checks -----------------------------------------------------------

    def test_conformance_preset_is_valid(self):
        self.assertEqual(preset_problems(self.preset, self.spec.provider), [])

    def test_conformance_public_api(self):
        self.assertEqual(api_problems(self.spec, self.module), [])

    def test_conformance_credentials_go_to_the_base_url_only(self):
        for kind in self._http_kinds():
            with self.subTest(auth_kind=kind):
                connection = self._connect(kind)
                material = connection.material()
                fake = self._fake({"status": 302, "headers": {"Location": "https://elsewhere.invalid/landing"}},
                                  self._ok())
                self.call(self.module)
                self.assertEqual(len(fake.requests), 2)
                self._assert_auth(fake.requests[0], material)
                hop = fake.requests[1]
                self.assertTrue(hop.url.startswith("https://elsewhere.invalid/"))
                for name in material["headers"]:
                    self.assertNotIn(name, hop.headers, f"header {name} went to another host")
                for value in material["params"].values():
                    self.assertNotIn(value, hop.url)

    def test_conformance_a_401_gets_new_credentials_and_one_retry(self):
        connection = self._connect()
        fake = self._fake({"status": 401}, self._ok())
        self.call(self.module)
        self.assertEqual(len(fake.requests), 2)
        self._assert_auth(fake.requests[1], connection.material())
        self.assertEqual([refused for _name, refused in self.connections.calls].count(True), 1)

    def test_conformance_a_second_401_is_unauthorized(self):
        self._connect()
        fake = self._fake({"status": 401}, {"status": 401})
        error = self._raises("UNAUTHORIZED")
        self.assertEqual((error.status, len(fake.requests)), (401, 2))

    def test_conformance_a_401_with_nothing_new_is_not_retried(self):
        self._connect(rotate=False)
        fake = self._fake({"status": 401})
        self._raises("UNAUTHORIZED")
        self.assertEqual(len(fake.requests), 1)

    def test_conformance_a_429_waits_for_retry_after(self):
        self._connect()
        self._fake({"status": 429, "headers": {"Retry-After": "7"}}, self._ok())
        self.call(self.module)
        self.assertEqual(self.sleeps, [7.0])

    def test_conformance_429s_back_off_then_rate_limited(self):
        self._connect()
        self._fake(*[{"status": 429}] * (self.integration_client.MAX_RETRIES + 1))
        error = self._raises("RATE_LIMITED")
        self.assertEqual(self.sleeps, [1.0, 2.0, 4.0][: self.integration_client.MAX_RETRIES])
        self.assertIsNone(error.retry_after)

    def test_conformance_a_long_retry_after_is_not_waited_for(self):
        self._connect()
        self._fake({"status": 429, "headers": {"Retry-After": "3600"}})
        error = self._raises("RATE_LIMITED")
        self.assertEqual((error.retry_after, self.sleeps), (3600.0, []))

    def test_conformance_a_503_with_retry_after_is_retried(self):
        self._connect()
        self._fake({"status": 503, "headers": {"Retry-After": "2"}}, self._ok())
        self.call(self.module)
        self.assertEqual(self.sleeps, [2.0])

    def test_conformance_errors_map_to_codes(self):
        cases = {400: "INVALID_REQUEST", 403: "FORBIDDEN", 404: "NOT_FOUND", 409: "CONFLICT",
                 422: "INVALID_REQUEST", 418: "HTTP_ERROR", 500: "SERVER_ERROR"}
        for status, code in cases.items():
            with self.subTest(status=status):
                self._connect()
                self.fake_http().default(status=status, json=echo)
                error = self._raises(code)
                self.assertEqual(error.status, status)
                self.assertIn("boom", str(error))
                self.assertNoSecrets(str(error))
        for failure in ("UNREACHABLE", "TIMEOUT"):
            with self.subTest(failure=failure):
                self._connect()
                self.fake_http().default(error=failure)
                error = self._raises(failure)
                self.assertIsNone(error.status)
        with self.subTest(connection="missing"):
            self.connections.by_name.clear()
            error = self._raises("CONNECTION_NOT_FOUND")
            self.assertIsInstance(error, self.pyrunner_connections.ConnectionsError)

    def test_conformance_no_credentials_in_reprs_or_logs(self):
        self._connect()
        fake = self.fake_http()
        fake.add(None, None, status=429, headers={"Retry-After": "0"}, json=echo)
        fake.default(status=401, json=echo)
        with self.assertLogs("pyrunner_integrations", "DEBUG") as logs:
            error = self._raises("UNAUTHORIZED")
        self.assertEqual(len(fake.requests), 3)
        client = self.integration_client.shared(None, self.spec.provider)
        for text in [*logs.output, str(error), repr(error), repr(error.response), repr(client),
                     *(repr(r) for r in fake.requests)]:
            self.assertNoSecrets(text)
        if "client" in self.spec.functions:
            self.assertNoSecrets(repr(self.module.client()))

    def test_conformance_pagination(self):
        if self.pages is None:
            self.skipTest("no paginated function")
        self._connect()
        fake = self._fake(*self.pages)
        self.assertEqual(list(self.paginate_call(self.module)), list(self.page_items))
        self.assertEqual(len(fake.requests), len(self.pages), "one request per page, none after the last")
        self.assertNotEqual((fake.requests[0].url, fake.requests[0].body),
                            (fake.requests[1].url, fake.requests[1].body), "page 2 asked for page 1 again")
        fake = self._fake(*self.pages)
        self.assertEqual(list(self.paginate_call(self.module, limit=1)), list(self.page_items)[:1])
        self.assertEqual(len(fake.requests), 1, "limit=1 stops before the next page")
