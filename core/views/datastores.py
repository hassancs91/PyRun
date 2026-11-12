"""
Data Store management views for the control panel.

The detail page is the DataStore explorer (2.5 Stage 9): the store's entries in
the shared grid (core/grid.py) with key search, filters, sort and pages. Values
up to ``GRID_EDIT_CHARS`` are edited in place with the grid's JSON editor;
bigger ones open on the entry page, which shows values up to ``VIEW_CAP_CHARS``
in full and, beyond that, a truncated preview with a download link. The store
exports as one JSON object and imports the same shape as an upsert, previewed
before it is applied (core/services/datastore_io.py).
"""

import json
import re
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Case, Count, F, Q, Sum, TextField, When
from django.db.models.functions import Coalesce, Length, Substr
from django.http import HttpRequest, HttpResponse, JsonResponse, StreamingHttpResponse
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.forms import DataStoreForm, DataStoreEntryForm
from core.grid import Grid, GridColumn, display_value, json_cell_value, next_querystring
from core.models import APIToken, DataStore, DataStoreEntry
from core.services import DatastoreService, datastore_io
from core.tables.columns import parse_datetime
from core.views.ownership import owned_block_message, owned_delete_blocked

# Values up to this many characters travel in the page and are edited in the
# grid; bigger ones show a preview there and open on the entry page.
GRID_EDIT_CHARS = 16 * 1024
# The entry page shows and edits values up to this size in full; beyond it, a
# preview of the first PREVIEW_CHARS characters and a download link.
VIEW_CAP_CHARS = 256 * 1024
PREVIEW_CHARS = 4 * 1024
# What the grid reads of a value it doesn't carry whole (its preview).
GRID_PREVIEW_CHARS = 200
JSON_HINT = 'Any JSON value: "text" in quotes, a number, true or false, null, a [list] or an {object}. Empty saves null.'
MAX_SIZE_FILTER = 2**62
KIND_BY_FIRST_CHAR = {"{": "object", "[": "array", '"': "string", "t": "boolean", "f": "boolean", "n": "null"}


@login_required
def datastore_list_view(request: HttpRequest) -> HttpResponse:
    """List the active workspace's data stores."""
    datastores = DatastoreService.get_datastores_with_stats(request.workspace)

    # Owner filter (Plugin Platform v2). get_datastores_with_stats returns a list,
    # so owners + filtering are done in Python.
    owners = sorted({ds.owner_plugin for ds in datastores if ds.owner_plugin})
    owner_filter = request.GET.get("owner_plugin")
    if owner_filter:
        datastores = [ds for ds in datastores if ds.owner_plugin == owner_filter]

    # API tokens bound to a store (single-store tokens and Unified API bindings)
    # are deleted with it (FK cascade); the delete confirm names their count.
    bound = dict(
        APIToken.objects.filter(datastore__in=[ds.pk for ds in datastores])
        .order_by()
        .values_list("datastore")
        .annotate(n=Count("id"))
    )

    # Format size for each datastore
    for ds in datastores:
        ds.size_display = DatastoreService.format_size(ds.size_bytes)
        ds.bound_tokens = bound.get(ds.pk, 0)

    total_size = DatastoreService.get_total_size(request.workspace)
    total_size_display = DatastoreService.format_size(total_size)

    return render(
        request,
        "cpanel/datastores/list.html",
        {
            "datastores": datastores,
            "total_size_display": total_size_display,
            "datastore_count": len(datastores),
            "owners": owners,
            "selected_owner": owner_filter or "",
        },
    )


@login_required
def datastore_create_view(request: HttpRequest) -> HttpResponse:
    """Create a new data store."""
    if request.method == "POST":
        form = DataStoreForm(request.POST, workspace=request.workspace)
        if form.is_valid():
            datastore = form.save(commit=False)
            datastore.created_by = request.user
            datastore.workspace = request.workspace
            datastore.save()

            messages.success(request, f'Data store "{datastore.name}" created successfully.')
            return redirect("cpanel:datastore_detail", pk=datastore.pk)
    else:
        form = DataStoreForm(workspace=request.workspace)

    return render(
        request,
        "cpanel/datastores/create.html",
        {
            "form": form,
        },
    )


def _value_kind(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return "array" if isinstance(value, list) else "object"


def _grid_columns() -> list:
    return [
        GridColumn("key", "key", "text"),
        GridColumn("type", "type", "kind", sortable=False, filterable=False),
        GridColumn("value", "value", "json", sortable=False, filterable=False, editable=True),
        GridColumn("size", "size (chars)", "number"),
        GridColumn("updated_at", "updated", "timestamp"),
    ]


def _condition(item) -> Q:
    """One grid filter as a Q on the annotated entries; ValueError for a value that doesn't fit."""
    if item.column == "updated_at":
        moment = parse_datetime(item.value)
        if moment is None:
            raise ValueError(f'"updated" compares with a date and time; got "{item.value}".')
        return Q(**{f"updated_at__{item.op}": moment})
    if item.column == "key":
        if item.op in ("empty", "not_empty"):
            # A key is never empty.
            return Q(pk__in=[]) if item.op == "empty" else Q()
        if item.op == "in":
            return Q(key__in=[v.strip() for v in item.value.split(",") if v.strip()])
        lookup = {"eq": "exact", "ne": "exact", "contains": "icontains", "prefix": "istartswith"}[item.op]
        q = Q(**{f"key__{lookup}": item.value})
        return ~q if item.op == "ne" else q
    # "size" (a number).
    if item.op in ("empty", "not_empty"):
        return Q(size=0) if item.op == "empty" else Q(size__gt=0)
    try:
        numbers = [int(v) for v in item.value.split(",")] if item.op == "in" else [int(item.value)]
    except ValueError:
        numbers = []
    if not numbers or any(abs(n) > MAX_SIZE_FILTER for n in numbers):
        # Past 64 bits the database itself refuses the number (a 500 otherwise).
        raise ValueError(f'"size" compares with a whole number; got "{item.value}".')
    if item.op == "in":
        return Q(size__in=numbers)
    if item.op in ("eq", "ne"):
        q = Q(size=numbers[0])
        return ~q if item.op == "ne" else q
    return Q(**{f"size__{item.op}": numbers[0]})


def _entries(datastore, grid, search: str):
    """The grid's entries (search, filters, sort), with ``size`` and ``cell`` (the value, or its preview)."""
    qs = datastore.entries.annotate(size=Length("value_json")).annotate(
        cell=Case(
            When(size__lte=GRID_EDIT_CHARS, then=F("value_json")),
            default=Substr("value_json", 1, GRID_PREVIEW_CHARS),
            output_field=TextField(),
        )
    ).defer("value_json")
    if search:
        qs = qs.filter(key__icontains=search)
    kept = []
    for item in grid.filters:
        try:
            qs = qs.filter(_condition(item))
        except ValueError as exc:
            grid.problems.append(str(exc))
        else:
            kept.append(item)
    grid.filters = kept
    sort = grid.sort or "key"
    # Keys are unique per store, so the key breaks every tie.
    return qs.order_by(sort) if sort.lstrip("-") == "key" else qs.order_by(sort, "key")


def _row_values(entry) -> tuple:
    """(grid values, whether the value cell can be edited in place)."""
    values = {
        "key": entry.key,
        "size": entry.size,
        "updated_at": entry.updated_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
    }
    if entry.size <= GRID_EDIT_CHARS:
        try:
            value = json.loads(entry.cell)
        except ValueError:
            # Not JSON (written around the helpers): show the text, edit it on the entry page.
            values.update(type="text", value=entry.cell)
            return values, False
        values.update(type=_value_kind(value), value=value)
        return values, True
    first = entry.cell.lstrip()[:1]
    kind = KIND_BY_FIRST_CHAR.get(first) or ("number" if first and first in "-0123456789" else "text")
    values.update(type=kind, value=entry.cell)
    return values, False


@login_required
def datastore_detail_view(request: HttpRequest, pk) -> HttpResponse:
    """The DataStore explorer: the store's entries in the shared grid."""
    datastore = get_object_or_404(DataStore, pk=pk, workspace=request.workspace)
    search = request.GET.get("q", "").strip()
    grid = Grid.from_request(
        request, _grid_columns(), default_sort="key", editable=True, csrf_token=get_token(request),
        extra_params={"q": search} if search else {},
    )
    qs = _entries(datastore, grid, search)
    total = qs.count()
    grid.clamp_page(total)
    rows = []
    for entry in qs[grid.offset:grid.offset + grid.page_size]:
        values, inline = _row_values(entry)
        kwargs = {"pk": datastore.pk, "entry_pk": entry.pk}
        row = grid.make_row(
            entry.pk, values,
            cell_url=reverse("cpanel:datastore_entry_cell", kwargs=kwargs),
            delete_url=reverse("cpanel:datastore_entry_delete", kwargs=kwargs),
            links={"key": reverse("cpanel:datastore_entry_edit", kwargs=kwargs)},
            delete_confirm=f'Delete entry "{entry.key}"? This cannot be undone.',
        )
        if not inline:
            # A preview only (the text as stored, cut short); the whole value is on the entry page.
            cell = next(c for c in row.cells if c.column.key == "value")
            text = entry.cell
            cell.display = text if len(text) < GRID_PREVIEW_CHARS and entry.size == len(text) \
                else text[: GRID_PREVIEW_CHARS - 1] + "…"
            cell.raw, cell.editable = "", False
        rows.append(row)
    grid.set_rows(rows, total)
    search_params = [(name, value) for name, value in grid.hidden_params if name != "q"]
    stats = datastore.entries.aggregate(n=Count("id"), size=Coalesce(Sum(Length("value_json")), 0))

    return render(
        request,
        "cpanel/datastores/detail.html",
        {
            "datastore": datastore,
            "grid": grid,
            "search": search,
            # The search form keeps the grid's sort and filters (a new search starts at page 1).
            "search_params": search_params,
            "clear_search_qs": urlencode(search_params),
            "json_hint": JSON_HINT,
            "grid_empty_text": "No key matches this search." if search else "No entries.",
            "entry_count": stats["n"],
            "size_display": DatastoreService.format_size(stats["size"]),
            "grid_edit_kb": GRID_EDIT_CHARS // 1024,
        },
    )


def _back(request, datastore) -> HttpResponse:
    """Back to the explorer, keeping the grid's view (the ``next`` query string a row's form sends)."""
    return redirect(reverse("cpanel:datastore_detail", kwargs={"pk": datastore.pk})
                    + next_querystring(request.POST.get("next", "")))


def _filename(name: str, fallback: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")[:100] or fallback


@login_required
@require_POST
def datastore_entry_cell_view(request: HttpRequest, pk, entry_pk) -> JsonResponse:
    """The grid's JSON editor: POST {"column": "value", "value", "text"} -> {"html", "raw"} or {"error"}."""
    datastore = get_object_or_404(DataStore, pk=pk, workspace=request.workspace)
    entry = DataStoreEntry.objects.filter(pk=entry_pk, datastore=datastore).first()
    if entry is None:
        return JsonResponse({"error": "This entry is gone (deleted meanwhile?). Reload the page."}, status=404)
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"error": "The request must be JSON."}, status=400)
    if not isinstance(body, dict) or body.get("column") != "value" or "value" not in body:
        return JsonResponse({"error": "Only an entry's value can be edited here."}, status=400)
    try:
        value = json_cell_value(body)
    except ValueError as exc:
        return JsonResponse({"error": f"Not valid JSON: {exc}"}, status=400)
    entry.set_value(value)
    entry.save(update_fields=["value_json", "updated_at"])
    cell = {"column": GridColumn("value", "value", "json", editable=True), "value": value,
            "display": display_value("json", value)}
    return JsonResponse({
        "html": render_to_string("cpanel/_grid_cell.html", {"cell": cell}).strip(),
        "raw": json.dumps(value, ensure_ascii=False),
    })


@login_required
def datastore_entry_download_view(request: HttpRequest, pk, entry_pk) -> HttpResponse:
    """One entry's value as a .json file, as stored."""
    datastore = get_object_or_404(DataStore, pk=pk, workspace=request.workspace)
    entry = get_object_or_404(DataStoreEntry, pk=entry_pk, datastore=datastore)
    response = HttpResponse(entry.value_json, content_type="application/json; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{_filename(entry.key, "value")}.json"'
    return response


@login_required
def datastore_export_view(request: HttpRequest, pk) -> HttpResponse:
    """Every entry as one JSON object ``{"key": value}``, streamed (it imports again as is)."""
    datastore = get_object_or_404(DataStore, pk=pk, workspace=request.workspace)
    response = StreamingHttpResponse(datastore_io.stream_json(datastore),
                                     content_type="application/json; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{_filename(datastore.name, "datastore")}.json"'
    return response


@login_required
def datastore_import_view(request: HttpRequest, pk) -> HttpResponse:
    """Import a JSON object of key: value pairs as an upsert: preview first, then confirm.

    POST ``file``; without ``confirm=1`` it only previews (nothing written);
    with it, every new or changed key is written in one transaction. A request
    that accepts JSON gets ``{"html", "changes"}`` (the rendered preview) or
    ``{"error", "html"}``.
    """
    datastore = get_object_or_404(DataStore, pk=pk, workspace=request.workspace)
    context = {"datastore": datastore, "max_mb": datastore_io.MAX_IMPORT_BYTES // (1024 * 1024)}
    if request.method != "POST":
        return render(request, "cpanel/datastores/import.html", context)
    wants_json = "application/json" in request.headers.get("Accept", "")
    confirm = request.POST.get("confirm") == "1"
    try:
        upload = request.FILES.get("file")
        if upload is None:
            raise datastore_io.DataStoreImportError("Choose a JSON file to import.")
        if upload.size > datastore_io.MAX_IMPORT_BYTES:
            raise datastore_io.DataStoreImportError(datastore_io.TOO_LARGE)
        items = datastore_io.parse(upload.read())
        report = (datastore_io.apply_import if confirm else datastore_io.plan_import)(datastore, items)
    except datastore_io.DataStoreImportError as exc:
        context.update(error=exc.message, errors=exc.errors)
        if wants_json:
            html = render_to_string("cpanel/datastores/_import_report.html", context, request=request)
            return JsonResponse({"error": exc.message, "html": html}, status=400)
        return render(request, "cpanel/datastores/import.html", context, status=400)
    if confirm:
        messages.success(
            request,
            f"Import done: {report['new']} new, {report['updated']} updated, {report['unchanged']} unchanged "
            f"(of {report['total']} in the file).",
        )
        return redirect("cpanel:datastore_detail", pk=datastore.pk)
    context["report"] = report
    if wants_json:
        html = render_to_string("cpanel/datastores/_import_report.html", context, request=request)
        return JsonResponse({"html": html, "changes": report["new"] + report["updated"]})
    return render(request, "cpanel/datastores/import.html", context)


@login_required
def datastore_edit_view(request: HttpRequest, pk) -> HttpResponse:
    """Edit a data store's metadata."""
    datastore = get_object_or_404(DataStore, pk=pk, workspace=request.workspace)

    if request.method == "POST":
        form = DataStoreForm(request.POST, instance=datastore, workspace=request.workspace)
        if form.is_valid():
            form.save()
            messages.success(request, f'Data store "{datastore.name}" updated successfully.')
            return redirect("cpanel:datastore_detail", pk=datastore.pk)
    else:
        form = DataStoreForm(instance=datastore, workspace=request.workspace)

    return render(
        request,
        "cpanel/datastores/edit.html",
        {
            "form": form,
            "datastore": datastore,
        },
    )


@login_required
@require_POST
def datastore_delete_view(request: HttpRequest, pk) -> HttpResponse:
    """Delete a data store and all its entries."""
    datastore = get_object_or_404(DataStore, pk=pk, workspace=request.workspace)

    if owned_delete_blocked(request, datastore):
        messages.error(request, owned_block_message(datastore, "data store"))
        return redirect("cpanel:datastore_list")

    name = datastore.name
    datastore.delete()

    messages.success(request, f'Data store "{name}" deleted successfully.')
    return redirect("cpanel:datastore_list")


@login_required
@require_POST
def datastore_clear_view(request: HttpRequest, pk) -> HttpResponse:
    """Clear all entries from a data store."""
    datastore = get_object_or_404(DataStore, pk=pk, workspace=request.workspace)
    count = datastore.entries.count()
    datastore.entries.all().delete()

    messages.success(request, f'Cleared {count} entries from "{datastore.name}".')
    return redirect("cpanel:datastore_detail", pk=datastore.pk)


# =============================================================================
# Entry Views
# =============================================================================


@login_required
def datastore_entry_create_view(request: HttpRequest, pk) -> HttpResponse:
    """Add a new entry to a data store."""
    datastore = get_object_or_404(DataStore, pk=pk, workspace=request.workspace)

    if request.method == "POST":
        form = DataStoreEntryForm(request.POST, datastore=datastore)
        if form.is_valid():
            entry = DataStoreEntry(
                datastore=datastore,
                key=form.cleaned_data["key"],
                value_json=form.cleaned_data["value"],
            )
            entry.save()

            messages.success(request, f'Entry "{entry.key}" added successfully.')
            return redirect("cpanel:datastore_detail", pk=datastore.pk)
    else:
        form = DataStoreEntryForm(datastore=datastore)

    return render(
        request,
        "cpanel/datastores/entry_create.html",
        {
            "form": form,
            "datastore": datastore,
        },
    )


@login_required
def datastore_entry_edit_view(request: HttpRequest, pk, entry_pk) -> HttpResponse:
    """Edit an existing entry."""
    datastore = get_object_or_404(DataStore, pk=pk, workspace=request.workspace)
    entry = get_object_or_404(DataStoreEntry, pk=entry_pk, datastore=datastore)

    size = len(entry.value_json)
    too_big = size > VIEW_CAP_CHARS

    if request.method == "POST":
        form = DataStoreEntryForm(request.POST, datastore=datastore, instance=entry)
        if form.is_valid():
            entry.key = form.cleaned_data["key"]
            entry.value_json = form.cleaned_data["value"]
            entry.save()

            messages.success(request, f'Entry "{entry.key}" updated successfully.')
            return redirect("cpanel:datastore_detail", pk=datastore.pk)
    else:
        form = DataStoreEntryForm(datastore=datastore, instance=entry)
        if not too_big:
            # Pretty-printed to read; saved as typed (Format re-indents it).
            try:
                form.fields["value"].initial = json.dumps(json.loads(entry.value_json), indent=2, ensure_ascii=False)
            except ValueError:
                pass

    return render(
        request,
        "cpanel/datastores/entry_edit.html",
        {
            "form": form,
            "datastore": datastore,
            "entry": entry,
            "size_display": DatastoreService.format_size(size),
            "too_big": too_big,
            "preview": entry.value_json[:PREVIEW_CHARS] if too_big else "",
            "view_cap_display": DatastoreService.format_size(VIEW_CAP_CHARS),
        },
    )


@login_required
@require_POST
def datastore_entry_delete_view(request: HttpRequest, pk, entry_pk) -> HttpResponse:
    """Delete an entry from a data store."""
    datastore = get_object_or_404(DataStore, pk=pk, workspace=request.workspace)
    entry = get_object_or_404(DataStoreEntry, pk=entry_pk, datastore=datastore)
    key = entry.key
    entry.delete()

    messages.success(request, f'Entry "{key}" deleted successfully.')
    return _back(request, datastore)
