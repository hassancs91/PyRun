"""
The console's data grid: one component for Tables, DataStores and Databases.

It knows nothing about where rows come from. A page builds a ``Grid`` from
its columns and the request (sort, filter chips, page number come from the
query string), asks its own data source for ``grid.page_size`` rows starting at
``grid.offset`` that match ``grid.filters`` in ``grid.sort`` order, hands them
back with ``grid.set_rows(rows, total)``, and renders
``{% include "cpanel/_grid.html" with grid=grid %}``.

- **Columns** are ``GridColumn(key, label, type)``. ``type`` picks the type
  badge, the cell format, the filter operators (``core.tables.query.OPS_BY_TYPE``
  is the shared list) and the inline editor (``static/js/data_grid.js``).
- **Filters** arrive as repeated ``f=<column>:<op>:<value>`` parameters and
  come out as ``GridFilter(column, op, value)``; a malformed one is dropped and
  named in ``grid.problems``. The source turns them into its own query
  (Tables: ``core.tables.query.condition_q``; the op names are the same).
- **Editing** is opt-in per grid: pass ``editable=True`` plus each row's
  ``cell_url`` (POST JSON ``{"column", "value"}`` → ``{"html", "raw"}``, the
  re-rendered ``cpanel/_grid_cell.html`` and the value as JSON, or ``{"error"}``) and ``delete_url``; a read-only grid (Databases) leaves
  them out and renders no editors. The JSON dialog also sends ``text`` (the
  JSON as typed): read a JSON cell's value with ``json_cell_value(body)``.
  A cell can opt out of editing (``cell.editable = False``) or link somewhere
  (``make_row(..., links={key: url})``); ``delete_confirm`` names the row in
  the delete prompt.
"""

import json
from dataclasses import dataclass, field
from urllib.parse import urlencode

from django.core.paginator import Paginator

from core.tables.query import OP_LABELS, OPS_BY_TYPE, VALUELESS

PAGE_SIZE = 50
MAX_FILTERS = 10
TYPE_BADGES = {
    "text": "Aa",
    "number": "#",
    "boolean": "y/n",
    "date": "date",
    "datetime": "time",
    "json": "{ }",
    "link": "url",
    "id": "id",
    "timestamp": "time",
    # A per-row type pill (the DataStore explorer's value types); no header badge.
    "kind": "",
}
JSON_PREVIEW_CHARS = 120


@dataclass(frozen=True)
class GridColumn:
    key: str
    label: str
    type: str = "text"
    sortable: bool = True
    filterable: bool = True
    editable: bool = False
    required: bool = False

    @property
    def badge(self) -> str:
        return TYPE_BADGES.get(self.type, self.type)

    @property
    def ops(self) -> tuple:
        return OPS_BY_TYPE.get(self.type, ())

    @property
    def numeric(self) -> bool:
        return self.type in ("number", "id")


@dataclass(frozen=True)
class GridFilter:
    column: str
    op: str
    value: str = ""

    @property
    def param(self) -> str:
        return f"{self.column}:{self.op}:{self.value}"


@dataclass
class GridCell:
    column: GridColumn
    value: object
    display: str
    raw: str  # JSON of the value, for the inline editor
    # False keeps this one cell read-only in an editable column (e.g. a value
    # too big to carry in the page); ``href`` renders the cell as a link.
    editable: bool = True
    href: str = ""


@dataclass
class GridRow:
    id: object
    cells: list
    cell_url: str = ""
    delete_url: str = ""
    delete_confirm: str = "Delete this row? This cannot be undone."


def display_value(kind: str, value) -> str:
    """The text a cell shows (empty cells are rendered by the template)."""
    if value is None:
        return ""
    if kind == "boolean":
        return "Yes" if value else "No"
    if kind == "json":
        text = json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
        return text if len(text) <= JSON_PREVIEW_CHARS else text[: JSON_PREVIEW_CHARS - 1] + "…"
    if kind == "datetime" and isinstance(value, str) and value.endswith("Z"):
        return value[:-1].replace("T", " ") + " UTC"
    return str(value)


def next_querystring(value: str) -> str:
    """A row form's ``next`` (the grid's ``?query``) when it is safe to append to the page URL, else ""."""
    if value.startswith("?") and "\n" not in value and "\r" not in value:
        return value
    return ""


def json_cell_value(body: dict):
    """The value a JSON cell's save sends: its ``text`` (the JSON as typed) when present, else ``value``.

    The grid's JSON dialog sends both; ``text`` is read here, in Python, so a
    number JavaScript would round (a long integer) or reprint (``1.0``) keeps
    its exact value. Empty text is null. Raises ValueError for invalid JSON.
    """
    text = body.get("text")
    if isinstance(text, str):
        return json.loads(text) if text.strip() else None
    return body.get("value")


@dataclass
class Grid:
    columns: list
    sort: str = ""
    filters: list = field(default_factory=list)
    page_number: int = 1
    page_size: int = PAGE_SIZE
    problems: list = field(default_factory=list)
    editable: bool = False
    add_url: str = ""
    csrf_token: str = ""
    rows: list = field(default_factory=list)
    total: int = 0
    page_obj: object = None
    page_range: object = None
    # Extra query parameters the page keeps (e.g. a tab); never sort/f/page.
    extra_params: dict = field(default_factory=dict)

    @classmethod
    def from_request(cls, request, columns, *, default_sort="", page_size=PAGE_SIZE, **kwargs):
        by_key = {c.key: c for c in columns}
        grid = cls(columns=list(columns), page_size=page_size, **kwargs)
        sort = request.GET.get("sort", default_sort) or ""
        column = by_key.get(sort.lstrip("-"))
        if column is not None and column.sortable:
            grid.sort = sort
        elif sort:
            grid.sort = default_sort
            grid.problems.append(f'Can\'t sort by "{sort.lstrip("-")}".')
        raws = request.GET.getlist("f")
        # The add-filter form sends its new filter as fc/fo/fv (data_grid.js
        # folds them into one ``f`` first when it runs).
        if request.GET.get("fc"):
            raws.append(f'{request.GET["fc"]}:{request.GET.get("fo", "")}:{request.GET.get("fv", "")}')
        for raw in raws[:MAX_FILTERS]:
            key, _, rest = raw.partition(":")
            op, _, value = rest.partition(":")
            column = by_key.get(key)
            if column is None or not column.filterable or op not in column.ops:
                grid.problems.append(f'Ignored the filter "{raw}".')
                continue
            if op not in VALUELESS and value == "":
                grid.problems.append(f'Ignored the filter on "{column.label}": it needs a value.')
                continue
            grid.filters.append(GridFilter(key, op, "" if op in VALUELESS else value))
        try:
            grid.page_number = max(1, int(request.GET.get("page", 1)))
        except (TypeError, ValueError):
            grid.page_number = 1
        return grid

    # ----------------------------------------------------------- for the source

    @property
    def offset(self) -> int:
        return (self.page_number - 1) * self.page_size

    def clamp_page(self, total: int) -> None:
        """Move past-the-end page numbers to the last page (call before reading rows)."""
        last = max(1, -(-total // self.page_size))
        if self.page_number > last:
            self.page_number = last

    def set_rows(self, rows: list, total: int) -> None:
        """``rows`` are GridRows (see ``make_row``); ``total`` counts every matching row."""
        self.rows, self.total = rows, total
        paginator = Paginator(range(total), self.page_size)
        self.page_obj = paginator.get_page(self.page_number)
        self.page_range = paginator.get_elided_page_range(self.page_obj.number, on_each_side=2, on_ends=1)

    def make_row(self, row_id, values: dict, *, cell_url="", delete_url="", links=None,
                 delete_confirm="") -> GridRow:
        """``links`` maps a column key to the URL its cell links to; ``delete_confirm`` names the row in the prompt."""
        cells = []
        for column in self.columns:
            value = values.get(column.key)
            cells.append(GridCell(
                column=column, value=value, display=display_value(column.type, value),
                raw=json.dumps(value, ensure_ascii=False), href=(links or {}).get(column.key, ""),
            ))
        row = GridRow(id=row_id, cells=cells, cell_url=cell_url, delete_url=delete_url)
        if delete_confirm:
            row.delete_confirm = delete_confirm
        return row

    # ---------------------------------------------------------- for the template

    def _params(self, *, sort=None, filters=None, page=None) -> list:
        params = list(self.extra_params.items())
        sort = self.sort if sort is None else sort
        if sort:
            params.append(("sort", sort))
        for item in self.filters if filters is None else filters:
            params.append(("f", item.param))
        if page and page > 1:
            params.append(("page", page))
        return params

    @property
    def querystring(self) -> str:
        """Every parameter but the page (what ``_pagination.html`` appends)."""
        return urlencode(self._params())

    @property
    def headers(self) -> list:
        out = []
        for column in self.columns:
            current = self.sort.lstrip("-") == column.key
            descending = current and self.sort.startswith("-")
            next_sort = (column.key if descending else f"-{column.key}") if current else column.key
            out.append({
                "column": column,
                "sorted": "desc" if descending else ("asc" if current else ""),
                "sort_qs": urlencode(self._params(sort=next_sort)) if column.sortable else "",
            })
        return out

    @property
    def chips(self) -> list:
        labels = {c.key: c.label for c in self.columns}
        out = []
        for item in self.filters:
            rest = [f for f in self.filters if f is not item]
            text = f"{labels.get(item.column, item.column)} {OP_LABELS.get(item.op, item.op)}"
            if item.op not in VALUELESS:
                text += f" {item.value}"
            out.append({"label": text, "remove_qs": urlencode(self._params(filters=rest))})
        return out

    @property
    def clear_qs(self) -> str:
        return urlencode(self._params(filters=[]))

    @property
    def hidden_params(self) -> list:
        """(name, value) pairs the add-filter form carries so it keeps the current view."""
        return self._params()

    @property
    def filter_columns(self) -> list:
        return [c for c in self.columns if c.filterable and c.ops]

    @property
    def ops_json(self) -> str:
        """Per-column operators for the add-filter form's script."""
        return json.dumps({c.key: [[op, OP_LABELS[op]] for op in c.ops] for c in self.filter_columns})

    @property
    def valueless_json(self) -> str:
        return json.dumps(list(VALUELESS))
