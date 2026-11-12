"""Tables over REST, MCP and Py AI (2.5 Stage 9): reads, and row writes behind the switch.

Every call goes through ``TableService``, the same door the console and the
``pyrunner_tables`` helper use, so values are checked and converted against
the columns, the row cap holds and ``row_count`` stays exact. A table is named
by its slug (or its name) in the caller's workspace, plugin tables included
(the console shows them to every member too). A token bound to one table
(``APIToken.table``) reaches that table alone and may leave the name out; any
other table answers 403. Row values are stored data and come back as stored
(not masked), like datastore values. Table and column changes stay in the
console (and the plugin SDK): over the API only rows change.
"""

from core.api.errors import ApiError
from core.api.registry import endpoint
from core.models import Table
from core.tables.errors import STATUS_FOR_CODE, TableError
from core.tables.service import MAX_PAGE, TableService, serialize_row

MAX_WRITE_ROWS = 1000
NAME = {"type": "string", "maxLength": 100,
        "description": "The table's slug (or name); optional for a token bound to one table"}
ROW = {"type": "object", "additionalProperties": True,
       "description": "Column key: value. None (null) empties a cell."}
ROW_ID = {"type": "integer", "minimum": 1, "description": "The row's id"}
WHERE = {
    "type": "object",
    "additionalProperties": True,
    "description": (
        'Conditions that must all match: {"field": value} (null: empty) or {"field": {"$op": value}} '
        "with $eq, $ne, $gt, $gte, $lt, $lte, $in (a list), $contains / $prefix (text, ignoring case) "
        "or $empty (true/false). Fields are column keys plus id, created_at and updated_at "
        '(e.g. {"created_at": {"$gte": "2026-11-09"}} for rows added since that day).'
    ),
}
ORDER = {
    "type": ["string", "array"],
    "items": {"type": "string", "maxLength": 70},
    "maxItems": 5,
    "description": 'A field to sort by, "-field" for descending, or a list of them. Default: id.',
}


def table_input(properties=None, required=()):
    return {
        "type": "object",
        "properties": {"table": NAME, **(properties or {})},
        "required": list(required),
    }


def tables_for(ctx):
    """The tables this caller may list: its workspace's, or its bound table."""
    tables = Table.objects.for_workspace(ctx.workspace)
    if ctx.table_id is not None:
        tables = tables.filter(pk=ctx.table_id)
    return tables


def resolve_table(ctx, name):
    """The table ``name`` names in the caller's workspace; a bound token may leave it out."""
    if name is None:
        if ctx.table_id is None:
            raise ApiError("invalid_input", "$.table: pass the table's slug or name.", status=400)
        table = tables_for(ctx).first()
    else:
        try:
            table = Table.resolve_for_workspace(name, ctx.workspace.pk)
        except Table.DoesNotExist:
            table = None
    if table is None:
        raise ApiError("not_found", f"Table '{name}' not found" if name is not None else "Unknown table.",
                       status=404)
    if ctx.table_id is not None and table.pk != ctx.table_id:
        raise ApiError("forbidden", "Token does not have access to this table", status=403)
    return table


def _service(operation):
    try:
        return operation()
    except TableError as exc:
        raise ApiError(exc.code, exc.message, status=STATUS_FOR_CODE.get(exc.code, 400)) from None


def summary(table):
    return {
        "name": table.name,
        "slug": table.slug,
        "description": table.description,
        "columns": [{"key": c["key"], "type": c["type"], "required": c["required"]} for c in table.columns],
        "row_count": table.row_count,
        "owner_plugin": table.owner_plugin,
        "created_at": table.created_at.isoformat(),
        "updated_at": table.updated_at.isoformat(),
    }


@endpoint(
    "tables.list",
    title="List tables",
    description=(
        "Tables in this workspace (or the one this token is bound to), by name, with their slug, "
        "columns (key, type, required) and row counts."
    ),
    input={"type": "object", "properties": {}},
    permission="tables:read",
    pyai_tier="channel",
)
def list_tables(ctx, params):
    tables = list(tables_for(ctx).order_by("name"))
    return {"tables": [summary(t) for t in tables], "count": len(tables)}


@endpoint(
    "tables.get",
    title="Get table",
    description="One table's columns (with their defaults), row count, row limit and timestamps.",
    input=table_input(),
    permission="tables:read",
    pyai_tier="channel",
)
def get_table(ctx, params):
    table = resolve_table(ctx, params.get("table"))
    out = summary(table)
    out["columns"] = TableService.describe(table)["columns"]
    out["row_cap"] = table.row_cap
    return out


@endpoint(
    "tables.rows",
    title="Query table rows",
    description=(
        "A table's rows that match `where`, sorted by `order`, a page at a time "
        f"(limit 1-{MAX_PAGE}, default 100, from offset). count is how many rows match in all; "
        "pass limit 1 to just count. Each row has id, every column, created_at and updated_at (UTC)."
    ),
    input=table_input({
        "where": WHERE,
        "order": ORDER,
        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE},
        "offset": {"type": "integer", "minimum": 0},
    }),
    permission="tables:read",
    pyai_tier="channel",
)
def query_rows(ctx, params):
    table = resolve_table(ctx, params.get("table"))
    where, order = params.get("where"), params.get("order")
    limit, offset = params.get("limit", 100), params.get("offset", 0)
    rows = _service(lambda: TableService.rows(table, where=where, order=order, limit=limit, offset=offset))
    count = _service(lambda: TableService.count(table, where=where))
    return {"table": table.slug, "rows": rows, "count": count, "limit": limit, "offset": offset}


@endpoint(
    "tables.insert",
    title="Insert table rows",
    description=(
        f"Insert 1-{MAX_WRITE_ROWS} rows (objects of column: value), all or nothing. Values are "
        "checked and converted against the columns; unknown columns, missing required values and "
        "rows past the table's row limit are refused."
    ),
    input=table_input(
        {"rows": {"type": "array", "items": ROW, "maxItems": MAX_WRITE_ROWS}},
        ("rows",),
    ),
    access="write",
    permission="tables:write",
    rate=(30, 60),
)
def insert_rows(ctx, params):
    table = resolve_table(ctx, params.get("table"))
    created = _service(lambda: TableService.insert(table, params["rows"]))
    return {"table": table.slug, "inserted": len(created), "ids": [row.id for row in created]}


@endpoint(
    "tables.update",
    title="Update a table row",
    description="Change some cells of one row (column: value; null empties a cell) and get the row back.",
    input=table_input({"id": ROW_ID, "values": ROW}, ("id", "values")),
    access="write",
    permission="tables:write",
    rate=(30, 60),
)
def update_row(ctx, params):
    table = resolve_table(ctx, params.get("table"))
    row = _service(lambda: TableService.update_row(table, params["id"], params["values"]))
    return {"table": table.slug, "row": serialize_row(row, table.columns)}


@endpoint(
    "tables.delete",
    title="Delete table rows",
    description=(
        f"Delete rows by id (1-{MAX_WRITE_ROWS}). Ids that aren't there are skipped; deleted says "
        "how many went."
    ),
    input=table_input(
        {"ids": {"type": "array", "items": ROW_ID, "maxItems": MAX_WRITE_ROWS}},
        ("ids",),
    ),
    access="write",
    permission="tables:write",
    rate=(30, 60),
)
def delete_rows(ctx, params):
    table = resolve_table(ctx, params.get("table"))
    return {"table": table.slug, "deleted": _service(lambda: TableService.delete_rows(table, params["ids"]))}

