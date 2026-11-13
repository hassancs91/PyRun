"""
Tables console (2.5 Stage 9): create tables with typed columns, work with
their rows in the shared grid (core/grid.py).

Open to every member of the workspace, like Data Stores: a table is data the
workspace's scripts read and write, and nothing here spends a provider budget.
Every write goes through ``TableService`` (the same checks as ``pyrunner_tables``).
A plugin's table keeps its columns (the plugin provisions them); its rows can
be edited here, and only the plugin or a superuser's force-delete removes it.
"""

import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse, StreamingHttpResponse
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.grid import Grid, GridColumn, display_value, json_cell_value, next_querystring
from core.models import Table
from core.tables import csv_io
from core.tables.columns import TYPE_LABELS, TYPES, coerce
from core.tables.errors import TableError
from core.tables.forms import TableForm
from core.tables.service import TableService
from core.views.ownership import owned_block_message, owned_delete_blocked


def _table_or_404(request, pk) -> Table:
    return get_object_or_404(Table, pk=pk, workspace=request.workspace)


def _grid_columns(table: Table) -> list:
    columns = [GridColumn("id", "id", "id")]
    columns += [
        GridColumn(c["key"], c["key"], c["type"], sortable=c["type"] != "json", editable=True, required=c["required"])
        for c in table.columns
    ]
    columns += [GridColumn("updated_at", "updated", "timestamp", filterable=True)]
    return columns


def _row_values(row) -> dict:
    values = dict(row.data)
    values["id"] = row.id
    values["updated_at"] = row.updated_at.strftime("%Y-%m-%d %H:%M:%S UTC")
    return values


@login_required
def table_list_view(request: HttpRequest) -> HttpResponse:
    tables = list(Table.objects.for_workspace(request.workspace).select_related("created_by").order_by("name"))
    owners = sorted({t.owner_plugin for t in tables if t.owner_plugin})
    owner_filter = request.GET.get("owner_plugin")
    if owner_filter:
        tables = [t for t in tables if t.owner_plugin == owner_filter]
    return render(request, "cpanel/tables/list.html", {
        "tables": tables, "owners": owners, "selected_owner": owner_filter or "",
    })


def _form_context(form, table=None) -> dict:
    types = [(t, TYPE_LABELS[t]) for t in TYPES]
    return {"form": form, "table": table, "types": types, "types_json": json.dumps(types)}


@login_required
def table_create_view(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = TableForm(request.POST, workspace=request.workspace)
        if form.is_valid():
            try:
                table = TableService.create_table(
                    workspace=request.workspace, name=form.cleaned_data["name"], slug=form.cleaned_data["slug"],
                    description=form.cleaned_data["description"], columns=form.cleaned_data["columns"],
                    created_by=request.user,
                )
            except TableError as exc:
                form.add_error(None, exc.message)
            else:
                messages.success(request, f'Table "{table.name}" created.')
                return redirect("cpanel:table_detail", pk=table.pk)
    else:
        form = TableForm(workspace=request.workspace)
    return render(request, "cpanel/tables/form.html", _form_context(form))


@login_required
def table_edit_view(request: HttpRequest, pk) -> HttpResponse:
    table = _table_or_404(request, pk)
    if request.method == "POST":
        form = TableForm(request.POST, workspace=request.workspace, instance=table)
        if form.is_valid():
            try:
                TableService.update_table(
                    table, name=form.cleaned_data["name"], slug=form.cleaned_data["slug"],
                    description=form.cleaned_data["description"],
                    # A plugin's columns are the plugin's; the console leaves them alone.
                    columns=None if table.owner_plugin else form.cleaned_data["columns"],
                )
            except TableError as exc:
                table.refresh_from_db()
                form.add_error(None, exc.message)
            else:
                messages.success(request, f'Table "{table.name}" updated.')
                return redirect("cpanel:table_detail", pk=table.pk)
    else:
        form = TableForm(workspace=request.workspace, instance=table)
    return render(request, "cpanel/tables/form.html", _form_context(form, table))


@login_required
@require_POST
def table_delete_view(request: HttpRequest, pk) -> HttpResponse:
    table = _table_or_404(request, pk)
    if owned_delete_blocked(request, table):
        messages.error(request, owned_block_message(table, "table"))
        return redirect("cpanel:table_list")
    name = table.name
    TableService.delete_table(table)
    messages.success(request, f'Table "{name}" and its rows deleted.')
    return redirect("cpanel:table_list")


def _filtered_rows(table, grid):
    """The grid's rows (its filters and sort); a filter that doesn't fit is dropped with a note."""
    # "is one of" comes from the grid as comma-separated text (as on the other grids).
    conditions = [
        (f.column, f.op, [v.strip() for v in f.value.split(",") if v.strip()] if f.op == "in" else f.value)
        for f in grid.filters
    ]
    try:
        return TableService.queryset(table, conditions=conditions, order=grid.sort or "id")
    except TableError as exc:
        # A filter value that doesn't fit its column (text in a number filter).
        grid.problems.append(exc.message)
        grid.filters = []
        return TableService.queryset(table, order=grid.sort or "id")


@login_required
def table_detail_view(request: HttpRequest, pk) -> HttpResponse:
    table = _table_or_404(request, pk)
    # What was typed into a refused Add row form (table_row_add_view).
    kept = request.session.pop(_kept_key(table), None)
    grid = Grid.from_request(
        request, _grid_columns(table), default_sort="id", editable=True,
        add_url=reverse("cpanel:table_row_add", kwargs={"pk": table.pk}), csrf_token=get_token(request),
    )
    qs = _filtered_rows(table, grid)
    total = qs.count()
    grid.clamp_page(total)
    rows = [
        grid.make_row(
            row.id, _row_values(row),
            cell_url=reverse("cpanel:table_row_cell", kwargs={"pk": table.pk, "row_id": row.id}),
            delete_url=reverse("cpanel:table_row_delete", kwargs={"pk": table.pk, "row_id": row.id}),
        )
        for row in qs[grid.offset:grid.offset + grid.page_size]
    ]
    grid.set_rows(rows, total)
    query = request.GET.copy()
    query.pop("page", None)
    return render(request, "cpanel/tables/detail.html", {
        "table": table, "grid": grid,
        "export_url": reverse("cpanel:table_export", kwargs={"pk": table.pk})
        + (f"?{query.urlencode()}" if query else ""),
        "add_columns": _add_columns(table, kept),
        "add_open": not grid.total or kept is not None,
        "cap_percent": min(100, round(table.row_count * 100 / table.row_cap)) if table.row_cap else 0,
    })


def _back(request, table, next_qs=None) -> HttpResponse:
    next_qs = next_qs if next_qs is not None else request.POST.get("next", "")
    return redirect(reverse("cpanel:table_detail", kwargs={"pk": table.pk}) + next_querystring(next_qs))


def _kept_key(table) -> str:
    return f"pyrun_table_add_{table.pk}"


def _add_columns(table, kept):
    """The Add row form's fields, each with the value to show: what was typed
    before a refused submit (``kept``), else the column's default."""
    columns = []
    for c in table.columns:
        if kept is not None:
            value = kept.get(c["key"], "")
        elif c.get("default") is None or c["type"] == "json":
            value = ""
        elif c["type"] == "boolean":
            value = "true" if c["default"] else "false"
        else:
            value = str(c["default"])
        columns.append(dict(c, label=TYPE_LABELS[c["type"]], value=value))
    return columns


@login_required
@require_POST
def table_row_add_view(request: HttpRequest, pk) -> HttpResponse:
    """The grid's Add row form: one field ``c_<key>`` per column (empty = no value).

    Every bad column is reported at once, and a refused row comes back with
    what was typed (kept in the session for the next page view).
    """
    table = _table_or_404(request, pk)
    typed, values, errors, bad = {}, {}, [], set()
    for column in table.columns:
        name = f"c_{column['key']}"
        if name not in request.POST:
            continue
        raw = typed[column["key"]] = request.POST.get(name, "")
        if column["type"] == "json" and raw.strip():
            try:
                raw = json.loads(raw)
            except ValueError as exc:
                errors.append(f'Column "{column["key"]}": not valid JSON ({exc}).')
                bad.add(column["key"])
                continue
        if raw == "":
            continue
        try:
            coerce(column, raw)
        except TableError as exc:
            errors.append(exc.message)
            bad.add(column["key"])
            continue
        values[column["key"]] = raw
    missing = [
        c["key"] for c in table.columns
        if c.get("required") and c.get("default") is None and c["key"] not in values
        and c["key"] not in bad
    ]
    if missing:
        errors.append(f"Required column{'s' if len(missing) > 1 else ''} without a value: {', '.join(missing)}.")
    if not errors:
        try:
            row = TableService.insert(table, [values], user=request.user)[0]
        except TableError as exc:
            errors.append(exc.message)
    if errors:
        for error in errors:
            messages.error(request, error)
        request.session[_kept_key(table)] = typed
        return _back(request, table)
    messages.success(request, f"Row {row.id} added.")
    return _back(request, table, "?sort=-id")


@login_required
@require_POST
def table_row_cell_view(request: HttpRequest, pk, row_id) -> JsonResponse:
    """Inline edit of one cell: JSON {"column", "value"} -> {"html", "raw"} or 400 {"error"}."""
    table = _table_or_404(request, pk)
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"error": "The request must be JSON."}, status=400)
    column = next((c for c in table.columns if isinstance(body, dict) and c["key"] == body.get("column")), None)
    if column is None:
        return JsonResponse({"error": "Unknown column."}, status=400)
    try:
        value = json_cell_value(body) if column["type"] == "json" else body.get("value")
    except ValueError as exc:
        return JsonResponse({"error": f"Not valid JSON: {exc}"}, status=400)
    try:
        row = TableService.update_row(table, row_id, {column["key"]: value})
    except TableError as exc:
        return JsonResponse({"error": exc.message}, status=404 if exc.code == "row_not_found" else 400)
    value = row.data.get(column["key"])
    cell = {
        "column": GridColumn(column["key"], column["key"], column["type"], editable=True),
        "value": value, "display": display_value(column["type"], value),
    }
    return JsonResponse({
        "html": render_to_string("cpanel/_grid_cell.html", {"cell": cell}).strip(),
        "raw": json.dumps(value, ensure_ascii=False),
    })


@login_required
@require_POST
def table_row_delete_view(request: HttpRequest, pk, row_id) -> HttpResponse:
    table = _table_or_404(request, pk)
    try:
        deleted = TableService.delete_rows(table, [row_id])
    except TableError as exc:
        messages.error(request, exc.message)
        return _back(request, table)
    if deleted:
        messages.success(request, f"Row {row_id} deleted.")
    else:
        messages.error(request, f"Row {row_id} was already gone.")
    return _back(request, table)


@login_required
def table_export_view(request: HttpRequest, pk) -> HttpResponse:
    """The grid's rows as CSV (its filters and sort; every page), streamed."""
    table = _table_or_404(request, pk)
    grid = Grid.from_request(request, _grid_columns(table), default_sort="id")
    response = StreamingHttpResponse(csv_io.stream_csv(table, _filtered_rows(table, grid)),
                                     content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{table.slug}.csv"'
    return response


@login_required
def table_import_view(request: HttpRequest, pk) -> HttpResponse:
    """Import a CSV file: a header line, then one row per line; checked whole before anything is written.

    POST ``file``; ``mapping`` (optional JSON: one column key or null per header
    cell, as the page's mapping step sends it, or ``{"CSV header": "column key"}``;
    without it headers match keys by name);
    ``dry_run`` to check only. A request that accepts JSON gets the report as JSON.
    """
    table = _table_or_404(request, pk)
    context = {"table": table, "columns_json": json.dumps([c["key"] for c in table.columns]),
               "max_mb": csv_io.MAX_IMPORT_BYTES // (1024 * 1024)}
    if request.method != "POST":
        return render(request, "cpanel/tables/import.html", context)
    wants_json = "application/json" in request.headers.get("Accept", "")
    dry_run = bool(request.POST.get("dry_run"))
    try:
        upload = request.FILES.get("file")
        if upload is None:
            raise TableError("Choose a CSV file to import.", "invalid")
        if upload.size > csv_io.MAX_IMPORT_BYTES:
            raise TableError(f"The file is larger than {context['max_mb']} MB. Split it, or import it from a "
                             "script with pyrunner_tables (no size limit there).", "too_large")
        mapping = None
        if request.POST.get("mapping"):
            try:
                mapping = json.loads(request.POST["mapping"])
            except ValueError:
                raise TableError("The column mapping isn't valid JSON.", "invalid") from None
            if not isinstance(mapping, (dict, list)):
                raise TableError("The column mapping must map CSV headers to column keys.", "invalid")
        report = csv_io.import_csv(table, csv_io.open_text(upload.file), mapping=mapping, dry_run=dry_run,
                                   user=request.user)
    except TableError as exc:
        if wants_json:
            return JsonResponse({"error": exc.message}, status=400)
        context["error"] = exc.message
        return render(request, "cpanel/tables/import.html", context, status=400)
    if wants_json:
        return JsonResponse(report)
    if report["inserted"]:
        messages.success(request, f"Imported {report['inserted']} row{'s' if report['inserted'] != 1 else ''}.")
        return redirect("cpanel:table_detail", pk=table.pk)
    context["report"] = report
    return render(request, "cpanel/tables/import.html", context)
