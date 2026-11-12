"""
Database explorer service — the read paths behind the Databases Monitor + View
UI (Stage 3), deliberately split from ``database_service`` (lifecycle).

Two connection paths, by design, plus the API's query path:

- **View (structure + data grid)** connects as the database's OWN scoped role
  inside a ``READ ONLY`` transaction — the viewer physically cannot write, and
  cannot see outside its schema, because Postgres enforces both. ``execute()``
  is the seam the deferred Stage-5 SQL console will reuse with
  ``read_only=False``; the explorer itself only ever passes ``True``. The
  table page's rows (2.5, T41) come from ``grid_page`` / ``grid_csv``: the
  shared console grid's filters compiled to parameterised SQL
  (``grid_condition``), a count capped at GRID_COUNT_CAP, then one page, in
  one read-only session (``_read_session``) with the viewer timeout.

- **Monitor (activity / sizes / slow queries)** connects as the PROVISIONER
  (``pg_stat_*`` needs cross-role visibility) but every query filters on the
  role names of the ACTIVE WORKSPACE's databases — an Owner/Admin never sees
  another workspace's connections or query text. Query text additionally needs
  the provisioner to hold ``pg_read_all_stats`` (the compose init script grants
  it); without it Postgres masks other roles' text as ``<insufficient
  privilege>`` and the page says why.

- **Query (``databases.query``, 2.5)** runs ONE caller-written SELECT through
  ``query()``: the View path's role and ``READ ONLY`` transaction, behind the
  SELECT-only guard (``read_only_statement``), with a 5 s statement timeout and
  200 rows read through a server-side cursor. It raises typed errors
  (``QueryRefused``, ``QueryTimeout``, ``DatabaseUnavailable``) that the
  endpoint maps to answers.

Everything here degrades instead of raising: a down server, a missing
``pg_stat_statements`` extension, or a permission gap yields a result object
with ``ok=False``/availability flags so the pages render with a banner, never a
500.
"""

import datetime
import decimal
import logging
import math
import re
from contextlib import contextmanager
from dataclasses import dataclass, field

from core.services.database_service import DatabaseService

logger = logging.getLogger(__name__)

# The explorer's own leash, independent of the role's default statement
# timeout: viewer pages should feel instant or fail fast, never hold a slot.
VIEWER_TIMEOUT_MS = 15000
_VIEWER_PREFIXES = ("begin", "commit", "rollback", "set ", "show ", "select set_config(")
_VIEWER_MARKERS = ("pg_catalog.", "information_schema.", "pg_class", "pg_namespace",
                   "pg_total_relation_size", "pg_index", "pg_attribute")
_VIEWER_PAGE_RE = re.compile(
    r'^select \* from "[^"]+"(\."[^"]+")? limit \$\d+ offset \$\d+$', re.IGNORECASE
)


# Every statement of the table grid carries this comment, so the slow-query list
# can leave the console's own browsing out (its WHERE/ORDER BY vary per filter).
GRID_MARKER = "/* pyrun:grid */"


def is_viewer_statement(query: str) -> bool:
    """Whether a pg_stat_statements entry is PyRun's own explorer traffic (U44):
    transaction/session statements, catalog reads and the table browser's pages."""
    q = " ".join((query or "").split()).lower()
    if q.startswith(_VIEWER_PREFIXES) or GRID_MARKER in q:
        return True
    if any(marker in q for marker in _VIEWER_MARKERS):
        return True
    return bool(_VIEWER_PAGE_RE.match(q))


GRID_PAGE_ROWS = 50
# Tables up to this size with no planner estimate get an exact count(*) (U40).
EXACT_COUNT_MAX_BYTES = 8 * 1024 * 1024
CSV_ROW_CAP = 10000
# The table grid counts matching rows up to this many; pages stop there.
GRID_COUNT_CAP = 100_000
# A text/JSON/other cell in the grid carries at most this many characters.
GRID_CELL_CHARS = 300
# A number filter's exponent bound: past it a float column overflows (a page error).
_MAX_EXPONENT = 300
_MAX_IN = 100
LONG_RUNNING_SECONDS = 30


@dataclass
class QueryResult:
    """Result of ``execute()``: column names, rows, and a truncation flag."""

    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    truncated: bool = False


class DatabaseExplorerError(Exception):
    """A viewer/monitor read failed (server down, bad table, timeout)."""


# ``databases.query`` (2.5 Stage 3): one caller-written SELECT, read-only.
QUERY_ROW_CAP = 200
QUERY_TIMEOUT_MS = 5000
_READ_STATEMENTS = ("select", "with", "values", "table")
# Calls a read-only transaction still allows that reach outside the query:
# session settings, other sessions, advisory locks and notifications.
_DENIED_CALL = re.compile(
    r"\b(set_config|pg_cancel_backend|pg_terminate_backend|pg_notify|pg_\w*advisory\w*)\s*\(",
    re.IGNORECASE,
)
_DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")


class QueryRefused(DatabaseExplorerError):
    """The text is not one read-only statement (the SELECT-only guard)."""


class QueryTimeout(DatabaseExplorerError):
    """The statement ran past its timeout."""


class DatabaseUnavailable(DatabaseExplorerError):
    """The data server could not be reached as the database's role."""


def _is_word(char: str) -> bool:
    return char.isalnum() or char in "_$"


def _code_only(text: str) -> str:
    """``text`` with comments and the inside of string literals blanked, the same length.

    A literal keeps its quotes, so the end of the statement is still where it
    was. Quoted identifiers keep their word characters (``"set_config"`` still
    names the function); the rest is blanked. Raises ``QueryRefused`` on an
    unterminated literal or comment, which Postgres would refuse anyway.
    """
    out, i, n = list(text), 0, len(text)

    def blank(start, end):
        out[start:end] = " " * (end - start)

    while i < n:
        if text.startswith("--", i):
            end = text.find("\n", i)
            end = n if end < 0 else end
            blank(i, end)
            i = end
        elif text.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if text.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif text.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            if depth:
                raise QueryRefused("The query has an unterminated comment.")
            blank(i, j)
            i = j
        elif text[i] in "'\"":
            quote = text[i]
            # E'...' strings take backslash escapes; a plain 'e' ending a word doesn't.
            escapes = (
                quote == "'" and i > 0 and text[i - 1] in "eE"
                and (i < 2 or not _is_word(text[i - 2]))
            )
            j = i + 1
            while True:
                if j >= n:
                    raise QueryRefused("The query has an unterminated quoted text.")
                if escapes and text[j] == "\\":
                    j += 2
                elif text[j] == quote and text[j + 1 : j + 2] == quote:
                    j += 2
                elif text[j] == quote:
                    break
                else:
                    j += 1
            if quote == "'":
                blank(i + 1, j)
            else:
                # Keep the name's word characters; anything else (";") is a gap.
                out[i : j + 1] = [c if _is_word(c) else " " for c in text[i : j + 1]]
            i = j + 1
        elif text[i] == "$" and not (i > 0 and _is_word(text[i - 1])):
            tag = _DOLLAR_TAG.match(text, i)
            if tag is None:
                i += 1
                continue
            end = text.find(tag.group(0), tag.end())
            if end < 0:
                raise QueryRefused("The query has an unterminated $-quoted text.")
            blank(tag.end(), end)
            i = end + len(tag.group(0))
        else:
            i += 1
    return "".join(out)


def read_only_statement(text) -> str:
    """``text`` as one read-only statement, or raise ``QueryRefused``.

    The SELECT-only guard of ``databases.query``: one statement that starts
    with SELECT, WITH, VALUES or TABLE, with no call from ``_DENIED_CALL``. A
    trailing semicolon (and what follows it) is dropped. It is a guard, not
    the boundary: the read-only transaction and the database's own role are
    what Postgres enforces.
    """
    if not isinstance(text, str):
        raise QueryRefused("Pass the query as text.")
    code = _code_only(text).rstrip()
    while code.endswith(";"):
        code = code[:-1].rstrip()
    if ";" in code:
        raise QueryRefused("Send one statement at a time.")
    first = re.match(r"[\s(]*([A-Za-z]+)", code)
    if first is None or first.group(1).lower() not in _READ_STATEMENTS:
        raise QueryRefused("Only a SELECT statement (SELECT, WITH, VALUES or TABLE) can run here.")
    denied = _DENIED_CALL.search(code)
    if denied:
        raise QueryRefused(f"{denied.group(1).lower()}() can't be called from a read-only query.")
    return text[: len(code)]


def json_value(value):
    """One column value as JSON.

    Numbers, text, booleans and null stay as they are; ``numeric`` is text (no
    precision lost), dates and times are ISO text, ``bytea`` is ``\\x`` + hex as
    psql shows it, and anything else (uuid, interval, inet, ranges) its text.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "\\x" + bytes(value).hex()
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    return str(value)


# ---------------------------------------------------------------- table grid
#
# The console's table view reads a page of rows with the grid's filters and
# sort (core/grid.py). A column's Postgres type picks its grid kind, which picks
# the filter operators (core.tables.query.OPS_BY_TYPE) and how it is compared:
# numbers, dates, times and booleans as themselves; everything else as its text.

_NUMBER_TYPES = {"smallint", "integer", "bigint", "numeric", "real", "double precision"}
_DATETIME_TYPES = {"timestamp without time zone", "timestamp with time zone"}
# Text-compared types that also have an ordering (so the column can be sorted).
_SORTABLE_TEXT_TYPES = {
    "text", "character varying", "character", "name", "uuid", "jsonb", "bytea", "money",
    "inet", "cidr", "macaddr", "interval", "time without time zone", "time with time zone",
}
_TRUE = {"true", "yes", "1", "t", "y"}
_FALSE = {"false", "no", "0", "f", "n"}


def grid_kind(data_type: str) -> str:
    """The grid kind (core.grid column type) for an ``information_schema`` data type."""
    if data_type in _NUMBER_TYPES:
        return "number"
    if data_type == "boolean":
        return "boolean"
    if data_type == "date":
        return "date"
    if data_type in _DATETIME_TYPES:
        return "datetime"
    if data_type in ("json", "jsonb"):
        return "json"
    return "text"


def grid_sortable(data_type: str) -> bool:
    """Whether ORDER BY works on the type (``json``, arrays, geometry and custom types have no safe ordering)."""
    return grid_kind(data_type) in ("number", "boolean", "date", "datetime") or data_type in _SORTABLE_TEXT_TYPES


def _previewed(column: dict) -> bool:
    """Whether the grid reads only the first GRID_CELL_CHARS of the column, as text."""
    return grid_kind(column["type"]) in ("text", "json")


def _like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _typed(column: dict, value: str):
    """A filter value as the column's Python type; ValueError names the column and the value."""
    name, data_type = column["name"], column["type"]
    kind = grid_kind(data_type)
    text = value.strip()
    if kind == "number":
        try:
            number = decimal.Decimal(text)
        except decimal.InvalidOperation:
            number = None
        if number is None or not number.is_finite() or abs(number.adjusted()) > _MAX_EXPONENT:
            raise ValueError(f'"{name}" compares with a number; got "{value}".')
        return number
    if kind == "boolean":
        if text.lower() in _TRUE | _FALSE:
            return text.lower() in _TRUE
        raise ValueError(f'"{name}" compares with true or false; got "{value}".')
    if kind == "date":
        try:
            return datetime.date.fromisoformat(text)
        except ValueError:
            raise ValueError(f'"{name}" compares with a date (YYYY-MM-DD); got "{value}".') from None
    if kind == "datetime":
        from core.tables.columns import parse_datetime

        moment = parse_datetime(text)
        if moment is None:
            raise ValueError(f'"{name}" compares with a date and time; got "{value}".')
        # A zone-less column holds wall times; compare with the UTC wall time.
        return moment if data_type == "timestamp with time zone" else moment.replace(tzinfo=None)
    return value


def grid_condition(column: dict, op: str, value: str = ""):
    """One grid filter as ``(sql.Composable, params)`` on ``column`` (a ``columns()`` entry).

    ``op`` is one of the column kind's grid operators (the grid checked it).
    Text-compared columns compare their text (``"col"::text``), case-insensitive
    for contains / starts with. "is not" also matches an empty cell, and a text
    cell is empty when it is NULL or ''. Raises ValueError for a value that
    doesn't fit the column.
    """
    from psycopg import sql

    col = sql.Identifier(column["name"])
    kind = grid_kind(column["type"])
    as_text = kind in ("text", "json")
    target = sql.SQL("{}::text").format(col) if as_text else col
    if op in ("empty", "not_empty"):
        if kind == "text":
            clause = "({0} IS NULL OR {0}::text = '')" if op == "empty" else "({0} IS NOT NULL AND {0}::text <> '')"
        else:
            clause = "{0} IS NULL" if op == "empty" else "{0} IS NOT NULL"
        return sql.SQL(clause).format(col), []
    if op in ("contains", "prefix"):
        pattern = _like(value)
        pattern = f"%{pattern}%" if op == "contains" else f"{pattern}%"
        return sql.SQL("{} ILIKE %s").format(target), [pattern]
    if op == "in":
        values = [v.strip() for v in value.split(",") if v.strip()]
        if not values:
            raise ValueError(f'"{column["name"]}" is one of needs values separated by commas.')
        if len(values) > _MAX_IN:
            raise ValueError(f'"{column["name"]}" is one of takes at most {_MAX_IN} values.')
        return sql.SQL("{} = ANY(%s)").format(target), [[_typed(column, v) for v in values]]
    typed = value if as_text else _typed(column, value)
    operator = {"eq": "=", "ne": "IS DISTINCT FROM", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[op]
    return sql.SQL("{} " + operator + " %s").format(target), [typed]


@dataclass
class GridPage:
    """One page of the table grid: ``rows`` (tuples in ``columns`` order), the
    matching row count up to GRID_COUNT_CAP (``capped`` when more match), and
    the page number actually read (a past-the-end page moves to the last)."""

    rows: list = field(default_factory=list)
    total: int = 0
    capped: bool = False
    page: int = 1


class DatabaseExplorerService:
    # ------------------------------------------------------------------
    # The scoped-role execution seam (Stage 5 reuses with read_only=False)
    # ------------------------------------------------------------------

    @classmethod
    def _scoped_connect(cls, database):
        """Connection as the database's own role (patched in tests)."""
        import psycopg

        return psycopg.connect(DatabaseService.scoped_dsn(database), connect_timeout=10)

    @classmethod
    def execute(
        cls,
        database,
        query,
        params=None,
        *,
        read_only=True,
        row_cap=GRID_PAGE_ROWS,
        timeout_ms=VIEWER_TIMEOUT_MS,
    ) -> QueryResult:
        """Run one statement as the database's OWN role.

        ``read_only=True`` (the only mode the explorer uses) marks the session
        read-only BEFORE the first statement, so any write is rejected by
        Postgres itself — not by PyRun inspecting SQL. ``row_cap`` bounds
        the result (``truncated`` reports whether more rows existed);
        ``timeout_ms`` bounds the runtime via ``set_config`` inside the
        session. Raises ``QueryTimeout`` past the timeout and
        ``DatabaseExplorerError`` on any other server-side failure.
        """
        from psycopg.errors import QueryCanceled

        try:
            with cls._scoped_connect(database) as conn:
                conn.read_only = bool(read_only)
                with conn.cursor() as cur:
                    # set_config (not SET) so the value is parametrizable; it
                    # is legal inside a read-only transaction.
                    cur.execute(
                        "SELECT set_config('statement_timeout', %s, false)",
                        (str(int(timeout_ms)),),
                    )
                    cur.execute(query, params)
                    if cur.description is None:
                        return QueryResult()
                    columns = [d.name for d in cur.description]
                    if row_cap:
                        rows = cur.fetchmany(int(row_cap) + 1)
                        truncated = len(rows) > int(row_cap)
                        if truncated:
                            rows = rows[: int(row_cap)]
                    else:
                        rows, truncated = cur.fetchall(), False
                    return QueryResult(columns, list(rows), truncated)
        except DatabaseExplorerError:
            raise
        except QueryCanceled as e:
            # Postgres' own "canceling statement due to statement timeout"
            # isn't for the page (2.5 test pass 8).
            raise QueryTimeout(
                f"Reading the database took longer than {int(timeout_ms) / 1000:g} seconds and was "
                "stopped. Try again in a moment."
            ) from e
        except Exception as e:
            raise DatabaseExplorerError(str(e)) from e

    @classmethod
    def query(
        cls, database, text, *, row_cap=QUERY_ROW_CAP, timeout_ms=QUERY_TIMEOUT_MS, params=None
    ) -> QueryResult:
        """Run a caller-written SELECT as the database's OWN role (``databases.query``).

        Three layers: ``read_only_statement`` refuses anything but one SELECT;
        the transaction is ``READ ONLY`` before the first statement, so Postgres
        rejects any write; and the database's role reaches its own schema only.
        The rows come through a server-side cursor (one ``FETCH`` of
        ``row_cap + 1``), so a query over a huge table never transfers more,
        and the cursor's ``DECLARE`` goes over the extended protocol, which
        takes a single statement. ``statement_timeout`` bounds each statement.

        ``params`` (the ``db_poll`` watcher's cursor value) are bound to ``%s``
        placeholders; with params, a literal ``%`` in ``text`` is written ``%%``.

        Raises ``QueryRefused``, ``DatabaseUnavailable`` (no connection),
        ``QueryTimeout`` or ``DatabaseExplorerError`` (the server's message).
        """
        from psycopg.errors import QueryCanceled

        statement = read_only_statement(text)
        try:
            conn = cls._scoped_connect(database)
        except Exception as e:
            raise DatabaseUnavailable(str(e)) from e
        try:
            with conn:
                conn.read_only = True
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT set_config('statement_timeout', %s, false)",
                        (str(int(timeout_ms)),),
                    )
                with conn.cursor(name="pyrun_query") as cur:
                    cur.execute(statement, params)
                    if cur.description is None:
                        return QueryResult()
                    columns = [d.name for d in cur.description]
                    rows = cur.fetchmany(int(row_cap) + 1)
        except QueryCanceled as e:
            raise QueryTimeout(
                f"The query ran longer than {int(timeout_ms) / 1000:g} seconds and was stopped."
            ) from e
        except Exception as e:
            raise DatabaseExplorerError(str(e).strip()) from e
        truncated = len(rows) > int(row_cap)
        return QueryResult(columns, [list(r) for r in rows[: int(row_cap)]], truncated)

    # ------------------------------------------------------------------
    # View — structure + data, all through the scoped role
    # ------------------------------------------------------------------

    @classmethod
    def tables(cls, database) -> list:
        """The database's tables with row estimates and on-disk sizes.

        ``reltuples`` is the planner's estimate (refreshed by ANALYZE/vacuum) —
        instant on any table size, clearly labeled '~' in the UI. -1 means the
        table has never been analyzed.
        """
        result = cls.execute(
            database,
            """
            SELECT c.relname,
                   GREATEST(c.reltuples, 0)::bigint,
                   pg_total_relation_size(c.oid)
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relkind IN ('r', 'p')
            ORDER BY c.relname
            """,
            (database.schema_name,),
            row_cap=None,
        )
        from psycopg import sql

        from core.services import DatastoreService

        tables = []
        for name, rows, size in result.rows:
            entry = {
                "name": name,
                "row_estimate": rows,
                "row_exact": False,
                "size_bytes": size,
                "size_display": DatastoreService.format_size(size),
            }
            # A table never analysed reads "~0 rows" (U40): count small ones exactly.
            if not rows and (size or 0) <= EXACT_COUNT_MAX_BYTES:
                try:
                    counted = cls.execute(
                        database,
                        sql.SQL(GRID_MARKER + " SELECT count(*) FROM {}").format(sql.Identifier(name)),
                        row_cap=None,
                    )
                    entry["row_estimate"] = counted.rows[0][0]
                    entry["row_exact"] = True
                except Exception:
                    pass
            tables.append(entry)
        return tables

    @classmethod
    def table_or_none(cls, database, table_name: str):
        """The ``tables()`` entry for ``table_name``, or None.

        Every table-addressed page resolves through this first, so a
        user-supplied name is only ever used after it matched a real table in
        the database's OWN schema (and is then quoted as an identifier anyway).
        """
        return next((t for t in cls.tables(database) if t["name"] == table_name), None)

    @classmethod
    def columns(cls, database, table_name: str) -> list:
        """The table's columns in order: name, type, nullable, default, and
        ``primary`` (part of the primary key)."""
        result = cls.execute(
            database,
            """
            SELECT c.column_name, c.data_type, c.is_nullable, c.column_default,
                   EXISTS (
                       SELECT 1 FROM pg_index i
                       JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                       WHERE i.indisprimary AND a.attname = c.column_name
                         AND i.indrelid = format('%%I.%%I', c.table_schema, c.table_name)::regclass
                   )
            FROM information_schema.columns c
            WHERE c.table_schema = %s AND c.table_name = %s
            ORDER BY c.ordinal_position
            """,
            (database.schema_name, table_name),
            row_cap=None,
        )
        return [
            {
                "name": name,
                "type": dtype,
                "nullable": nullable == "YES",
                "default": default or "",
                "primary": bool(primary and primary[0]),
            }
            for name, dtype, nullable, default, *primary in result.rows
        ]

    @classmethod
    def indexes(cls, database, table_name: str) -> list:
        result = cls.execute(
            database,
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = %s AND tablename = %s ORDER BY indexname",
            (database.schema_name, table_name),
            row_cap=None,
        )
        return [{"name": n, "definition": d} for n, d in result.rows]

    @classmethod
    @contextmanager
    def _read_session(cls, database, timeout_ms=None):
        """A cursor as the database's OWN role in a READ ONLY session with ``statement_timeout``.

        Raises ``QueryTimeout`` when a statement runs past the timeout and
        ``DatabaseExplorerError`` for any other server-side failure.
        """
        from psycopg.errors import QueryCanceled

        timeout_ms = timeout_ms or VIEWER_TIMEOUT_MS
        try:
            with cls._scoped_connect(database) as conn:
                conn.read_only = True
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT set_config('statement_timeout', %s, false)",
                        (str(int(timeout_ms)),),
                    )
                    yield cur
        except DatabaseExplorerError:
            raise
        except QueryCanceled as e:
            raise QueryTimeout(
                f"Reading the table took longer than {int(timeout_ms) / 1000:g} seconds and was "
                "stopped. Filter on an indexed column, or sort by one."
            ) from e
        except Exception as e:
            raise DatabaseExplorerError(str(e).strip()) from e

    @staticmethod
    def _grid_sql(table_name, columns, conditions, sort, *, preview):
        """(select list, the table + WHERE, `` ORDER BY …`` or empty, the WHERE's params) for the grid."""
        from psycopg import sql

        select = sql.SQL(", ").join(
            sql.SQL("left({}::text, {})").format(sql.Identifier(c["name"]), sql.Literal(GRID_CELL_CHARS + 1))
            if preview and _previewed(c) else sql.Identifier(c["name"])
            for c in columns
        )
        source = sql.Identifier(table_name)
        params = []
        if conditions:
            source = sql.SQL("{} WHERE {}").format(
                source, sql.SQL(" AND ").join(clause for clause, _ in conditions)
            )
            for _, values in conditions:
                params.extend(values)
        order = sql.SQL("")
        by_name = {c["name"]: c for c in columns}
        key = (sort or "").lstrip("-")
        if key in by_name and grid_sortable(by_name[key]["type"]):
            direction = sql.SQL("DESC" if sort.startswith("-") else "ASC")
            # Qualified, so the name never reads one of the select list's outputs.
            keys = [sql.SQL("{} {}").format(sql.Identifier(table_name, key), direction)]
            # The primary key breaks ties, so a page boundary stays put.
            primary = [c["name"] for c in columns if c.get("primary")]
            if len(primary) == 1 and primary[0] != key:
                keys.append(sql.Identifier(table_name, primary[0]))
            order = sql.SQL(" ORDER BY {}").format(sql.SQL(", ").join(keys))
        return select, source, order, params

    @classmethod
    def grid_page(
        cls, database, table_name, columns, *, conditions=(), sort="", page=1, per_page=GRID_PAGE_ROWS
    ) -> GridPage:
        """One page of ``table_name`` for the console grid, in one read-only session.

        ``columns`` are ``columns()`` entries (every column, in order);
        ``conditions`` are ``grid_condition`` results (AND); ``sort`` is a
        column name, ``-`` first for descending (ignored when the column can't
        be sorted; no sort reads the table's stored order). Rows are counted up
        to GRID_COUNT_CAP first, then one page is read; text and JSON cells
        carry their first GRID_CELL_CHARS + 1 characters (more means cut).
        The caller validated ``table_name`` via ``table_or_none``.
        """
        from psycopg import sql

        per_page = max(1, min(int(per_page), GRID_PAGE_ROWS))
        select, source, order, params = cls._grid_sql(table_name, columns, conditions, sort, preview=True)
        with cls._read_session(database) as cur:
            cur.execute(
                sql.SQL(GRID_MARKER + " SELECT count(*) FROM (SELECT 1 FROM {} LIMIT %s) AS matching").format(source),
                [*params, GRID_COUNT_CAP + 1],
            )
            total = cur.fetchone()[0]
            capped = total > GRID_COUNT_CAP
            total = min(total, GRID_COUNT_CAP)
            last = max(1, -(-total // per_page))
            page = min(max(1, int(page)), last)
            cur.execute(
                sql.SQL(GRID_MARKER + " SELECT {} FROM {}{} LIMIT %s OFFSET %s").format(select, source, order),
                [*params, per_page, (page - 1) * per_page],
            )
            rows = list(cur.fetchall())
        return GridPage(rows=rows, total=total, capped=capped, page=page)

    @classmethod
    def grid_csv(cls, database, table_name, columns, *, conditions=(), sort="") -> QueryResult:
        """The grid's matching rows (its filters and sort, full values), up to CSV_ROW_CAP.

        ``truncated`` says more rows matched. Same session rules as ``grid_page``.
        """
        from psycopg import sql

        select, source, order, params = cls._grid_sql(table_name, columns, conditions, sort, preview=False)
        with cls._read_session(database) as cur:
            cur.execute(
                sql.SQL(GRID_MARKER + " SELECT {} FROM {}{} LIMIT %s").format(select, source, order),
                [*params, CSV_ROW_CAP + 1],
            )
            rows = list(cur.fetchall())
        return QueryResult(
            [c["name"] for c in columns], rows[:CSV_ROW_CAP], len(rows) > CSV_ROW_CAP
        )

    # ------------------------------------------------------------------
    # Monitor — provisioner connection, always filtered to the workspace
    # ------------------------------------------------------------------

    @classmethod
    def _workspace_role_map(cls, workspace) -> dict:
        """role_name → Database for the workspace (the monitor's filter set)."""
        from core.models import Database

        return {
            d.role_name: d
            for d in Database.objects.for_workspace(workspace).order_by("name")
        }

    @classmethod
    def stats_for_workspace(cls, workspace) -> dict:
        """Per-database size / table count / live connections for the list page.

        Returns ``{database_id: {...}}``; ``{}`` when the server is
        unreachable or no databases exist (the page renders without metrics).
        """
        role_map = cls._workspace_role_map(workspace)
        if not role_map:
            return {}
        schemas = [d.schema_name for d in role_map.values()]
        roles = list(role_map.keys())

        from core.services import DatastoreService

        stats = {}
        try:
            with DatabaseService._admin_connect() as conn:
                sizes = conn.execute(
                    """
                    SELECT n.nspname,
                           COALESCE(SUM(pg_total_relation_size(c.oid)), 0)::bigint,
                           COUNT(c.oid) FILTER (WHERE c.relkind IN ('r', 'p'))
                    FROM pg_namespace n
                    LEFT JOIN pg_class c ON c.relnamespace = n.oid
                    WHERE n.nspname = ANY(%s)
                    GROUP BY n.nspname
                    """,
                    (schemas,),
                ).fetchall()
                conns = conn.execute(
                    "SELECT usename, COUNT(*) FROM pg_stat_activity "
                    "WHERE usename = ANY(%s) GROUP BY usename",
                    (roles,),
                ).fetchall()
        except Exception as e:
            logger.warning("Databases stats unavailable: %s", e)
            return {}

        size_by_schema = {row[0]: (row[1], row[2]) for row in sizes}
        conn_by_role = {row[0]: row[1] for row in conns}
        for database in role_map.values():
            size_bytes, table_count = size_by_schema.get(database.schema_name, (0, 0))
            stats[database.id] = {
                "size_bytes": size_bytes,
                "size_display": DatastoreService.format_size(size_bytes),
                "table_count": table_count,
                "connections": conn_by_role.get(database.role_name, 0),
            }
        return stats

    @classmethod
    def activity_for_workspace(cls, workspace) -> dict:
        """Live sessions of the workspace's database roles, bucketed for the
        monitor page: active (with runtime), idle-in-transaction, blocked.

        ``ok=False`` + ``error`` when the server can't be reached.
        """
        role_map = cls._workspace_role_map(workspace)
        result = {
            "ok": True,
            "error": "",
            "sessions": [],
            "active": 0,
            "idle_in_transaction": 0,
            "blocked": 0,
            "long_running": 0,
        }
        if not role_map:
            return result
        try:
            with DatabaseService._admin_connect() as conn:
                rows = conn.execute(
                    """
                    SELECT a.pid, a.usename, a.state,
                           EXTRACT(EPOCH FROM (now() - a.query_start))::int,
                           a.wait_event_type,
                           cardinality(pg_blocking_pids(a.pid)),
                           a.query
                    FROM pg_stat_activity a
                    WHERE a.usename = ANY(%s)
                    ORDER BY a.query_start
                    """,
                    (list(role_map.keys()),),
                ).fetchall()
        except Exception as e:
            logger.warning("Databases activity unavailable: %s", e)
            return {**result, "ok": False, "error": str(e)}

        for pid, role, state, age, wait_type, blockers, query in rows:
            database = role_map.get(role)
            session = {
                "pid": pid,
                "database": database.name if database else role,
                "state": state or "",
                "seconds": age if age is not None and age >= 0 else None,
                "wait": wait_type or "",
                "blocked_by": blockers or 0,
                "query": (query or "").strip(),
                "long_running": bool(
                    state == "active" and age and age >= LONG_RUNNING_SECONDS
                ),
            }
            result["sessions"].append(session)
            if session["blocked_by"]:
                result["blocked"] += 1
            if state == "active":
                result["active"] += 1
                if session["long_running"]:
                    result["long_running"] += 1
            elif state == "idle in transaction":
                result["idle_in_transaction"] += 1
        return result

    @classmethod
    def slow_queries_for_workspace(cls, workspace, *, limit: int = 15) -> dict:
        """Top statements by total time from ``pg_stat_statements``, scoped to
        the workspace's roles. Degrades with a reason instead of failing:
        ``available=False`` + ``reason`` when the extension isn't installed or
        the server is unreachable (sandbox_check discipline).
        """
        role_map = cls._workspace_role_map(workspace)
        result = {"available": True, "reason": "", "queries": []}
        if not role_map:
            return result
        try:
            with DatabaseService._admin_connect() as conn:
                installed = conn.execute(
                    "SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'"
                ).fetchone()
                if not installed:
                    return {
                        **result,
                        "available": False,
                        "reason": (
                            "pg_stat_statements is not installed in the data "
                            "database. A server admin can enable it with: "
                            "CREATE EXTENSION pg_stat_statements; (the server "
                            "must preload it via shared_preload_libraries)."
                        ),
                    }
                rows = conn.execute(
                    """
                    SELECT r.rolname, s.calls,
                           ROUND(s.total_exec_time::numeric, 1),
                           ROUND(s.mean_exec_time::numeric, 2),
                           s.rows, s.query
                    FROM pg_stat_statements s
                    JOIN pg_roles r ON r.oid = s.userid
                    JOIN pg_database d ON d.oid = s.dbid
                    WHERE r.rolname = ANY(%s) AND d.datname = current_database()
                    ORDER BY s.total_exec_time DESC
                    LIMIT %s
                    """,
                    # Fetch extra rows: the explorer's own statements are dropped below.
                    (list(role_map.keys()), int(limit) * 4),
                ).fetchall()
        except Exception as e:
            logger.warning("Databases slow-query stats unavailable: %s", e)
            return {**result, "available": False, "reason": str(e)}

        result["queries"] = [
            {
                "database": role_map[role].name if role in role_map else role,
                "calls": calls,
                "total_ms": float(total_ms),
                "mean_ms": float(mean_ms),
                "rows": nrows,
                "query": (query or "").strip(),
            }
            for role, calls, total_ms, mean_ms, nrows, query in rows
            if not is_viewer_statement(query)
        ][: int(limit)]
        return result
