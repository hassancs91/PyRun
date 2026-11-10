"""
Tests for the integration framework and testing kit (2.5 Stage 6, T26).

The http integration's own tests (conformance + cassettes) are in
core/integrations/http/tests/test_api.py; these cover what they rest on:

- the shared client over the real transports (urllib, and requests when
  installed) against in-process servers: credentials only to the base URL,
  redirects, timeouts, unreachable hosts;
- retry rules the conformance suite leaves open (POST and 5xx, Retry-After as
  a date), decoding, the check hook, pagination guards;
- a 401's "refused" digest: the helper sends it and PyRun refreshes an OAuth
  token that is still the refused one (both engines), at most once a minute;
- a real script run using ``pyrunner_integrations.http`` through the loopback;
- the kit itself: the fake service, the no-network guard, cassettes recorded
  and replayed, and the conformance suite failing a module that breaks the rules;
- ``new_integration`` (its output passes conformance untouched),
  ``integration_smoke`` and ``integration_docs`` (the committed page is current
  and help.search finds it).
"""

import hashlib
import http.server
import io
import json
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.management import CommandError, call_command
from django.test import LiveServerTestCase, SimpleTestCase, TestCase, override_settings

from core.executor import execute_run
from core.integrations import docs, framework, testing
from core.integrations.presets import PRESET_MODULES
from core.models import Connection, ConnectionGrant, Run
from core.services import connections as svc
from core.services import help_docs
from core.test_connections import _ai_off, _Fixture
from core.test_connections_oauth import REFRESH, _ProviderMixin

TOKEN = "t26-bearer-token-0001"
KEY = "t26-api-key-0002"


def _digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


# --------------------------------------------------------------------------- #
# In-process API servers
# --------------------------------------------------------------------------- #


class _Api(http.server.BaseHTTPRequestHandler):
    def _reply(self, status, body=None, headers=()):
        raw = json.dumps(body).encode() if body is not None else b""
        self.send_response(status)
        if raw:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(raw)
        except ConnectionError:
            pass  # the client gave up (the timeout test)

    def _handle(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.seen.append((self.command, self.path, dict(self.headers), raw))
        path = self.path.split("?")[0]
        if path == "/away":
            return self._reply(302, headers=[("Location", self.server.other + "/landed")])
        if path == "/see-other":
            return self._reply(303, headers=[("Location", "/v1/me")])
        if path == "/slow":
            time.sleep(1.0)
            return self._reply(200, {})
        if path == "/v1/me":
            if self.headers.get("Authorization") not in (f"Bearer {TOKEN}", None) and "key=" not in self.path:
                return self._reply(401, {"error": {"message": f"bad {self.headers.get('Authorization')}"}})
            return self._reply(200, {"id": 7, "name": "Ada"})
        return self._reply(200, {"path": self.path})

    do_GET = do_POST = do_PUT = do_DELETE = _handle

    def log_message(self, *args):
        pass


def _serve():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Api)
    server.seen, server.other = [], ""
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class _Servers:
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.api, cls.elsewhere = _serve(), _serve()
        cls.base = f"http://127.0.0.1:{cls.api.server_port}"
        cls.api.other = f"http://127.0.0.1:{cls.elsewhere.server_port}"

    @classmethod
    def tearDownClass(cls):
        for server in (cls.api, cls.elsewhere):
            server.shutdown()
            server.server_close()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.api.seen.clear()
        self.elsewhere.seen.clear()


def _no_requests():
    """Hide requests, so the client takes the standard-library path."""
    return mock.patch.dict(sys.modules, {"requests": None})


# --------------------------------------------------------------------------- #
# The shared client over real sockets
# --------------------------------------------------------------------------- #


class RealTransportTests(_Servers, testing.IntegrationTestMixin, SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.integration_client.TRANSPORT = None  # the real network, to 127.0.0.1 only
        self.http = self.integration("http")

    def _both(self):
        yield "urllib", _no_requests()
        try:
            import requests  # noqa: F401
        except ImportError:
            return
        yield "requests", mock.patch.dict(sys.modules, {})

    def test_credentials_query_and_body_reach_the_base_url(self):
        self.use_connection("api", auth_kind="api_key", config={"base_url": self.base, "location": "query",
                                                                "param_name": "key"},
                            credentials={"api_key": KEY})
        for label, patch in self._both():
            with self.subTest(transport=label), patch:
                self.api.seen.clear()
                self.integration_client.reset()
                out = self.http.post("/v1/items", json={"name": "Ada"}, params={"q": "x y"})
                method, path, headers, raw = self.api.seen[0]
                self.assertEqual((method, json.loads(raw)), ("POST", {"name": "Ada"}))
                self.assertIn(f"key={KEY}", path)
                self.assertIn("q=x+y", path)
                self.assertEqual(headers["Content-Type"], "application/json")
                self.assertTrue(headers["User-Agent"].startswith("pyrunner-integrations/"))
                self.assertEqual(out["path"], path)

    def test_a_redirect_off_the_base_url_goes_on_without_credentials(self):
        self.use_connection("api", auth_kind="bearer", config={"base_url": self.base}, credentials={"token": TOKEN})
        for label, patch in self._both():
            with self.subTest(transport=label), patch:
                self.elsewhere.seen.clear()
                self.integration_client.reset()
                self.assertEqual(self.http.get("/away", headers={"X-Trace": "1"}), {"path": "/landed"})
                _method, _path, headers, _raw = self.elsewhere.seen[0]
                self.assertNotIn("Authorization", headers)
                self.assertEqual(headers.get("X-Trace"), "1")

    def test_a_303_after_a_post_is_a_get_without_the_body(self):
        self.use_connection("api", auth_kind="bearer", config={"base_url": self.base}, credentials={"token": TOKEN})
        with _no_requests():
            self.assertEqual(self.http.post("/see-other", json={"a": 1}), {"id": 7, "name": "Ada"})
        method, _path, headers, raw = self.api.seen[1]
        self.assertEqual((method, raw), ("GET", b""))
        self.assertEqual(headers["Authorization"], f"Bearer {TOKEN}")

    def test_timeouts_and_unreachable_hosts(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            closed = s.getsockname()[1]
        for label, patch in self._both():
            with self.subTest(transport=label), patch:
                self.connections.by_name.clear()
                self.use_connection("api", auth_kind="bearer", config={"base_url": self.base},
                                    credentials={"token": TOKEN})
                self.integration_client.reset()
                with self.assertRaises(self.integration_client.IntegrationError) as ctx:
                    self.http.request("POST", "/slow", timeout=0.2)
                self.assertEqual(ctx.exception.code, "TIMEOUT")
                self.connections.by_name.clear()
                self.use_connection("api", auth_kind="bearer", config={"base_url": f"http://127.0.0.1:{closed}"},
                                    credentials={"token": TOKEN})
                self.integration_client.reset()
                with self.assertRaises(self.integration_client.IntegrationError) as ctx:
                    self.http.post("/v1/me")
                self.assertEqual(ctx.exception.code, "UNREACHABLE")
                self.assertNotIn(TOKEN, str(ctx.exception))


# --------------------------------------------------------------------------- #
# Retry rules, decoding, pagination guards (on the fake service)
# --------------------------------------------------------------------------- #


class ClientRuleTests(testing.IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.c = self.integration_client
        self.use_connection("api", auth_kind="bearer", config={"base_url": "https://api.example.test"},
                            credentials={"token": TOKEN})
        self.fake = self.fake_http()

    def _client(self, **kwargs):
        return self.c.Client("api", **kwargs)

    def test_a_post_is_not_repeated_after_a_5xx_unless_marked_idempotent(self):
        self.fake.default(status=500)
        with self.assertRaises(self.c.IntegrationError) as ctx:
            self._client().post("/x", json={})
        self.assertEqual((ctx.exception.code, len(self.fake.requests)), ("SERVER_ERROR", 1))
        self.fake.requests.clear()
        with self.assertRaises(self.c.IntegrationError):
            self._client().post("/search", json={}, idempotent=True)
        self.assertEqual(len(self.fake.requests), 4)

    def test_a_429_is_retried_for_a_post_too(self):
        self.fake.add("POST", "/x", status=429, headers={"Retry-After": "1"}).add("POST", "/x", json={"ok": 1})
        self.assertEqual(self._client().post("/x", json={}), {"ok": 1})
        self.assertEqual(self.sleeps, [1.0])

    def test_retry_after_as_an_http_date(self):
        when = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + 30))
        self.fake.add(None, "/x", status=429, headers={"Retry-After": when}).add(None, "/x", json={})
        self._client().get("/x")
        self.assertTrue(25 <= self.sleeps[0] <= 31, self.sleeps)

    def test_broken_json_is_a_bad_response(self):
        self.fake.default(body="{nope", headers={"Content-Type": "application/json"})
        with self.assertRaises(self.c.IntegrationError) as ctx:
            self._client().get("/x")
        self.assertEqual(ctx.exception.code, "BAD_RESPONSE")

    def test_a_text_reply_is_decoded_with_its_charset(self):
        self.fake.add(None, "/latin", body="café".encode("latin-1"),
                      headers={"Content-Type": "text/plain; charset=ISO-8859-1"})
        self.fake.add(None, "/odd", body="café".encode(), headers={"Content-Type": "text/plain; charset=nope"})
        self.assertEqual(self._client().get("/latin"), "café")
        self.assertEqual(self._client().get("/odd"), "café")

    def test_the_check_hook_turns_a_2xx_into_an_error(self):
        def slack_style(response, body):
            if isinstance(body, dict) and body.get("ok") is False:
                raise self.c.IntegrationError(f"Slack said {body['error']}", "INVALID_REQUEST", response.status_code)

        self.fake.default(json={"ok": False, "error": "channel_not_found"})
        with self.assertRaises(self.c.IntegrationError) as ctx:
            self._client(check=slack_style).post("/chat.postMessage", json={})
        self.assertEqual((ctx.exception.code, ctx.exception.status), ("INVALID_REQUEST", 200))

    def test_too_many_redirects(self):
        self.fake.default(status=302, headers={"Location": "/again"})
        with self.assertRaises(self.c.IntegrationError) as ctx:
            self._client().get("/x")
        self.assertEqual(ctx.exception.code, "BAD_RESPONSE")
        self.assertEqual(len(self.fake.requests), self.c.MAX_REDIRECTS + 1)

    def test_credentials_are_read_before_every_request(self):
        self.fake.default(json={})
        client = self._client()
        client.get("/a")
        self.connections.by_name["api"].rotate_credentials()  # PyRun refreshed it meanwhile
        client.get("/b")
        self.assertEqual(self.fake.requests[1].headers["authorization"], f"Bearer {TOKEN}-rotated1")

    def test_an_smtp_connection_is_not_http(self):
        self.use_connection("mail", provider="smtp")
        with self.assertRaises(self.c.IntegrationError) as ctx:
            self.c.Client("mail").get("/x")
        self.assertEqual(ctx.exception.code, "NOT_HTTP")

    def test_paginate_guards(self):
        self.fake.default(json={"items": [1], "next": "same"})
        with self.assertRaises(self.c.IntegrationError) as ctx:
            list(self._client().paginate("/x", cursor="next"))
        self.assertIn("same page cursor twice", str(ctx.exception))
        self.assertEqual(list(self._client().paginate("/x", cursor="next", limit=0)), [])
        counter = iter(range(10))
        self.fake.default(json=lambda request: {"items": [1], "next": f"c{next(counter)}"})
        with self.assertRaises(self.c.IntegrationError) as ctx:
            list(self._client().paginate("/x", cursor="next", max_pages=3))
        self.assertIn("More than 3 pages", str(ctx.exception))
        for wrong in ({}, {"cursor": "next", "link": True}):
            with self.assertRaises(ValueError):
                self._client().paginate("/x", **wrong)
        self.fake.default(json={"rows": []})
        with self.assertRaises(self.c.IntegrationError) as ctx:
            list(self._client().paginate("/x", items="records", cursor="offset"))
        self.assertEqual(ctx.exception.code, "BAD_RESPONSE")

    def test_reply_headers_ignore_case_and_join_repeats(self):
        headers = self.c.Headers([("Link", "<a>; rel=\"next\""), ("link", "<b>; rel=\"last\""), ("X-A", "1")])
        self.assertEqual(headers["LINK"], '<a>; rel="next", <b>; rel="last"')
        self.assertIn("x-a", headers)
        self.assertEqual(self.c.next_link(headers.get("link")), "a")


# --------------------------------------------------------------------------- #
# A refused token: the helper's digest and PyRun's early refresh
# --------------------------------------------------------------------------- #


class HelperRefusedTests(SimpleTestCase):
    def test_refused_sends_the_cached_tokens_digest_and_replaces_the_cache(self):
        from core.script_helpers import pyrunner_connections as pc

        pc._cache.clear()
        self.addCleanup(pc._cache.clear)
        answers = iter([{"ok": True, "token": "old-token-1", "expires_in": 3600},
                        {"ok": True, "token": "new-token-2", "expires_in": 3600}])
        with mock.patch.object(pc, "_post", side_effect=lambda endpoint, payload: next(answers)) as post:
            self.assertEqual(pc.credentials("google")["token"], "old-token-1")
            self.assertEqual(pc.credentials("google")["token"], "old-token-1")  # cached
            self.assertEqual(pc.credentials("google", refused=True)["token"], "new-token-2")
            self.assertEqual(pc.credentials("google")["token"], "new-token-2")
        self.assertEqual([c.args[1] for c in post.call_args_list],
                         [{"connection": "google"}, {"connection": "google", "refused": _digest("old-token-1")}])


class RefusedRefreshTests(_ProviderMixin, _Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.app = self._app()
        self.script = self._script()
        self.run = self._run(self.script)

    def _issue(self, c, refused=None):
        return svc.issue(self.run, Connection.objects.get(pk=c.pk), refused=refused)

    def test_a_refused_token_still_current_is_refreshed_early(self):
        c = self._oauth(app=self.app)  # good for an hour
        self.assertEqual(self._issue(c)["token"], "at-t24-old-0000")
        material = self._issue(c, refused=_digest("at-t24-old-0000"))
        self.assertEqual(self.st.refresh_calls, 1)
        self.assertNotEqual(material["token"], "at-t24-old-0000")
        self.assertIn(material["token"], svc.issued_masks(self.run.pk).values())
        c.refresh_from_db()
        self.assertEqual(c.get_credentials()["refresh_token"], REFRESH)

    def test_a_stale_digest_is_not_refreshed(self):
        c = self._oauth(app=self.app)
        self.assertEqual(self._issue(c, refused=_digest("an-older-token"))["token"], "at-t24-old-0000")
        self.assertEqual(self.st.refresh_calls, 0)

    def test_at_most_one_early_refresh_a_minute(self):
        c = self._oauth(app=self.app)
        first = self._issue(c, refused=_digest("at-t24-old-0000"))["token"]
        again = self._issue(c, refused=_digest(first))["token"]
        self.assertEqual((self.st.refresh_calls, again), (1, first))

    def test_no_refresh_token_means_nothing_to_refresh(self):
        c = self._oauth(app=self.app, refresh_token=None)
        self.assertEqual(self._issue(c, refused=_digest("at-t24-old-0000"))["token"], "at-t24-old-0000")
        self.assertEqual(self.st.refresh_calls, 0)

    def test_a_refused_refresh_marks_the_connection_expired(self):
        self.st.fail = "refuse"
        c = self._oauth(app=self.app)
        with mock.patch("core.services.notification_service.NotificationService.send_email"), \
                self.assertRaises(svc.ConnectionLookupError) as ctx:
            self._issue(c, refused=_digest("at-t24-old-0000"))
        self.assertEqual(ctx.exception.code, "CONNECTION_EXPIRED")
        c.refresh_from_db()
        self.assertEqual(c.status, Connection.Status.EXPIRED)

    def test_the_loopback_takes_the_digest(self):
        from core.services.datastore_token import mint_datastore_token

        c = self._oauth(app=self.app)
        ConnectionGrant.objects.create(script=self.script, connection=c)
        auth = {"HTTP_AUTHORIZATION": f"Bearer {mint_datastore_token(self.run.id)}"}
        url = "/internal/connections/token"
        bad = self.client.post(url, {"connection": c.name, "refused": "nope"}, content_type="application/json",
                               **auth)
        self.assertEqual((bad.status_code, bad.json()["error"]["code"]), (400, "BAD_REQUEST"))
        ok = self.client.post(url, {"connection": c.name, "refused": _digest("at-t24-old-0000")},
                              content_type="application/json", **auth)
        self.assertEqual(ok.status_code, 200, ok.content)
        self.assertNotEqual(ok.json()["token"], "at-t24-old-0000")
        self.assertEqual(self.st.refresh_calls, 1)

    def test_expiry_driven_refresh_is_unchanged(self):
        c = self._oauth(app=self.app, expires_in=10)
        self._issue(c)
        self.assertEqual(self.st.refresh_calls, 1)


# --------------------------------------------------------------------------- #
# A real run through the loopback
# --------------------------------------------------------------------------- #


class EndToEndRunTests(_Servers, _Fixture, LiveServerTestCase):
    host = "127.0.0.1"  # the internal API answers loopback only

    CODE = (
        "import pyrunner_connections, pyrunner_run\n"
        "from pyrunner_integrations import IntegrationError, http\n"
        "me = http.get('/v1/me')\n"
        "print('name=' + me['name'])\n"
        "print('token=' + pyrunner_connections.token('http'))\n"
        "try:\n"
        "    http.get('/v1/me', headers={'Authorization': 'Bearer wrong'})\n"
        "except IntegrationError as e:\n"
        "    print('error=' + e.code + ' ' + str(e))\n"
        "pyrunner_run.set_result({'name': me['name']})\n"
    )

    def test_a_script_calls_an_api_through_its_connection(self):
        script = self._script(code=self.CODE)
        c = self._connection(name="api", provider="http", auth_kind="bearer", credentials={"token": TOKEN},
                             config={"base_url": self.base, "test_path": ""})
        ConnectionGrant.objects.create(script=script, connection=c)
        run = self._run(script, status=Run.Status.PENDING)
        with override_settings(PYRUNNER_INTERNAL_BASE_URL=self.live_server_url), _ai_off(), \
                mock.patch("core.executor.resolve_secrets_for_run", return_value={}), \
                mock.patch("core.executor._validate_environment", return_value=sys.executable):
            execute_run(run)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.SUCCESS, run.stderr)
        self.assertIn("name=Ada", run.stdout)
        self.assertIn("token=[connection:api.token:MASKED]", run.stdout)
        self.assertIn("error=UNAUTHORIZED", run.stdout)
        self.assertNotIn(TOKEN, run.stdout + run.stderr)
        self.assertEqual(run.result, {"name": "Ada"})
        self.assertEqual(self.api.seen[0][2]["Authorization"], f"Bearer {TOKEN}")


# --------------------------------------------------------------------------- #
# The kit
# --------------------------------------------------------------------------- #


class KitTests(_Servers, testing.IntegrationTestMixin, SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.http = self.integration("http")
        self.tmp = Path(tempfile.mkdtemp())
        self.cassette_dir = self.tmp

    def test_nothing_reaches_the_network_by_default(self):
        self.use_connection("api", config={"base_url": self.base})
        with self.assertRaisesRegex(AssertionError, "made a real request"):
            self.http.get("/v1/me")
        self.assertEqual(self.api.seen, [])

    def test_the_fake_service_matches_paths_queries_and_full_urls(self):
        self.use_connection("api", auth_kind="bearer", config={"base_url": "https://api.example.test/v2"})
        fake = self.fake_http()
        fake.add("GET", "/items?page=2", json={"p": 2}).add("GET", "/items", json={"p": 1}, times=2)
        fake.add(None, "https://cdn.example.test/file", body="data")
        self.assertEqual(self.http.get("/items", params={"page": 2}), {"p": 2})
        self.assertEqual(self.http.get("/items"), {"p": 1})
        self.assertEqual(self.http.get("/items", params={"page": 9}), {"p": 1})
        self.assertEqual(self.http.get("https://cdn.example.test/file"), "data")
        self.assertEqual(fake.requests[0].path, "/v2/items")
        self.assertEqual(fake.requests[0].params, {"page": "2"})
        self.assertNotIn("authorization", fake.requests[3].headers)
        with self.assertRaisesRegex(AssertionError, "no fake reply matches"):
            self.http.get("/items")

    def test_a_connection_error_and_the_other_helper_functions(self):
        pc = self.pyrunner_connections
        self.use_connection("old", provider="google", error=("CONNECTION_EXPIRED", 409))
        self.use_connection("api", auth_kind="bearer", config={"base_url": "https://api.example.test"},
                            credentials={"token": TOKEN})
        with self.assertRaises(pc.ConnectionsError) as ctx:
            pc.token("old")
        self.assertEqual((ctx.exception.code, ctx.exception.status), ("CONNECTION_EXPIRED", 409))
        self.assertEqual(pc.token("http"), TOKEN)  # by provider: the one http connection
        self.assertEqual(pc.info("api")["base_url"], "https://api.example.test")
        self.assertEqual([c["name"] for c in pc.list()], ["api"])
        with self.assertRaises(pc.ConnectionsError) as ctx:
            pc.token("missing")
        self.assertEqual(ctx.exception.code, "CONNECTION_NOT_FOUND")

    def _record(self, name, fn):
        creds = self.tmp / "creds.json"
        creds.write_text(json.dumps({"api": {"auth_kind": "api_key", "config": {
            "base_url": self.base, "location": "query", "param_name": "key"}, "credentials": {"api_key": KEY}}}))
        env = {testing.RECORD_ENV: "1", testing.CREDENTIALS_ENV: str(creds)}
        with mock.patch.dict("os.environ", env):
            self.use_connection("api", provider="http")
            self.integration_client.TRANSPORT = None
            with self.cassette(name):
                fn()
        self.connections.by_name.clear()
        self.integration_client.TRANSPORT = testing._no_network

    def test_a_cassette_is_recorded_scrubbed_then_replayed_offline(self):
        self._record("me", lambda: self.http.get("/v1/me"))
        text = (self.tmp / "me.json").read_text()
        self.assertNotIn(KEY, text)
        self.assertNotIn(str(self.api.server_port), text)
        self.assertIn("/v1/me?key=[MASKED]", text)
        seen = len(self.api.seen)
        self.use_connection("api", auth_kind="api_key", config={"base_url": "https://api.example.test",
                                                                "location": "query", "param_name": "key"})
        with self.cassette("me"):
            self.assertEqual(self.http.get("/v1/me"), {"id": 7, "name": "Ada"})
        self.assertEqual(len(self.api.seen), seen)  # replay made no request

    def test_replay_refuses_a_different_request_and_unused_ones(self):
        self._record("two", lambda: (self.http.get("/v1/me"), self.http.get("/v1/other")))
        self.use_connection("api", auth_kind="api_key", config={"base_url": "https://api.example.test",
                                                                "location": "query", "param_name": "key"})
        with self.assertRaisesRegex(AssertionError, "request 1 was GET /v1/nope"):
            with self.cassette("two"):
                self.http.get("/v1/nope")
        with self.assertRaisesRegex(AssertionError, "1 recorded request"):
            with self.cassette("two"):
                self.http.get("/v1/me")
        with self.cassette("two", allow_unused=True):
            self.http.get("/v1/me")

    def test_a_missing_cassette_and_recording_without_real_credentials(self):
        self.use_connection("api", config={"base_url": self.base})
        with self.assertRaisesRegex(AssertionError, testing.RECORD_ENV):
            with self.cassette("absent"):
                pass
        with mock.patch.dict("os.environ", {testing.RECORD_ENV: "1"}):
            with self.assertRaisesRegex(AssertionError, "needs real credentials for api"):
                with self.cassette("absent"):
                    pass


def _bad_module():
    """An integration module that breaks the rules: no retries, the token in its errors."""
    module = types.ModuleType("t26_bad_integration")
    module.__doc__ = "A bad one."
    module.PROVIDER = "http"
    _pc, client = testing.helpers()

    def get(path, *, connection=None):
        """GET without retries, leaking the token."""
        try:
            return client.Client(connection, provider="http", max_retries=0).get(path)
        except client.IntegrationError as e:
            token = _pc.credentials(connection or "http").get("token") or ""
            raise client.IntegrationError(f"{e} (sent {token})", e.code, e.status, e.retry_after) from None

    get.__module__ = module.__name__
    module.get = get
    return module


class ConformanceCatchesTests(SimpleTestCase):
    def test_the_suite_fails_a_module_that_breaks_the_rules(self):
        bad = _bad_module()
        sys.modules[bad.__name__] = bad
        self.addCleanup(sys.modules.pop, bad.__name__, None)

        class BadConformance(testing.ConformanceSuite):
            integration = framework.Integration(key="bad", provider="http", title="Bad", functions=("get",),
                                                module=bad.__name__)

            def call(self, module):
                return module.get("/x")

        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(BadConformance).run(result)
        failed = {test.id().rsplit(".", 1)[-1].split(" (")[0] for test, _tb in result.failures + result.errors}
        self.assertTrue({
            "test_conformance_a_429_waits_for_retry_after",
            "test_conformance_429s_back_off_then_rate_limited",
            "test_conformance_a_503_with_retry_after_is_retried",
            "test_conformance_errors_map_to_codes",
            "test_conformance_no_credentials_in_reprs_or_logs",
        } <= failed, failed)
        self.assertNotIn("test_conformance_preset_is_valid", failed)

    def test_api_and_preset_problems_are_named(self):
        from core.integrations.presets import Preset

        bad = _bad_module()
        bad.helper = lambda: None
        bad.helper.__module__ = bad.__name__
        integration = framework.Integration(key="bad", provider="smtp", title="Bad", functions=("get", "nope"))
        problems = testing.api_problems(integration, bad)
        self.assertIn("t26_bad_integration.PROVIDER isn't 'smtp'", problems)
        self.assertIn("declared function 'nope' isn't a function of t26_bad_integration", problems)
        self.assertTrue(any("<lambda>" in p or "helper" in p for p in problems), problems)
        preset = Preset(key="x", label="X", description="", fields={"bearer": ()}, base_url="http://x.test")
        self.assertEqual(testing.preset_problems(preset, "x"), [
            "a preset needs a label and a description", "bearer: no secret field", "base URL 'http://x.test' isn't https",
        ])


# --------------------------------------------------------------------------- #
# Registry, docs, scaffold, smoke
# --------------------------------------------------------------------------- #


class RegistryAndDocsTests(SimpleTestCase):
    def test_every_integration_loads_and_its_preset_is_shipped(self):
        for integration in framework.integrations():
            with self.subTest(integration.key):
                self.assertIn(integration.provider, PRESET_MODULES)
                self.assertEqual(testing.api_problems(integration, framework.script_module(integration)), [])
        with self.assertRaises(LookupError):
            framework.load("nope")
        with self.assertRaises(ValueError):
            framework.load("Bad-Key")

    def test_the_committed_pages_are_current(self):
        for integration in framework.integrations():
            with self.subTest(integration.key):
                page = docs.path_for(integration).read_text(encoding="utf-8")
                self.assertEqual(page, docs.render(integration), "run manage.py integration_docs")
        call_command("integration_docs", "--check", stdout=io.StringIO())

    def test_help_search_finds_the_generated_page(self):
        index = help_docs.build_index(Path(settings.BASE_DIR))
        results = help_docs.search("retry after a 429 rate limited integration", index=index)["results"]
        self.assertIn("docs/integrations/http.md", [r["doc"] for r in results[:3]], results[:3])


class ScaffoldTests(SimpleTestCase):
    def _scaffold(self, key, *extra):
        root = Path(tempfile.mkdtemp())
        out = io.StringIO()
        call_command("new_integration", key, "--root", str(root), *extra, stdout=out)
        return root, out.getvalue()

    def _run_generated(self, root, key):
        import core.integrations

        testing.helpers()
        import pyrunner_integrations

        paths = [(core.integrations, root / "core" / "integrations"),
                 (pyrunner_integrations, root / "core" / "script_helpers" / "pyrunner_integrations")]
        for package, path in paths:
            package.__path__.append(str(path))

        def undo():
            for package, path in paths:
                package.__path__.remove(str(path))
            for name in [n for n in sys.modules if n.startswith((f"core.integrations.{key}",
                                                                 f"pyrunner_integrations.{key}"))]:
                del sys.modules[name]

        self.addCleanup(undo)
        suite = unittest.defaultTestLoader.loadTestsFromName(f"core.integrations.{key}.tests.test_api")
        result = unittest.TestResult()
        suite.run(result)
        return result

    def test_a_new_integration_passes_conformance_untouched(self):
        root, out = self._scaffold("acme_crm")
        for path in ("core/integrations/acme_crm/preset.py", "core/integrations/acme_crm/api.py",
                     "core/integrations/acme_crm/tests/test_api.py",
                     "core/integrations/acme_crm/tests/cassettes/list_items.json",
                     "core/script_helpers/pyrunner_integrations/acme_crm.py", "docs/integrations/acme_crm.md"):
            self.assertTrue((root / path).is_file(), path)
        self.assertIn("INTEGRATION_MODULES", out)
        result = self._run_generated(root, "acme_crm")
        self.assertEqual(result.failures + result.errors, [])
        self.assertEqual((result.testsRun, len(result.skipped)), (14, 0))

    def test_on_an_existing_preset(self):
        root, _out = self._scaffold("tasks", "--provider", "http")
        self.assertFalse((root / "core/integrations/tasks/preset.py").exists())
        result = self._run_generated(root, "tasks")
        self.assertEqual(result.failures + result.errors, [])

    def test_existing_files_are_kept_and_bad_keys_refused(self):
        root, _out = self._scaffold("acme")
        module = root / "core/script_helpers/pyrunner_integrations/acme.py"
        module.write_text("# mine\n")
        out = io.StringIO()
        call_command("new_integration", "acme", "--root", str(root), stdout=out)
        self.assertIn("Kept (exists): core/script_helpers/pyrunner_integrations/acme.py", out.getvalue())
        self.assertEqual(module.read_text(), "# mine\n")
        for bad in ("Acme", "class", "a", "_x", "acme-crm"):
            with self.subTest(bad), self.assertRaises(CommandError):
                call_command("new_integration", bad, "--root", str(root), stdout=io.StringIO())
        with self.assertRaises(CommandError):
            call_command("new_integration", "x2", "--provider", "nope", "--root", str(root), stdout=io.StringIO())


class SmokeCommandTests(_Servers, _Fixture, TestCase):
    def _smoke(self, *args):
        out = io.StringIO()
        call_command("integration_smoke", "http", *args, stdout=out)
        return out.getvalue()

    def test_skips_without_a_connection(self):
        self.assertIn("SKIP: no http connection", self._smoke())

    def test_passes_and_fails_with_masked_output(self):
        self._connection(name="api", provider="http", auth_kind="bearer", credentials={"token": TOKEN},
                         config={"base_url": self.base, "test_path": "/v1/me"})
        self.assertIn("PASS get: GET /v1/me answered with dict", self._smoke())
        self.assertEqual(self.api.seen[0][2]["Authorization"], f"Bearer {TOKEN}")
        wrong = self._connection(name="wrong", provider="http", auth_kind="bearer",
                                 credentials={"token": "t26-wrong-token-0009"},
                                 config={"base_url": self.base, "test_path": "/v1/me"})
        with self.assertRaises(CommandError):
            self._smoke()  # two connections: name one
        out = io.StringIO()
        with self.assertRaises(CommandError):
            call_command("integration_smoke", "http", "--connection", wrong.name, stdout=out)
        self.assertIn("FAIL get: UNAUTHORIZED", out.getvalue())
        self.assertNotIn("t26-wrong-token-0009", out.getvalue())

    def test_a_revoked_connection_is_not_used(self):
        self._connection(name="api", provider="http", auth_kind="bearer", credentials={"token": TOKEN},
                         config={"base_url": self.base}, status=Connection.Status.REVOKED)
        self.assertIn("SKIP", self._smoke())

