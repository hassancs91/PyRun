"""
Internal Tables API (loopback-only) — the door ``pyrunner_tables`` uses.

A run has no database access, so the helper POSTs here and PyRun does the work
through ``TableService`` (the same code the console uses). Authenticated by the
signed per-run token (``internal_datastore_token_required``); the scope comes
from the run, server-side:

- the **workspace**: the table is resolved by slug (else name) inside the
  run's workspace, so a script can't reach another tenant's table;
- the **access rule**: a plugin-owned table is reachable from that plugin's
  scripts only, any other table from every script in the workspace. A table the
  run may not use answers exactly like a missing one (404).

POST /internal/tables/<name>/<op>, ``op`` one of:

    info    {}                                       -> {"table": {...}}
    insert  {"rows": [{...}, ...]}                   -> {"inserted", "ids", "rows"}
    rows    {"where"?, "order"?, "limit"?, "offset"?} -> {"rows": [...]}
    get     {"id"}                                   -> {"row": {...} | null}
    update  {"id", "values": {...}}                  -> {"row": {...}}
    upsert  {"key", "values": {...}}                 -> {"row": {...}, "created": bool}
    delete  {"ids": [...]}                           -> {"deleted": n}
    count   {"where"?}                               -> {"count": n}
    csv_check   {"header", "mapping"?, "lines", "first_line"?} -> {"rows", "errors", "error_count", "mapping"}
    csv_insert  {"header", "mapping"?, "lines", "first_line"?} -> {"inserted": n}
    csv_export  {"where"?, "order"?, "limit"?, "offset"?}       -> {"header": [...], "lines": [[...]]}

The ``csv_*`` ops serve the helper's ``import_csv`` / ``export_csv``: CSV text
cells in and out, converted by ``core.tables.csv_io`` exactly as the console's
import and export do (``mapping``: one column key or null per header cell, or
``{header: key}``; left out, headers match column keys by name).

Errors: ``{"error": {"code", "message"}}`` with the ``TableError`` code, mapped
by ``STATUS_FOR_CODE`` (``not_found`` 404, ``invalid`` 400, ``row_cap`` 409,
``too_large`` 413, ...).
"""

import json

from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from core.tables.errors import STATUS_FOR_CODE, TableError
from core.views.api.decorators import internal_datastore_token_required

# One request carries at most this much JSON and this many rows; the helper
# sends inserts in batches well below both.
MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_ITEMS = 1000
OPS = ("info", "insert", "rows", "get", "update", "upsert", "delete", "count", "csv_check", "csv_insert",
       "csv_export")


def _error(code: str, message: str) -> JsonResponse:
    return JsonResponse({"error": {"code": code, "message": message}}, status=STATUS_FOR_CODE.get(code, 500))


def _payload(request):
    raw = request.read(MAX_BODY_BYTES + 1)
    if len(raw) > MAX_BODY_BYTES:
        return None, _error("too_large", f"Request exceeds {MAX_BODY_BYTES} bytes.")
    try:
        data = json.loads(raw or b"{}")
    except (ValueError, TypeError):
        return None, _error("invalid", "Body must be valid JSON.")
    if not isinstance(data, dict):
        return None, _error("invalid", "Body must be a JSON object.")
    return data, None


def _resolve(request, name):
    """The table this run may use, or ``(None, error_response)``."""
    from core.models import Run, Table

    run = (
        Run.objects.filter(id=request.datastore_run.get("run_id"))
        .values("script_id", "script__owner_plugin")
        .first()
    )
    missing = _error("not_found", f"Table '{name}' does not exist or is not available to this script.")
    if not run or run["script_id"] is None:
        return None, missing
    try:
        table = Table.resolve_for_workspace(name, request.datastore_workspace)
    except Table.DoesNotExist:
        return None, missing
    if table.owner_plugin and table.owner_plugin != run["script__owner_plugin"]:
        return None, missing
    return table, None


@csrf_exempt
@require_http_methods(["POST"])
@internal_datastore_token_required
def table_op(request: HttpRequest, name: str, op: str) -> JsonResponse:
    if op not in OPS:
        return _error("invalid", f"Unknown operation '{op}'.")
    data, err = _payload(request)
    if err:
        return err
    table, err = _resolve(request, name)
    if err:
        return err

    from core.tables.service import TableService, serialize_row

    try:
        if op == "info":
            return JsonResponse({"table": TableService.describe(table)})
        if op == "insert":
            items = data.get("rows")
            if isinstance(items, list) and len(items) > MAX_ITEMS:
                raise TableError(f"Insert at most {MAX_ITEMS} rows per call.", "too_large")
            created = TableService.insert(table, items)
            body = {"inserted": len(created), "ids": [r.id for r in created]}
            if len(created) == 1:
                body["rows"] = [serialize_row(created[0], table.columns)]
            return JsonResponse(body)
        if op == "rows":
            return JsonResponse({"rows": TableService.rows(
                table, where=data.get("where"), order=data.get("order"),
                limit=data.get("limit"), offset=data.get("offset"),
            )})
        if op in ("csv_check", "csv_insert"):
            from core.tables import csv_io

            lines, first_line = data.get("lines"), data.get("first_line", 2)
            if isinstance(lines, list) and len(lines) > MAX_ITEMS:
                raise TableError(f"Send at most {MAX_ITEMS} CSV lines per call.", "too_large")
            if isinstance(first_line, bool) or not isinstance(first_line, int):
                raise TableError("first_line must be a whole number.", "invalid")
            args = (table, data.get("header"), data.get("mapping"), lines)
            if op == "csv_check":
                return JsonResponse(csv_io.check_lines(*args, first_line=first_line))
            return JsonResponse({"inserted": csv_io.insert_lines(*args, first_line=first_line)})
        if op == "csv_export":
            from core.tables import csv_io

            rows = TableService.rows(table, where=data.get("where"), order=data.get("order"),
                                     limit=data.get("limit"), offset=data.get("offset"))
            types = {c["key"]: c["type"] for c in table.columns}
            header = csv_io.header_for(table.columns)
            return JsonResponse({"header": header, "lines": [
                [csv_io.cell_to_csv(types.get(key, "text"), row.get(key)) for key in header] for row in rows
            ]})
        if op == "count":
            return JsonResponse({"count": TableService.count(table, where=data.get("where"))})
        if op == "get":
            try:
                row = TableService.get_row(table, data.get("id"))
            except TableError as exc:
                if exc.code != "row_not_found":
                    raise
                return JsonResponse({"row": None})
            return JsonResponse({"row": serialize_row(row, table.columns)})
        if op == "update":
            row = TableService.update_row(table, data.get("id"), data.get("values"))
            return JsonResponse({"row": serialize_row(row, table.columns)})
        if op == "upsert":
            row, created = TableService.upsert(table, data.get("key"), data.get("values"))
            return JsonResponse({"row": serialize_row(row, table.columns), "created": created})
        ids = data.get("ids")
        if not isinstance(ids, list) or len(ids) > MAX_ITEMS:
            raise TableError(f"delete takes a list of at most {MAX_ITEMS} row ids.", "invalid")
        return JsonResponse({"deleted": TableService.delete_rows(table, ids)})
    except TableError as exc:
        return _error(exc.code, exc.message)
