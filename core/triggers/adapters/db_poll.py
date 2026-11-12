"""
``db_poll``: a run per new row of a query over a managed database (Databases).

The watcher names one of its workspace's databases, a read-only query and a
cursor column: a column of the query whose values only grow (a serial id, a
created_at). Each check asks for the rows past the last cursor value it
reported, in cursor order, through the Databases seam's read path
(``DatabaseExplorerService.query``: the database's own role, a READ ONLY
transaction, the SELECT-only guard, a statement timeout). The query is
wrapped, so it may be any SELECT the database's role can run:

    SELECT * FROM (<query>) AS pyrun_poll WHERE <cursor> > <last> ORDER BY <cursor> LIMIT <n>

State: ``{"cursor": <the last cursor value reported, as text, or None>}``.
The first check marks the query's largest cursor value and reports nothing.
Each row is one event; its id is the ``id_column`` value (default: the
cursor column), so rows that share a cursor value (two signups in the same
second) are told apart when an id column is given. Rows whose cursor is
NULL are never reported.
"""

import datetime
import re

from .base import TEST_ITEMS, Adapter, AdapterError, with_defaults

COLUMN_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"
# Rows asked for per check when the runner gives no limit (Test, a manual check).
DEFAULT_LIMIT = 100
# The query's statement timeout (the runner stops waiting for an adapter after 25 s).
TIMEOUT_MS = 10_000


def _text(value):
    """A cursor value as text Postgres casts back to the column's type (``> %s``)."""
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    return str(value)


def _jsonable(value):
    """A column value as JSON, as ``databases.query`` gives it (dates ISO text, numeric text...)."""
    from core.api.endpoints.databases import cell

    return cell(value)


def _wrapped(select, statement) -> str:
    """``SELECT <select> FROM (<statement>) AS pyrun_poll``, the statement on lines of its own
    (a trailing ``--`` comment in it ends there instead of hiding the rest)."""
    return f"SELECT {select} FROM (\n{statement}\n) AS pyrun_poll"


def _column(columns, name, setting):
    """The index of ``name`` among the query's columns (Postgres folds unquoted names to lower case)."""
    folded = name.lower()
    for index, column in enumerate(columns):
        if column == folded or column == name:
            return index
    raise AdapterError(f'The query has no column "{folded}" (the {setting}).')


class DbPollAdapter(Adapter):
    key = "db_poll"
    title = "New row from a database query"
    description = (
        "Runs a read-only query on one of this workspace's databases and starts a run for each row "
        "past the last cursor value (an id or a timestamp that only grows)."
    )
    config_schema = {
        "type": "object",
        "required": ["database", "query", "cursor_column"],
        "additionalProperties": False,
        "properties": {
            "database": {
                "type": "string", "maxLength": 100, "title": "Database",
                "description": "The name of a database of this workspace (Databases).",
            },
            "query": {
                "type": "string", "maxLength": 5000, "title": "Query",
                "description": "One SELECT, e.g. SELECT id, email, created_at FROM signups. Runs read-only "
                               "as the database's own role.",
            },
            "cursor_column": {
                "type": "string", "maxLength": 63, "pattern": COLUMN_PATTERN, "title": "Cursor column",
                "description": "A column of the query whose values only grow (id, created_at). Rows past "
                               "the last value start runs.",
            },
            "id_column": {
                "type": "string", "maxLength": 63, "pattern": COLUMN_PATTERN, "title": "Id column",
                "default": "",
                "description": "The column that identifies a row, when the cursor can repeat (a timestamp). "
                               "Empty: the cursor column.",
            },
        },
    }

    def _database(self, ctx, name):
        from core.models import Database
        from core.services.database_service import DatabaseService

        if not DatabaseService.is_configured():
            raise AdapterError("This instance has no data server (PYRUNNER_DATA_DB_URL), so no database to read.")
        if ctx.workspace is None:
            raise AdapterError("No workspace to find the database in.")
        database = Database.objects.filter(workspace=ctx.workspace, name=name).first()
        if database is None:
            raise AdapterError(f'This workspace has no database named "{name}".')
        if not database.is_ready:
            raise AdapterError(f'The database "{name}" isn\'t ready ({database.status}).')
        return database

    def _query(self, ctx, config, sql, params=None, rows=DEFAULT_LIMIT):
        from core.services.database_explorer import DatabaseExplorerError, DatabaseExplorerService

        database = self._database(ctx, config["database"])
        try:
            return DatabaseExplorerService.query(database, sql, row_cap=rows, timeout_ms=TIMEOUT_MS, params=params)
        except DatabaseExplorerError as e:
            raise AdapterError(f"The query failed: {e}") from None

    @staticmethod
    def _settings(config):
        from core.services.database_explorer import QueryRefused, read_only_statement

        try:
            statement = read_only_statement(config.get("query") or "")
        except QueryRefused as e:
            raise AdapterError(str(e)) from None
        for name in ("cursor_column", "id_column"):
            if config.get(name) and not re.match(COLUMN_PATTERN, config[name]):
                raise AdapterError(f"The {name.replace('_', ' ')} must be a plain column name.")
        # The wrapper binds a parameter: a literal % in the query is written %%.
        return statement.replace("%", "%%"), config["cursor_column"], config.get("id_column") or config["cursor_column"]

    def _rows(self, ctx, config, cursor, limit):
        statement, column, id_column = self._settings(config)
        where, params = (f"{column} > %s", (cursor,)) if cursor is not None else (f"{column} IS NOT NULL", None)
        result = self._query(
            ctx, config,
            f"{_wrapped('*', statement)} WHERE {where} ORDER BY {column} LIMIT {limit + 1}",
            params, rows=limit + 1,
        )
        at = _column(result.columns, column, "cursor column")
        key = _column(result.columns, id_column, "id column")
        return [(row[at], str(_jsonable(row[key])), dict(zip(result.columns, (_jsonable(v) for v in row))))
                for row in result.rows]

    def check(self, ctx, config, state):
        config = with_defaults(self.config_schema, config)
        if not isinstance(state, dict):
            statement, column, _id = self._settings(config)
            result = self._query(ctx, config, _wrapped(f"max({column})", statement), rows=1)
            top = result.rows[0][0] if result.rows else None
            return [], {"cursor": _text(top) if top is not None else None}
        cursor = state.get("cursor")
        limit = ctx.limit if ctx.limit is not None else DEFAULT_LIMIT
        rows = self._rows(ctx, config, cursor, limit)
        if len(rows) > limit:
            # Cut at a cursor value boundary: rows sharing the first value left
            # out are all reported next time (the cursor only moves past
            # values whose rows were all returned).
            boundary = rows[limit][0]
            rows = [row for row in rows[:limit] if row[0] != boundary]
            if not rows:
                # The runner's limit is what is left of the watcher's fire cap
                # this hour. A group that fits a whole hour of the cap waits
                # for it (cursor kept, nothing reported) like any item past
                # the cap; only a group bigger than the cap itself is an error.
                cap = getattr(ctx.watcher, "fire_cap_per_hour", None) if ctx.limit is not None else None
                if cap and limit < cap:
                    group = self._rows(ctx, config, cursor, cap)
                    if sum(1 for row in group if row[0] == boundary) <= cap:
                        return [], {"cursor": cursor}
                if cap:
                    raise AdapterError(
                        f"More than {cap} rows share the cursor value {_text(boundary)!r}, more than the "
                        f"watcher's fire cap ({cap} runs an hour) can start: raise the fire cap, or pick a "
                        "cursor column whose values don't repeat that much."
                    )
                raise AdapterError(
                    f"More than {limit} rows share the cursor value {_text(boundary)!r}: pick a cursor column "
                    "whose values don't repeat that much."
                )
        events = [{"id": row_id, "item": item} for _value, row_id, item in rows]
        if rows:
            cursor = _text(rows[-1][0])
        return events, {"cursor": cursor}

    def test(self, ctx, config):
        config = with_defaults(self.config_schema, config)
        statement, column, _id = self._settings(config)
        result = self._query(
            ctx, config,
            f"{_wrapped('*', statement)} WHERE {column} IS NOT NULL ORDER BY {column} DESC LIMIT {TEST_ITEMS}",
            rows=TEST_ITEMS,
        )
        _column(result.columns, column, "cursor column")
        _column(result.columns, config.get("id_column") or column, "id column")
        if not result.rows:
            return "The query runs; it has no rows with a cursor value yet.", []
        newest = _text(result.rows[0][_column(result.columns, column, "cursor column")])
        items = [dict(zip(result.columns, (_jsonable(v) for v in row))) for row in result.rows]
        return f"The query runs; the newest {column} is {newest!r}.", items
