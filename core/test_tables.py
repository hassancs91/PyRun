"""
Tables (2.5 T38): columns and coercion, the filter compiler on both engines,
TableService (row cap, row_count, schema changes), the ``pyrunner_tables``
helper end to end through the real loopback view, the console pages and the
shared grid, and backup/restore.
"""

import io
import json
import time
import urllib.error
from unittest import mock
from urllib.parse import urlparse

from django.db import connection
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from core.grid import Grid, GridColumn
from core.models import (
    Environment,
    GlobalSettings,
    Run,
    Script,
    Table,
    TableRow,
    User,
    Workspace,
    WorkspaceMembership,
)
from core.services.backup_service import BackupService
from core.services.datastore_token import mint_datastore_token
from core.tables.columns import clean_columns, clean_row, coerce
from core.tables.errors import TableError
from core.tables.service import TableService

COLUMNS = [
    {"key": "email", "type": "text", "required": True},
    {"key": "score", "type": "number"},
    {"key": "active", "type": "boolean", "default": True},
    {"key": "joined", "type": "date"},
    {"key": "seen", "type": "datetime"},
    {"key": "meta", "type": "json"},
    {"key": "site", "type": "link"},
]


def _admin_exists():
    """The setup wizard sends everyone to /setup/admin/ until a superuser exists."""
    User.objects.create_superuser(username="root@example.com", email="root@example.com", password="x-Pass-12345!")


def _ws():
    return Workspace.get_default() or Workspace.objects.create(name="Default", is_default=True)


class ColumnTests(SimpleTestCase):
    def test_clean_columns_normalizes_and_refuses_bad_lists(self):
        cleaned = clean_columns([{"key": "n", "type": "number", "default": "5", "extra": 1}])
        self.assertEqual(cleaned, [{"key": "n", "type": "number", "required": False, "default": 5}])
        for bad, needle in [
            ([], "at least one"),
            ([{"key": "id", "type": "text"}], "used by every row"),
            ([{"key": "a", "type": "text"}, {"key": "A", "type": "text"}], "twice"),
            ([{"key": "1a", "type": "text"}], "must start"),
            ([{"key": "a", "type": "money"}], "type must be"),
            ([{"key": "a", "type": "number", "default": "lots"}], "The default"),
            ([{"key": "a", "type": "text", "required": "yes"}], "required must"),
        ]:
            with self.subTest(bad=bad), self.assertRaisesMessage(TableError, needle):
                clean_columns(bad)

    def test_coerce_each_type(self):
        def c(kind, value):
            return coerce({"key": "k", "type": kind}, value)

        self.assertEqual(c("text", 5), "5")
        self.assertEqual(c("text", ""), "")
        self.assertEqual((c("number", "12"), c("number", " 2.5 "), c("number", 3)), (12, 2.5, 3))
        self.assertIsNone(c("number", ""))
        self.assertEqual((c("boolean", "Yes"), c("boolean", 0), c("boolean", "off")), (True, False, False))
        self.assertEqual(c("date", "2026-11-12T23:30:00-05:00"), "2026-11-12")
        self.assertEqual(c("datetime", "2026-11-12T10:30:00+02:00"), "2026-11-12T08:30:00Z")
        self.assertEqual(c("datetime", "2026-11-12"), "2026-11-12T00:00:00Z")
        self.assertEqual(c("datetime", "2026-11-12T08:30:00.123456Z"), "2026-11-12T08:30:00Z")
        self.assertEqual(c("datetime", "0999-01-02T03:04:05Z"), "0999-01-02T03:04:05Z")  # fixed width on Linux too
        self.assertEqual(c("json", {"a": [1]}), {"a": [1]})
        self.assertEqual(c("link", " https://example.com/x "), "https://example.com/x")
        for kind, value in [("number", True), ("number", "NaN"), ("number", "1,5"), ("boolean", "maybe"),
                            ("date", "12/11/2026"), ("datetime", "soon"), ("json", float("inf")),
                            ("link", "javascript:alert(1)"), ("link", "ftp://x"), ("text", [1])]:
            with self.subTest(kind=kind, value=value), self.assertRaises(TableError):
                c(kind, value)

    def test_clean_row_defaults_required_unknown_and_merge(self):
        cols = clean_columns(COLUMNS)
        self.assertEqual(clean_row(cols, {"email": "a@x"}), {"email": "a@x", "active": True})
        with self.assertRaisesMessage(TableError, "Required column without a value: email"):
            clean_row(cols, {"score": 1})
        with self.assertRaisesMessage(TableError, "Unknown column: emial"):
            clean_row(cols, {"emial": "a@x"})
        merged = clean_row(cols, {"score": None, "active": "no"}, existing={"email": "a@x", "score": 3, "gone": 1})
        self.assertEqual(merged, {"email": "a@x", "active": False})


class _TableFixture(TestCase):
    def setUp(self):
        self.ws = _ws()
        self.table = TableService.create_table(workspace=self.ws, name="Leads", columns=COLUMNS)

    def insert(self, *items):
        return TableService.insert(self.table, list(items))

    def ids(self, **kwargs):
        return [r["id"] for r in TableService.rows(self.table, limit=1000, **kwargs)]


class FilterTests(_TableFixture):
    """Every operator on SQLite and Postgres (the same assertions run on both)."""

    def setUp(self):
        super().setUp()
        rows = self.insert(
            {"email": "Ana@x.com", "score": 10, "joined": "2026-01-05", "seen": "2026-01-05T10:00:00Z",
             "meta": {"k": 1}, "site": "https://ana.example"},
            {"email": "bob@y.org", "score": 2.5, "active": False, "joined": "2025-12-31",
             "seen": "2026-01-05T09:59:59Z"},
            {"email": "cy@x.com", "score": 7},
            {"email": "dee@z.net"},
        )
        self.a, self.b, self.c, self.d = (r.id for r in rows)

    def test_equality_and_ne_includes_empty_cells(self):
        self.assertEqual(self.ids(where={"score": 7}), [self.c])
        self.assertEqual(self.ids(where={"score": "7"}), [self.c])  # coerced like a write
        self.assertEqual(self.ids(where={"email": "cy@x.com"}), [self.c])
        self.assertEqual(self.ids(where={"active": False}), [self.b])
        self.assertEqual(self.ids(where={"active": {"$ne": False}}), [self.a, self.c, self.d])
        self.assertEqual(self.ids(where={"score": {"$ne": 7}}), [self.a, self.b, self.d])
        self.assertEqual(self.ids(where={"joined": "2026-01-05"}), [self.a])

    def test_ranges_on_numbers_dates_datetimes_and_system_fields(self):
        self.assertEqual(self.ids(where={"score": {"$gt": 2.5}}), [self.a, self.c])
        self.assertEqual(self.ids(where={"score": {"$gte": 2.5, "$lt": 10}}), [self.b, self.c])
        self.assertEqual(self.ids(where={"joined": {"$gte": "2026-01-01"}}), [self.a])
        self.assertEqual(self.ids(where={"seen": {"$lt": "2026-01-05T11:00:00+01:00"}}), [self.b])
        self.assertEqual(self.ids(where={"id": {"$gt": self.b}}), [self.c, self.d])
        self.assertEqual(self.ids(where={"id": {"$in": [self.d, self.a]}}), [self.a, self.d])
        self.assertEqual(len(self.ids(where={"created_at": {"$gte": "2000-01-01"}})), 4)

    def test_text_matching_ignores_case_on_both_engines(self):
        self.assertEqual(self.ids(where={"email": {"$contains": "X.COM"}}), [self.a, self.c])
        self.assertEqual(self.ids(where={"email": {"$prefix": "ana"}}), [self.a])
        self.assertEqual(self.ids(where={"email": {"$in": ["bob@y.org", "dee@z.net"]}}), [self.b, self.d])
        self.assertEqual(self.ids(where={"site": {"$contains": "example"}}), [self.a])

    def test_empty_and_not_empty(self):
        self.assertEqual(self.ids(where={"score": None}), [self.d])
        self.assertEqual(self.ids(where={"meta": {"$empty": False}}), [self.a])
        self.assertEqual(self.ids(where={"joined": {"$empty": True}}), [self.c, self.d])

    def test_order_with_empty_cells_last_and_id_ties(self):
        self.assertEqual(self.ids(order="score"), [self.b, self.c, self.a, self.d])
        self.assertEqual(self.ids(order="-score"), [self.a, self.c, self.b, self.d])
        self.assertEqual(self.ids(order=["-active", "-id"]), [self.d, self.c, self.a, self.b])
        self.assertEqual(self.ids(order="-joined"), [self.a, self.b, self.c, self.d])

    def test_bad_filters_and_orders_are_refused_with_a_code(self):
        for where in ({"nope": 1}, {"meta": {"$gt": 1}}, {"score": {"$gt": "lots"}}, {"score": {"$like": 1}},
                      {"email": {"$in": []}}, {"id": {"$gt": "x"}}, ["score"]):
            with self.subTest(where=where), self.assertRaises(TableError) as ctx:
                self.ids(where=where)
            self.assertEqual(ctx.exception.code, "unsupported_filter")
        for order in ("meta", "nope", ["score", 3]):
            with self.subTest(order=order), self.assertRaises(TableError):
                self.ids(order=order)

    def test_column_keys_named_like_lookups_filter_as_keys(self):
        # "contains", "gt", "in", "exact", "isnull" are Django lookup names; a
        # bare data__<key> would apply the lookup to the whole row instead.
        keys = ("contains", "gt", "in", "exact", "isnull")
        table = TableService.create_table(
            workspace=self.ws, name="Lookups", columns=[{"key": k, "type": "text"} for k in keys],
        )
        one, two = TableService.insert(table, [{k: "a" for k in keys}, {"gt": "b"}])
        for key in keys:
            with self.subTest(key=key):
                found = TableService.rows(table, where={key: "a"})
                self.assertEqual([r["id"] for r in found], [one.id])
                self.assertEqual([r["id"] for r in TableService.rows(table, where={key: {"$ne": "a"}})], [two.id])
                expected = [one.id] if key != "gt" else [one.id, two.id]
                self.assertEqual([r["id"] for r in TableService.rows(table, where={key: {"$empty": False}})], expected)
        row, created = TableService.upsert(table, "in", {"in": "a", "exact": "z"})
        self.assertEqual((row.id, created), (one.id, False))


class ServiceTests(_TableFixture):
    def test_insert_update_upsert_delete_keep_row_count_exact(self):
        first, second = self.insert({"email": "a@x", "score": "4"}, {"email": "b@x"})
        self.assertEqual(Table.objects.get(pk=self.table.pk).row_count, 2)
        self.assertEqual(first.data, {"email": "a@x", "score": 4, "active": True})
        row = TableService.update_row(self.table, first.id, {"score": None, "meta": [1, 2]})
        self.assertEqual(row.data, {"email": "a@x", "active": True, "meta": [1, 2]})
        self.assertGreaterEqual(row.updated_at, row.created_at)
        updated, created = TableService.upsert(self.table, "email", {"email": "b@x", "score": 9})
        self.assertEqual((updated.id, created, updated.data["score"]), (second.id, False, 9))
        fresh, created = TableService.upsert(self.table, "email", {"email": "c@x"})
        self.assertTrue(created)
        self.assertEqual(TableService.count(self.table), 3)
        self.assertEqual(TableService.delete_rows(self.table, [first.id, 999999]), 1)
        self.assertEqual(Table.objects.get(pk=self.table.pk).row_count, 2)
        self.assertEqual(TableService.count(self.table, where={"score": 9}), 1)
        with self.assertRaises(TableError) as ctx:
            TableService.get_row(self.table, first.id)
        self.assertEqual(ctx.exception.code, "row_not_found")

    def test_a_bad_row_in_a_batch_writes_nothing(self):
        with self.assertRaisesMessage(TableError, "Row 2: Column \"score\" expects a number"):
            self.insert({"email": "a@x"}, {"email": "b@x", "score": "many"})
        self.assertEqual((TableRow.objects.count(), Table.objects.get(pk=self.table.pk).row_count), (0, 0))

    def test_oversized_row_is_refused(self):
        with self.assertRaises(TableError) as ctx:
            self.insert({"email": "a", "meta": "x" * (300 * 1024)})
        self.assertEqual(ctx.exception.code, "too_large")

    @override_settings(PYRUNNER_TABLE_ROW_CAP=3)
    def test_row_cap_is_enforced_all_or_nothing(self):
        self.insert({"email": "a"}, {"email": "b"})
        with self.assertRaises(TableError) as ctx:
            self.insert({"email": "c"}, {"email": "d"})
        self.assertEqual(ctx.exception.code, "row_cap")
        self.assertIn("managed Database", ctx.exception.message)
        self.assertEqual(TableRow.objects.filter(table=self.table).count(), 2)
        self.insert({"email": "c"})
        table = Table.objects.get(pk=self.table.pk)
        self.assertEqual(table.row_count, 3)
        self.assertTrue(table.at_cap)
        TableService.delete_rows(self.table, [TableRow.objects.first().id])
        self.insert({"email": "e"})

    def test_schema_change_converts_removes_and_refuses(self):
        a, b = self.insert({"email": "a", "score": 3, "joined": "2026-02-01"}, {"email": "b", "score": 4.5})
        cols = [dict(c) for c in COLUMNS]
        next(c for c in cols if c["key"] == "score")["type"] = "text"
        cols = [c for c in cols if c["key"] != "joined"]
        TableService.update_table(self.table, columns=cols)
        self.assertEqual(TableRow.objects.get(pk=a.id).data, {"email": "a", "score": "3", "active": True})
        self.assertEqual(TableRow.objects.get(pk=b.id).data["score"], "4.5")
        TableService.update_row(self.table, b.id, {"score": "lots"})
        back = [dict(c, type="number") if c["key"] == "score" else c for c in cols]
        with self.assertRaisesMessage(TableError, f"row {b.id} (score)"):
            TableService.update_table(self.table, columns=back)
        self.table.refresh_from_db()
        self.assertEqual(TableRow.objects.get(pk=a.id).data["score"], "3")  # nothing written
        required = [dict(c, required=True) if c["key"] == "site" else c for c in cols]
        with self.assertRaisesMessage(TableError, "2 rows have no value"):
            TableService.update_table(self.table, columns=required)

    def test_names_and_slugs_are_unique_per_workspace(self):
        with self.assertRaises(TableError):
            TableService.create_table(workspace=self.ws, name="Leads", slug="other", columns=COLUMNS)
        with self.assertRaises(TableError):
            TableService.create_table(workspace=self.ws, name="Other", slug="leads", columns=COLUMNS)
        other_ws = Workspace.objects.create(name="Other")
        TableService.create_table(workspace=other_ws, name="Leads", columns=COLUMNS)
        self.assertEqual(Table.resolve_for_workspace("Leads", self.ws.pk), self.table)
        self.assertEqual(Table.resolve_for_workspace("leads", other_ws.pk).workspace, other_ws)
        with self.assertRaises(Table.DoesNotExist):
            Table.resolve_for_workspace("leads", None)

    def test_update_row_rereads_the_row_so_other_cells_survive(self):
        (row,) = self.insert({"email": "a@x.com"})
        stale = TableRow.objects.get(pk=row.pk)
        TableService.update_row(self.table, row.pk, {"score": 3})
        TableService.update_row(self.table, None, {"joined": "2026-01-01"}, row=stale)
        self.assertEqual(TableRow.objects.get(pk=row.pk).data,
                         {"email": "a@x.com", "active": True, "score": 3, "joined": "2026-01-01"})
        with self.assertRaises(TableError) as ctx:
            TableService.update_row(self.table, 999999, {"score": 1})
        self.assertEqual(ctx.exception.code, "row_not_found")

    def test_the_service_checks_names_and_slugs_for_every_door(self):
        # The console form checks these too; the helper/SDK doors call the service directly.
        for name, slug in (("   ", ""), ("Ok", "Has Space"), ("Ok", "UPPER"), ("Ok", "-lead"), ("x" * 101, "")):
            with self.subTest(name=name, slug=slug), self.assertRaises(TableError) as ctx:
                TableService.create_table(workspace=self.ws, name=name, slug=slug, columns=COLUMNS)
            self.assertEqual(ctx.exception.code, "invalid")
        self.assertEqual(TableService.create_table(workspace=self.ws, name="Mes Clés", columns=COLUMNS).slug,
                         "mes-cles")
        with self.assertRaises(TableError):
            TableService.update_table(self.table, slug="bad slug")
        with self.assertRaises(TableError):
            TableService.update_table(self.table, name=" ")
        self.table.refresh_from_db()
        self.assertEqual((self.table.name, self.table.slug), ("Leads", "leads"))


class _Body:
    def __init__(self, content):
        self._content = content

    def read(self):
        return self._content

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@override_settings(ALLOWED_HOSTS=["*"])
class _HelperFixture(_TableFixture):
    """pyrunner_tables end to end through the real loopback view."""

    def setUp(self):
        super().setUp()
        from core.script_helpers import pyrunner_tables

        self.pt = pyrunner_tables
        env = Environment.objects.create(name="tables-env", path="/tmp/tables-env")
        self.script = Script.objects.create(name="t", code="", environment=env, workspace=self.ws)
        self.run = Run.objects.create(script=self.script, workspace=self.ws)
        self.requests = 0
        env_patch = mock.patch.dict("os.environ", {
            "PYRUNNER_INTERNAL_URL": "http://127.0.0.1:8000",
            "PYRUNNER_INTERNAL_TOKEN": mint_datastore_token(self.run.id),
        })
        env_patch.start()
        self.addCleanup(env_patch.stop)
        route = mock.patch.object(self.pt.urllib.request, "urlopen", side_effect=self._route)
        route.start()
        self.addCleanup(route.stop)

    def _route(self, req, timeout=None):
        self.requests += 1
        resp = self.client.post(
            urlparse(req.full_url).path, data=req.data, content_type="application/json",
            REMOTE_ADDR="127.0.0.1", HTTP_AUTHORIZATION=req.get_header("Authorization"),
        )
        if resp.status_code >= 400:
            raise urllib.error.HTTPError(req.full_url, resp.status_code, "error", {}, io.BytesIO(resp.content))
        return _Body(resp.content)


class HelperTests(_HelperFixture):
    def test_the_whole_surface(self):
        leads = self.pt.table("Leads")  # by name; calls then use the slug
        self.assertEqual((leads.slug, [c["key"] for c in leads.columns][:2]), ("leads", ["email", "score"]))
        row = leads.insert({"email": "a@x", "score": "7", "joined": "2026-03-04T05:06:07Z"})
        self.assertEqual((row["score"], row["joined"], row["active"], row["meta"]), (7, "2026-03-04", True, None))
        self.assertTrue(row["created_at"].endswith("Z"))
        ids = leads.insert([{"email": f"u{i}@x", "score": i} for i in range(3)])
        self.assertEqual(len(ids), 3)
        self.assertEqual([r["email"] for r in leads.rows(where={"score": {"$lt": 2}}, order="-score")],
                         ["u1@x", "u0@x"])
        self.assertEqual(leads.rows(limit=1, offset=1)[0]["id"], ids[0])
        self.assertEqual(leads.get(row["id"])["email"], "a@x")
        self.assertIsNone(leads.get(999999))
        self.assertEqual(leads.update(row["id"], {"score": None})["score"], None)
        self.assertEqual(leads.upsert("email", {"email": "a@x", "score": 1})["id"], row["id"])
        self.assertNotEqual(leads.upsert("email", {"email": "new@x"})["id"], row["id"])
        self.assertEqual(leads.count(), 5)
        self.assertEqual(leads.count(where={"score": 1}), 2)
        self.assertTrue(leads.delete(row["id"]))
        self.assertFalse(leads.delete(row["id"]))
        self.assertEqual(Table.objects.get(pk=self.table.pk).row_count, 4)

    def test_errors_carry_codes(self):
        with self.assertRaises(self.pt.TableNotFound) as ctx:
            self.pt.table("nope")
        self.assertIsInstance(ctx.exception, LookupError)
        leads = self.pt.table("leads")
        for call, code, status in [
            (lambda: leads.insert({"email": "a", "bogus": 1}), "invalid", 400),
            (lambda: leads.rows(where={"score": {"$like": 1}}), "unsupported_filter", 400),
            (lambda: leads.rows(limit=5000), "invalid", 400),
            (lambda: leads.update(424242, {"score": 1}), "row_not_found", 404),
        ]:
            with self.subTest(code=code), self.assertRaises(self.pt.TableError) as ctx:
                call()
            self.assertEqual((ctx.exception.code, ctx.exception.status), (code, status))
        self.assertIsInstance(ctx.exception, self.pt.RowNotFound)
        with override_settings(PYRUNNER_TABLE_ROW_CAP=1):
            leads.insert({"email": "a"})
            with self.assertRaises(self.pt.TableError) as ctx:
                leads.insert({"email": "b"})
        self.assertEqual((ctx.exception.code, ctx.exception.status), ("row_cap", 409))

    def test_scope_is_the_runs_workspace_and_plugin_tables_stay_private(self):
        other = Workspace.objects.create(name="Elsewhere")
        TableService.create_table(workspace=other, name="Secret", columns=COLUMNS)
        TableService.create_table(workspace=self.ws, name="Shop orders", slug="shop-orders", columns=COLUMNS,
                                  owner_plugin="shop", owner_key="orders")
        for name in ("secret", "shop-orders"):
            with self.subTest(name=name), self.assertRaises(self.pt.TableNotFound):
                self.pt.table(name)
        Script.objects.filter(pk=self.script.pk).update(owner_plugin="shop")
        self.assertEqual(self.pt.table("shop-orders").slug, "shop-orders")

    def test_the_loopback_refuses_strangers(self):
        url = reverse("internal:tables_op", args=["leads", "count"])
        self.assertEqual(self.client.post(url, "{}", content_type="application/json", REMOTE_ADDR="203.0.113.9",
                                          HTTP_AUTHORIZATION="Bearer x").status_code, 403)
        self.assertEqual(self.client.post(url, "{}", content_type="application/json", REMOTE_ADDR="127.0.0.1",
                                          HTTP_AUTHORIZATION="Bearer nope").status_code, 401)
        token = "Bearer " + mint_datastore_token(self.run.id)
        resp = self.client.post(reverse("internal:tables_op", args=["leads", "drop"]), "{}",
                                content_type="application/json", REMOTE_ADDR="127.0.0.1", HTTP_AUTHORIZATION=token)
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post(reverse("internal:tables_op", args=["leads", "insert"]),
                                json.dumps({"rows": [{"email": "x"}] * 1001}), content_type="application/json",
                                REMOTE_ADDR="127.0.0.1", HTTP_AUTHORIZATION=token)
        self.assertEqual(resp.status_code, 413)


class _Console(_TableFixture):
    def setUp(self):
        super().setUp()
        gs = GlobalSettings.get_settings()
        gs.setup_completed = True
        gs.save()
        Environment.objects.create(name="tenv", path="/tmp/tenv", is_default=True)
        _admin_exists()
        self.user = User.objects.create_user(username="t@example.com", email="t@example.com", password="x-Pass-12345!")
        WorkspaceMembership.ensure(self.user, self.ws, role=WorkspaceMembership.ROLE_MEMBER)
        self.client.force_login(self.user)

    def detail(self, query=""):
        return self.client.get(reverse("cpanel:table_detail", args=[self.table.pk]) + query)


class PerformanceTests(_HelperFixture):
    """The acceptance check: 10k rows through the helper, then a grid filter in under a second."""

    def test_ten_thousand_rows_then_filter_in_the_grid(self):
        leads = self.pt.table("leads")
        ids = leads.insert([{"email": f"user{i}@x.com", "score": i % 100, "joined": f"2026-01-{i % 28 + 1:02d}"}
                            for i in range(10_000)])
        self.assertEqual((len(ids), self.requests), (10_000, 1 + 10_000 // self.pt.INSERT_BATCH))
        self.assertEqual(Table.objects.get(pk=self.table.pk).row_count, 10_000)
        user = User.objects.create_user(username="p@example.com", email="p@example.com", password="x-Pass-12345!")
        WorkspaceMembership.ensure(user, self.ws, role=WorkspaceMembership.ROLE_MEMBER)
        gs = GlobalSettings.get_settings()
        gs.setup_completed = True
        gs.save()
        Environment.objects.create(name="penv", path="/tmp/penv", is_default=True)
        _admin_exists()
        self.client.force_login(user)
        url = reverse("cpanel:table_detail", args=[self.table.pk]) + "?f=score:gte:90&f=email:contains:9&sort=-score"
        started = time.monotonic()
        resp = self.client.get(url)
        elapsed = time.monotonic() - started
        self.assertEqual(resp.status_code, 200)
        expected = sum(1 for i in range(10_000) if i % 100 >= 90 and "9" in f"user{i}@x.com")
        self.assertEqual(resp.context["grid"].total, expected)
        if connection.vendor == "sqlite":
            self.assertLess(elapsed, 1.0)


class ConsoleTests(_Console):
    def test_list_create_edit_and_delete(self):
        resp = self.client.get(reverse("cpanel:table_list"))
        self.assertContains(resp, "Leads")
        resp = self.client.post(reverse("cpanel:table_create"), {
            "name": "Reading list", "slug": "", "description": "Books",
            "columns": json.dumps([{"key": "title", "type": "text", "required": True}, {"key": "pages", "type": "number"}]),
        })
        table = Table.objects.get(name="Reading list")
        self.assertRedirects(resp, reverse("cpanel:table_detail", args=[table.pk]))
        self.assertEqual((table.slug, table.created_by, table.workspace), ("reading-list", self.user, self.ws))
        resp = self.client.post(reverse("cpanel:table_create"), {
            "name": "reading LIST", "columns": json.dumps([{"key": "id", "type": "text"}]),
        })
        self.assertContains(resp, "already has a table with this name")
        self.assertContains(resp, "used by every row")
        TableService.insert(table, [{"title": "Dune", "pages": "412"}])
        resp = self.client.post(reverse("cpanel:table_edit", args=[table.pk]), {
            "name": "Reading list", "slug": "books", "description": "",
            "columns": json.dumps([{"key": "title", "type": "text"}, {"key": "pages", "type": "text"}]),
        })
        self.assertRedirects(resp, reverse("cpanel:table_detail", args=[table.pk]))
        table.refresh_from_db()
        self.assertEqual((table.slug, table.rows.get().data["pages"]), ("books", "412"))
        resp = self.client.post(reverse("cpanel:table_edit", args=[table.pk]), {
            "name": "Reading list", "slug": "books",
            "columns": json.dumps([{"key": "title", "type": "text"}, {"key": "pages", "type": "boolean"}]),
        })
        self.assertContains(resp, "can&#x27;t be converted")
        self.client.post(reverse("cpanel:table_delete", args=[table.pk]))
        self.assertFalse(Table.objects.filter(pk=table.pk).exists())

    def test_grid_sorts_filters_pages_and_reports_bad_params(self):
        TableService.insert(self.table, [{"email": f"u{i:02d}@x", "score": i} for i in range(60)])
        resp = self.detail()
        grid = resp.context["grid"]
        self.assertEqual((grid.total, len(grid.rows), grid.page_obj.paginator.num_pages), (60, 50, 2))
        self.assertContains(resp, 'data-cell-url="')
        self.assertContains(resp, "js/data_grid.")
        grid = self.detail("?sort=-score&page=2").context["grid"]
        self.assertEqual([r.id for r in grid.rows][-1], TableRow.objects.order_by("id").first().id)
        resp = self.detail("?f=score:lt:3&f=email:prefix:U0&sort=score")
        grid = resp.context["grid"]
        self.assertEqual(grid.total, 3)
        self.assertContains(resp, "score &lt; 3")
        self.assertContains(resp, "email starts with U0")
        grid = self.detail("?fc=score&fo=gte&fv=58").context["grid"]  # the no-JS add-filter form
        self.assertEqual(grid.total, 2)
        resp = self.detail("?f=score:gt:lots&f=nope:eq:1&f=meta:gt:1&sort=meta&page=99")
        grid = resp.context["grid"]
        self.assertEqual((grid.total, grid.page_obj.number), (60, 2))
        problems = " ".join(grid.problems)
        for needle in ("nope:eq:1", "meta:gt:1", 'sort by "meta"', 'expects a number'):
            self.assertIn(needle, problems)

    def test_cell_edit_add_row_and_delete_row(self):
        row = TableService.insert(self.table, [{"email": "a@x"}])[0]
        url = reverse("cpanel:table_row_cell", args=[self.table.pk, row.id])
        resp = self.client.post(url, json.dumps({"column": "score", "value": "41.5"}), content_type="application/json")
        self.assertEqual((resp.status_code, resp.json()["raw"]), (200, "41.5"))
        resp = self.client.post(url, json.dumps({"column": "site", "value": "https://e.x/?a=<b>"}),
                                content_type="application/json")
        self.assertIn('href="https://e.x/?a=&lt;b&gt;"', resp.json()["html"])
        resp = self.client.post(url, json.dumps({"column": "meta", "value": {"tags": ["a"]}}),
                                content_type="application/json")
        self.assertEqual(json.loads(resp.json()["raw"]), {"tags": ["a"]})
        for body, status in [({"column": "score", "value": "x"}, 400), ({"column": "nope", "value": 1}, 400),
                             ({"column": "email", "value": None}, 400)]:
            with self.subTest(body=body):
                resp = self.client.post(url, json.dumps(body), content_type="application/json")
                self.assertEqual(resp.status_code, status)
                self.assertIn("error", resp.json())
        missing = reverse("cpanel:table_row_cell", args=[self.table.pk, 999999])
        self.assertEqual(self.client.post(missing, json.dumps({"column": "score", "value": 1}),
                                          content_type="application/json").status_code, 404)
        self.assertEqual(TableRow.objects.get(pk=row.id).data,
                         {"email": "a@x", "active": True, "score": 41.5, "site": "https://e.x/?a=<b>",
                          "meta": {"tags": ["a"]}})

        resp = self.client.post(reverse("cpanel:table_row_add", args=[self.table.pk]), {
            "c_email": "b@x", "c_score": "", "c_active": "false", "c_meta": '{"x": 1}', "c_joined": "2026-05-06",
        })
        added = TableRow.objects.latest("id")
        self.assertRedirects(resp, reverse("cpanel:table_detail", args=[self.table.pk]) + "?sort=-id")
        self.assertEqual(added.data, {"email": "b@x", "active": False, "meta": {"x": 1}, "joined": "2026-05-06"})
        self.assertEqual(added.created_by, self.user)
        resp = self.client.post(reverse("cpanel:table_row_add", args=[self.table.pk]), {"c_score": "1"}, follow=True)
        self.assertContains(resp, "Required column without a value: email")
        resp = self.client.post(reverse("cpanel:table_row_delete", args=[self.table.pk, added.id]),
                                {"next": "?sort=-score&page=2"})
        self.assertRedirects(resp, reverse("cpanel:table_detail", args=[self.table.pk]) + "?sort=-score&page=2",
                             fetch_redirect_response=False)
        self.assertEqual(Table.objects.get(pk=self.table.pk).row_count, 1)

    @override_settings(PYRUNNER_TABLE_ROW_CAP=10)
    def test_cap_banners(self):
        TableService.insert(self.table, [{"email": f"{i}"} for i in range(9)])
        resp = self.detail()
        self.assertContains(resp, "9 of its 10 rows (90%)")
        TableService.insert(self.table, [{"email": "last"}])
        resp = self.detail()
        self.assertContains(resp, "This table is full")
        self.assertContains(resp, reverse("cpanel:database_list"))
        self.assertNotContains(resp, "Add a row")

    def test_other_workspaces_tables_are_404_and_plugin_tables_are_protected(self):
        other = TableService.create_table(workspace=Workspace.objects.create(name="Other"), name="X", columns=COLUMNS)
        for name in ("table_detail", "table_edit"):
            self.assertEqual(self.client.get(reverse(f"cpanel:{name}", args=[other.pk])).status_code, 404)
        row = TableService.insert(other, [{"email": "a"}])[0]
        self.assertEqual(self.client.post(reverse("cpanel:table_row_cell", args=[other.pk, row.id]), "{}",
                                          content_type="application/json").status_code, 404)
        plugin = TableService.create_table(workspace=self.ws, name="P", columns=COLUMNS, owner_plugin="shop",
                                           owner_key="p")
        resp = self.client.get(reverse("cpanel:table_list"))
        self.assertContains(resp, "Plugin-managed")
        self.client.post(reverse("cpanel:table_delete", args=[plugin.pk]))
        self.assertTrue(Table.objects.filter(pk=plugin.pk).exists())
        self.client.post(reverse("cpanel:table_edit", args=[plugin.pk]), {
            "name": "P2", "slug": "p", "columns": json.dumps([{"key": "only", "type": "text"}]),
        })
        plugin.refresh_from_db()
        self.assertEqual((plugin.name, len(plugin.columns)), ("P2", len(COLUMNS)))

    def test_sidebar_links_tables(self):
        resp = self.client.get(reverse("cpanel:table_list"))
        self.assertContains(resp, f'href="{reverse("cpanel:table_list")}"')


class GridTests(SimpleTestCase):
    def grid(self, query, **kwargs):
        request = RequestFactory().get("/x" + query)
        columns = [GridColumn("id", "id", "id"), GridColumn("name", "Name"), GridColumn("n", "N", "number"),
                   GridColumn("j", "J", "json", sortable=False)]
        return Grid.from_request(request, columns, default_sort="id", **kwargs)

    def test_state_links_and_chips(self):
        grid = self.grid("?sort=-n&f=name:contains:a%20b&f=n:empty:&page=3")
        self.assertEqual((grid.sort, grid.page_number, grid.offset), ("-n", 3, 100))
        self.assertEqual([f.param for f in grid.filters], ["name:contains:a b", "n:empty:"])
        headers = {h["column"].key: h for h in grid.headers}
        self.assertEqual(headers["n"]["sorted"], "desc")
        self.assertIn("sort=n&", headers["n"]["sort_qs"])
        self.assertEqual(headers["j"]["sort_qs"], "")
        self.assertEqual([c["label"] for c in grid.chips], ["Name contains a b", "N is empty"])
        self.assertNotIn("contains", grid.chips[0]["remove_qs"])
        self.assertNotIn("page=", grid.querystring)
        grid.clamp_page(120)
        grid.set_rows([], 120)
        self.assertEqual(grid.page_obj.number, 3)

    def test_bad_params_become_problems(self):
        grid = self.grid("?sort=j&f=j:gt:1&f=zz:eq:1&f=name:eq:&page=x")
        self.assertEqual((grid.sort, grid.filters, grid.page_number), ("id", [], 1))
        self.assertEqual(len(grid.problems), 4)


class BackupTests(_TableFixture):
    def test_round_trip_keeps_ids_data_times_and_count(self):
        user = User.objects.create(email="b@example.com", username="b")
        rows = TableService.insert(self.table, [{"email": "a", "meta": {"z": [1, {"y": 2}]}}, {"email": "b"}], user=user)
        TableService.delete_rows(self.table, [rows[1].id])
        plugin = TableService.create_table(workspace=self.ws, name="P", columns=COLUMNS, owner_plugin="shop",
                                           owner_key="p")
        data = json.loads(json.dumps(BackupService.create_backup(include_runs=False)))
        exported = next(t for t in data["tables"] if t["name"] == "Leads")
        self.assertEqual([r["id"] for r in exported["rows"]], [rows[0].id])
        preview = BackupService.get_backup_preview(data)
        self.assertEqual((preview["counts"]["tables"], preview["counts"]["table_rows"]), (2, 1))

        Table.objects.all().delete()
        result = BackupService.restore_backup(data, restore_runs=False, current_user=user)
        self.assertTrue(result["success"], result)
        self.assertEqual((result["counts"]["tables"], result["counts"]["table_rows"]), (2, 1))
        table = Table.objects.get(pk=self.table.pk)
        self.assertEqual((table.slug, table.columns, table.row_count, table.workspace), (
            "leads", self.table.columns, 1, self.ws))
        row = TableRow.objects.get(pk=rows[0].id)
        self.assertEqual((row.data, row.created_by, row.created_at), (rows[0].data, user, rows[0].created_at))
        self.assertEqual(table.created_at, self.table.created_at)
        restored_plugin = Table.objects.get(pk=plugin.pk)
        self.assertEqual((restored_plugin.owner_plugin, restored_plugin.owner_key), ("shop", "p"))
        # The id sequence moved past the restored ids (Postgres).
        self.assertGreater(TableService.insert(table, [{"email": "c"}])[0].id, rows[1].id)

    def test_a_backup_without_tables_leaves_them_alone(self):
        TableService.insert(self.table, [{"email": "a"}])
        data = json.loads(json.dumps(BackupService.create_backup(include_runs=False)))
        data.pop("tables")
        result = BackupService.restore_backup(data, restore_runs=False, current_user=None)
        self.assertTrue(result["success"], result)
        self.assertNotIn("tables", result["counts"])
        self.assertEqual(Table.objects.get(pk=self.table.pk).rows.count(), 1)
