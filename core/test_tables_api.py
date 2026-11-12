"""
Tables over the API, MCP, Py AI, the plugin SDK and CSV (2.5 Stage 9, ticket T39).

What this pins:

- ``tables.list/get/rows`` are reads (``tables:read``, channel-safe Py AI
  tools); ``tables.insert/update/delete`` are writes (``tables:write``): hidden
  and refused with ``writes_disabled`` while API writes are off, working and
  audited in ``ApiCall`` while on. Every call goes through ``TableService``
  (coercion, the row cap). Another workspace's table is unknown.
- A Unified API token bound to one table (``APIToken.table``) reaches that
  table alone and may leave its name out; one binding at most (model and
  database); the token form offers the binding; a restore preview names
  table-bound tokens when the backup replaces tables.
- ``TableAPI(OWNER).provision(key, columns)`` is idempotent, found by owner and
  key, keeps columns it isn't given unless asked; the plugin's scripts reach the
  table, others don't; plugin delete with remove_data deletes it and the
  preview counts it.
- CSV: the console export (the grid's filters, every page) imports back
  unchanged; import is checked whole first (dry run counts, errors list line
  numbers, nothing written), maps headers by name or by an explicit mapping;
  the helper's ``import_csv`` / ``export_csv`` do the same through the loopback.
- Py AI answers "how many leads were added this week?" from ``tables.rows``.
"""

import csv
import io
import json
import uuid
from datetime import timedelta
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.api.mcp import LEGACY
from core.api.registry import registry
from core.api.tools import agent_context, build_tools
from core.forms import APITokenForm
from core.models import (
    ApiCall,
    APIToken,
    Environment,
    GlobalSettings,
    Script,
    Table,
    TableRow,
    Workspace,
)
from core.plugins.api import TableAPI, TableError
from core.services.backup_service import BackupService
from core.services.plugin_service import PluginService
from core.tables import csv_io
from core.tables.service import TableService
from core.test_plugin_api import _mock_setup
from core.test_tables import COLUMNS, _Console, _HelperFixture, _ws

ACCEPT = "application/json, text/event-stream"
READS = ("tables.get", "tables.list", "tables.rows")
WRITES = ("tables.delete", "tables.insert", "tables.update")


def _set_writes(on):
    gs = GlobalSettings.get_settings()
    gs.api_writes_enabled = on
    gs.save()


class DeclarationTests(SimpleTestCase):
    def test_classes_permissions_and_tiers(self):
        for slug in READS:
            spec = registry.get(slug)
            self.assertEqual((spec.access, spec.permission, spec.pyai_tier),
                             ("read", "tables:read", "channel"), slug)
        for slug in WRITES:
            spec = registry.get(slug)
            self.assertEqual((spec.access, spec.permission, spec.rate),
                             ("write", "tables:write", (30, 60)), slug)
        self.assertIn("tables:read", registry.read_permissions())
        self.assertIn("tables:write", registry.write_permissions())
        self.assertIn("tables:read", dict(APIToken.API_PERMISSIONS))


class _Api(TestCase):
    def setUp(self):
        _mock_setup(self)
        self.ws = _ws()
        self.other = Workspace.objects.create(name="Other T39 workspace")
        self.leads = TableService.create_table(workspace=self.ws, name="Leads", columns=[
            {"key": "email", "type": "text", "required": True},
            {"key": "score", "type": "number"},
            {"key": "meta", "type": "json"},
        ])
        self.notes = TableService.create_table(workspace=self.ws, name="Notes",
                                               columns=[{"key": "body", "type": "text"}])
        self.foreign = TableService.create_table(workspace=self.other, name="Leads",
                                                 columns=[{"key": "x", "type": "text"}])
        TableService.insert(self.leads, [
            {"email": "a@x", "score": 3}, {"email": "b@x", "score": 9, "meta": {"k": [1]}},
            {"email": "c@x", "score": 5},
        ])
        self.raw, self.token = self.api_token(["tables:read"])

    def api_token(self, permissions, **kwargs):
        raw = APIToken.generate_token(scope="api")
        token = APIToken.objects.create(
            name=f"t{uuid.uuid4().hex[:6]}", scope="api", token=raw,
            workspace=kwargs.pop("workspace", self.ws), permissions=permissions, **kwargs,
        )
        return raw, token

    def rest(self, slug, params=None, raw=None):
        return self.client.post("/api/v1/" + slug, params or {}, content_type="application/json",
                                HTTP_AUTHORIZATION="Bearer " + (raw or self.raw))

    def data(self, slug, params=None, raw=None):
        response = self.rest(slug, params, raw)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["data"]

    def error(self, slug, params=None, raw=None):
        response = self.rest(slug, params, raw)
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


class ReadTests(_Api):
    def test_list_get_and_rows_scoped_to_the_workspace(self):
        self.assertEqual({e["slug"] for e in self.data("_catalog")["endpoints"]}, set(READS))
        self.assertEqual(self.tools(), {s.replace(".", "-") for s in READS})
        listed = self.data("tables.list")
        self.assertEqual([(t["slug"], t["row_count"]) for t in listed["tables"]], [("leads", 3), ("notes", 0)])
        self.assertEqual(listed["tables"][0]["columns"][0], {"key": "email", "type": "text", "required": True})
        got = self.data("tables.get", {"table": "Leads"})  # by name too
        self.assertEqual((got["slug"], got["row_cap"], got["columns"][1]["type_label"]),
                         ("leads", 100_000, "Number"))
        body = self.data("tables.rows", {"table": "leads", "where": {"score": {"$gte": 5}},
                                         "order": "-score", "limit": 1})
        self.assertEqual((body["count"], [r["email"] for r in body["rows"]]), (2, ["b@x"]))
        self.assertEqual(set(body["rows"][0]), {"id", "email", "score", "meta", "created_at", "updated_at"})
        self.assertEqual(body["rows"][0]["meta"], {"k": [1]})
        # MCP returns the same rows.
        result = self.call("tables-rows", {"table": "leads", "order": ["score"]})
        self.assertEqual([r["email"] for r in result["structuredContent"]["rows"]], ["a@x", "c@x", "b@x"])
        # Another workspace's table (same name) is never reached: ours answers.
        self.assertEqual(self.data("tables.get", {"table": "leads"})["columns"][0]["key"], "email")
        self.foreign.slug = "elsewhere"
        self.foreign.save()
        self.assertEqual(self.error("tables.get", {"table": "elsewhere"}), (404, "not_found"))

    def test_bad_input_is_a_client_error(self):
        self.assertEqual(self.error("tables.rows", {"table": "leads", "where": {"nope": 1}}),
                         (400, "unsupported_filter"))
        self.assertEqual(self.error("tables.rows", {"table": "leads", "limit": 5000}), (400, "invalid_input"))
        self.assertEqual(self.error("tables.get", {}), (400, "invalid_input"))
        result = self.call("tables-rows", {"table": "leads", "where": {"score": {"$gt": "many"}}})
        self.assertTrue(result["isError"])

    def test_a_token_without_the_permission_sees_nothing(self):
        raw, _ = self.api_token(["datastores:read"])
        self.assertEqual(self.error("tables.list", raw=raw), (403, "forbidden"))
        self.assertFalse(any(t.startswith("tables-") for t in self.tools(raw)))


class WriteTests(_Api):
    def setUp(self):
        super().setUp()
        _set_writes(True)
        self.writer_raw, self.writer = self.api_token(["tables:read", "tables:write"])
        _set_writes(False)

    def test_writes_are_hidden_and_refused_while_off(self):
        self.assertFalse(any(t in self.tools(self.writer_raw) for t in ("tables-insert", "tables-delete")))
        self.assertEqual(self.error("tables.insert", {"table": "leads", "rows": [{"email": "z@x"}]},
                                    self.writer_raw), (403, "writes_disabled"))
        self.assertEqual(TableRow.objects.filter(table=self.leads).count(), 3)
        self.assertTrue(ApiCall.objects.filter(token=self.writer, slug="tables.insert", ok=False).exists())

    def test_insert_update_delete_while_on(self):
        _set_writes(True)
        self.assertLessEqual({"tables-insert", "tables-update", "tables-delete"}, self.tools(self.writer_raw))
        body = self.data("tables.insert", {"table": "leads", "rows": [
            {"email": "d@x", "score": "12"}, {"email": "e@x"}]}, self.writer_raw)
        self.assertEqual(body["inserted"], 2)
        new_id = body["ids"][0]
        self.assertEqual(TableRow.objects.get(pk=new_id).data["score"], 12)  # coerced
        self.leads.refresh_from_db()
        self.assertEqual(self.leads.row_count, 5)
        row = self.data("tables.update", {"table": "leads", "id": new_id, "values": {"score": None}},
                        self.writer_raw)["row"]
        self.assertEqual((row["email"], row["score"]), ("d@x", None))
        self.assertEqual(self.data("tables.delete", {"table": "leads", "ids": [new_id, 999999]},
                                   self.writer_raw)["deleted"], 1)
        self.leads.refresh_from_db()
        self.assertEqual(self.leads.row_count, 4)
        # The service's checks answer with their codes; nothing is half-written.
        self.assertEqual(self.error("tables.insert", {"table": "leads", "rows": [{"email": "f@x"}, {"score": 1}]},
                                    self.writer_raw), (400, "invalid"))
        self.assertEqual(self.error("tables.update", {"table": "leads", "id": 999999, "values": {}},
                                    self.writer_raw), (404, "row_not_found"))
        with self.settings(PYRUNNER_TABLE_ROW_CAP=5):
            self.assertEqual(self.error("tables.insert", {"table": "leads", "rows": [{"email": "f@x"}, {"email": "g@x"}]},
                                        self.writer_raw), (409, "row_cap"))
        self.assertEqual(TableRow.objects.filter(table=self.leads).count(), 4)
        audited = set(ApiCall.objects.filter(token=self.writer).values_list("slug", "ok"))
        self.assertLessEqual({("tables.insert", True), ("tables.update", True), ("tables.delete", True),
                              ("tables.insert", False)}, audited)
        # MCP writes the same way.
        result = self.call("tables-insert", {"table": "notes", "rows": [{"body": "hi"}]}, self.writer_raw)
        self.assertFalse(result.get("isError"), result)
        self.assertEqual(TableRow.objects.get(table=self.notes).data, {"body": "hi"})


class BindingTests(_Api):
    def test_a_bound_token_reaches_its_table_only(self):
        raw, token = self.api_token(["tables:read"], table=self.leads)
        self.assertEqual([t["slug"] for t in self.data("tables.list", raw=raw)["tables"]], ["leads"])
        self.assertEqual(self.data("tables.rows", {}, raw)["count"], 3)  # name optional
        self.assertEqual(self.error("tables.get", {"table": "notes"}, raw), (403, "forbidden"))
        self.assertEqual(self.error("tables.get", {"table": "missing"}, raw), (404, "not_found"))

    def test_one_binding_at_most(self):
        env = Environment.objects.create(name="t39-env", path="/tmp/t39-env")
        script = Script.objects.create(name="s", code="", workspace=self.ws, environment=env)
        with self.assertRaises(ValidationError):
            self.api_token(["tables:read"], table=self.leads, script=script)
        with self.assertRaises(ValidationError):  # another workspace's table
            self.api_token(["tables:read"], table=self.foreign)
        _, token = self.api_token(["tables:read"], table=self.leads)
        with self.assertRaises(IntegrityError), transaction.atomic():
            APIToken.objects.filter(pk=token.pk).update(script=script)
        with self.assertRaises(IntegrityError), transaction.atomic():
            APIToken.objects.filter(pk=token.pk).update(scope="datastores", permissions=[])
        # The token list names the binding.
        from django.template.loader import render_to_string

        self.assertIn("Table: <span", render_to_string("cpanel/api_tokens/_token_scope.html", {"token": token}))
        # Deleting the table deletes its token (never widened to every table).
        TableService.delete_table(self.leads)
        self.assertFalse(APIToken.objects.filter(pk=token.pk).exists())

    def test_the_token_form_binds_a_table_of_this_workspace(self):
        form = APITokenForm(workspace=self.ws)
        self.assertEqual(set(form.fields["table"].queryset), {self.leads, self.notes})
        form = APITokenForm({"name": "bound", "scope": "api", "permissions": ["tables:read"],
                             "kind": "bearer", "table": str(self.leads.pk)}, workspace=self.ws)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["table"], self.leads)
        form = APITokenForm({"name": "foreign", "scope": "api", "permissions": ["tables:read"],
                             "kind": "bearer", "table": str(self.foreign.pk)}, workspace=self.ws)
        self.assertFalse(form.is_valid())
        form = APITokenForm({"name": "store", "scope": "datastores", "table": str(self.leads.pk)},
                            workspace=self.ws)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.cleaned_data["table"])  # only a Unified API token binds

    def test_the_restore_preview_names_table_bound_tokens(self):
        _, token = self.api_token(["tables:read"], table=self.leads)
        names = {t["name"]: t["bound_to"] for t in BackupService.tokens_removed_by_restore()}
        self.assertEqual(names[token.name], "table Leads")
        # A backup without tables leaves them (and their tokens) alone.
        names = {t["name"] for t in BackupService.tokens_removed_by_restore(with_tables=False)}
        self.assertNotIn(token.name, names)


class PyAITests(_Api):
    def test_how_many_leads_were_added_this_week(self):
        old = TableRow.objects.filter(table=self.leads).order_by("id").first()
        TableRow.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=30))
        week = (timezone.now() - timedelta(days=7)).date().isoformat()
        names = {t.name for t in build_tools(self.ws)}
        self.assertLessEqual({"tables-list", "tables-get", "tables-rows"}, names)
        self.assertFalse(names & {"tables-insert", "tables-update", "tables-delete"})
        for agent in ("pyai", "telegram:42"):  # console and channel tiers
            answer = registry.call("tables.rows", agent_context(self.ws, agent), {
                "table": "leads", "where": {"created_at": {"$gte": week}}, "limit": 1})
            self.assertEqual(answer["count"], 2, agent)
        from core.services.pyai.runtime import base_system_prompt

        self.assertIn("tables-rows", base_system_prompt(self.ws))


class SdkTests(_HelperFixture):
    OWNER = "shop"

    def test_provision_is_idempotent_and_owned(self):
        api = TableAPI(self.OWNER, workspace=self.ws)
        orders = api.provision("orders", [{"key": "sku", "type": "text", "required": True}],
                               description="Orders")
        table = orders.model
        self.assertEqual((table.name, table.slug, table.owner_plugin, table.owner_key),
                         ("shop:orders", "shop-orders", "shop", "orders"))
        again = api.provision("orders", [{"key": "sku", "type": "text", "required": True}])
        self.assertEqual(again.model.pk, table.pk)
        self.assertEqual(Table.objects.filter(owner_plugin="shop").count(), 1)
        # A console rename doesn't break the lookup by owner and key.
        Table.objects.filter(pk=table.pk).update(name="Shop orders", slug="orders-x")
        grown = api.provision("orders", [{"key": "qty", "type": "number"}])
        self.assertEqual(grown.model.pk, table.pk)
        self.assertEqual([c["key"] for c in grown.columns], ["qty", "sku"])  # sku kept
        row = grown.insert({"sku": "A1", "qty": "2"})
        self.assertEqual(grown.get(row["id"])["qty"], 2)
        self.assertIsNone(grown.get(999999))
        self.assertEqual(grown.upsert("sku", {"sku": "A1", "qty": 3})["qty"], 3)
        self.assertEqual(grown.count(where={"qty": {"$gt": 1}}), 1)
        self.assertEqual(grown.insert([{"sku": "B"}, {"sku": "C"}]).__len__(), 2)
        self.assertTrue(grown.delete(row["id"]))
        self.assertEqual([r["sku"] for r in grown.rows(order="sku")], ["B", "C"])
        trimmed = api.provision("orders", [{"key": "qty", "type": "number"}], remove_missing=True)
        self.assertEqual([c["key"] for c in trimmed.columns], ["qty"])
        self.assertNotIn("sku", TableRow.objects.filter(table=table).first().data)
        with self.assertRaises(TableError):
            api.provision("Bad Key", [{"key": "a"}])
        self.assertEqual([h.slug for h in api.list()], ["orders-x"])
        self.assertIsNone(TableAPI("other", workspace=self.ws).get("orders"))
        # A user table with that slug is not the plugin's.
        TableService.create_table(workspace=self.ws, name="mine", columns=[{"key": "a"}])
        self.assertIsNone(TableAPI(self.OWNER, workspace=self.ws).get("mine"))
        self.assertEqual(TableAPI(workspace=self.ws).get("mine").name, "mine")

    def test_scripts_reach_the_plugin_table_only_from_the_plugin(self):
        TableAPI(self.OWNER, workspace=self.ws).provision("orders", [{"key": "sku", "type": "text"}])
        with self.assertRaises(self.pt.TableNotFound):
            self.pt.table("shop-orders")
        Script.objects.filter(pk=self.script.pk).update(owner_plugin=self.OWNER)
        self.pt.table("shop-orders").insert({"sku": "A"})
        self.assertEqual(TableRow.objects.filter(table__owner_plugin="shop").count(), 1)

    def test_plugin_delete_with_remove_data_deletes_its_tables(self):
        TableAPI(self.OWNER, workspace=self.ws).provision("orders", [{"key": "sku", "type": "text"}])
        counts = PluginService.owned_resource_counts(self.OWNER)
        self.assertEqual(counts["tables"], 1)
        self.assertIn("1 table", PluginService.owned_resource_summary(self.OWNER))
        removed = PluginService._cleanup_owned_resources(self.OWNER)
        self.assertEqual(removed.get("tables"), 1)
        self.assertFalse(Table.objects.filter(owner_plugin=self.OWNER).exists())
        self.assertTrue(Table.objects.filter(pk=self.table.pk).exists())  # a user table stays


def _csv(*lines):
    out = io.StringIO()
    csv.writer(out).writerows(lines)
    return out.getvalue()


class CsvTests(_Console):
    def test_auto_mapping(self):
        mapping = csv_io.auto_mapping(["ID", "E-mail", "Email", " score ", "created_at", "x"], COLUMNS)
        self.assertEqual(mapping, [None, None, "email", "score", None, None])

    def upload(self, text, as_json=False, **extra):
        data = {"file": SimpleUploadedFile("rows.csv", text.encode("utf-8"), content_type="text/csv"), **extra}
        headers = {"HTTP_ACCEPT": "application/json"} if as_json else {}
        return self.client.post(reverse("cpanel:table_import", args=[self.table.pk]), data, **headers)

    def test_export_then_import_round_trip(self):
        TableService.insert(self.table, [
            {"email": "a@x", "score": 1.5, "active": False, "joined": "2026-01-02",
             "seen": "2026-01-02T03:04:05Z", "meta": {"k": ["v", 1]}, "site": "https://x.example"},
            {"email": "b, \"quoted\"\nline", "score": 7},
            {"email": "skip@x", "score": 0},
        ])
        # The grid's filters apply to the export.
        response = self.client.get(reverse("cpanel:table_export", args=[self.table.pk]) + "?f=score:gte:1")
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn('filename="leads.csv"', response["Content-Disposition"])
        text = b"".join(response.streaming_content).decode("utf-8")
        lines = list(csv.reader(io.StringIO(text)))
        self.assertEqual(lines[0], ["id", "email", "score", "active", "joined", "seen", "meta", "site",
                                    "created_at", "updated_at"])
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[1][2:8], ["1.5", "false", "2026-01-02", "2026-01-02T03:04:05Z", '{"k":["v",1]}',
                                         "https://x.example"])
        copy = TableService.create_table(workspace=self.ws, name="Copy", columns=COLUMNS)
        report = csv_io.import_csv(copy, io.StringIO(text))
        self.assertEqual((report["rows"], report["inserted"], report["error_count"]), (2, 2, 0))
        source = {r["email"]: r for r in TableService.rows(self.table, where={"score": {"$gte": 1}})}
        for row in TableService.rows(copy):
            original = source[row["email"]]
            for key in ("score", "active", "joined", "seen", "meta", "site"):
                self.assertEqual(row[key], original[key], key)
        copy.refresh_from_db()
        self.assertEqual(copy.row_count, 2)

    def test_dry_run_errors_and_mapping(self):
        good = _csv(["Email", "Score", "id"], ["a@x", "3", "99"], [], ["b@x", ""])
        body = self.upload(good, dry_run="1", as_json=True).json()
        self.assertEqual((body["rows"], body["inserted"], body["dry_run"]), (2, 0, True))
        self.assertEqual(body["mapping"], {"Email": "email", "Score": "score"})
        self.assertFalse(TableRow.objects.filter(table=self.table).exists())
        bad = _csv(["email", "score", "meta"], ["a@x", "lots", ""], ["", "1", ""], ["c@x", "2", "{bad"],
                   ["d@x", "4", "[1]"])
        page = self.upload(bad)
        self.assertContains(page, "4 rows checked; 3 can")
        self.assertContains(page, "Line 2:")
        self.assertContains(page, "Line 4:")
        self.assertFalse(TableRow.objects.filter(table=self.table).exists())
        # An explicit mapping: a header that isn't a key, a header skipped.
        mapped = _csv(["Contact", "Points", "Notes"], ["m@x", "8", "ignored"])
        response = self.upload(mapped, mapping=json.dumps({"Contact": "email", "Points": "score"}))
        self.assertRedirects(response, reverse("cpanel:table_detail", args=[self.table.pk]),
                             fetch_redirect_response=False)
        self.assertEqual(TableService.rows(self.table)[0]["score"], 8)
        for mapping, message in ((json.dumps({"Nope": "email"}), "header lacks"),
                                 (json.dumps({"Contact": "ghost"}), "isn"),
                                 ("[1", "valid JSON")):
            page = self.upload(mapped, mapping=mapping)
            self.assertEqual(page.status_code, 400)
            self.assertContains(page, message, status_code=400)
        # The page's mapping step sends one key per header cell, so repeated header names stay apart.
        twice = _csv(["x", "x"], ["n@x", "9"])
        self.upload(twice, mapping=json.dumps(["email", "score"]))
        self.assertEqual(TableService.rows(self.table, where={"email": "n@x"})[0]["score"], 9)

    def test_files_that_cannot_be_imported(self):
        self.assertContains(self.upload("", as_json=False), "empty", status_code=400)
        response = self.client.post(reverse("cpanel:table_import", args=[self.table.pk]), {
            "file": SimpleUploadedFile("x.csv", "email\n\xe9t\xe9\n".encode("latin-1"))})
        self.assertContains(response, "UTF-8", status_code=400)
        with mock.patch.object(csv_io, "MAX_IMPORT_BYTES", 10):
            self.assertContains(self.upload(_csv(["email"], ["a-long@example.com"])), "larger", status_code=400)
        bom = "﻿" + _csv(["email"], ["bom@x"])
        self.upload(bom)
        self.assertEqual(TableService.rows(self.table)[0]["email"], "bom@x")
        with self.settings(PYRUNNER_TABLE_ROW_CAP=2):
            body = self.upload(_csv(["email"], ["a"], ["b"]), as_json=True).json()
        self.assertEqual((body["inserted"], body["error_count"]), (0, 1))
        self.assertIn("limit", body["errors"][0]["message"])

        class Stream(io.StringIO):
            def seekable(self):
                return False

        with self.assertRaisesMessage(TableError, "seekable"):
            csv_io.import_csv(self.table, Stream(_csv(["email"], ["a"])))

    def test_the_pages_link_import_and_export(self):
        page = self.detail("?f=score:gte:1&page=2")
        self.assertContains(page, reverse("cpanel:table_import", args=[self.table.pk]))
        self.assertContains(page, reverse("cpanel:table_export", args=[self.table.pk]) + "?f=score%3Agte%3A1")
        self.assertContains(self.client.get(reverse("cpanel:table_import", args=[self.table.pk])), "Dry run")


class HelperCsvTests(_HelperFixture):
    def test_import_and_export_through_the_loopback(self):
        import os
        import tempfile

        leads = self.pt.table("leads")
        folder = tempfile.mkdtemp()
        path = os.path.join(folder, "in.csv")
        with open(path, "w", newline="", encoding="utf-8") as fh:
            fh.write(_csv(["email", "score", "meta"], *[[f"u{i}@x", str(i), '{"i": %d}' % i] for i in range(7)]))
        with mock.patch.object(self.pt, "CSV_BATCH", 3):
            dry = leads.import_csv(path, dry_run=True)
            self.assertEqual((dry["rows"], dry["inserted"], dry["error_count"]), (7, 0, 0))
            self.assertEqual(TableRow.objects.filter(table=self.table).count(), 0)
            done = leads.import_csv(path)
            self.assertEqual(done["inserted"], 7)
            self.assertEqual(TableService.rows(self.table, order="score")[6]["meta"], {"i": 6})
            out = os.path.join(folder, "out.csv")
            self.assertEqual(leads.export_csv(out, where={"score": {"$gte": 2}}, order="score"), 5)
            with open(out, newline="", encoding="utf-8") as fh:
                lines = list(csv.reader(fh))
            self.assertEqual(lines[0][:3], ["id", "email", "score"])
            self.assertEqual([line[2] for line in lines[1:]], ["2", "3", "4", "5", "6"])
            # Errors on lines past the first batch carry their line numbers; nothing written.
            with open(path, "w", newline="", encoding="utf-8") as fh:
                fh.write(_csv(["Mail", "score"], *[["v@x", "1"]] * 4, ["w@x", "nan?"]))
            report = leads.import_csv(path, mapping={"Mail": "email", "score": "score"})
        self.assertEqual((report["error_count"], report["errors"][0]["line"], report["inserted"]), (1, 6, 0))
        self.assertEqual(TableRow.objects.filter(table=self.table).count(), 7)
        # The row limit is checked for the whole file before the first batch is written.
        with open(path, "w", newline="", encoding="utf-8") as fh:
            fh.write(_csv(["email", "score"], *[[f"w{i}@x", str(i)] for i in range(3)]))
        with self.settings(PYRUNNER_TABLE_ROW_CAP=9), mock.patch.object(self.pt, "CSV_BATCH", 2):
            report = leads.import_csv(path)
        self.assertEqual((report["error_count"], report["inserted"]), (1, 0))
        self.assertIn("9-row limit", report["errors"][0]["message"])
        self.assertEqual(TableRow.objects.filter(table=self.table).count(), 7)
        with self.assertRaises(self.pt.TableError):
            leads.import_csv(io.StringIO(""))


class OwnerPillTests(_Console):
    def test_a_provisioned_table_shows_the_plugin_pill(self):
        TableAPI("shop", workspace=self.ws).provision("orders", [{"key": "sku", "type": "text"}])
        page = self.client.get(reverse("cpanel:table_list"))
        self.assertContains(page, "shop:orders")
        self.assertContains(page, "shop")
        detail = self.client.get(reverse("cpanel:table_detail", args=[Table.objects.get(owner_plugin="shop").pk]))
        self.assertContains(detail, "Managed by the")

