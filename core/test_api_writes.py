"""
Endpoint classes and the API writes switch (2.5 Stage 2, ticket T11, plan D8).

What this pins, roughly in order of how much it would hurt to get wrong:

- While ``GlobalSettings.api_writes_enabled`` is off (the default), a write
  endpoint can't be reached: a call returns 403 ``writes_disabled`` over REST,
  MCP and in-process, and its handler never runs, even for a token that holds
  the write permission (granted while writes were on).
- Off, write endpoints are absent from ``_catalog``, ``_openapi.json``, MCP
  ``tools/list`` and the in-process listing Py AI builds its tools from.
- Off, a write permission can't be granted: the token form shows it disabled
  with the reason and refuses a crafted POST; the model refuses a new grant,
  while a token that already holds one can still be saved (deactivated).
- On, write permissions are grantable and write calls succeed for tokens that
  hold them (and only for those).
- ``brain.propose-feed`` is a ``run`` endpoint: listed and callable either way.
- The registry accepts only read|run|write and keeps write permissions apart.
- Settings → API & MCP flips the switch (superusers only), Connect states the
  mode, and backups round-trip it (an older backup leaves writes off).

A fixture endpoint ``fixture.write`` with its own ``fixture:write`` permission
stands in for a write endpoint here; the real ones (``datastores.set`` and
``datastores.delete``) are covered in core/test_api_datastores.py.
"""

import json
from unittest import mock

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from core.api.context import CallContext
from core.api.errors import ApiError
from core.api.mcp import LEGACY, META, MODERN
from core.api.registry import Endpoint, Registry, endpoint, registry
from core.models import APIToken, GlobalSettings, User, Workspace
from core.services.backup_service import BackupService
from core.test_plugin_api import _mock_setup

ACCEPT = "application/json, text/event-stream"
INPUT = {
    "type": "object",
    "properties": {"n": {"type": "integer"}},
    "required": ["n"],
    "additionalProperties": False,
}
PERMISSIONS = APIToken.API_PERMISSIONS + (("fixture:write", "Write fixtures"),)


def _spec(slug, access, permission):
    return Endpoint(slug, lambda ctx, p: p, INPUT, "T", "", access, permission, (60, 60))


def _set_writes(on):
    gs = GlobalSettings.get_settings()
    gs.api_writes_enabled = on
    gs.save()


class EndpointClassTests(SimpleTestCase):
    def test_registry_accepts_only_read_run_and_write(self):
        r = Registry()
        r.register(_spec("t.read", "read", "t:read"))
        r.register(_spec("t.run", "run", "t:run"))
        r.register(_spec("t.write", "write", "t:write"))
        for access in ("admin", "", "Write", "readonly", None):
            with self.subTest(access=access), self.assertRaises(ValueError):
                r.register(_spec("t.other", access, "t:read"))
        self.assertEqual([e.slug for e in r.writes()], ["t.write"])
        self.assertEqual(r.write_permissions(), frozenset({"t:write"}))

    def test_write_permissions_stay_apart_from_read_and_run(self):
        r = Registry()
        r.register(_spec("t.read", "read", "t:read"))
        r.register(_spec("t.run", "run", "t:act"))
        r.register(_spec("t.write", "write", "t:write"))
        refused = [
            ("t.w2", "write", "t:read"),  # not a :write permission (URL tokens hold :read)
            ("t.w3", "write", "t:act"),  # shared with a run endpoint
            ("t.r2", "read", "t:write"),  # a write permission guarding a read
            ("t.r3", "run", "t:write"),  # ... or a run
        ]
        for slug, access, permission in refused:
            with self.subTest(slug=slug), self.assertRaises(ValueError):
                r.register(_spec(slug, access, permission))
        r.register(_spec("t.w4", "write", "t:write"))  # two writes may share one

    def test_brain_propose_feed_is_a_run_endpoint(self):
        spec = registry.get("brain.propose-feed")
        self.assertEqual((spec.access, spec.permission), ("run", "brain:write"))
        self.assertNotIn("brain:write", registry.write_permissions())


class WritesToggleTests(TestCase):
    def setUp(self):
        _mock_setup(self)
        self.ws = Workspace.get_default()
        saved = dict(registry._endpoints)
        self.addCleanup(lambda: setattr(registry, "_endpoints", saved))
        patcher = mock.patch.object(APIToken, "API_PERMISSIONS", PERMISSIONS)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = []

        @endpoint("fixture.write", title="Write", input=INPUT, access="write",
                  permission="fixture:write")
        def write(ctx, params):
            self.calls.append(params["n"])
            return {"written": params["n"]}

        _set_writes(True)
        self.raw = APIToken.generate_token(scope="api")
        self.token = APIToken.objects.create(
            name="writer", scope="api", token=self.raw, workspace=self.ws,
            permissions=["brain:read", "fixture:write"],
        )
        self.reader_raw = APIToken.generate_token(scope="api")
        APIToken.objects.create(
            name="reader", scope="api", token=self.reader_raw, workspace=self.ws,
            permissions=["brain:read"],
        )
        _set_writes(False)

    # -- transports -----------------------------------------------------------

    def rest(self, slug="fixture.write", body=None, raw=None, method="post"):
        auth = "Bearer " + (raw or self.raw)
        if method == "get":
            return self.client.get("/api/v1/" + slug, HTTP_AUTHORIZATION=auth)
        return self.client.post(
            "/api/v1/" + slug, {"n": 3} if body is None else body,
            content_type="application/json", HTTP_AUTHORIZATION=auth,
        )

    def rpc(self, method="tools/list", params=None, raw=None, version=LEGACY[0]):
        params = dict(params or {})
        headers = {}
        if version == MODERN:
            params["_meta"] = {META + "protocolVersion": MODERN, META + "clientCapabilities": {}}
            headers["HTTP_MCP_METHOD"] = method
            if method == "tools/call":
                headers["HTTP_MCP_NAME"] = params["name"]
        return self.client.post(
            "/mcp", {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            content_type="application/json", HTTP_ACCEPT=ACCEPT,
            HTTP_MCP_PROTOCOL_VERSION=version, HTTP_AUTHORIZATION="Bearer " + (raw or self.raw),
            **headers,
        )

    def listings(self, raw=None):
        catalog = self.rest("_catalog", raw=raw, method="get").json()["data"]["endpoints"]
        openapi = self.rest("_openapi.json", raw=raw, method="get").json()["paths"]
        tools = self.rpc(raw=raw).json()["result"]["tools"]
        return {e["slug"]: e for e in catalog}, openapi, {t["name"]: t for t in tools}

    # -- toggle off -----------------------------------------------------------

    def test_off_hides_write_endpoints_from_every_listing(self):
        catalog, openapi, tools = self.listings()
        self.assertNotIn("fixture.write", catalog)
        self.assertNotIn("/fixture.write", openapi)
        self.assertNotIn("fixture-write", tools)
        self.assertIn("brain.ping", catalog)  # the rest of the catalog is untouched
        # The in-process listing Py AI builds its tools from, for any principal.
        agent = CallContext(workspace=self.ws, principal="agent", principal_id="pyai",
                            permissions=frozenset({"brain:read", "fixture:write"}))
        self.assertNotIn("fixture.write", [e.slug for e in registry.visible(agent)])
        self.assertIn("fixture.write", [e.slug for e in registry.permitted(agent)])

    def test_off_calls_return_writes_disabled_on_every_transport(self):
        response = self.rest()
        self.assertEqual(response.status_code, 403)
        error = response.json()["error"]
        self.assertEqual(error["code"], "writes_disabled")
        self.assertIn("Settings → API & MCP", error["message"])
        for version in (LEGACY[0], MODERN):
            with self.subTest(version=version):
                result = self.rpc(
                    "tools/call", {"name": "fixture-write", "arguments": {"n": 3}},
                    version=version,
                ).json()["result"]
                self.assertTrue(result["isError"])
                self.assertEqual(result["structuredContent"]["error"]["code"], "writes_disabled")
        ctx = CallContext(workspace=self.ws, principal="user", principal_id="1",
                          permissions=frozenset({"fixture:write"}))
        with self.assertRaises(ApiError) as err:
            registry.call("fixture.write", ctx, {"n": 3})
        self.assertEqual((err.exception.code, err.exception.status), ("writes_disabled", 403))
        self.assertEqual(self.calls, [])

    def test_a_token_without_the_permission_learns_nothing_new(self):
        for on in (False, True):
            _set_writes(on)
            with self.subTest(writes=on):
                response = self.rest(raw=self.reader_raw)
                self.assertEqual((response.status_code, response.json()["error"]["code"]),
                                 (403, "forbidden"))
                hidden = self.rpc("tools/call", {"name": "fixture-write", "arguments": {"n": 1}},
                                  raw=self.reader_raw).json()
                missing = self.rpc("tools/call", {"name": "missing", "arguments": {}},
                                   raw=self.reader_raw).json()
                self.assertEqual(hidden, missing)
        self.assertEqual(self.calls, [])

    # -- toggle on ------------------------------------------------------------

    def test_on_lists_and_runs_writes_for_tokens_holding_the_permission(self):
        _set_writes(True)
        catalog, openapi, tools = self.listings()
        self.assertEqual(catalog["fixture.write"]["access"], "write")
        self.assertEqual(set(openapi["/fixture.write"]), {"post"})  # never a GET
        self.assertFalse(tools["fixture-write"]["annotations"]["readOnlyHint"])
        response = self.rest()
        self.assertEqual((response.status_code, response.json()["data"]), (200, {"written": 3}))
        self.assertEqual(self.rest(method="get").status_code, 405)
        result = self.rpc("tools/call", {"name": "fixture-write", "arguments": {"n": 4}}).json()
        self.assertFalse(result["result"]["isError"])
        self.assertEqual(result["result"]["structuredContent"], {"written": 4})
        self.assertEqual(self.calls, [3, 4])
        # Turning writes back off closes the door again at once.
        _set_writes(False)
        self.assertEqual(self.rest().json()["error"]["code"], "writes_disabled")
        self.assertEqual(self.calls, [3, 4])

    # -- Brain proposals are run, not write -----------------------------------

    def test_propose_feed_is_listed_and_callable_with_writes_off(self):
        raw = APIToken.generate_token(scope="api")
        APIToken.objects.create(name="proposer", scope="api", token=raw, workspace=self.ws,
                                permissions=["brain:read", "brain:write"])
        for on in (False, True):
            _set_writes(on)
            with self.subTest(writes=on):
                catalog, openapi, tools = self.listings(raw=raw)
                self.assertEqual(catalog["brain.propose-feed"]["access"], "run")
                self.assertIn("/brain.propose-feed", openapi)
                self.assertIn("brain-propose-feed", tools)
                with mock.patch("core.api.endpoints.brain.propose",
                                return_value={"feed_id": "f1"}) as propose:
                    response = self.rest("brain.propose-feed", {"content": "note"}, raw=raw)
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(response.json()["data"], {"feed_id": "f1"})
                propose.assert_called_once()

    # -- granting -------------------------------------------------------------

    def test_token_form_disables_write_permissions_while_off(self):
        admin = User.objects.create(email="writes-admin@example.com", is_superuser=True)
        self.client.force_login(admin)
        url = reverse("cpanel:api_token_create")
        page = self.client.get(url)
        self.assertRegex(page.content.decode(), r'value="fixture:write"[^>]*disabled')
        self.assertNotRegex(page.content.decode(), r'value="brain:read"[^>]*disabled')
        self.assertContains(page, "(writes are off)")
        self.assertContains(page, "write permissions can&#x27;t be granted")
        post = {"name": "new", "scope": "api", "permissions": ["fixture:write"],
                "kind": "bearer", "brain_tier": "public"}
        refused = self.client.post(url, post)
        self.assertEqual(refused.status_code, 200)
        self.assertContains(refused, "write permissions can&#x27;t be granted")
        self.assertFalse(APIToken.objects.filter(name="new").exists())

        _set_writes(True)
        page = self.client.get(url)
        self.assertNotContains(page, "(writes are off)")
        self.assertNotRegex(page.content.decode(), r'value="fixture:write"[^>]*disabled')
        self.assertEqual(self.client.post(url, post).status_code, 302)
        self.assertEqual(APIToken.objects.get(name="new").permissions, ["fixture:write"])

    def test_model_refuses_a_new_grant_but_keeps_an_existing_one(self):
        fresh = APIToken(name="fresh", scope="api", token=APIToken.generate_token(scope="api"),
                         workspace=self.ws, permissions=["fixture:write"])
        with self.assertRaises(ValidationError) as err:
            fresh.save()
        self.assertIn("permissions", err.exception.message_dict)
        reader = APIToken.objects.get(name="reader")
        reader.permissions = ["brain:read", "fixture:write"]
        with self.assertRaises(ValidationError):
            reader.save()
        # The writer got fixture:write while writes were on: it keeps it and can
        # still be deactivated (the console toggle saves the row).
        admin = User.objects.create(email="writes-owner@example.com", is_superuser=True)
        self.client.force_login(admin)
        self.client.post(reverse("cpanel:api_token_toggle", args=[self.token.pk]))
        self.token.refresh_from_db()
        self.assertFalse(self.token.is_active)
        self.assertEqual(self.token.permissions, ["brain:read", "fixture:write"])

    # -- console --------------------------------------------------------------

    def test_settings_switch_is_superuser_only(self):
        admin = User.objects.create(email="writes-super@example.com", is_superuser=True)
        self.client.force_login(admin)
        page = self.client.get(reverse("cpanel:settings"))
        self.assertContains(page, "API &amp; MCP")
        self.assertContains(page, "Allow API writes on this instance")
        self.assertContains(page, "<code class=\"text-xs\">fixture.write</code>")
        url = reverse("cpanel:api_writes_settings")
        response = self.client.post(url, {"enabled": "on"})
        self.assertRedirects(response, reverse("cpanel:settings") + "#api",
                             fetch_redirect_response=False)
        self.assertTrue(GlobalSettings.get_settings().api_writes_enabled)
        self.client.post(url, {})
        self.assertFalse(GlobalSettings.get_settings().api_writes_enabled)

        member = User.objects.create(email="writes-member@example.com")
        self.client.force_login(member)
        with self.assertLogs("django.request", level="WARNING"):
            self.assertEqual(self.client.post(url, {"enabled": "on"}).status_code, 403)
        self.assertFalse(GlobalSettings.get_settings().api_writes_enabled)

    def test_connect_page_states_the_mode(self):
        admin = User.objects.create(email="writes-connect@example.com", is_superuser=True)
        self.client.force_login(admin)
        connect = reverse("cpanel:connect")
        off = self.client.get(connect)
        self.assertContains(off, "Writes are off")
        self.assertContains(off, "Change in Settings")
        self.assertNotContains(self.client.get(connect, {"token": self.token.pk}),
                               "fixture.write")
        _set_writes(True)
        on = self.client.get(connect)
        self.assertContains(on, "Writes are on")
        self.assertContains(self.client.get(connect, {"token": self.token.pk}), "fixture.write")

    # -- backups --------------------------------------------------------------

    def test_backup_round_trips_the_switch(self):
        user = User.objects.create(email="writes-backup@example.com", is_superuser=True)
        _set_writes(True)
        data = BackupService.create_backup(include_runs=False)
        self.assertIs(data["global_settings"]["api_writes_enabled"], True)
        _set_writes(False)
        BackupService.restore_backup(data, restore_runs=False, current_user=user)
        self.assertTrue(GlobalSettings.get_settings().api_writes_enabled)
        # An older backup (no key) or a value that isn't literally true: off.
        for value in (None, "yes", 1):
            with self.subTest(value=value):
                copy = json.loads(json.dumps(data))
                if value is None:
                    del copy["global_settings"]["api_writes_enabled"]
                else:
                    copy["global_settings"]["api_writes_enabled"] = value
                _set_writes(True)
                BackupService.restore_backup(copy, restore_runs=False, current_user=user)
                self.assertFalse(GlobalSettings.get_settings().api_writes_enabled)
