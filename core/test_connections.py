"""
Connections for API keys, basic auth and SMTP (2.5 Stage 5, ticket T23).

What this pins, roughly in order of how much it would hurt to get wrong:

- A credential never reaches stored output: a real script that prints the key,
  token and basic-auth header it got from ``pyrunner_connections`` has them
  masked in stdout, result and progress, also when the value was handed out
  after the run started (the per-run mask set, ``RunMask``), and none of them
  is in the script's environment. The recorded copies go when the run ends.
- The loopback checks the grant: an ungranted, another workspace's, a revoked
  or an ambiguous connection is refused; missing and ungranted look the same.
- The grant rule: explicit active grants, plus "grant to all my scripts" for
  the user scripts the connection's creator made in its workspace.
- ``client()`` sends the credentials only to the base URL's origin, also
  across redirects (stdlib client and requests), and ``smtp()`` logs in.
- Console: add via the provider picker, secret fields write-only (blank keeps),
  Test (whoami / SMTP login, through the SSRF guard; errors masked), revoke,
  delete, grants from both sides, workspace scoping.
- Backups round-trip connections (still encrypted), grants and the wildcard;
  ``connections.list`` over the registry never carries a credential.

No test reaches a real provider: HTTP goes to in-process servers or stubs, SMTP
to a mocked smtplib.
"""

import base64
import http.server
import json
import sys
import threading
import urllib.parse
import uuid
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import LiveServerTestCase, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from core.executor import _build_script_environment, execute_run, mask_set_for_display
from core.integrations.presets import auth_material, get_preset, mask_values, presets
from core.models import (
    APIToken,
    Connection,
    ConnectionGrant,
    Environment,
    Run,
    RunMask,
    Script,
    Workspace,
    WorkspaceMembership,
)
from core.script_helpers import pyrunner_connections
from core.services import connections as svc
from core.services.backup_service import BackupService
from core.services.datastore_token import mint_datastore_token
from core.services.run_reports import store_result
from core.services.script_service import fork_script

User = get_user_model()

AIRTABLE_KEY = "pat-t23-airtable-key-0001"
BEARER = "t23-bearer-token-0002"
PASSWORD = "t23-basic-password-0003"
SMTP_PASSWORD = "t23-smtp-password-0004"


def _setup_wizard_off(test):
    for target in (
        "core.services.setup_service.SetupService.is_setup_needed",
        "core.services.setup_service.SetupService.needs_admin_setup",
    ):
        p = mock.patch(target, return_value=False)
        p.start()
        test.addCleanup(p.stop)


def _ai_off():
    return mock.patch("core.executor.ClaudeService.get_script_env", return_value={})


def _netguard_ok():
    return mock.patch("core.netguard.is_safe_endpoint_url", return_value=(True, ""))


class _Fixture:
    def setUp(self):
        _setup_wizard_off(self)
        self.ws = Workspace.get_default() or Workspace.objects.create(name="T23", is_default=True)
        self.other_ws = Workspace.objects.create(name="Other T23")
        self.user = User.objects.create_user(
            username=f"u{uuid.uuid4().hex[:6]}", email=f"{uuid.uuid4().hex[:6]}@t23.test", password="pw"
        )
        WorkspaceMembership.objects.update_or_create(
            user=self.user, workspace=self.ws, defaults={"role": WorkspaceMembership.ROLE_MEMBER}
        )
        self.colleague = User.objects.create_user(
            username=f"c{uuid.uuid4().hex[:6]}", email=f"{uuid.uuid4().hex[:6]}@t23.test", password="pw"
        )
        self.env = Environment.objects.create(name="t23env", path=f"t23env{uuid.uuid4().hex[:6]}")

    def _script(self, name=None, **kwargs):
        kwargs.setdefault("workspace", self.ws)
        kwargs.setdefault("code", "pass")
        kwargs.setdefault("created_by", self.user)
        return Script.objects.create(name=name or f"s{uuid.uuid4().hex[:6]}", environment=self.env, **kwargs)

    def _connection(self, name="airtable", provider="airtable", auth_kind="api_key", credentials=None,
                    config=None, workspace=None, **kwargs):
        kwargs.setdefault("created_by", self.user)
        c = Connection(workspace=workspace or self.ws, name=name, provider=provider,
                       auth_kind=auth_kind, config=config or {}, **kwargs)
        c.set_credentials(credentials if credentials is not None else {"api_key": AIRTABLE_KEY})
        c.save()
        return c

    def _basic(self, name="intranet", base_url="https://intranet.example.com"):
        return self._connection(name=name, provider="http", auth_kind="basic",
                                config={"base_url": base_url, "username": "robot", "test_path": ""},
                                credentials={"password": PASSWORD})

    def _run(self, script, status=Run.Status.RUNNING):
        return Run.objects.create(script=script, workspace=self.ws, status=status)


# --------------------------------------------------------------------------- #
# Presets
# --------------------------------------------------------------------------- #


class PresetTests(SimpleTestCase):
    def test_the_batch_one_presets_are_valid(self):
        self.assertEqual(
            list(presets()),
            ["airtable", "google", "microsoft", "github", "slack", "notion", "http", "smtp", "imap"],
        )
        for preset in presets().values():
            with self.subTest(preset=preset.key):
                self.assertTrue(preset.auth_kinds)
                for kind in preset.auth_kinds:
                    self.assertIn(kind, Connection.AuthKind.values)
                    names = [f.name for f in preset.fields_for(kind)]
                    self.assertEqual(len(names), len(set(names)))
                    self.assertNotIn("name", names)  # the form's own field

    def test_auth_material_per_kind(self):
        airtable = get_preset("airtable")
        self.assertEqual(
            auth_material(airtable, "api_key", {}, {"api_key": "k1"})["headers"],
            {"Authorization": "Bearer k1"},
        )
        http = get_preset("http")
        query = auth_material(http, "api_key", {"location": "query", "param_name": "key"}, {"api_key": "k2"})
        self.assertEqual((query["headers"], query["params"], query["token"]), ({}, {"key": "k2"}, "k2"))
        prefixed = auth_material(http, "api_key", {"param_name": "Authorization", "prefix": "Token "},
                                 {"api_key": "k3"})
        self.assertEqual(prefixed["headers"], {"Authorization": "Token k3"})
        basic = auth_material(http, "basic", {"username": "u"}, {"password": "p"})
        self.assertEqual(basic["headers"]["Authorization"], "Basic " + base64.b64encode(b"u:p").decode())
        self.assertIsNone(basic["token"])
        smtp = auth_material(get_preset("smtp"), "smtp", {"host": "h", "port": 465, "security": "ssl"},
                             {"password": "p"})["smtp"]
        self.assertEqual((smtp["host"], smtp["port"], smtp["security"], smtp["password"]), ("h", 465, "ssl", "p"))

    def test_mask_values_cover_the_derived_basic_header(self):
        masks = mask_values("intranet", get_preset("http"), "basic", {"username": "robot"}, {"password": PASSWORD})
        pair = base64.b64encode(f"robot:{PASSWORD}".encode()).decode()
        self.assertIn(PASSWORD, masks.values())
        self.assertIn(pair, masks.values())
        self.assertNotIn("robot", masks.values())  # the username is not a secret
        self.assertIn("connection:intranet.password", masks)

    def test_mask_values_skip_a_bare_prefix_when_nothing_secret_is_stored(self):
        # A revoked connection has no credentials left: its header would be
        # "Bearer " (or "Token ", "Basic <user:>"), and masking that hid the
        # word in every output of a script the connection is granted to.
        http = get_preset("http")
        self.assertEqual(mask_values("api", http, "bearer", {}, {}), {})
        self.assertEqual(mask_values("api", http, "api_key", {"param_name": "Authorization", "prefix": "Token "}, {}), {})
        self.assertEqual(mask_values("intranet", http, "basic", {"username": "robot"}, {}), {})
        self.assertEqual(mask_values("g", get_preset("google"), "oauth2", {}, {"refresh_token": "r1"}),
                         {"connection:g.refresh_token": "r1"})
        # With the secret there, the header is still masked whole.
        self.assertEqual(mask_values("api", http, "bearer", {}, {"token": "t1"}),
                         {"connection:api.token": "t1", "connection:api.authorization": "Bearer t1"})


# --------------------------------------------------------------------------- #
# Model and grants
# --------------------------------------------------------------------------- #


class GrantTests(_Fixture, TestCase):
    def test_credentials_are_encrypted_at_rest(self):
        c = self._connection()
        raw = Connection.objects.filter(pk=c.pk).values_list("credentials_encrypted", flat=True).get()
        self.assertNotIn(AIRTABLE_KEY, raw)
        self.assertEqual(Connection.objects.get(pk=c.pk).get_credentials(), {"api_key": AIRTABLE_KEY})

    def test_a_name_is_unique_per_workspace(self):
        self._connection()
        self._connection(workspace=self.other_ws)  # same name elsewhere is fine
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError), transaction.atomic():
            self._connection()

    def test_explicit_grants_and_the_wildcard(self):
        mine, theirs = self._script(), self._script(created_by=self.colleague)
        plugin_script = self._script(owner_plugin="acme")
        explicit = self._connection(name="explicit")
        wildcard = self._connection(name="wildcard", grant_all_scripts=True)
        inactive = self._connection(name="inactive")
        foreign = self._connection(name="foreign", workspace=self.other_ws)
        ConnectionGrant.objects.create(script=theirs, connection=explicit)
        ConnectionGrant.objects.create(script=mine, connection=inactive, active=False)
        ConnectionGrant.objects.create(script=mine, connection=foreign)  # other workspace: ignored

        def names(script):
            return [c.name for c in svc.granted_connections(script)]

        self.assertEqual(names(mine), ["wildcard"])
        self.assertEqual(names(theirs), ["explicit"])
        self.assertEqual(names(plugin_script), [])  # the wildcard is for user scripts
        self.assertEqual([s.pk for s in svc.scripts_using(wildcard)], [mine.pk])
        self.assertEqual([s.pk for s in svc.scripts_using(explicit)], [theirs.pk])

    def test_a_script_without_a_workspace_gets_nothing(self):
        script = self._script(workspace=None)
        self._connection(grant_all_scripts=True)
        self.assertEqual(list(svc.granted_connections(script)), [])

    def test_a_workspace_with_connections_cannot_be_deleted(self):
        from core.views.workspaces import _owned_row_counts

        self._connection(workspace=self.other_ws)
        self.assertEqual(_owned_row_counts(self.other_ws), {"connection": 1})

    def test_a_fork_keeps_the_explicit_grants(self):
        source = self._script()
        c = self._connection()
        ConnectionGrant.objects.create(script=source, connection=c)
        fork = fork_script(source, user=self.user)
        self.assertTrue(ConnectionGrant.objects.filter(script=fork, connection=c, active=True).exists())


# --------------------------------------------------------------------------- #
# The loopback endpoints
# --------------------------------------------------------------------------- #


class LoopbackTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.script = self._script()
        self.run = self._run(self.script)
        self.airtable = self._connection()
        ConnectionGrant.objects.create(script=self.script, connection=self.airtable)

    def _post(self, name, body, run=None):
        return self.client.post(
            reverse(f"internal:connections_{name}"), data=json.dumps(body), content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {mint_datastore_token((run or self.run).id)}",
        )

    def test_token_returns_the_auth_material_and_records_the_masks_first(self):
        resp = self._post("token", {"connection": "airtable"})
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()
        self.assertEqual(data["token"], AIRTABLE_KEY)
        self.assertEqual(data["headers"], {"Authorization": f"Bearer {AIRTABLE_KEY}"})
        self.assertEqual(data["base_url"], "https://api.airtable.com")
        self.assertEqual(data["auth_kind"], "api_key")
        self.assertIn(AIRTABLE_KEY, svc.issued_masks(self.run.pk).values())
        self._post("token", {"connection": "airtable"})
        self.assertEqual(RunMask.objects.filter(run=self.run).count(), 2)  # key + header, once each

    def test_a_provider_key_names_the_only_connection_of_that_provider(self):
        self.assertEqual(self._post("token", {"connection": "airtable"}).status_code, 200)
        self.airtable.name = "work-base"
        self.airtable.save()
        self.assertEqual(self._post("token", {"connection": "airtable"}).json()["name"], "work-base")
        second = self._connection(name="home-base")
        ConnectionGrant.objects.create(script=self.script, connection=second)
        resp = self._post("token", {"connection": "airtable"})
        self.assertEqual((resp.status_code, resp.json()["error"]["code"]), (409, "AMBIGUOUS"))
        self.assertEqual(self._post("token", {"connection": "home-base"}).status_code, 200)

    def test_ungranted_and_missing_look_the_same(self):
        self._connection(name="secret-one")
        self._connection(name="elsewhere", workspace=self.other_ws)
        for name in ("secret-one", "elsewhere", "nope"):
            with self.subTest(name=name):
                resp = self._post("token", {"connection": name})
                self.assertEqual(resp.status_code, 404)
                self.assertEqual(resp.json()["error"]["code"], "CONNECTION_NOT_FOUND")
        self.assertEqual(RunMask.objects.count(), 0)

    def test_a_revoked_connection_is_refused(self):
        svc.revoke(self.airtable)
        resp = self._post("token", {"connection": "airtable"})
        self.assertEqual((resp.status_code, resp.json()["error"]["code"]), (409, "CONNECTION_REVOKED"))
        self.assertEqual(Connection.objects.get(pk=self.airtable.pk).credentials_encrypted, "")

    def test_a_finished_run_gets_no_credentials(self):
        Run.objects.filter(pk=self.run.pk).update(status=Run.Status.SUCCESS)
        resp = self._post("token", {"connection": "airtable"})
        self.assertEqual((resp.status_code, resp.json()["error"]["code"]), (409, "RUN_NOT_RUNNING"))
        self.assertNotIn(AIRTABLE_KEY, resp.content.decode())
        self.assertEqual(RunMask.objects.count(), 0)

    def test_info_and_list_carry_no_secret(self):
        basic = self._basic()
        ConnectionGrant.objects.create(script=self.script, connection=basic)
        for resp in (self._post("info", {"connection": "intranet"}), self._post("list", {})):
            self.assertEqual(resp.status_code, 200)
            text = resp.content.decode()
            self.assertNotIn(PASSWORD, text)
            self.assertNotIn(AIRTABLE_KEY, text)
            self.assertNotIn(base64.b64encode(f"robot:{PASSWORD}".encode()).decode(), text)
        names = [c["name"] for c in self._post("list", {}).json()["connections"]]
        self.assertEqual(names, ["airtable", "intranet"])
        self.assertEqual(RunMask.objects.count(), 0)

    def test_bad_bodies(self):
        self.assertEqual(self._post("token", {}).status_code, 400)
        resp = self.client.post(reverse("internal:connections_token"), data="[]", content_type="application/json",
                                HTTP_AUTHORIZATION=f"Bearer {mint_datastore_token(self.run.id)}")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            self.client.post(reverse("internal:connections_token"), data="{}", content_type="application/json",
                             HTTP_AUTHORIZATION="Bearer forged").status_code, 401)


# --------------------------------------------------------------------------- #
# Masking
# --------------------------------------------------------------------------- #


class MaskingTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.script = self._script()
        self.run = self._run(self.script)

    def test_granted_connection_values_join_the_display_mask_set(self):
        c = self._basic()
        ConnectionGrant.objects.create(script=self.script, connection=c)
        with _ai_off(), mock.patch("core.executor.resolve_secrets_for_run", return_value={}):
            masks = mask_set_for_display(self.run)
        self.assertIn(PASSWORD, masks.values())

    def test_the_environment_gets_no_connection_credential(self):
        c = self._connection(grant_all_scripts=True)
        self.assertIn(c, svc.granted_connections(self.script))
        with _ai_off(), mock.patch("core.executor.resolve_secrets_for_run", return_value={}):
            env = _build_script_environment(run=self.run)
        self.assertFalse([k for k, v in env.items() if AIRTABLE_KEY in v])

    def test_a_result_stored_after_a_late_issue_is_masked(self):
        # Granted after the run started: only the per-run mask set knows it.
        c = self._connection(name="late", credentials={"api_key": "t23-late-key-9"})
        svc._record_masks(self.run.pk, svc.masks_for(c))
        with _ai_off(), mock.patch("core.executor.resolve_secrets_for_run", return_value={}):
            store_result(self.run.pk, {"echo": "t23-late-key-9"})
        self.run.refresh_from_db()
        self.assertEqual(self.run.result, {"echo": "[connection:late.api_key:MASKED]"})

    def test_issued_masks_keep_two_values_under_one_label(self):
        svc._record_masks(self.run.pk, {"connection:x.api_key": "first-value"})
        svc._record_masks(self.run.pk, {"connection:x.api_key": "rotated-value"})
        self.assertEqual(sorted(svc.issued_masks(self.run.pk).values()), ["first-value", "rotated-value"])
        svc.forget_issued(self.run.pk)
        self.assertEqual(svc.issued_masks(self.run.pk), {})


class EndToEndTests(_Fixture, LiveServerTestCase):
    """A real script uses its connections through the real loopback."""

    host = "127.0.0.1"  # the internal API answers loopback only

    CODE = (
        "import os, json, pyrunner_connections as pc, pyrunner_run\n"
        "key = pc.token('airtable')\n"
        "header = pc.credentials('intranet')['headers']['Authorization']\n"
        "print('key=' + key)\n"
        "print('header=' + header)\n"
        "print('in_env=' + str(any(key in v or header in v for v in os.environ.values())))\n"
        "pyrunner_run.progress(50, 'using ' + key)\n"
        "pyrunner_run.set_result({'key': key, 'header': header})\n"
    )

    def _execute(self, script, **patches):
        run = self._run(script, status=Run.Status.PENDING)
        with override_settings(PYRUNNER_INTERNAL_BASE_URL=self.live_server_url), _ai_off(), \
                mock.patch("core.executor.resolve_secrets_for_run", return_value={}), \
                mock.patch("core.executor._validate_environment", return_value=sys.executable):
            if patches.get("no_spawn_masks"):
                with mock.patch("core.services.connections.connection_masks", return_value={}):
                    execute_run(run)
            else:
                execute_run(run)
        run.refresh_from_db()
        return run

    def _check(self, run):
        pair = base64.b64encode(f"robot:{PASSWORD}".encode()).decode()
        self.assertEqual(run.status, Run.Status.SUCCESS, run.stderr)
        for value in (AIRTABLE_KEY, pair, PASSWORD):
            self.assertNotIn(value, run.stdout)
            self.assertNotIn(value, json.dumps(run.result))
            self.assertNotIn(value, run.progress["message"])
        self.assertIn("key=[connection:airtable.api_key:MASKED]", run.stdout)
        self.assertIn("in_env=False", run.stdout)
        self.assertEqual(run.result["key"], "[connection:airtable.api_key:MASKED]")
        self.assertEqual(RunMask.objects.filter(run=run).count(), 0)  # cleared at the end

    def test_credentials_are_masked_everywhere_and_never_in_the_environment(self):
        script = self._script(code=self.CODE)
        for c in (self._connection(), self._basic()):
            ConnectionGrant.objects.create(script=script, connection=c)
        self._check(self._execute(script))

    def test_values_issued_after_spawn_are_masked_by_the_per_run_set(self):
        script = self._script(code=self.CODE)
        for c in (self._connection(), self._basic()):
            ConnectionGrant.objects.create(script=script, connection=c)
        self._check(self._execute(script, no_spawn_masks=True))


# --------------------------------------------------------------------------- #
# The helper's clients
# --------------------------------------------------------------------------- #


class _Recorder(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):  # noqa: N802 - http.server API
        type(self).seen.append((self.server.server_port, self.path, dict(self.headers)))
        if self.path.startswith("/hop"):
            self.send_response(302)
            self.send_header("Location", self.server.redirect_to)
            self.end_headers()
            return
        if self.path.startswith("/go?"):  # 302 to ?to=, "@away" standing for the second server
            to = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)["to"][0]
            self.send_response(302)
            self.send_header("Location", to.replace("@away", self.server.away))
            self.end_headers()
            return
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class HelperClientTests(SimpleTestCase):
    def setUp(self):
        _Recorder.seen = []
        self.servers = []
        for _ in range(2):
            server = http.server.HTTPServer(("127.0.0.1", 0), _Recorder)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.servers.append(server)
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        self.home = f"http://127.0.0.1:{self.servers[0].server_port}"
        self.away = f"http://127.0.0.1:{self.servers[1].server_port}"
        self.servers[0].redirect_to = self.away + "/landed"
        self.servers[0].away = self.away
        self.material = {
            "base_url": self.home, "headers": {"X-Api-Key": "k-secret"}, "params": {"sig": "q-secret"},
            "token": "k-secret", "basic": None, "smtp": None,
        }

    def _headers(self, port):
        return [h for p, _path, h in _Recorder.seen if p == port]

    def _paths(self, port):
        return [path for p, path, _h in _Recorder.seen if p == port]

    def _exercise(self, client):
        self.assertEqual(client.get("/v1/me").json(), {"ok": True})
        client.get(self.away + "/elsewhere")
        client.get("/hop")
        home, away = self.servers[0].server_port, self.servers[1].server_port
        self.assertEqual([h.get("X-Api-Key") for h in self._headers(home)], ["k-secret", "k-secret"])
        self.assertIn("/v1/me?sig=q-secret", self._paths(home))
        self.assertEqual([h.get("X-Api-Key") for h in self._headers(away)], [None, None])
        self.assertEqual(self._paths(away), ["/elsewhere", "/landed"])

    def test_stdlib_client_sends_credentials_to_the_base_url_only(self):
        self._exercise(pyrunner_connections.Client(self.material, timeout=5))

    def test_requests_session_sends_credentials_to_the_base_url_only(self):
        try:
            import requests
        except ImportError:  # pragma: no cover - requests ships with PyRun
            self.skipTest("requests is not installed")
        self._exercise(pyrunner_connections._requests_session(requests, self.material, 5))

    def _query_key_redirects(self, client):
        # Test pass 5 (O4): a query-parameter key goes once on every hop to the base URL's
        # origin and never on a hop elsewhere, even when the Location carries it.
        client.get("/go", params={"to": "/back?r=1"})
        client.get("/go", params={"to": self.home + "/abs?sig=q-secret"})
        client.get("/go", params={"to": "@away/moved?sig=q-secret&r=2"})
        home, away = self.servers[0].server_port, self.servers[1].server_port
        hops = [p for p in self._paths(home) if not p.startswith("/go")]
        self.assertEqual(hops, ["/back?r=1&sig=q-secret", "/abs?sig=q-secret"])
        self.assertEqual(self._paths(away), ["/moved?r=2"])
        self.assertEqual([h.get("X-Api-Key") for h in self._headers(home)], ["k-secret"] * 5)

    def test_stdlib_client_keeps_a_query_key_on_redirects_to_the_base_url_only(self):
        self._query_key_redirects(pyrunner_connections.Client(self.material, timeout=5))

    def test_requests_session_keeps_a_query_key_on_redirects_to_the_base_url_only(self):
        try:
            import requests
        except ImportError:  # pragma: no cover - requests ships with PyRun
            self.skipTest("requests is not installed")
        self._query_key_redirects(pyrunner_connections._requests_session(requests, self.material, 5))

    def test_stdlib_response_basics(self):
        client = pyrunner_connections.Client(self.material, timeout=5)
        resp = client.get("/v1/me")
        self.assertTrue(resp.ok)
        resp.raise_for_status()
        bad = pyrunner_connections.Response(404, {}, b"", "http://x/y")
        with self.assertRaises(pyrunner_connections.HTTPError):
            bad.raise_for_status()

    def test_smtp_connects_upgrades_and_logs_in(self):
        smtp = {"host": "mail.example.com", "port": 587, "security": "starttls", "username": "robot",
                "password": SMTP_PASSWORD, "from_address": "robot@example.com"}
        with mock.patch.object(pyrunner_connections, "credentials", return_value={"smtp": smtp}), \
                mock.patch("smtplib.SMTP") as smtp_cls:
            server = pyrunner_connections.smtp("mail")
        smtp_cls.assert_called_once_with("mail.example.com", 587, timeout=30)
        server.starttls.assert_called_once()
        server.login.assert_called_once_with("robot", SMTP_PASSWORD)
        self.assertEqual(server.from_address, "robot@example.com")

    def test_client_refuses_an_smtp_connection_and_token_a_basic_one(self):
        with mock.patch.object(pyrunner_connections, "credentials", return_value={"base_url": None}):
            with self.assertRaises(pyrunner_connections.ConnectionsError):
                pyrunner_connections.client("mail")
        with mock.patch.object(pyrunner_connections, "credentials",
                               return_value={"token": None, "auth_kind": "basic"}):
            with self.assertRaises(pyrunner_connections.ConnectionsError):
                pyrunner_connections.token("intranet")


# --------------------------------------------------------------------------- #
# Console
# --------------------------------------------------------------------------- #


class _FakeResponse:
    def __init__(self, body):
        self.body = body

    def read(self, n=-1):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ConsoleTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def test_the_sidebar_links_connections_and_lights_it_up(self):
        html = self.client.get(reverse("cpanel:connection_list")).content.decode()
        aside = html[html.index('<aside id="app-sidebar"'):html.index("</aside>")]
        self.assertIn(reverse("cpanel:connection_list"), aside)
        self.assertIn("No connections yet", html)
        # The empty page names the OAuth providers too (O6).
        for label in ("Google", "Microsoft 365", "GitHub", "Slack", "Notion", "Airtable", "SMTP"):
            self.assertIn(label, html[html.index("No connections yet"):])

    def test_the_picker_lists_every_preset(self):
        html = self.client.get(reverse("cpanel:connection_create")).content.decode()
        for label in ("Airtable", "HTTP API", "SMTP"):
            self.assertIn(label, html)

    def test_the_imap_pages_show_imap_usage_that_follows_its_security(self):
        # Test pass 7 F3: no HTTP/SMTP helpers on an IMAP connection's pages, and the example
        # opens the mailbox the way the stored security setting says.
        html = self.client.get(reverse("cpanel:connection_create") + "?provider=imap").content.decode()
        self.assertIn("pick it in an IMAP watcher", html)
        for helper in ("client(", "smtp("):
            self.assertNotIn(f"pyrunner_connections.{helper}&quot;name&quot;)", html)
            self.assertNotIn(f'pyrunner_connections.{helper}"name")', html)
        self.assertIn("imaplib.IMAP4_SSL(", html)
        expected = {"ssl": ("imaplib.IMAP4_SSL(", None), "starttls": ("imaplib.IMAP4(", "box.starttls()"),
                    "none": ("imaplib.IMAP4(", None)}
        for security, (opener, starttls) in expected.items():
            c = self._connection(f"mail-{security}", "imap", "basic", {"password": "pw"},
                                 config={"host": "mail.example.com", "port": 993, "security": security,
                                         "username": "me@example.com"})
            page = self.client.get(reverse("cpanel:connection_edit", kwargs={"pk": c.pk})).content.decode()
            code = page[page.index("import imaplib"):page.index("</code>", page.index("import imaplib"))]
            self.assertIn(opener, code)
            self.assertIn(f'# security "{security}"', code)
            if security != "ssl":
                self.assertNotIn("IMAP4_SSL", code)
            self.assertEqual("box.starttls()" in code, starttls is not None)
            self.assertNotIn("api.get(", code)

    def test_add_an_airtable_connection(self):
        resp = self.client.post(reverse("cpanel:connection_create"), {
            "provider": "airtable", "auth_kind": "api_key", "name": "airtable", "api_key": AIRTABLE_KEY,
        })
        c = Connection.objects.get(name="airtable")
        self.assertRedirects(resp, reverse("cpanel:connection_edit", kwargs={"pk": c.pk}))
        self.assertEqual((c.workspace, c.created_by, c.provider, c.auth_kind), (self.ws, self.user, "airtable", "api_key"))
        self.assertEqual(c.get_credentials(), {"api_key": AIRTABLE_KEY})
        self.assertEqual(c.config, {})
        page = self.client.get(reverse("cpanel:connection_edit", kwargs={"pk": c.pk})).content.decode()
        self.assertNotIn(AIRTABLE_KEY, page)

    def test_an_http_key_keeps_its_prefix_space_and_the_url_is_guarded(self):
        data = {"provider": "http", "auth_kind": "api_key", "name": "crm", "base_url": "https://api.crm.test",
                "api_key": "k", "location": "header", "param_name": "Authorization", "prefix": "Token "}
        with mock.patch("core.netguard.is_safe_endpoint_url", return_value=(False, "Internal endpoints are not allowed")):
            resp = self.client.post(reverse("cpanel:connection_create"), data)
        self.assertContains(resp, "Internal endpoints are not allowed")
        self.assertFalse(Connection.objects.exists())
        with _netguard_ok():
            self.client.post(reverse("cpanel:connection_create"), data)
        c = Connection.objects.get(name="crm")
        self.assertEqual(c.config["prefix"], "Token ")
        self.assertEqual(auth_material(get_preset("http"), "api_key", c.config, c.get_credentials())["headers"],
                         {"Authorization": "Token k"})

    def test_an_smtp_host_may_be_a_compose_service_name(self):
        data = {"provider": "smtp", "auth_kind": "smtp", "name": "mail", "host": "mailpit", "port": 1025,
                "security": "none", "username": "", "password": "", "from_address": ""}
        with _netguard_ok() as guard:
            self.client.post(reverse("cpanel:connection_create"), data)
        guard.assert_called_once_with("smtp://mailpit", operator_hint=False)
        self.assertEqual(Connection.objects.get(name="mail").config["host"], "mailpit")
        data.update(name="mail2", host="smtp://mailpit")
        self.assertContains(self.client.post(reverse("cpanel:connection_create"), data), "host name only")

    def test_names_are_checked(self):
        self._connection(name="taken")
        for name, error in (("taken", "already exists"), ("bad name", "Letters, digits")):
            with self.subTest(name=name):
                resp = self.client.post(reverse("cpanel:connection_create"), {
                    "provider": "airtable", "auth_kind": "api_key", "name": name, "api_key": "x"})
                self.assertContains(resp, error)

    def test_a_blank_secret_field_keeps_the_saved_value(self):
        c = self._basic()
        url = reverse("cpanel:connection_edit", kwargs={"pk": c.pk})
        with _netguard_ok():
            self.client.post(url, {"name": "intranet", "base_url": "https://intranet.example.com",
                                   "username": "robot2", "password": "", "test_path": ""})
        c.refresh_from_db()
        self.assertEqual((c.config["username"], c.get_credentials()), ("robot2", {"password": PASSWORD}))
        with _netguard_ok():
            self.client.post(url, {"name": "intranet", "base_url": "https://intranet.example.com",
                                   "username": "robot2", "password": "new-pass-1", "test_path": ""})
        c.refresh_from_db()
        self.assertEqual(c.get_credentials(), {"password": "new-pass-1"})

    def test_another_workspaces_connection_is_404(self):
        c = self._connection(workspace=self.other_ws)
        for name in ("connection_edit", "connection_test", "connection_revoke", "connection_delete"):
            with self.subTest(view=name):
                url = reverse(f"cpanel:{name}", kwargs={"pk": c.pk})
                resp = self.client.post(url) if name != "connection_edit" else self.client.get(url)
                self.assertEqual(resp.status_code, 404)
        self.assertTrue(Connection.objects.filter(pk=c.pk).exists())

    def test_test_calls_whoami_and_names_the_account(self):
        c = self._connection()
        opener = mock.Mock()
        opener.open.return_value = _FakeResponse(b'{"id": "usr1", "email": "ops@acme.test"}')
        with _netguard_ok(), mock.patch("core.netguard.guarded_opener", return_value=opener):
            self.client.post(reverse("cpanel:connection_test", kwargs={"pk": c.pk}))
        request = opener.open.call_args[0][0]
        self.assertEqual(request.full_url, "https://api.airtable.com/v0/meta/whoami")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {AIRTABLE_KEY}")
        c.refresh_from_db()
        self.assertEqual((c.status, c.account_label, c.last_error), ("ok", "ops@acme.test", ""))
        self.assertIsNotNone(c.last_tested_at)

    def test_a_failed_test_is_stored_masked(self):
        c = self._connection(provider="http", config={"base_url": "https://x.test", "location": "query",
                                                     "param_name": "key", "test_path": "/me"})
        opener = mock.Mock()
        opener.open.side_effect = OSError(f"boom at https://x.test/me?key={AIRTABLE_KEY}")
        with _netguard_ok(), mock.patch("core.netguard.guarded_opener", return_value=opener):
            resp = self.client.post(reverse("cpanel:connection_test", kwargs={"pk": c.pk}), follow=True)
        self.assertEqual(opener.open.call_args[0][0].full_url, f"https://x.test/me?key={AIRTABLE_KEY}")
        c.refresh_from_db()
        self.assertEqual(c.status, "error")
        self.assertNotIn(AIRTABLE_KEY, c.last_error)
        self.assertIn("[connection:airtable.api_key:MASKED]", c.last_error)
        self.assertNotIn(AIRTABLE_KEY, resp.content.decode())

    def test_a_value_python_escapes_in_the_error_is_masked_too(self):
        # Test pass 5 (F6): http.client refused a key holding a line break with
        # "Invalid header value b'...'", where the escaped key didn't match the
        # plain value, so the key was stored and shown in the error.
        key = "t23-key-with-break\r\nX-Evil: 1"
        c = self._connection(provider="http", config={"base_url": "https://x.test", "location": "header",
                                                     "param_name": "X-API-Key", "test_path": "/me"},
                             credentials={"api_key": key})
        opener = mock.Mock()
        opener.open.side_effect = ValueError(f"Invalid header value {key.encode()!r}")
        with _netguard_ok(), mock.patch("core.netguard.guarded_opener", return_value=opener):
            ok, message = svc.check_connection(c)
        self.assertFalse(ok)
        c.refresh_from_db()
        for text in (message, c.last_error):
            self.assertNotIn("t23-key-with-break", text)
            self.assertEqual(text, "Invalid header value b'[connection:airtable.api_key:MASKED]'")

    def test_pyrun_own_fault_in_a_test_shows_neutral_text(self):
        # T48 sweep: a service's answer (ValueError, OSError, ...) is shown as it is;
        # any other exception is PyRun's own fault and stays in the server log.
        c = self._connection(provider="http", config={"base_url": "https://x.test", "location": "header",
                                                     "param_name": "X-API-Key", "test_path": "/me"},
                             credentials={"api_key": "k"})
        opener = mock.Mock()
        opener.open.side_effect = KeyError("internal_field_name")
        with _netguard_ok(), mock.patch("core.netguard.guarded_opener", return_value=opener), \
                self.assertLogs("core.services.connections", "ERROR"):
            ok, message = svc.check_connection(c)
        self.assertFalse(ok)
        c.refresh_from_db()
        for text in (message, c.last_error):
            self.assertEqual(text, svc.UNEXPECTED_TEST_ERROR)
        self.assertEqual(c.status, Connection.Status.ERROR)
        opener.open.side_effect = OSError("Connection refused")
        with _netguard_ok(), mock.patch("core.netguard.guarded_opener", return_value=opener):
            ok, message = svc.check_connection(c)
        self.assertEqual((ok, message), (False, "Connection refused"))

    def test_a_field_with_a_line_break_is_refused(self):
        data = {"provider": "http", "auth_kind": "api_key", "name": "crm", "base_url": "https://api.crm.test",
                "api_key": "first-line\nsecond-line", "location": "header", "param_name": "X-API-Key",
                "prefix": ""}
        with _netguard_ok():
            resp = self.client.post(reverse("cpanel:connection_create"), data)
            self.assertContains(resp, "Remove the line breaks and other control characters.")
            self.assertFalse(Connection.objects.exists())
            data.update(api_key="k", param_name="X-API-Key\r\nX-Evil")
            self.assertContains(self.client.post(reverse("cpanel:connection_create"), data), "control characters")
            data.update(param_name="X-API-Key")
            self.client.post(reverse("cpanel:connection_create"), data)
        self.assertEqual(Connection.objects.get(name="crm").get_credentials(), {"api_key": "k"})

    def test_an_smtp_test_logs_in_through_the_guard(self):
        c = self._connection(name="mail", provider="smtp", auth_kind="smtp",
                             config={"host": "smtp.example.com", "port": 587, "security": "starttls",
                                     "username": "robot", "from_address": ""},
                             credentials={"password": SMTP_PASSWORD})
        with mock.patch("core.services.connections._GuardedSMTP") as smtp_cls:
            ok, message = svc.check_connection(c)
        self.assertTrue(ok, message)
        smtp_cls.assert_called_once_with("smtp.example.com", 587, timeout=svc.TEST_TIMEOUT)
        smtp_cls.return_value.login.assert_called_once_with("robot", SMTP_PASSWORD)
        self.assertEqual(Connection.objects.get(pk=c.pk).account_label, "robot")

    def test_revoke_then_reconnect(self):
        c = self._connection()
        self.client.post(reverse("cpanel:connection_revoke", kwargs={"pk": c.pk}))
        c.refresh_from_db()
        self.assertEqual((c.status, c.credentials_encrypted), ("revoked", ""))
        # Nothing is saved any more, so the page doesn't offer to keep it (O7).
        edit = self.client.get(reverse("cpanel:connection_edit", kwargs={"pk": c.pk})).content.decode()
        self.assertNotIn("Leave blank to keep the saved value", edit)
        self.assertIn("Nothing is saved: the connection was revoked. Enter it to reconnect.", edit)
        self.client.post(reverse("cpanel:connection_edit", kwargs={"pk": c.pk}),
                         {"name": "airtable", "api_key": "pat-new"})
        c.refresh_from_db()
        self.assertEqual((c.status, c.get_credentials()), ("ok", {"api_key": "pat-new"}))
        edit = self.client.get(reverse("cpanel:connection_edit", kwargs={"pk": c.pk})).content.decode()
        self.assertIn("Leave blank to keep the saved value", edit)

    def test_delete(self):
        c = self._connection()
        ConnectionGrant.objects.create(script=self._script(), connection=c)
        self.client.post(reverse("cpanel:connection_delete", kwargs={"pk": c.pk}))
        self.assertFalse(Connection.objects.exists())
        self.assertFalse(ConnectionGrant.objects.exists())

    def test_grants_from_the_connection_page_and_the_used_by_column(self):
        c = self._connection()
        a, b = self._script("Alpha"), self._script("Beta")
        archived = self._script("Old", archived_at="2026-01-01T00:00:00Z")
        ConnectionGrant.objects.create(script=archived, connection=c)
        ConnectionGrant.objects.create(script=b, connection=c)
        self.client.post(reverse("cpanel:connection_edit", kwargs={"pk": c.pk}),
                         {"name": "airtable", "api_key": "", "granted_script_ids": [str(a.pk)]})
        self.assertEqual(
            set(ConnectionGrant.objects.filter(connection=c).values_list("script_id", flat=True)),
            {a.pk, archived.pk},  # the archived script's grant wasn't on the page
        )
        html = self.client.get(reverse("cpanel:connection_list")).content.decode()
        self.assertIn("Alpha", html)

    def test_the_script_editor_box_grants_and_keeps(self):
        c = self._connection()
        script = self._script()
        url = reverse("cpanel:script_edit", kwargs={"pk": script.pk})
        html = self.client.get(url).content.decode()
        self.assertIn('name="granted_connection_ids"', html)
        self.assertIn('name="connections_section"', html)
        svc.set_grants(script, [str(c.pk)], self.ws)
        self.assertEqual([g.connection_id for g in script.connection_grants.all()], [c.pk])
        svc.set_grants(script, [], self.ws)
        self.assertFalse(script.connection_grants.exists())
        foreign = self._connection(workspace=self.other_ws)
        svc.set_grants(script, [str(foreign.pk)], self.ws)  # another workspace's: ignored
        self.assertFalse(script.connection_grants.exists())

    def test_the_script_form_saves_the_box_only_when_posted(self):
        from core.views.scripts import _reconcile_connections

        c = self._connection()
        script = self._script()
        ConnectionGrant.objects.create(script=script, connection=c)
        request = mock.Mock(workspace=self.ws, POST=mock.MagicMock())
        request.POST.__contains__.return_value = False
        _reconcile_connections(request, script)
        self.assertTrue(script.connection_grants.exists())
        request.POST.__contains__.return_value = True
        request.POST.getlist.return_value = []
        _reconcile_connections(request, script)
        self.assertFalse(script.connection_grants.exists())


# --------------------------------------------------------------------------- #
# Backup
# --------------------------------------------------------------------------- #


class BackupTests(_Fixture, TestCase):
    def test_connections_and_grants_round_trip_encrypted(self):
        script = self._script()
        c = self._basic()
        wild = self._connection(grant_all_scripts=True, account_label="ops@acme.test")
        ConnectionGrant.objects.create(script=script, connection=c, active=False)
        data = BackupService.create_backup(created_by_user=self.user)
        exported = {row["name"]: row for row in data["connections"]}
        self.assertNotIn(PASSWORD, json.dumps(data))
        self.assertEqual(exported["intranet"]["credentials_encrypted"], c.credentials_encrypted)
        result = BackupService.restore_backup(data, current_user=self.user)
        self.assertTrue(result["success"], result["errors"])
        self.assertEqual(result["counts"]["connections"], 2)
        back = Connection.objects.get(pk=c.pk)
        self.assertEqual((back.get_credentials(), back.config["username"]), ({"password": PASSWORD}, "robot"))
        self.assertEqual(back.created_at, c.created_at)
        self.assertTrue(Connection.objects.get(pk=wild.pk).grant_all_scripts)
        self.assertEqual(Connection.objects.get(pk=wild.pk).account_label, "ops@acme.test")
        grant = ConnectionGrant.objects.get(connection=back)
        self.assertEqual((grant.script_id, grant.active), (script.pk, False))

    def test_an_older_backup_leaves_connections_alone(self):
        data = BackupService.create_backup(created_by_user=self.user)
        del data["connections"]
        c = self._connection()
        result = BackupService.restore_backup(data, current_user=self.user)
        self.assertTrue(result["success"], result["errors"])
        self.assertTrue(Connection.objects.filter(pk=c.pk).exists())
        self.assertNotIn("connections", result["counts"])

    def test_an_unknown_provider_is_restored_unusable_with_a_warning(self):
        self._connection()
        data = BackupService.create_backup(created_by_user=self.user)
        data["connections"][0]["provider"] = "gopher"
        result = BackupService.restore_backup(data, current_user=self.user)
        self.assertTrue(result["success"], result["errors"])
        self.assertTrue(any("gopher" in w for w in result["warnings"]))
        self.assertEqual(Connection.objects.get().status, "error")

    def test_the_preview_counts_connections(self):
        self._connection()
        data = BackupService.create_backup(created_by_user=self.user)
        self.assertEqual(BackupService.get_backup_preview(data)["counts"]["connections"], 1)


# --------------------------------------------------------------------------- #
# connections.list
# --------------------------------------------------------------------------- #


class ConnectionsListEndpointTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.script = self._script("Alpha")
        self.airtable = self._connection(last_error=f"refused {AIRTABLE_KEY}", status="error")
        self.basic = self._basic()
        ConnectionGrant.objects.create(script=self.script, connection=self.basic)
        self._connection(name="foreign", workspace=self.other_ws)

    def _token(self, permissions):
        raw = APIToken.generate_token(scope="api")
        APIToken.objects.create(name=f"t{uuid.uuid4().hex[:6]}", scope="api", token=raw,
                                workspace=self.ws, permissions=permissions)
        return raw

    def _get(self, raw, params=None):
        return self.client.get("/api/v1/connections.list", params or {}, HTTP_AUTHORIZATION="Bearer " + raw)

    def test_lists_the_workspace_without_credentials(self):
        resp = self._get(self._token(["connections:read"]))
        self.assertEqual(resp.status_code, 200, resp.content)
        text = resp.content.decode()
        for value in (AIRTABLE_KEY, PASSWORD, base64.b64encode(f"robot:{PASSWORD}".encode()).decode()):
            self.assertNotIn(value, text)
        data = resp.json()["data"]
        by_name = {c["name"]: c for c in data["connections"]}
        self.assertEqual(sorted(by_name), ["airtable", "intranet"])
        self.assertEqual(by_name["airtable"]["last_error"], "refused [connection:airtable.api_key:MASKED]")
        self.assertEqual(by_name["intranet"]["scripts"], [{"id": str(self.script.pk), "name": "Alpha"}])

    def test_one_scripts_connections_and_the_permission(self):
        raw = self._token(["connections:read"])
        data = self._get(raw, {"script": str(self.script.pk)}).json()["data"]
        self.assertEqual([c["name"] for c in data["connections"]], ["intranet"])
        self.assertEqual(self._get(self._token(["scripts:read"])).status_code, 403)

    def test_console_py_ai_only(self):
        from core.api.registry import registry

        endpoint = registry.get("connections.list")
        self.assertTrue(endpoint.allows_tier("console"))
        self.assertFalse(endpoint.allows_tier("channel"))
        from core.services.pyai.runtime import _BASE_SYSTEM

        self.assertIn("connections-list", _BASE_SYSTEM)
