"""
Databases and vector collections on the unified registry, for REST, MCP and Py AI
(2.5 Stage 3, ticket T17).

What this pins, roughly in order of how much it would hurt to get wrong:

- ``databases.query`` runs one SELECT and nothing else: the guard refuses
  anything but one SELECT/WITH/VALUES/TABLE statement before a connection is
  opened; the session is read-only before its first statement, with a 5 s
  statement timeout; the rows come through a server-side cursor capped at 200.
  On Postgres (when the test database is Postgres) a real role and schema are
  provisioned and the server itself refuses a write, stops a slow statement and
  keeps the role out of the core tables.
- An instance without a data server (SQLite-only) still lists its databases,
  with the reason, and answers ``not_configured`` (409) for tables and queries.
- Py AI on a chat channel can't call ``databases.query``; the console can. Run
  and write endpoints (``vectors.upsert``/``delete``) never become Py AI tools.
- ``vectors.*`` see the workspace's collections minus those a Brain uses for
  recall (their points are the Brain's notes at every tier). A search embeds the
  query with the collection's provider and is counted on the collection and the
  provider. The writes are hidden and refused while API writes are off, audited
  while on, and never create a collection.
- The ticket's acceptance question ("which tables are in my leads database and
  how many rows were added this week?") through the console bridge.

No test calls a model; the SDK's ``query`` is stubbed where the runtime runs.
Embeddings are faked at the client's HTTP seam (``core.test_vectors``).
"""

import datetime
import decimal
import json
import tempfile
import urllib.parse
import uuid
from pathlib import Path
from unittest import mock, skipUnless

from asgiref.sync import async_to_sync
from cryptography.fernet import Fernet
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings

from core.api.context import CallContext
from core.api.endpoints.databases import NOT_CONFIGURED, UNAVAILABLE, cell
from core.api.errors import ApiError
from core.api.mcp import LEGACY
from core.api.registry import registry
from core.api.tools import agent_context, build_tools
from core.models import (
    ApiCall,
    APIToken,
    Brain,
    Database,
    GlobalSettings,
    Script,
    VectorCollection,
    Workspace,
)
from core.services.database_explorer import (
    QUERY_ROW_CAP,
    QUERY_TIMEOUT_MS,
    DatabaseExplorerError,
    DatabaseExplorerService,
    DatabaseUnavailable,
    QueryRefused,
    QueryResult,
    QueryTimeout,
    read_only_statement,
)
from core.services.pyai import runtime
from core.services.pyai import tools as pyai_tools
from core.test_plugin_api import _mock_setup
from core.test_pyai_registry_tools import call, names
from core.test_vectors import _provider, fake_embeddings
from core.vectors.local_store import clear_cache
from core.vectors.service import VectorService

ACCEPT = "application/json, text/event-stream"
SERVER_URL = "postgres://provisioner:adminpw@dbhost:5432/pyrunner_data"
KEY = Fernet.generate_key().decode()
TMP = Path(tempfile.mkdtemp(prefix="pyrun-t17-"))
CONSOLE, CHANNEL = "pyai", "telegram:17"
DB_READS = ("databases.list", "databases.tables", "databases.query")
VECTOR_READS = ("vectors.list", "vectors.search")
VECTOR_WRITES = ("vectors.upsert", "vectors.delete")
WEEK_SQL = "SELECT count(*) AS added FROM leads WHERE created_at >= now() - interval '7 days'"


def _set_writes(on):
    gs = GlobalSettings.get_settings()
    gs.api_writes_enabled = on
    gs.save()


def _database(workspace, name="leads", status=Database.STATUS_READY):
    ident = f"db_t17_{uuid.uuid4().hex[:8]}"
    db = Database(name=name, workspace=workspace, schema_name=ident, role_name=ident,
                  status=status)
    db.set_password("pw")
    db.save()
    return db


# --------------------------------------------------------------------------- guard


class GuardTests(SimpleTestCase):
    def test_one_read_statement_passes_with_a_trailing_semicolon_dropped(self):
        cases = {
            "SELECT 1": "SELECT 1",
            "select 1;  -- done": "select 1",
            "WITH x AS (SELECT 1) SELECT * FROM x;": "WITH x AS (SELECT 1) SELECT * FROM x",
            "(select 1) union (select 2)": "(select 1) union (select 2)",
            "TABLE leads": "TABLE leads",
            "VALUES (1), (2)": "VALUES (1), (2)",
            "SELECT ';' AS a, $$;$$, $t$ ; $t$, 'it''s;'": "SELECT ';' AS a, $$;$$, $t$ ; $t$, 'it''s;'",
            "SELECT E'\\';' AS q": "SELECT E'\\';' AS q",
            'SELECT "a;b" FROM t /* ; /* nested */ */': 'SELECT "a;b" FROM t',
            "SELECT * FROM t WHERE name LIKE 'a%'": "SELECT * FROM t WHERE name LIKE 'a%'",
        }
        for text, statement in cases.items():
            with self.subTest(text=text):
                self.assertEqual(read_only_statement(text), statement)

    def test_anything_else_is_refused(self):
        for text in ("", "  ;", "INSERT INTO t VALUES (1)", "update t set a = 1", "DELETE FROM t",
                     "COPY t TO STDOUT", "EXPLAIN ANALYZE DELETE FROM t", "SET ROLE postgres",
                     "SELECT 1; DROP TABLE t", "select 1;;select 2",
                     "SELECT 'a\\'; DROP TABLE t; --'",
                     "SELECT set_config('statement_timeout', '0', false)",
                     'SELECT "set_config"(\'statement_timeout\', \'0\', false)',
                     "SELECT PG_TERMINATE_BACKEND (1)", "SELECT pg_cancel_backend(1)",
                     "SELECT pg_advisory_lock(1)", "SELECT pg_notify('c', 'x')",
                     "SELECT 'open", "SELECT 1 /* open", "SELECT $$ open", None, 42):
            with self.subTest(text=text), self.assertRaises(QueryRefused):
                read_only_statement(text)


# --------------------------------------------------------------------------- declarations


class DeclarationTests(SimpleTestCase):
    def test_classes_permissions_tiers_and_rates(self):
        expected = {
            "databases.list": ("read", "databases:read", "channel"),
            "databases.tables": ("read", "databases:read", "channel"),
            "databases.query": ("read", "databases:read", "console"),
            "vectors.list": ("read", "vectors:read", "channel"),
            "vectors.search": ("read", "vectors:read", "channel"),
            "vectors.upsert": ("write", "vectors:write", "console"),
            "vectors.delete": ("write", "vectors:write", "console"),
        }
        for slug, (access, permission, tier) in expected.items():
            spec = registry.get(slug)
            self.assertEqual((spec.access, spec.permission, spec.pyai_tier),
                             (access, permission, tier), slug)
        for slug in ("databases.query", "vectors.search", *VECTOR_WRITES):
            self.assertEqual(registry.get(slug).rate, (30, 60), slug)
        self.assertTrue({"databases:read", "vectors:read"} <= registry.read_permissions())
        self.assertIn("vectors:write", registry.write_permissions())
        offered = {p for p, _ in APIToken.API_PERMISSIONS}
        self.assertTrue({"databases:read", "vectors:read", "vectors:write"} <= offered)

    def test_cells_become_json(self):
        when = datetime.datetime(2026, 11, 9, 8, 30, tzinfo=datetime.UTC)
        ident = uuid.uuid4()
        row = [None, True, 3, "x", 0.5, float("nan"), decimal.Decimal("12.50"), when,
               when.date(), b"\x00\xff", memoryview(b"\x01"), ident, datetime.timedelta(hours=1),
               [1, when], {"a": [decimal.Decimal("1")], 2: None}]
        self.assertEqual([cell(v) for v in row], [
            None, True, 3, "x", 0.5, "nan", "12.50", "2026-11-09T08:30:00+00:00", "2026-11-09",
            "\\x00ff", "\\x01", str(ident), "1:00:00", [1, "2026-11-09T08:30:00+00:00"],
            {"a": ["1"], "2": None},
        ])
        json.dumps([cell(v) for v in row], allow_nan=False)


# --------------------------------------------------------------------------- the query seam


class _Cursor:
    def __init__(self, conn, name=None):
        self.conn, self.name, self.description = conn, name, None

    def execute(self, query, params=None):
        self.conn.statements.append((self.name, query, params))
        if self.name is not None:
            if self.conn.fail:
                raise self.conn.fail
            self.description = [type("D", (), {"name": c})() for c in self.conn.columns]

    def fetchmany(self, n):
        self.conn.fetched = n
        return list(self.conn.rows[:n])

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Conn:
    def __init__(self, columns=("n",), rows=(), fail=None):
        self.columns, self.rows, self.fail = columns, rows, fail
        self.statements, self.read_only, self.fetched = [], None, None

    def cursor(self, name=None):
        if name is not None:
            # Read-only before the first statement, as psycopg sends BEGIN READ ONLY.
            assert self.read_only is True
        return _Cursor(self, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@override_settings(PYRUNNER_DATA_DB_URL=SERVER_URL, ENCRYPTION_KEY=KEY)
class QuerySeamTests(TestCase):
    def setUp(self):
        self.db = _database(Workspace.objects.create(name="T17 seam"))

    def run_query(self, conn, text="SELECT n FROM t"):
        with mock.patch.object(DatabaseExplorerService, "_scoped_connect", return_value=conn):
            return DatabaseExplorerService.query(self.db, text)

    def test_read_only_timeout_then_one_capped_fetch_from_a_server_cursor(self):
        conn = _Conn(rows=[(i,) for i in range(250)])
        result = self.run_query(conn, "SELECT n FROM t;")
        self.assertIs(conn.read_only, True)
        timeout, declared = conn.statements
        self.assertIsNone(timeout[0])
        self.assertIn("statement_timeout", timeout[1])
        self.assertEqual(timeout[2], (str(QUERY_TIMEOUT_MS),))
        self.assertEqual(QUERY_TIMEOUT_MS, 5000)
        self.assertEqual(declared, ("pyrun_query", "SELECT n FROM t", None))
        self.assertEqual(conn.fetched, QUERY_ROW_CAP + 1)
        self.assertEqual((len(result.rows), result.truncated), (QUERY_ROW_CAP, True))
        self.assertEqual(result.columns, ["n"])
        small = self.run_query(_Conn(rows=[(1,), (2,)]))
        self.assertEqual((small.rows, small.truncated), ([[1], [2]], False))

    def test_a_refused_statement_never_connects(self):
        with mock.patch.object(DatabaseExplorerService, "_scoped_connect") as connect:
            with self.assertRaises(QueryRefused):
                DatabaseExplorerService.query(self.db, "DELETE FROM t")
        connect.assert_not_called()

    def test_failures_are_told_apart(self):
        from psycopg.errors import QueryCanceled, SyntaxError

        with mock.patch.object(DatabaseExplorerService, "_scoped_connect",
                               side_effect=OSError("connection refused at 10.0.0.5")):
            with self.assertRaises(DatabaseUnavailable):
                DatabaseExplorerService.query(self.db, "SELECT 1")
        with self.assertRaisesRegex(QueryTimeout, "5 seconds"):
            self.run_query(_Conn(fail=QueryCanceled("canceling statement")))
        with self.assertRaisesRegex(DatabaseExplorerError, "near FROM"):
            self.run_query(_Conn(fail=SyntaxError("syntax error at or near FROM")))


# --------------------------------------------------------------------------- REST/MCP fixture


class _Client:
    def setUp(self):
        _mock_setup(self)
        self.ws = Workspace.get_default() or Workspace.objects.create(name="T17", is_default=True)
        self.other = Workspace.objects.create(name="Other T17 workspace")

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
        body = response.json()["error"]
        return response.status_code, body["code"], body["message"]

    def rpc(self, method="tools/list", params=None, raw=None):
        return self.client.post(
            "/mcp", {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
            content_type="application/json", HTTP_ACCEPT=ACCEPT,
            HTTP_MCP_PROTOCOL_VERSION=LEGACY[0], HTTP_AUTHORIZATION="Bearer " + (raw or self.raw),
        )

    def tools(self, raw=None):
        return {t["name"] for t in self.rpc(raw=raw).json()["result"]["tools"]}

    def tool_call(self, tool, arguments, raw=None):
        return self.rpc("tools/call", {"name": tool, "arguments": arguments}, raw).json()["result"]


# --------------------------------------------------------------------------- databases


@override_settings(PYRUNNER_DATA_DB_URL=SERVER_URL, ENCRYPTION_KEY=KEY)
class DatabaseEndpointTests(_Client, TestCase):
    def setUp(self):
        super().setUp()
        self.leads = _database(self.ws, "leads")
        self.broken = _database(self.ws, "broken", status=Database.STATUS_ERROR)
        self.foreign = _database(self.other, "foreign")
        self.raw, _ = self.api_token(["databases:read"])

    def patch(self, method, **kwargs):
        patcher = mock.patch.object(DatabaseExplorerService, method, **kwargs)
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def test_list_is_the_workspace_with_the_server_state(self):
        data = self.data("databases.list")
        self.assertEqual([d["name"] for d in data["databases"]], ["broken", "leads"])
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["data_server"], {"configured": True})
        self.assertEqual(data["databases"][1]["status"], "ready")
        self.assertNotIn("foreign", json.dumps(data))
        self.assertNotIn(self.leads.role_name, json.dumps(data))

    def test_without_a_data_server_list_says_why_and_reads_are_refused(self):
        with override_settings(PYRUNNER_DATA_DB_URL=""):
            data = self.data("databases.list")
            self.assertEqual(data["count"], 2)
            self.assertEqual(data["data_server"], {"configured": False, "reason": NOT_CONFIGURED})
            for slug, params in (("databases.tables", {"database": "leads"}),
                                 ("databases.query", {"database": "leads", "sql": "SELECT 1"})):
                self.assertEqual(self.error(slug, params, method="post")[:2],
                                 (409, "not_configured"), slug)

    def test_tables_and_one_tables_columns(self):
        tables = [{"name": "leads", "row_estimate": 12, "row_exact": True, "size_bytes": 8192,
                   "size_display": "8 KB"},
                  {"name": "notes", "row_estimate": 0, "row_exact": False, "size_bytes": 0,
                   "size_display": "0 B"}]
        self.patch("tables", return_value=tables)
        columns = [{"name": "created_at", "type": "timestamp with time zone", "nullable": False,
                    "default": "now()"}]
        self.patch("columns", return_value=columns)
        data = self.data("databases.tables", {"database": "leads"})
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["tables"][0], {"name": "leads", "row_estimate": 12,
                                             "row_exact": True, "size_bytes": 8192})
        one = self.data("databases.tables", {"database": "leads", "table": "leads"})
        self.assertEqual(one["tables"], [{**data["tables"][0], "columns": columns}])
        self.assertEqual(self.error("databases.tables", {"database": "leads", "table": "nope"})[:2],
                         (404, "not_found"))

    def test_unknown_foreign_or_unready_databases(self):
        for name, expected in (("nope", (404, "not_found")), ("foreign", (404, "not_found")),
                               ("broken", (409, "not_ready"))):
            for slug, params in (("databases.tables", {}), ("databases.query", {"sql": "SELECT 1"})):
                with self.subTest(name=name, slug=slug):
                    got = self.error(slug, {"database": name, **params}, method="post")
                    self.assertEqual(got[:2], expected)

    def test_a_server_failure_is_503_without_the_servers_message(self):
        self.patch("tables", side_effect=DatabaseExplorerError("could not connect to 10.0.0.5"))
        status, code, message = self.error("databases.tables", {"database": "leads"})
        self.assertEqual((status, code, message), (503, "unavailable", UNAVAILABLE))
        self.patch("query", side_effect=DatabaseUnavailable("connection to 10.0.0.5 failed"))
        status, code, message = self.error(
            "databases.query", {"database": "leads", "sql": "SELECT 1"}, method="post")
        self.assertEqual((status, code), (503, "unavailable"))
        self.assertNotIn("10.0.0.5", message)

    def test_query_answers_rows_as_json_lists(self):
        when = datetime.datetime(2026, 11, 9, tzinfo=datetime.UTC)
        query = self.patch("query", return_value=QueryResult(
            ["n", "at", "amount"], [[3, when, decimal.Decimal("9.90")]], False))
        data = self.data("databases.query", {"database": "leads", "sql": WEEK_SQL}, method="post")
        self.assertEqual(data, {"database": "leads", "columns": ["n", "at", "amount"],
                                "rows": [[3, when.isoformat(), "9.90"]], "row_count": 1,
                                "truncated": False, "row_cap": QUERY_ROW_CAP})
        self.assertEqual(query.call_args.args, (self.leads, WEEK_SQL))

    def test_query_errors(self):
        cases = (
            ("DELETE FROM leads", None, (400, "invalid_input")),
            ("SELECT pg_sleep(9)", QueryTimeout("The query ran longer than 5 seconds"),
             (400, "query_timeout")),
            ("SELECT nope", DatabaseExplorerError('column "nope" does not exist'),
             (400, "query_failed")),
        )
        for sql, failure, expected in cases:
            with self.subTest(sql=sql):
                if failure is not None:
                    self.patch("query", side_effect=failure)
                status, code, message = self.error(
                    "databases.query", {"database": "leads", "sql": sql}, method="post")
                self.assertEqual((status, code), expected)
        self.assertIn('column "nope" does not exist', message)

    def test_permission_mcp_and_catalog(self):
        other_raw, _ = self.api_token(["scripts:read"])
        self.assertEqual(self.error("databases.list", raw=other_raw)[:2], (403, "forbidden"))
        self.assertTrue({"databases-list", "databases-tables", "databases-query"} <= self.tools())
        self.assertFalse({"databases-list"} & self.tools(other_raw))
        catalog = {e["slug"] for e in self.data("_catalog")["endpoints"]}
        self.assertTrue(set(DB_READS) <= catalog)
        self.patch("query", return_value=QueryResult(["added"], [[3]], False))
        result = self.tool_call("databases-query", {"database": "leads", "sql": WEEK_SQL})
        self.assertFalse(result.get("isError"), result)
        self.assertEqual(result["structuredContent"]["rows"], [[3]])


# --------------------------------------------------------------------------- vectors


@override_settings(ENCRYPTION_KEY=KEY, VECTORS_DIR=TMP / "api")
class VectorEndpointTests(_Client, TestCase):
    def setUp(self):
        super().setUp()
        clear_cache()
        self.provider = _provider()
        self.docs = VectorService.create_collection(
            workspace=self.ws, name="docs", embedding_provider=self.provider)
        self.raw_only = VectorService.create_collection(
            workspace=self.ws, name="raw", embedding_provider=None, dimension=3)
        self.recall = VectorService.create_collection(
            workspace=self.ws, name="brain-recall", embedding_provider=self.provider)
        Brain.objects.create(workspace=self.ws, name="B", slug="b-t17",
                             vector_collection=self.recall)
        self.foreign = VectorCollection.objects.create(
            workspace=self.other, name="docs", connection=self.docs.connection, dimension=8,
            backend_ref="vectors/foreign.sqlite")
        with fake_embeddings():
            VectorService.handle(self.docs).add_texts([
                {"id": "a", "text": "schedule a script every morning", "payload": {"lang": "en"}},
                {"id": "b", "text": "rotate the api key", "payload": {"lang": "de"}},
            ])
            VectorService.handle(self.recall).add_texts([{"id": "p", "text": "private note"}])
        self.raw, _ = self.api_token(["vectors:read"])

    def test_list_leaves_out_brain_collections_and_other_workspaces(self):
        data = self.data("vectors.list")
        self.assertEqual([c["name"] for c in data["collections"]], ["docs", "raw"])
        docs = data["collections"][0]
        self.assertEqual((docs["points"], docs["dimension"], docs["backend"], docs["text_search"]),
                         (2, 8, "local", True))
        self.assertFalse(data["collections"][1]["text_search"])

    def test_search_embeds_with_the_collections_provider_and_counts_it(self):
        before = (self.docs.tokens_embedded, self.docs.embed_calls)
        with fake_embeddings():
            data = self.data("vectors.search", {"collection": "docs",
                                                "query": "schedule a script every morning", "k": 1})
        self.assertEqual(data["count"], 1)
        hit = data["results"][0]
        self.assertEqual((hit["id"], hit["payload"], hit["text"]),
                         ("a", {"lang": "en"}, "schedule a script every morning"))
        self.docs.refresh_from_db()
        self.provider.refresh_from_db()
        self.assertEqual(self.docs.embed_calls, before[1] + 1)
        self.assertGreater(self.docs.tokens_embedded, before[0])
        self.assertEqual(self.provider.embed_calls, self.docs.embed_calls + 1)  # + recall's
        with fake_embeddings():
            filtered = self.data("vectors.search", {"collection": "docs", "query": "key",
                                                    "filter": json.dumps({"lang": "de"})})
        self.assertEqual([h["id"] for h in filtered["results"]], ["b"])

    def test_search_errors(self):
        with fake_embeddings():
            for params, expected in (
                ({"collection": "brain-recall", "query": "private"}, (404, "not_found")),
                ({"collection": "missing", "query": "x"}, (404, "not_found")),
                ({"collection": "raw", "query": "x"}, (400, "invalid")),
                ({"collection": "docs", "query": "x", "filter": '{"a.b": 1}'},
                 (400, "unsupported_filter")),
                ({"collection": "docs", "query": "x", "k": 51}, (400, "invalid_input")),
            ):
                with self.subTest(params=params):
                    self.assertEqual(self.error("vectors.search", params)[:2], expected)
        from core.vectors.embeddings import EmbeddingClient, EmbeddingError

        with mock.patch.object(EmbeddingClient, "_embed_batch",
                               side_effect=EmbeddingError("authentication failed")):
            self.assertEqual(self.error("vectors.search", {"collection": "docs", "query": "x"})[:2],
                             (502, "embedding_failed"))
        VectorCollection.objects.filter(pk=self.docs.pk).update(status="error")
        self.assertEqual(self.error("vectors.search", {"collection": "docs", "query": "x"})[:2],
                         (409, "not_ready"))

    def test_writes_are_hidden_and_refused_while_off(self):
        _set_writes(False)
        writer = APIToken(name="w", scope="api", workspace=self.ws, permissions=["vectors:write"],
                          token=APIToken.generate_token(scope="api"))
        with self.assertRaisesRegex(ValidationError, "write permissions can't be granted"):
            writer.full_clean()
        _set_writes(True)
        write_raw, _ = self.api_token(["vectors:read", "vectors:write"])
        _set_writes(False)
        self.assertFalse({"vectors-upsert", "vectors-delete"} & self.tools(write_raw))
        catalog = {e["slug"] for e in self.data("_catalog", raw=write_raw)["endpoints"]}
        self.assertFalse(set(VECTOR_WRITES) & catalog)
        params = {"collection": "docs", "ids": ["a"]}
        self.assertEqual(self.error("vectors.delete", params, write_raw, "post")[:2],
                         (403, "writes_disabled"))
        self.assertEqual(VectorCollection.objects.get(pk=self.docs.pk).points_count, 2)

    def test_upsert_and_delete_while_on_are_audited(self):
        _set_writes(True)
        write_raw, writer = self.api_token(["vectors:read", "vectors:write"])
        self.assertEqual(self.error("vectors.delete", {"collection": "docs", "ids": ["a"]},
                                    method="post")[:2], (403, "forbidden"))
        with fake_embeddings():
            data = self.data("vectors.upsert", {"collection": "docs", "items": [
                {"id": "c", "text": "back up the database nightly", "payload": {"lang": "en"}},
                {"id": "a", "text": "schedule a script every evening"},
            ]}, write_raw, "post")
        self.assertEqual((data["upserted"], data["points"]), (2, 3))
        self.assertGreater(data["tokens"], 0)
        raw_items = [{"id": "v1", "vector": [1, 0, 0], "text": "x"}, {"id": "v2", "vector": [0, 1, 0]}]
        data = self.data("vectors.upsert", {"collection": "raw", "items": raw_items},
                         write_raw, "post")
        self.assertEqual((data["upserted"], data["tokens"], data["points"]), (2, 0, 2))
        data = self.data("vectors.delete", {"collection": "docs", "filter": {"lang": "en"}},
                         write_raw, "post")
        self.assertEqual((data["deleted"], data["points"]), (1, 2))
        data = self.data("vectors.delete", {"collection": "raw", "ids": ["v1", "gone"]},
                         write_raw, "post")
        self.assertEqual((data["deleted"], data["points"]), (1, 1))
        audited = list(ApiCall.objects.filter(token_id=writer.pk).values_list("slug", "ok"))
        self.assertEqual(sorted(audited), sorted([("vectors.upsert", True)] * 2
                                                 + [("vectors.delete", True)] * 2))

    def test_write_input_errors(self):
        _set_writes(True)
        write_raw, _ = self.api_token(["vectors:write"])
        cases = (
            ("vectors.upsert", {"collection": "raw", "items": [{"id": "a", "vector": [1, 0, 0]},
                                                              {"id": "b", "text": "t"}]}),
            ("vectors.upsert", {"collection": "docs", "items": [{"id": "a"}]}),
            ("vectors.delete", {"collection": "docs"}),
            ("vectors.delete", {"collection": "docs", "ids": ["a"], "filter": {"lang": "en"}}),
            ("vectors.delete", {"collection": "docs", "filter": {}}),
        )
        for slug, params in cases:
            with self.subTest(params=params):
                self.assertEqual(self.error(slug, params, write_raw, "post")[:2],
                                 (400, "invalid_input"))
        self.assertEqual(self.error("vectors.upsert", {"collection": "raw", "items": [
            {"id": "a", "vector": [1, 0]}]}, write_raw, "post")[:2], (400, "dimension_mismatch"))
        for name in ("brain-recall", "nope"):
            self.assertEqual(self.error("vectors.delete", {"collection": name, "ids": ["p"]},
                                        write_raw, "post")[:2], (404, "not_found"))
        self.assertEqual(VectorCollection.objects.get(pk=self.recall.pk).points_count, 1)
        self.assertFalse(VectorCollection.objects.filter(name="nope").exists())


# --------------------------------------------------------------------------- Py AI


@override_settings(PYRUNNER_DATA_DB_URL=SERVER_URL, ENCRYPTION_KEY=KEY,
                   VECTORS_DIR=TMP / "pyai")
class PyAITests(TestCase):
    def setUp(self):
        clear_cache()
        self.ws = Workspace.objects.create(name="T17 Py AI")
        self.leads = _database(self.ws, "leads")
        self.provider = _provider()
        self.docs = VectorService.create_collection(
            workspace=self.ws, name="docs", embedding_provider=self.provider)

    def test_console_gets_query_and_channels_dont(self):
        reads = {"databases-list", "databases-tables", "vectors-list", "vectors-search"}
        for writes in (False, True):
            _set_writes(writes)
            console = names(build_tools(self.ws))
            channel = names(build_tools(self.ws, agent=CHANNEL))
            self.assertTrue(reads | {"databases-query"} <= console)
            self.assertTrue(reads <= channel)
            self.assertNotIn("databases-query", channel)
            self.assertFalse({"vectors-upsert", "vectors-delete"} & (console | channel))

    def test_the_registry_refuses_a_channel_query_even_with_the_permission(self):
        params = {"database": "leads", "sql": "SELECT 1"}
        with mock.patch.object(DatabaseExplorerService, "query") as query:
            for ctx in (agent_context(self.ws, CHANNEL),
                        CallContext(workspace=self.ws, principal="agent", principal_id=CHANNEL,
                                    permissions=frozenset({"databases:read"}),
                                    pyai_tier="channel")):
                with self.assertRaises(ApiError) as caught:
                    registry.call("databases.query", ctx, params)
                self.assertEqual(caught.exception.code, "forbidden")
            query.assert_not_called()

    def test_acceptance_tables_then_rows_added_this_week(self):
        """'Which tables are in my leads database and how many rows were added this week?'"""
        tools = build_tools(self.ws)
        tables = [{"name": "leads", "row_estimate": 40, "row_exact": True, "size_bytes": 16384,
                   "size_display": "16 KB"}]
        with mock.patch.object(DatabaseExplorerService, "tables", return_value=tables), \
             mock.patch.object(DatabaseExplorerService, "query",
                               return_value=QueryResult(["added"], [[3]], False)) as query:
            error, listed = call(tools, "databases-tables", {"database": "leads"})
            self.assertFalse(error, listed)
            self.assertEqual([t["name"] for t in listed["tables"]], ["leads"])
            error, answer = call(tools, "databases-query", {"database": "leads", "sql": WEEK_SQL})
        self.assertFalse(error, answer)
        self.assertEqual((answer["columns"], answer["rows"]), (["added"], [[3]]))
        self.assertEqual(query.call_args.args[1], WEEK_SQL)
        error, refused = call(tools, "databases-query", {"database": "leads", "sql": "DROP TABLE leads"})
        self.assertTrue(error)
        self.assertEqual(refused["error"]["code"], "invalid_input")

    def test_vector_search_through_the_channel_bridge(self):
        with fake_embeddings():
            VectorService.handle(self.docs).add_texts([{"id": "a", "text": "nightly backups"}])
            error, data = call(build_tools(self.ws, agent=CHANNEL), "vectors-search",
                               {"collection": "docs", "query": "nightly backups", "k": 1})
        self.assertFalse(error, data)
        self.assertEqual(data["results"][0]["id"], "a")

    def test_runtime_hands_the_sdk_the_tiered_tools_and_the_prompt_names_them(self):
        import claude_agent_sdk as sdk

        seen = {}

        async def query(prompt, options):
            seen["options"] = options
            if False:
                yield None

        prefix = f"mcp__{pyai_tools.SERVER_NAME}__"
        allowed = {}
        for agent in (CONSOLE, CHANNEL):
            with mock.patch.object(sdk, "query", query):
                async_to_sync(runtime.PyAIService._drive)(
                    "p", "s", "", {"ANTHROPIC_API_KEY": "k"}, self.ws, agent=agent)
            allowed[agent] = {n[len(prefix):] for n in seen["options"].allowed_tools}
        self.assertIn("databases-query", allowed[CONSOLE])
        self.assertNotIn("databases-query", allowed[CHANNEL])
        self.assertTrue({"databases-tables", "vectors-search"} <= allowed[CHANNEL])
        prompt = runtime.base_system_prompt(self.ws)
        for words in ("managed databases", "databases-query", "vector collections", "You can only READ"):
            self.assertIn(words, prompt)


# --------------------------------------------------------------------------- live Postgres


@skipUnless(connection.vendor == "postgresql", "needs a Postgres server (the test database's)")
class LivePostgresQueryTests(TestCase):
    """The real server: a role and schema provisioned in the test database itself."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        s = connection.settings_dict
        netloc = f"{urllib.parse.quote(s['USER'])}:{urllib.parse.quote(s['PASSWORD'])}@{s['HOST']}"
        url = f"postgres://{netloc}:{s['PORT'] or 5432}/{s['NAME']}"
        cls.server = override_settings(PYRUNNER_DATA_DB_URL=url)
        cls.server.enable()
        from core.services.database_service import DatabaseService

        cls.db = _database(Workspace.objects.create(name="T17 live"), "leads",
                           status=Database.STATUS_PROVISIONING)
        DatabaseService.provision(cls.db)
        DatabaseExplorerService.execute(cls.db, (
            "CREATE TABLE leads (id serial PRIMARY KEY, name text, created_at timestamptz);"
            "INSERT INTO leads (name, created_at) SELECT 'a' || g, now() - interval '1 day'"
            " FROM generate_series(1, 3) g;"
            "INSERT INTO leads (name, created_at) SELECT 'b' || g, now() - interval '30 days'"
            " FROM generate_series(1, 2) g"), read_only=False, row_cap=None)

    @classmethod
    def tearDownClass(cls):
        from core.services.database_service import DatabaseService

        try:
            DatabaseService.deprovision(cls.db)
        finally:
            cls.server.disable()
            super().tearDownClass()

    def query(self, text, **kwargs):
        return DatabaseExplorerService.query(self.db, text, **kwargs)

    def test_counts_rows_added_this_week(self):
        result = self.query(WEEK_SQL)
        self.assertEqual((result.columns, result.rows, result.truncated), (["added"], [[3]], False))
        like = self.query("SELECT count(*) FROM leads WHERE name LIKE 'a%'")
        self.assertEqual(like.rows, [[3]])

    def test_two_hundred_rows_at_most(self):
        result = self.query("SELECT g FROM generate_series(1, 100000) g")
        self.assertEqual((len(result.rows), result.truncated), (200, True))
        self.assertEqual(result.rows[-1], [200])

    def test_the_server_refuses_a_write_inside_a_select(self):
        # The guard lets both through (they start with WITH / SELECT); Postgres doesn't.
        with self.assertRaisesRegex(DatabaseExplorerError, "data-modifying"):
            self.query("WITH x AS (INSERT INTO leads (name) VALUES ('z') RETURNING id) SELECT * FROM x")
        with self.assertRaisesRegex(DatabaseExplorerError, "read-only transaction"):
            self.query("SELECT nextval('leads_id_seq')")
        self.assertEqual(self.query("SELECT count(*) FROM leads").rows, [[5]])

    def test_the_timeout_stops_a_slow_statement(self):
        self.assertEqual(self.query("SELECT current_setting('statement_timeout')").rows, [["5s"]])
        with self.assertRaises(QueryTimeout):
            self.query("SELECT pg_sleep(3)", timeout_ms=300)

    def test_the_role_stays_out_of_the_core_tables(self):
        with self.assertRaisesRegex(DatabaseExplorerError, "permission denied"):
            self.query(f"SELECT count(*) FROM public.{Script._meta.db_table}")
