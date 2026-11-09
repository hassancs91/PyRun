"""
Datastores on the unified registry (2.5 Stage 2, ticket T15).

What this pins, roughly in order of how much it would hurt to get wrong:

- A token without a workspace is refused on the new endpoints AND the legacy
  ``/api/v1/datastores/…`` URLs; it never falls back to the default workspace
  or to unassigned stores.
- The legacy URLs are aliases of the registry handlers: the same body, field for
  field, as ``datastores.list/get/entries/entry`` return, and their own error
  envelope (upper-case codes, the old messages). A legacy single-store token is
  bound to its store exactly like a Unified API token bound to that store.
- ``datastores.set`` / ``datastores.delete`` are write endpoints: hidden and
  refused (``writes_disabled``) while API writes are off, working and audited
  in ``ApiCall`` while on. A bound token writes its own store only, and a write
  never creates a store.
- The token form binds a Unified API token to one datastore, one binding at
  most (model and database); the list shows the binding; a restore preview names
  datastore-bound tokens.

The older datastore-token tests (core/test_plugin_api.py, test_tenancy_stage2/3,
test_token_hashing) still pass; the one that pinned a NULL-workspace token
seeing unassigned stores now pins the refusal.
"""

import uuid
from unittest import mock

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from core.api.context import CallContext
from core.api.errors import ApiError
from core.api.mcp import LEGACY
from core.api.registry import Endpoint, Registry, registry
from core.api.schema import ANY_VALUE, validate
from core.models import (
    ApiCall,
    APIToken,
    Brain,
    DataStore,
    DataStoreEntry,
    Environment,
    GlobalSettings,
    Script,
    User,
    Workspace,
)
from core.services.backup_service import BackupService
from core.test_plugin_api import _mock_setup

ACCEPT = "application/json, text/event-stream"
READS = ("datastores.list", "datastores.get", "datastores.entries", "datastores.entry")
WRITES = ("datastores.set", "datastores.delete")
NESTED = {"retries": 3, "tags": ["a", ["b", {"c": None}]], "ok": True, "ratio": 0.5}


def _set_writes(on):
    gs = GlobalSettings.get_settings()
    gs.api_writes_enabled = on
    gs.save()


def _legacy_auth(raw):
    return {"HTTP_AUTHORIZATION": f"Bearer {raw}"}


class DeclarationTests(SimpleTestCase):
    def test_reads_and_writes_have_their_classes_and_permissions(self):
        for slug in READS:
            spec = registry.get(slug)
            self.assertEqual((spec.access, spec.permission), ("read", "datastores:read"), slug)
        for slug in WRITES:
            spec = registry.get(slug)
            self.assertEqual((spec.access, spec.permission), ("write", "datastores:write"), slug)
            self.assertEqual(spec.rate, (30, 60))
        self.assertIn("datastores:read", registry.read_permissions())  # URL tokens may hold it
        self.assertIn("datastores:write", registry.write_permissions())
        offered = {p for p, _ in APIToken.API_PERMISSIONS}
        self.assertTrue({"datastores:read", "datastores:write"} <= offered)

    def test_a_value_may_be_any_json(self):
        schema = registry.get("datastores.set").input["properties"]["value"]
        for value in (NESTED, [1, [2, [3, {"x": [4]}]]], "text", 7, 0.25, False, None):
            with self.subTest(value=value):
                self.assertEqual(validate(value, schema), value)
        with self.assertRaises(ApiError):
            validate(float("inf"), schema)
        # Only an any-value schema may leave out ``items``; a typed array still can't.
        r = Registry()
        r.register(Endpoint("t.any", lambda c, p: p, {
            "type": "object", "properties": {"v": {"type": list(ANY_VALUE)}},
            "additionalProperties": False}, "T", "", "read", "t:read", (60, 60)))
        with self.assertRaises(ValueError):
            r.register(Endpoint("t.arr", lambda c, p: p, {
                "type": "object", "properties": {"v": {"type": ["array", "string"]}},
                "additionalProperties": False}, "T", "", "read", "t:read", (60, 60)))


class _Fixture:
    def setUp(self):
        _mock_setup(self)
        self.ws = Workspace.get_default()
        self.other = Workspace.objects.create(name="Other T15 workspace")
        self.alpha = DataStore.objects.create(name="alpha", workspace=self.ws,
                                              description="Alpha store")
        self.beta = DataStore.objects.create(name="beta", workspace=self.ws)
        self.foreign = DataStore.objects.create(name="gamma", workspace=self.other)
        self.entry(self.alpha, "config", NESTED)
        self.entry(self.alpha, "b-key", "two")
        self.entry(self.foreign, "config", "foreign-value")
        self.raw, self.token = self.api_token(["datastores:read"])
        self.bound_raw, self.bound = self.api_token(["datastores:read"], datastore=self.alpha)

    def entry(self, store, key, value):
        row = DataStoreEntry(datastore=store, key=key)
        row.set_value(value)
        row.save()
        return row

    def api_token(self, permissions, **kwargs):
        raw = APIToken.generate_token(scope="api")
        token = APIToken.objects.create(
            name=f"t{uuid.uuid4().hex[:6]}", scope="api", token=raw,
            workspace=kwargs.pop("workspace", self.ws), permissions=permissions, **kwargs,
        )
        return raw, token

    def rest(self, slug, params=None, raw=None, method="get"):
        auth = "Bearer " + (raw or self.raw)
        if method == "get":
            return self.client.get("/api/v1/" + slug, params or {}, HTTP_AUTHORIZATION=auth)
        return self.client.post("/api/v1/" + slug, params or {},
                                content_type="application/json", HTTP_AUTHORIZATION=auth)

    def data(self, slug, params=None, raw=None, method="get"):
        response = self.rest(slug, params, raw, method)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["data"]

    def error(self, slug, params=None, raw=None, method="get"):
        response = self.rest(slug, params, raw, method)
        return response.status_code, response.json()["error"]["code"]

    def rpc(self, method="tools/list", params=None, raw=None):
        return self.client.post(
            "/mcp", {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
            content_type="application/json", HTTP_ACCEPT=ACCEPT,
            HTTP_MCP_PROTOCOL_VERSION=LEGACY[0], HTTP_AUTHORIZATION="Bearer " + (raw or self.raw),
        )

    def tools(self, raw=None):
        return {t["name"] for t in self.rpc(raw=raw).json()["result"]["tools"]}

    def call(self, tool, arguments, raw=None):
        return self.rpc("tools/call", {"name": tool, "arguments": arguments}, raw).json()["result"]


class ReadTests(_Fixture, TestCase):
    def test_reads_are_listed_and_scoped_to_the_workspace(self):
        catalog = self.data("_catalog")["endpoints"]
        self.assertEqual({e["slug"] for e in catalog}, set(READS))
        self.assertEqual(self.tools(), {s.replace(".", "-") for s in READS})
        body = self.data("datastores.list")
        self.assertEqual([d["name"] for d in body["datastores"]], ["alpha", "beta"])
        self.assertEqual(body["count"], 2)
        alpha = body["datastores"][0]
        self.assertEqual((alpha["description"], alpha["entry_count"]), ("Alpha store", 2))
        # Another workspace's store is unknown, by any transport.
        self.assertEqual(self.error("datastores.get", {"name": "gamma"}), (404, "not_found"))
        result = self.call("datastores-entry", {"name": "gamma", "key": "config"})
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"]["code"], "not_found")

    def test_entries_pages_and_values_round_trip(self):
        for i in range(5):
            self.entry(self.beta, f"k{i}", i)
        page = self.data("datastores.entries", {"name": "beta", "page": 2, "page_size": 2})
        self.assertEqual([e["key"] for e in page["entries"]], ["k2", "k3"])
        self.assertEqual((page["count"], page["page"], page["total_pages"]), (5, 2, 3))
        past = self.data("datastores.entries", {"name": "beta", "page": 9, "page_size": 2})
        self.assertEqual((past["page"], [e["key"] for e in past["entries"]]), (3, ["k4"]))
        self.assertEqual(self.error("datastores.entries", {"name": "beta", "page_size": 101}),
                         (400, "invalid_input"))
        entry = self.data("datastores.entry", {"name": "alpha", "key": "config"})
        self.assertEqual(entry["value"], NESTED)
        mcp = self.call("datastores-entry", {"name": "alpha", "key": "config"})
        self.assertEqual(mcp["structuredContent"]["value"], NESTED)
        self.assertEqual(self.error("datastores.entry", {"name": "alpha", "key": "nope"}),
                         (404, "not_found"))

    def test_a_bound_token_reaches_its_store_alone(self):
        body = self.data("datastores.list", raw=self.bound_raw)
        self.assertEqual([d["name"] for d in body["datastores"]], ["alpha"])
        # The name may be left out; another store in the workspace is refused.
        self.assertEqual(self.data("datastores.get", raw=self.bound_raw)["name"], "alpha")
        self.assertEqual(
            self.data("datastores.entry", {"key": "b-key"}, raw=self.bound_raw)["value"], "two")
        self.assertEqual(self.error("datastores.get", {"name": "beta"}, raw=self.bound_raw),
                         (403, "forbidden"))
        self.assertEqual(self.error("datastores.get", raw=self.raw), (400, "invalid_input"))

    def test_a_store_without_a_workspace_is_never_reached(self):
        # The run helper's resolver falls back to an unassigned store; the API
        # (and so the legacy URLs) never does, for reads or writes.
        orphan = DataStore.objects.create(name="orphan", workspace=None)
        self.entry(orphan, "k", "orphan-value")
        legacy = APIToken.objects.create(name="all", token=APIToken.generate_token(),
                                         workspace=self.ws)
        self.assertEqual(self.error("datastores.entry", {"name": "orphan", "key": "k"}),
                         (404, "not_found"))
        response = self.client.get("/api/v1/datastores/orphan/entries/k/",
                                   **_legacy_auth(legacy.raw_token))
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("orphan", [d["name"] for d in self.data("datastores.list")["datastores"]])

    def test_a_token_without_the_permission_sees_no_datastore_endpoint(self):
        raw, _ = self.api_token(["scripts:read"])
        self.assertEqual(self.error("datastores.list", raw=raw), (403, "forbidden"))
        self.assertFalse(any(name.startswith("datastores-") for name in self.tools(raw)))


class LegacyAliasTests(_Fixture, TestCase):
    """The old URLs answer what the registry handlers return, unwrapped."""

    def setUp(self):
        super().setUp()
        self.legacy = APIToken.objects.create(name="legacy-all", token=APIToken.generate_token(),
                                              workspace=self.ws)
        self.legacy_one = APIToken.objects.create(
            name="legacy-one", token=APIToken.generate_token(), datastore=self.alpha)

    def get(self, url, token):
        return self.client.get(url, **_legacy_auth(token.raw_token))

    def test_legacy_bodies_are_the_registry_results(self):
        pairs = (
            ("/api/v1/datastores/", "datastores.list", {}),
            ("/api/v1/datastores/alpha/", "datastores.get", {"name": "alpha"}),
            ("/api/v1/datastores/alpha/entries/?page=1&page_size=1",
             "datastores.entries", {"name": "alpha", "page": 1, "page_size": 1}),
            ("/api/v1/datastores/alpha/entries/config/", "datastores.entry",
             {"name": "alpha", "key": "config"}),
        )
        # An all-datastores token = an unbound API token; a single-store token
        # = an API token bound to that store.
        for legacy, raw in ((self.legacy, self.raw), (self.legacy_one, self.bound_raw)):
            for url, slug, params in pairs:
                with self.subTest(token=legacy.name, slug=slug), \
                        mock.patch("core.views.api.datastores.registry.call",
                                   wraps=registry.call) as spy:
                    old = self.get(url, legacy)
                    self.assertEqual(old.status_code, 200, old.content)
                    self.assertEqual(spy.call_args.args[0], slug)
                    self.assertEqual(old.json(), self.data(slug, params, raw=raw))
                    self.assertNotIn("ok", old.json())
                    self.assertEqual(old["Access-Control-Allow-Origin"], "*")

    def test_legacy_errors_keep_their_envelope(self):
        cases = (
            ("/api/v1/datastores/nope/", self.legacy, 404, "NOT_FOUND",
             "Datastore 'nope' not found"),
            ("/api/v1/datastores/alpha/entries/nope/", self.legacy, 404, "NOT_FOUND",
             "Entry 'nope' not found"),
            ("/api/v1/datastores/gamma/", self.legacy, 404, "NOT_FOUND",
             "Datastore 'gamma' not found"),
            ("/api/v1/datastores/beta/", self.legacy_one, 403, "FORBIDDEN",
             "Token does not have access to this datastore"),
        )
        for url, token, status, code, message in cases:
            with self.subTest(url=url):
                response = self.get(url, token)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json(), {"error": {"code": code, "message": message}})

    def test_legacy_paging_stays_lenient(self):
        for i in range(3):
            self.entry(self.beta, f"k{i}", i)
        for query, page, size in (("page=abc&page_size=x", 1, 50), ("page=0&page_size=500", 1, 100),
                                  ("page=7&page_size=2", 2, 2), ("page=-3&page_size=0", 1, 1)):
            with self.subTest(query=query):
                body = self.get(f"/api/v1/datastores/beta/entries/?{query}", self.legacy).json()
                self.assertEqual((body["page"], body["page_size"]), (page, size))

    def test_unified_tokens_still_do_not_work_on_the_legacy_urls(self):
        response = self.client.get("/api/v1/datastores/", **_legacy_auth(self.raw))
        self.assertEqual(response.status_code, 401)


class NullWorkspaceTests(_Fixture, TestCase):
    """A token whose workspace is gone fails closed, on every URL."""

    def test_api_token_without_a_workspace_is_refused(self):
        APIToken.objects.filter(pk=self.token.pk).update(workspace=None)
        self.assertEqual(self.error("datastores.list"), (403, "scope_mismatch"))
        self.assertEqual(self.rpc().status_code, 403)
        ctx = CallContext(workspace=None, principal="token", principal_id="x",
                          permissions=frozenset({"datastores:read"}))
        with self.assertRaises(ApiError) as err:
            registry.call("datastores.list", ctx, {})
        self.assertEqual(err.exception.code, "forbidden")

    def test_legacy_tokens_without_a_workspace_are_refused(self):
        DataStore.objects.create(name="orphan", workspace=None)
        orphan_store = DataStore.objects.create(name="orphan2", workspace=None)
        bare = APIToken.objects.create(name="bare", token=APIToken.generate_token())
        one = APIToken.objects.create(name="one", token=APIToken.generate_token(),
                                      datastore=orphan_store)
        for token in (bare, one):
            for url in ("/api/v1/datastores/", "/api/v1/datastores/orphan/",
                        "/api/v1/datastores/orphan2/entries/"):
                with self.subTest(token=token.name, url=url):
                    response = self.client.get(url, **_legacy_auth(token.raw_token))
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(response.json()["error"], {
                        "code": "SCOPE_MISMATCH",
                        "message": "This token's workspace no longer exists"})


class WriteTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        _set_writes(True)
        self.writer_raw, self.writer = self.api_token(["datastores:read", "datastores:write"])
        self.bound_writer_raw, _ = self.api_token(["datastores:write"], datastore=self.alpha)
        _set_writes(False)

    def set(self, params, raw=None):
        return self.rest("datastores.set", params, raw or self.writer_raw, "post")

    def test_off_writes_are_hidden_and_refused(self):
        catalog = {e["slug"] for e in self.data("_catalog", raw=self.writer_raw)["endpoints"]}
        self.assertEqual(catalog, set(READS))
        openapi = self.rest("_openapi.json", raw=self.writer_raw).json()["paths"]
        self.assertNotIn("/datastores.set", openapi)
        self.assertFalse({"datastores-set", "datastores-delete"} & self.tools(self.writer_raw))
        response = self.set({"name": "alpha", "key": "new", "value": 1})
        self.assertEqual((response.status_code, response.json()["error"]["code"]),
                         (403, "writes_disabled"))
        result = self.call("datastores-delete", {"name": "alpha", "key": "config"},
                           raw=self.writer_raw)
        self.assertEqual(result["structuredContent"]["error"]["code"], "writes_disabled")
        self.assertFalse(self.alpha.entries.filter(key="new").exists())
        self.assertTrue(self.alpha.entries.filter(key="config").exists())
        refused = ApiCall.objects.filter(token=self.writer, slug="datastores.set").get()
        self.assertEqual((refused.ok, refused.status, refused.code),
                         (False, 403, "writes_disabled"))

    def test_on_set_and_delete_write_and_are_audited(self):
        _set_writes(True)
        self.assertTrue({"datastores-set", "datastores-delete"} <= self.tools(self.writer_raw))
        first = self.data("datastores.set", {"name": "beta", "key": "cfg", "value": NESTED},
                          raw=self.writer_raw, method="post")
        self.assertEqual((first["name"], first["key"], first["created"]), ("beta", "cfg", True))
        again = self.call("datastores-set", {"name": "beta", "key": "cfg", "value": [1, "two"]},
                          raw=self.writer_raw)
        self.assertFalse(again["isError"])
        self.assertFalse(again["structuredContent"]["created"])
        self.assertEqual(self.beta.entries.get(key="cfg").get_value(), [1, "two"])
        self.assertEqual(self.beta.entries.count(), 1)
        gone = self.data("datastores.delete", {"name": "beta", "key": "cfg"},
                         raw=self.writer_raw, method="post")
        self.assertEqual(gone, {"name": "beta", "key": "cfg", "deleted": True})
        missing = self.data("datastores.delete", {"name": "beta", "key": "cfg"},
                            raw=self.writer_raw, method="post")
        self.assertFalse(missing["deleted"])
        self.assertEqual(self.rest("datastores.set", raw=self.writer_raw).status_code, 405)
        rows = ApiCall.objects.filter(token=self.writer).values_list("slug", "ok", "status")
        self.assertEqual(sorted(rows), [
            ("datastores.delete", True, 200), ("datastores.delete", True, 200),
            ("datastores.set", False, 405), ("datastores.set", True, 200),
            ("datastores.set", True, 200),
        ])

    def test_writes_never_create_a_store_or_leave_the_binding(self):
        _set_writes(True)
        self.assertEqual(self.error("datastores.set", {"name": "new-store", "key": "k", "value": 1},
                                    raw=self.writer_raw, method="post"), (404, "not_found"))
        self.assertFalse(DataStore.objects.filter(name="new-store").exists())
        self.assertEqual(self.error("datastores.set", {"name": "gamma", "key": "k", "value": 1},
                                    raw=self.writer_raw, method="post"), (404, "not_found"))
        self.assertEqual(self.error("datastores.set", {"name": "beta", "key": "", "value": 1},
                                    raw=self.writer_raw, method="post"), (400, "invalid_input"))
        # A bound token writes its own store (name optional), never another.
        self.assertEqual(self.error("datastores.set", {"name": "beta", "key": "k", "value": 1},
                                    raw=self.bound_writer_raw, method="post"), (403, "forbidden"))
        self.assertFalse(self.beta.entries.exists())
        self.data("datastores.set", {"key": "k", "value": 1}, raw=self.bound_writer_raw,
                  method="post")
        self.assertEqual(self.alpha.entries.get(key="k").get_value(), 1)
        # Read permission alone never writes.
        self.assertEqual(self.error("datastores.set", {"name": "beta", "key": "k", "value": 1},
                                    method="post"), (403, "forbidden"))

    def test_a_set_racing_another_insert_replaces_the_value(self):
        _set_writes(True)
        raced = []

        def new_entry(**kwargs):
            # Another caller inserts the same key between the lookup and our insert.
            raced.append(1)
            DataStoreEntry.objects.create(value_json='"them"', **kwargs)
            return DataStoreEntry(**kwargs)

        with mock.patch("core.api.endpoints.datastores.DataStoreEntry", side_effect=new_entry):
            body = self.data("datastores.set", {"name": "beta", "key": "race", "value": "us"},
                             raw=self.writer_raw, method="post")
        self.assertEqual(raced, [1])
        self.assertFalse(body["created"])
        self.assertEqual(self.beta.entries.get(key="race").get_value(), "us")


class TokenBindingTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.member = User.objects.create(email="t15-form@example.com")
        self.client.force_login(self.member)

    def post(self, **extra):
        data = {"name": "Bound store", "scope": "api", "permissions": ["datastores:read"],
                "kind": "bearer", "brain_tier": "public", **extra}
        return self.client.post(reverse("cpanel:api_token_create"), data)

    def test_the_form_binds_one_datastore_of_this_workspace(self):
        page = self.client.get(reverse("cpanel:api_token_create"))
        choices = {str(c[0]) for c in page.context["form"].fields["datastore"].choices}
        self.assertIn(str(self.alpha.id), choices)
        self.assertNotIn(str(self.foreign.id), choices)
        self.assertContains(page, "Read datastores")
        self.assertRegex(page.content.decode(), r'value="datastores:write"[^>]*disabled')
        response = self.post(datastore=str(self.alpha.id))
        self.assertEqual(response.status_code, 302, getattr(response, "context", None)
                         and response.context["form"].errors)
        token = APIToken.objects.get(name="Bound store")
        self.assertEqual((token.scope, token.datastore_id), ("api", self.alpha.id))
        listing = self.client.get(reverse("cpanel:api_token_list"))
        self.assertContains(listing, 'Datastore: <span class="text-text">alpha</span>', html=False)
        # Another workspace's store is not a choice; a second binding is refused.
        self.assertEqual(self.post(name="Foreign", datastore=str(self.foreign.id)).status_code, 200)
        env = Environment.objects.create(name="t15env", path=f"t15env{uuid.uuid4().hex[:6]}")
        script = Script.objects.create(name="S", code="x", environment=env, workspace=self.ws)
        twice = self.post(name="Twice", datastore=str(self.alpha.id), script=str(script.id),
                          permissions=["datastores:read", "scripts:read"])
        self.assertContains(twice, "one datastore, not more")
        self.assertFalse(APIToken.objects.filter(name__in=["Foreign", "Twice"]).exists())
        # A scope switch still drops a stray store for the other scopes.
        self.post(name="All stores", scope="datastores", datastore=str(self.alpha.id))
        self.assertIsNone(APIToken.objects.get(name="All stores").datastore_id)

    def test_one_binding_at_most_in_the_model_and_the_database(self):
        brain = Brain.objects.create(name="B", slug="b-t15", workspace=self.ws)
        with self.assertRaises(ValidationError) as err:
            self.api_token(["datastores:read", "brain:read"], datastore=self.alpha, brain=brain)
        self.assertIn("one datastore, not more", str(err.exception))
        with self.assertRaises(ValidationError) as err:
            self.api_token(["datastores:read"], datastore=self.foreign)
        self.assertIn("Unknown datastore.", str(err.exception))
        for kwargs in ({"brain": brain}, {"scope": "plugin", "plugin_slug": "x", "permissions": []}):
            with self.subTest(kwargs=kwargs), self.assertRaises(IntegrityError), \
                    transaction.atomic():
                APIToken.objects.filter(pk=self.bound.pk).update(**kwargs)

    def test_the_store_delete_confirm_counts_its_bound_tokens(self):
        APIToken.objects.create(name="legacy-one", token=APIToken.generate_token(),
                                datastore=self.alpha)
        page = self.client.get(reverse("cpanel:datastore_list")).content.decode()
        self.assertIn("So will the 2 API tokens bound to it.", page)  # self.bound + legacy-one
        self.assertEqual(page.count("API token"), 1)  # beta has none
        self.client.post(reverse("cpanel:datastore_delete", args=[self.alpha.pk]))
        self.assertFalse(APIToken.objects.filter(datastore_id=self.alpha.pk).exists())

    def test_restore_preview_names_datastore_bound_tokens(self):
        bound = [t for t in BackupService.tokens_removed_by_restore() if t["name"] == self.bound.name]
        self.assertEqual(bound, [{"name": self.bound.name, "bound_to": "data store alpha"}])
        self.alpha.delete()  # the binding goes with its store (FK cascade)
        self.assertFalse(APIToken.objects.filter(pk=self.bound.pk).exists())

    def test_settings_reference_names_datastores(self):
        self.client.force_login(
            User.objects.create(email="t15-settings@example.com", is_superuser=True))
        page = self.client.get(reverse("cpanel:settings"))
        self.assertContains(page, "/api/v1/brain.*, scripts.*, runs.*, datastores.*, databases.*")
        self.assertContains(page, '<code class="text-xs">datastores.delete</code>')
        datastore_writes = [e.slug for e in registry.writes() if e.slug.startswith("datastores.")]
        self.assertEqual(datastore_writes, sorted(WRITES))
