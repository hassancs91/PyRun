"""Managed databases over REST, MCP and Py AI (2.5 Stage 3, T17).

Three reads, all ``databases:read``:

- ``databases.list``: the workspace's databases;
- ``databases.tables``: a database's tables, or one table with its columns;
- ``databases.query``: one SELECT, through ``DatabaseExplorerService.query``
  (the database's own role, a READ ONLY transaction, a 5 s statement timeout,
  200 rows at most, and the SELECT-only guard).

Py AI on a chat channel gets the first two; ``databases.query`` is console only
(plan D9). Rows are stored data and come back as stored, like datastore values.

The databases live on a separate Postgres data server (``PYRUNNER_DATA_DB_URL``).
An instance without one (a SQLite-only instance, typically) still lists its
database rows, with ``data_server.configured: false`` and the reason, and the
other two answer 409 ``not_configured`` with the same reason.
"""

import datetime
import decimal
import logging
import math

from core.api.errors import ApiError
from core.api.registry import endpoint
from core.models import Database
from core.services.database_explorer import (
    QUERY_ROW_CAP,
    QUERY_TIMEOUT_MS,
    DatabaseExplorerError,
    DatabaseExplorerService,
    DatabaseUnavailable,
    QueryRefused,
    QueryTimeout,
)
from core.services.database_service import DatabaseService

logger = logging.getLogger(__name__)

NOT_CONFIGURED = (
    "No data server is configured on this instance, so its databases can't be read. "
    "An administrator attaches one by setting PYRUNNER_DATA_DB_URL to a Postgres server."
)
UNAVAILABLE = "The data server can't be reached right now. Check the Databases page and retry."
MAX_SQL_CHARS = 20_000
NAME = {"type": "string", "maxLength": 100, "description": "The database's name"}


def databases_for(ctx):
    return Database.objects.for_workspace(ctx.workspace)


def resolve_database(ctx, name):
    """The database ``name`` names in the caller's workspace, ready to read."""
    database = databases_for(ctx).filter(name=name).first()
    if database is None:
        raise ApiError("not_found", f"Database '{name}' not found", status=404)
    if not DatabaseService.is_configured():
        raise ApiError("not_configured", NOT_CONFIGURED, status=409)
    if database.status != Database.STATUS_READY:
        raise ApiError(
            "not_ready",
            f"Database '{name}' is not ready ({database.get_status_display().lower()}); "
            "see its page in the console.",
            status=409,
        )
    return database


def unavailable(database, exc):
    """The 503 for a failed catalog read; the server's message stays in the log."""
    logger.warning("databases api: %s unavailable: %s", database.name, exc)
    return ApiError("unavailable", UNAVAILABLE, status=503)


def cell(value):
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
        return [cell(v) for v in value]
    if isinstance(value, dict):
        return {str(k): cell(v) for k, v in value.items()}
    return str(value)


def table_data(entry):
    return {
        "name": entry["name"],
        "row_estimate": entry["row_estimate"],
        "row_exact": entry["row_exact"],
        "size_bytes": entry["size_bytes"],
    }


@endpoint(
    "databases.list",
    title="List databases",
    description=(
        "Managed Postgres databases in this workspace, by name, with their status. "
        "data_server says whether this instance has a data server to read them on."
    ),
    input={"type": "object", "properties": {}},
    permission="databases:read",
    pyai_tier="channel",
)
def list_databases(ctx, params):
    rows = list(databases_for(ctx).order_by("name"))
    server = {"configured": DatabaseService.is_configured()}
    if not server["configured"]:
        server["reason"] = NOT_CONFIGURED
    return {
        "databases": [
            {
                "name": d.name,
                "description": d.description,
                "status": d.status,
                "created_at": d.created_at.isoformat(),
                "updated_at": d.updated_at.isoformat(),
            }
            for d in rows
        ],
        "count": len(rows),
        "data_server": server,
    }


@endpoint(
    "databases.tables",
    title="List database tables",
    description=(
        "A database's tables with row counts (row_exact false: the planner's estimate) "
        "and sizes. Pass table for that one table with its columns (name, type, "
        "nullable, default)."
    ),
    input={
        "type": "object",
        "properties": {
            "database": NAME,
            "table": {"type": "string", "maxLength": 63, "description": "One table's name"},
        },
        "required": ["database"],
    },
    permission="databases:read",
    pyai_tier="channel",
)
def list_tables(ctx, params):
    database = resolve_database(ctx, params["database"])
    try:
        tables = DatabaseExplorerService.tables(database)
        if "table" in params:
            tables = [t for t in tables if t["name"] == params["table"]]
            if not tables:
                raise ApiError(
                    "not_found",
                    f"Table '{params['table']}' not found in database '{database.name}'",
                    status=404,
                )
            columns = DatabaseExplorerService.columns(database, params["table"])
            return {
                "database": database.name,
                "tables": [{**table_data(tables[0]), "columns": columns}],
                "count": 1,
            }
    except DatabaseExplorerError as exc:
        raise unavailable(database, exc) from None
    return {
        "database": database.name,
        "tables": [table_data(t) for t in tables],
        "count": len(tables),
    }


@endpoint(
    "databases.query",
    title="Query a database",
    description=(
        "Run one read-only SELECT (or WITH, VALUES, TABLE) on a managed database, as "
        "its own role: name tables without a schema. A read-only transaction, at most "
        f"{QUERY_ROW_CAP} rows (truncated says there were more) and "
        f"{QUERY_TIMEOUT_MS // 1000} seconds. Prefer count(*), WHERE and LIMIT to "
        "answer with few rows. Rows are lists in the order of columns."
    ),
    input={
        "type": "object",
        "properties": {
            "database": NAME,
            "sql": {
                "type": "string",
                "maxLength": MAX_SQL_CHARS,
                "description": "One SELECT statement",
            },
        },
        "required": ["database", "sql"],
    },
    permission="databases:read",
    rate=(30, 60),
)
def query_database(ctx, params):
    database = resolve_database(ctx, params["database"])
    try:
        result = DatabaseExplorerService.query(database, params["sql"])
    except QueryRefused as exc:
        raise ApiError("invalid_input", f"$.sql: {exc}", status=400) from None
    except QueryTimeout as exc:
        raise ApiError("query_timeout", str(exc), status=400) from None
    except DatabaseUnavailable as exc:
        raise unavailable(database, exc) from None
    except DatabaseExplorerError as exc:
        raise ApiError("query_failed", str(exc) or "The query failed.", status=400) from None
    return {
        "database": database.name,
        "columns": result.columns,
        "rows": [[cell(v) for v in row] for row in result.rows],
        "row_count": len(result.rows),
        "truncated": result.truncated,
        "row_cap": QUERY_ROW_CAP,
    }
