"""
T41: a managed database's table in the shared grid, and vector points paged + searched.

- Databases → table view: the shared grid, read-only, through the database's
  own role in a READ ONLY session. Filters compile to parameterised SQL per
  column type (``grid_condition``), sort by a sortable column (the one-column
  primary key by default, ties broken by it), pages of 50 counted up to
  GRID_COUNT_CAP, text/JSON cells cut to GRID_CELL_CHARS. CSV export takes the
  grid's filters and sort, up to CSV_ROW_CAP. A statement past the viewer
  timeout is ``QueryTimeout`` and the page shows it instead of the rows.
- Vectors → collection detail: points paged by the store's cursor
  (``?after=&n=``), a long payload opening in full, and the search box runs
  ``vectors.search``'s own operation within its budget.

Unit tests use a fake connection; ``LiveGridTests`` run against the test
database's own Postgres (skipped on SQLite, where a data server isn't there).
"""

import datetime
import decimal
import urllib.parse
from unittest import mock, skipUnless

from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from core.models import Database, User, VectorCollection, VectorConnection, Workspace, WorkspaceMembership
from core.services.database_explorer import (
    CSV_ROW_CAP,
    GRID_CELL_CHARS,
    GRID_MARKER,
    DatabaseExplorerError,
    DatabaseExplorerService,
    GridPage,
    QueryResult,
    QueryTimeout,
    grid_condition,
    grid_kind,
    grid_sortable,
    is_viewer_statement,
)
from core.test_databases import _SERVER_URL, _TEST_KEY, _FakeScopedConn, _make_database, _setup_wizard_off
from core.test_vectors import _TMP, _provider, fake_embeddings
from core.vectors.local_store import clear_cache
from core.vectors.service import VectorService

SVC = DatabaseExplorerService


def _col(name, data_type, primary=False):
    return {"name": name, "type": data_type, "nullable": True, "default": "", "primary": primary}


def _sql(composable) -> str:
    return composable.as_string(None)


COLUMNS = [
    _col("id", "integer", primary=True),
    _col("name", "text"),
    _col("score", "numeric"),
    _col("active", "boolean"),
    _col("born", "date"),
    _col("seen", "timestamp with time zone"),
    _col("local_seen", "timestamp without time zone"),
    _col("meta", "jsonb"),
    _col("doc", "json"),
    _col("tags", "ARRAY"),
]
BY_NAME = {c["name"]: c for c in COLUMNS}


# --------------------------------------------------------------------------- conditions


class GridConditionTests(SimpleTestCase):
    def cond(self, name, op, value=""):
        clause, params = grid_condition(BY_NAME[name], op, value)
        return _sql(clause), params

    def test_kinds_and_sortable(self):
        kinds = {c["name"]: grid_kind(c["type"]) for c in COLUMNS}
        self.assertEqual(kinds, {
            "id": "number", "name": "text", "score": "number", "active": "boolean", "born": "date",
            "seen": "datetime", "local_seen": "datetime", "meta": "json", "doc": "json", "tags": "text",
        })
        self.assertEqual(grid_kind("uuid"), "text")
        self.assertTrue(grid_sortable("jsonb"))
        self.assertTrue(grid_sortable("uuid"))
        # No ordering operator: json, arrays (of any element), custom types.
        for data_type in ("json", "ARRAY", "USER-DEFINED", "point"):
            self.assertFalse(grid_sortable(data_type), data_type)

    def test_text_compares_its_text(self):
        self.assertEqual(self.cond("name", "eq", "Ann"), ('"name"::text = %s', ["Ann"]))
        self.assertEqual(self.cond("name", "ne", "Ann"), ('"name"::text IS DISTINCT FROM %s', ["Ann"]))
        self.assertEqual(self.cond("name", "in", " a, b ,,c"), ('"name"::text = ANY(%s)', [["a", "b", "c"]]))
        self.assertEqual(self.cond("tags", "contains", "x"), ('"tags"::text ILIKE %s', ["%x%"]))

    def test_like_wildcards_are_literal(self):
        self.assertEqual(self.cond("name", "contains", "50%_a\\b"), ('"name"::text ILIKE %s', ["%50\\%\\_a\\\\b%"]))
        self.assertEqual(self.cond("name", "prefix", "a_"), ('"name"::text ILIKE %s', ["a\\_%"]))

    def test_empty_text_is_null_or_blank(self):
        self.assertEqual(self.cond("name", "empty"), ("(\"name\" IS NULL OR \"name\"::text = '')", []))
        self.assertEqual(self.cond("name", "not_empty"), ("(\"name\" IS NOT NULL AND \"name\"::text <> '')", []))
        self.assertEqual(self.cond("meta", "empty"), ('"meta" IS NULL', []))
        self.assertEqual(self.cond("score", "not_empty"), ('"score" IS NOT NULL', []))

    def test_numbers_are_exact_decimals(self):
        self.assertEqual(self.cond("score", "gt", "1.50"), ('"score" > %s', [decimal.Decimal("1.50")]))
        # Past 64 bits is still a number (Postgres compares it as numeric, no overflow).
        big = "123456789012345678901234567890"
        self.assertEqual(self.cond("id", "eq", big), ('"id" = %s', [decimal.Decimal(big)]))
        self.assertEqual(self.cond("id", "in", "1, 2"), ('"id" = ANY(%s)', [[decimal.Decimal(1), decimal.Decimal(2)]]))
        for bad in ("abc", "NaN", "Infinity", "1e999", ""):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, '"score" compares with a number'):
                self.cond("score", "eq", bad)

    def test_booleans_dates_and_times(self):
        self.assertEqual(self.cond("active", "eq", "Yes"), ('"active" = %s', [True]))
        self.assertEqual(self.cond("active", "ne", "false"), ('"active" IS DISTINCT FROM %s', [False]))
        with self.assertRaisesRegex(ValueError, "true or false"):
            self.cond("active", "eq", "maybe")
        self.assertEqual(self.cond("born", "gte", "2026-01-31"), ('"born" >= %s', [datetime.date(2026, 1, 31)]))
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            self.cond("born", "eq", "31/01/2026")
        _, (aware,) = self.cond("seen", "lt", "2026-11-12T10:00:00+02:00")
        self.assertEqual(aware, datetime.datetime(2026, 11, 12, 8, tzinfo=datetime.timezone.utc))
        # A zone-less column holds wall times: compare with the UTC wall time.
        _, (naive,) = self.cond("local_seen", "lt", "2026-11-12T10:00:00+02:00")
        self.assertEqual(naive, datetime.datetime(2026, 11, 12, 8))
        with self.assertRaisesRegex(ValueError, "date and time"):
            self.cond("seen", "eq", "soon")

    def test_in_takes_at_most_a_hundred_values(self):
        with self.assertRaisesRegex(ValueError, "at most 100"):
            self.cond("name", "in", ",".join(str(i) for i in range(101)))
        with self.assertRaisesRegex(ValueError, "separated by commas"):
            self.cond("name", "in", " , ")

    def test_identifiers_are_quoted(self):
        clause, _ = grid_condition(_col('we"ird name', "text"), "eq", "x")
        self.assertEqual(_sql(clause), '"we""ird name"::text = %s')

    def test_grid_statements_are_left_out_of_slow_queries(self):
        self.assertTrue(is_viewer_statement(f'{GRID_MARKER} SELECT count(*) FROM (SELECT 1 FROM "t" WHERE "a" > $1 LIMIT $2) AS matching'))
        self.assertFalse(is_viewer_statement('SELECT count(*) FROM "t" WHERE "a" > $1'))


# --------------------------------------------------------------------------- service (fake connection)


@override_settings(PYRUNNER_DATA_DB_URL=_SERVER_URL, ENCRYPTION_KEY=_TEST_KEY)
class GridPageTests(TestCase):
    def setUp(self):
        self.db = _make_database(Workspace.objects.create(name="W"))

    def page(self, plan, **kwargs):
        conn = _FakeScopedConn(plan)
        with mock.patch.object(SVC, "_scoped_connect", return_value=conn):
            result = SVC.grid_page(self.db, "items", COLUMNS[:3], **kwargs)
        return conn, result

    def test_one_read_only_session_counts_then_reads_a_page(self):
        plan = [("count(*)", ["count"], [(120,)]), ("OFFSET", ["id", "name", "score"], [(51, "a", 1)])]
        conn, result = self.page(plan, conditions=[grid_condition(BY_NAME["name"], "contains", "a")],
                                 sort="-score", page=2)
        self.assertIs(conn.read_only, True)
        executed = conn.cur.executed
        self.assertIn("statement_timeout", executed[0][0])
        count_sql, count_params = executed[1]
        page_sql, page_params = executed[2]
        self.assertTrue(count_sql.startswith(GRID_MARKER) and page_sql.startswith(GRID_MARKER))
        self.assertIn('FROM "items" WHERE "name"::text ILIKE %s LIMIT %s', count_sql)
        self.assertEqual(count_params, ["%a%", 100_001])
        self.assertIn('left("name"::text, 301)', page_sql)
        self.assertIn('ORDER BY "items"."score" DESC, "items"."id" LIMIT %s OFFSET %s', page_sql)
        self.assertEqual(page_params, ["%a%", 50, 50])
        self.assertEqual((result.rows, result.total, result.capped, result.page), ([(51, "a", 1)], 120, False, 2))

    def test_past_the_end_page_moves_to_the_last(self):
        plan = [("count(*)", ["count"], [(120,)]), ("OFFSET", ["id"], [])]
        conn, result = self.page(plan, page=99)
        self.assertEqual(result.page, 3)
        self.assertEqual(conn.cur.executed[-1][1], [50, 100])

    def test_count_stops_at_the_cap(self):
        plan = [("count(*)", ["count"], [(100_001,)]), ("OFFSET", ["id"], [])]
        _, result = self.page(plan)
        self.assertEqual((result.total, result.capped), (100_000, True))

    def test_unsortable_column_and_no_sort_read_stored_order(self):
        plan = [("count(*)", ["count"], [(1,)]), ("OFFSET", ["id"], [])]
        for sort in ("", "doc", "-nope"):
            conn = _FakeScopedConn(plan)
            with mock.patch.object(SVC, "_scoped_connect", return_value=conn):
                SVC.grid_page(self.db, "items", COLUMNS, sort=sort)
            self.assertNotIn("ORDER BY", conn.cur.executed[-1][0], sort)

    def test_csv_reads_full_values_up_to_the_cap(self):
        rows = [(i, "x") for i in range(CSV_ROW_CAP + 1)]
        conn = _FakeScopedConn([("LIMIT", ["id", "name"], rows)])
        with mock.patch.object(SVC, "_scoped_connect", return_value=conn):
            result = SVC.grid_csv(self.db, "items", COLUMNS[:2], sort="name")
        text, params = conn.cur.executed[-1]
        self.assertNotIn("left(", text)
        self.assertIn('ORDER BY "items"."name" ASC, "items"."id" LIMIT %s', text)
        self.assertEqual(params, [CSV_ROW_CAP + 1])
        self.assertEqual((result.columns, len(result.rows), result.truncated), (["id", "name"], CSV_ROW_CAP, True))

    def test_timeout_and_failures_are_typed(self):
        from psycopg.errors import QueryCanceled

        conn = _FakeScopedConn([])
        conn.cur.execute = mock.Mock(side_effect=[None, QueryCanceled("canceling statement due to statement timeout")])
        with mock.patch.object(SVC, "_scoped_connect", return_value=conn), self.assertRaisesRegex(
            QueryTimeout, "longer than 15 seconds"
        ):
            SVC.grid_page(self.db, "items", COLUMNS[:1])
        with mock.patch.object(SVC, "_scoped_connect", side_effect=RuntimeError("server gone ")), \
                self.assertRaisesRegex(DatabaseExplorerError, "^server gone$"):
            SVC.grid_page(self.db, "items", COLUMNS[:1])


# --------------------------------------------------------------------------- view


@override_settings(PYRUNNER_DATA_DB_URL=_SERVER_URL, ENCRYPTION_KEY=_TEST_KEY)
class TableViewTests(TestCase):
    def setUp(self):
        _setup_wizard_off(self)
        self.ws = Workspace.objects.create(name="W")
        self.owner = User.objects.create(email="owner@example.com")
        WorkspaceMembership.objects.create(user=self.owner, workspace=self.ws, role=WorkspaceMembership.ROLE_OWNER)
        self.db = _make_database(self.ws)
        self.client.force_login(self.owner)
        for name, value in (
            ("tables", [{"name": "items", "row_estimate": 3, "row_exact": True, "size_bytes": 8192,
                         "size_display": "8.0 KB"}]),
            ("columns", COLUMNS),
            ("indexes", []),
        ):
            patcher = mock.patch.object(SVC, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def url(self, name="database_table", query=""):
        base = reverse(f"cpanel_ws:{name}", args=[self.ws.id, self.db.pk, "items"])
        return f"{base}?{query}" if query else base

    def get(self, query="", page=None, **grid_page):
        result = page or GridPage(rows=[], total=0)
        with mock.patch.object(SVC, "grid_page", return_value=result, **grid_page) as call:
            resp = self.client.get(self.url(query=query))
        return resp, call

    def test_read_only_grid_with_badges_and_the_note(self):
        long_text = "x" * (GRID_CELL_CHARS + 1)
        row = (7, long_text, decimal.Decimal("1.50"), True, datetime.date(2026, 1, 2), None, None,
               '{"a": 1}', None, "{1,2}")
        resp, call = self.get(page=GridPage(rows=[row], total=1))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("Read-only", body)
        self.assertIn("pyrunner_db", body)
        self.assertNotIn("data-edit", body)
        self.assertNotIn("data-grid-json", body)  # no JSON editor dialog
        self.assertIn('title="number"', body)
        self.assertIn("x" * GRID_CELL_CHARS + "…", body)
        self.assertNotIn(long_text, body)
        self.assertIn("1.50", body)
        self.assertIn("{&quot;a&quot;: 1}", body)
        # Default sort: the one-column primary key.
        self.assertEqual(call.call_args.kwargs["sort"], "id")
        self.assertIn('aria-sort="ascending"', body)

    def test_filters_compile_and_a_bad_value_is_dropped_with_a_note(self):
        query = urllib.parse.urlencode([("f", "name:contains:ann"), ("f", "score:gt:lots"), ("sort", "-score")])
        resp, call = self.get(query)
        kwargs = call.call_args.kwargs
        self.assertEqual([params for _, params in kwargs["conditions"]], [["%ann%"]])
        self.assertEqual(kwargs["sort"], "-score")
        body = resp.content.decode()
        self.assertIn("compares with a number", body)
        self.assertIn("name contains ann", body)

    def test_a_read_failure_keeps_the_page_and_its_filters(self):
        resp, _ = self.get("f=name:eq:a", side_effect=QueryTimeout("Reading the table took longer than 15 seconds"))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("Could not read the rows: Reading the table took longer than 15 seconds", body)
        self.assertIn("name is a", body)

    def test_capped_count_says_so(self):
        resp, _ = self.get(page=GridPage(rows=[(1,) + (None,) * 9], total=100_000, capped=True))
        self.assertIn("More than 100000 rows match", resp.content.decode())

    def test_csv_takes_the_grid_filters_and_sort(self):
        result = QueryResult(["id", "name", "meta", "active"], [(1, None, {"a": [1]}, False), (2, "b", None, True)])
        with mock.patch.object(SVC, "grid_csv", return_value=result) as call:
            resp = self.client.get(self.url("database_table_csv", "f=name:prefix:b&sort=name"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(call.call_args.kwargs["sort"], "name")
        self.assertEqual([p for _, p in call.call_args.kwargs["conditions"]], [["b%"]])
        self.assertEqual(resp.content.decode().splitlines(),
                         ["id,name,meta,active", '1,,"{""a"": [1]}",false', "2,b,,true"])
        self.assertEqual(resp["Content-Disposition"], 'attachment; filename="crm_items.csv"')

    def test_csv_failure_returns_to_the_filtered_grid(self):
        with mock.patch.object(SVC, "grid_csv", side_effect=DatabaseExplorerError("boom")):
            resp = self.client.get(self.url("database_table_csv", "f=name:eq:a"))
        self.assertRedirects(resp, self.url(query="sort=id&f=name%3Aeq%3Aa"), fetch_redirect_response=False)

    def test_export_link_carries_the_view(self):
        resp, _ = self.get("f=name:eq:a")
        self.assertIn("items/csv/?sort=id&amp;f=name%3Aeq%3Aa", resp.content.decode())

    def test_member_cannot_read(self):
        member = User.objects.create(email="member@example.com")
        WorkspaceMembership.objects.create(user=member, workspace=self.ws, role=WorkspaceMembership.ROLE_MEMBER)
        self.client.force_login(member)
        for name in ("database_table", "database_table_csv"):
            self.assertEqual(self.client.get(self.url(name)).status_code, 403)


# --------------------------------------------------------------------------- live Postgres


@skipUnless(connection.vendor == "postgresql", "needs a Postgres server (the test database's)")
class LiveGridTests(TestCase):
    """The real server: a role, a schema and a table provisioned in the test database."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        s = connection.settings_dict
        netloc = f"{urllib.parse.quote(s['USER'])}:{urllib.parse.quote(s['PASSWORD'])}@{s['HOST']}"
        cls.server = override_settings(PYRUNNER_DATA_DB_URL=f"postgres://{netloc}:{s['PORT'] or 5432}/{s['NAME']}")
        cls.server.enable()
        from core.services.database_service import DatabaseService

        cls.ws = Workspace.objects.create(name="T41 live")
        cls.db = _make_database(cls.ws, "shop", status=Database.STATUS_PROVISIONING)
        DatabaseService.provision(cls.db)
        SVC.execute(cls.db, (
            "CREATE TABLE items (id bigserial PRIMARY KEY, name text, score numeric, active boolean,"
            " born date, seen timestamptz, meta jsonb, doc json, tags int[], ratio double precision);"
            "INSERT INTO items (name, score, active, born, seen, meta, doc, tags, ratio)"
            " SELECT CASE WHEN g % 10 = 0 THEN NULL ELSE 'item ' || g END, g * 0.5, g % 2 = 0,"
            " date '2026-01-01' + g, timestamptz '2026-01-01 00:00Z' + g * interval '1 hour',"
            " jsonb_build_object('g', g), json_build_object('g', g), ARRAY[g, g + 1], g / 4.0"
            " FROM generate_series(1, 120) g;"
            "INSERT INTO items (name) VALUES ('50%_off'), (repeat('y', 1000))"), read_only=False, row_cap=None)

    @classmethod
    def tearDownClass(cls):
        from core.services.database_service import DatabaseService

        try:
            DatabaseService.deprovision(cls.db)
        finally:
            cls.server.disable()
            super().tearDownClass()

    def setUp(self):
        self.columns = SVC.columns(self.db, "items")
        self.by_name = {c["name"]: c for c in self.columns}

    def page(self, filters=(), sort="", page=1):
        conditions = [grid_condition(self.by_name[n], op, v) for n, op, v in filters]
        return SVC.grid_page(self.db, "items", self.columns, conditions=conditions, sort=sort, page=page)

    def ids(self, result):
        return [row[0] for row in result.rows]

    def test_columns_know_the_primary_key(self):
        self.assertEqual([c["name"] for c in self.columns if c["primary"]], ["id"])
        self.assertEqual(self.by_name["tags"]["type"], "ARRAY")

    def test_filters_sort_and_pages(self):
        result = self.page(sort="id")
        self.assertEqual((result.total, self.ids(result)[:2], len(result.rows)), (122, [1, 2], 50))
        self.assertEqual(self.ids(self.page(sort="id", page=3)), list(range(101, 123)))
        self.assertEqual(self.ids(self.page([("name", "contains", "ITEM 11")], sort="-id")),
                         [119, 118, 117, 116, 115, 114, 113, 112, 111, 11])
        # LIKE wildcards in the value are literal.
        self.assertEqual(self.ids(self.page([("name", "contains", "%_")])), [121])
        self.assertEqual(self.page([("name", "empty", "")]).total, 12)
        self.assertEqual(self.page([("score", "gte", "59.5"), ("active", "eq", "true")]).total, 1)
        self.assertEqual(self.page([("id", "gt", "123456789012345678901234567890")]).total, 0)
        self.assertEqual(self.ids(self.page([("id", "in", "3, 5")], sort="id")), [3, 5])
        self.assertEqual(self.page([("born", "lte", "2026-01-03")]).total, 2)
        self.assertEqual(self.page([("seen", "lt", "2026-01-01T03:30:00+00:00")]).total, 3)
        self.assertEqual(self.page([("tags", "contains", "{7,")]).total, 1)
        self.assertEqual(self.page([("meta", "not_empty", "")]).total, 120)
        # integer and float columns compare with the numeric value too.
        self.assertEqual(self.page([("ratio", "gt", "29.5"), ("ratio", "in", "30, 29.75")]).total, 2)
        # Sorted by a jsonb column (NULLs first when descending, as Postgres does; ties by id).
        self.assertEqual(self.ids(self.page(sort="-meta"))[:4], [121, 122, 120, 119])
        # json has no ordering: the stored order, and no error.
        self.assertEqual(self.page(sort="doc").total, 122)

    def test_long_text_is_cut_in_the_grid_but_not_in_the_csv(self):
        row = self.page([("id", "eq", "122")]).rows[0]
        self.assertEqual(len(row[1]), GRID_CELL_CHARS + 1)
        csv_rows = SVC.grid_csv(self.db, "items", self.columns, conditions=[grid_condition(self.by_name["id"], "eq", "122")])
        self.assertEqual(len(csv_rows.rows[0][1]), 1000)

    def test_count_cap_and_timeout(self):
        with mock.patch("core.services.database_explorer.GRID_COUNT_CAP", 100):
            result = self.page(page=9)
        self.assertEqual((result.total, result.capped, result.page), (100, True, 2))
        with mock.patch("core.services.database_explorer.VIEWER_TIMEOUT_MS", 200), self.assertRaises(QueryTimeout):
            with SVC._read_session(self.db) as cur:
                cur.execute("SELECT pg_sleep(2)")

    def test_the_session_cannot_write(self):
        with self.assertRaisesRegex(DatabaseExplorerError, "read-only transaction"), SVC._read_session(self.db) as cur:
            cur.execute("DELETE FROM items")
        self.assertEqual(self.page().total, 122)

    def test_the_console_page_and_csv(self):
        _setup_wizard_off(self)
        owner = User.objects.create(email="live-owner@example.com")
        WorkspaceMembership.objects.create(user=owner, workspace=self.ws, role=WorkspaceMembership.ROLE_OWNER)
        self.client.force_login(owner)
        base = reverse("cpanel_ws:database_table", args=[self.ws.id, self.db.pk, "items"])
        body = self.client.get(f"{base}?f=name:prefix:item%201&sort=-score").content.decode()
        self.assertIn("item 19", body)
        self.assertNotIn("item 2<", body)
        self.assertIn("name starts with item 1", body)
        csv_url = reverse("cpanel_ws:database_table_csv", args=[self.ws.id, self.db.pk, "items"])
        lines = self.client.get(f"{csv_url}?f=id:in:1,2&sort=id").content.decode().splitlines()
        self.assertEqual(lines[0], "id,name,score,active,born,seen,meta,doc,tags,ratio")
        self.assertEqual(lines[1], '1,item 1,0.5,false,2026-01-02,2026-01-01T01:00:00+00:00,'
                                   '"{""g"": 1}","{""g"": 1}","[1, 2]",0.25')
        self.assertEqual(len(lines), 3)


# --------------------------------------------------------------------------- vectors


@override_settings(ENCRYPTION_KEY=_TEST_KEY, VECTORS_DIR=_TMP / "t41")
class VectorBrowseTests(TestCase):
    def setUp(self):
        _setup_wizard_off(self)
        clear_cache()
        self.ws = Workspace.objects.create(name="W")
        self.owner = User.objects.create(email="owner@example.com")
        WorkspaceMembership.objects.create(user=self.owner, workspace=self.ws, role=WorkspaceMembership.ROLE_OWNER)
        self.client.force_login(self.owner)
        self.col = VectorService.create_collection(
            workspace=self.ws, name="docs", connection=VectorConnection.objects.get(name="Local"),
            embedding_provider=_provider(),
        )
        with fake_embeddings():
            VectorService.handle(self.col).add_texts([
                {"id": f"p{i:02d}", "text": f"text {i}", "payload": {"n": i, "pad": "z" * (200 if i == 3 else 1)}}
                for i in range(45)
            ])
        self.detail = reverse("cpanel_ws:vector_detail", args=[self.ws.id, self.col.pk])

    def test_points_page_by_cursor(self):
        body = self.client.get(self.detail).content.decode()
        self.assertIn("The first 20 points by id.", body)
        self.assertIn(">p00<", body)
        self.assertNotIn(">p20<", body)
        self.assertIn("?after=p19&amp;n=2", body)
        self.assertIn("45 points in all", body)
        body = self.client.get(f"{self.detail}?after=p39&n=3").content.decode()
        self.assertIn("Points 41–45 by id.", body)
        self.assertIn(">p44<", body)
        self.assertNotIn(">p39<", body)
        self.assertNotIn("after=", body)  # the last page has no Next
        self.assertIn("First page", body)
        body = self.client.get(f"{self.detail}?after=p44&n=4").content.decode()
        self.assertIn("No more points.", body)

    def test_a_long_payload_opens_in_full(self):
        body = self.client.get(self.detail).content.decode()
        self.assertEqual(body.count("<details>"), 1)
        self.assertIn("z" * 200, body)

    def test_qdrant_cursor_is_passed_back(self):
        conn = VectorConnection.objects.create(provider_type="qdrant", name="Q", config={"url": "http://q:6333"})
        col = VectorCollection.objects.create(workspace=self.ws, name="qcol", connection=conn, dimension=3,
                                              metric="cosine", backend_ref="pyrun_qcol", points_count=30)
        handle = mock.Mock()
        handle.scroll.return_value = ([{"id": "b", "text": "", "payload": {}}], "0f8c-uuid")
        with mock.patch("core.views.vectors.VectorService.handle", return_value=handle):
            body = self.client.get(reverse("cpanel_ws:vector_detail", args=[self.ws.id, col.pk])
                                   + "?after=prev-uuid&n=2").content.decode()
        handle.scroll.assert_called_once_with(cursor="prev-uuid", limit=20)
        self.assertIn("Page 2: 1 point from the server, sorted by id.", body)
        self.assertIn("?after=0f8c-uuid&amp;n=3", body)

    def test_search_runs_the_vectors_search_operation_within_its_budget(self):
        from core.api.endpoints import vectors as endpoint

        search_url = reverse("cpanel_ws:vector_search", args=[self.ws.id, self.col.pk])
        with fake_embeddings(), mock.patch.object(endpoint, "BudgetSession", wraps=endpoint.BudgetSession) as budget:
            body = self.client.post(search_url, {"query": "text 7", "k": "2", "filter": '{"n": 7}'}).content.decode()
        budget.assert_called_once()
        self.assertIn("score 1.0000", body)
        self.assertIn("p07", body)
        self.col.refresh_from_db()
        self.assertEqual(self.col.embed_calls, 13)  # 12 batches of 4 to add, 1 to search
