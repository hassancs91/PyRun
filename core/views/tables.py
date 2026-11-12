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
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.grid import Grid, GridColumn, display_value
from core.models import Table
from core.tables.columns import TYPE_LABELS, TYPES
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


@login_required
def table_detail_view(request: HttpRequest, pk) -> HttpResponse:
    table = _table_or_404(request, pk)
    grid = Grid.from_request(
        request, _grid_columns(table), default_sort="id", editable=True,
        add_url=reverse("cpanel:table_row_add", kwargs={"pk": table.pk}), csrf_token=get_token(request),
    )
    try:
        qs = TableService.queryset(table, conditions=[(f.column, f.op, f.value) for f in grid.filters],
                                   order=grid.sort or "id")
        total = qs.count()
    except TableError as exc:
        # A filter value that doesn't fit its column (text in a number filter).
        grid.problems.append(exc.message)
        grid.filters = []
        qs = TableService.queryset(table, order=grid.sort or "id")
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
    return render(request, "cpanel/tables/detail.html", {
        "table": table, "grid": grid,
        "add_columns": [dict(c, label=TYPE_LABELS[c["type"]]) for c in table.columns],
        "cap_percent": min(100, round(table.row_count * 100 / table.row_cap)) if table.row_cap else 0,
    })


def _back(request, table, next_qs=None) -> HttpResponse:
    url = reverse("cpanel:table_detail", kwargs={"pk": table.pk})
    next_qs = next_qs if next_qs is not None else request.POST.get("next", "")
    if next_qs.startswith("?") and "\n" not in next_qs and "\r" not in next_qs:
        url += next_qs
    return redirect(url)


@login_required
@require_POST
def table_row_add_view(request: HttpRequest, pk) -> HttpResponse:
    """The grid's Add row form: one field ``c_<key>`` per column (empty = no value)."""
    table = _table_or_404(request, pk)
    values = {}
    for column in table.columns:
        name = f"c_{column['key']}"
        if name not in request.POST:
            continue
        raw = request.POST.get(name, "")
        if column["type"] == "json" and raw.strip():
            try:
                raw = json.loads(raw)
            except ValueError as exc:
                messages.error(request, f'Column "{column["key"]}": not valid JSON ({exc}).')
                return _back(request, table)
        if raw != "":
            values[column["key"]] = raw
    try:
        row = TableService.insert(table, [values], user=request.user)[0]
    except TableError as exc:
        messages.error(request, exc.message)
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
        row = TableService.update_row(table, row_id, {column["key"]: body.get("value")})
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
