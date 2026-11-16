"""
CSV import and export for Tables: one implementation for the console grid,
the ``pyrunner_tables`` helper (through the loopback) and anything later.

A CSV cell is text. ``cell_from_csv`` turns it into the value ``clean_row``
checks for the column (an empty cell is no value; a JSON column's text is
parsed as JSON); ``cell_to_csv`` writes a stored value back the same way, so
an exported table imports again unchanged. The header names the columns; a
mapping says which header goes to which column. Without one, ``match_header``
pairs a header with the column key that reads the same once case, spaces,
dashes and underscores are ignored ("E-Mail" fills ``email``; "Lead Score" and
"lead-score" fill ``lead_score``), and skips ``id`` / ``created_at`` /
``updated_at``, which every row gets anew. A match that isn't one to one (two
headers for one key, or one header for two keys) is never guessed: the import
is refused until a mapping says which is meant.

An import is all or nothing: the whole file is checked first (every row
against its columns, the row cap), and rows are written only when nothing
failed. A dry run stops after the check and reports what would happen.
Exported values are written as stored: a cell that starts with ``=``, ``+``,
``-`` or ``@`` is not escaped (that would change the data on a round trip),
so open untrusted exports in a spreadsheet with care.
"""

import csv
import io
import json
import re

from django.conf import settings
from django.db import transaction

from .columns import SYSTEM_FIELDS, clean_row
from .errors import TableError

# Largest file the console takes; the helper streams any size.
MAX_IMPORT_BYTES = 50 * 1024 * 1024
# Errors listed in a report (the total is always counted).
MAX_ERRORS = 20
# A JSON cell may be as big as a row (256 KB); csv's default field limit is 128 KB.
FIELD_LIMIT = 1024 * 1024
_BATCH = 500
# What header matching ignores besides case: whitespace, dashes and underscores.
# The Import page's mapping step (static/js/table_import.js) is given this same pattern.
SEPARATORS = r"[\s\-_]+"
_SEPARATORS = re.compile(SEPARATORS)


def normalize_name(text: str) -> str:
    """A header cell or column key as matched: lower case, no spaces, dashes or underscores."""
    return _SEPARATORS.sub("", text or "").lower()


def match_header(header: list, columns: list) -> tuple:
    """``(mapping, conflicts)``: for each header cell the column key it fills
    (None: skipped), and one sentence per match that isn't one to one.

    A header that reads as ``id`` / ``created_at`` / ``updated_at`` is skipped.
    An ambiguous header is left unmapped and named in ``conflicts``: two headers
    that match the same key, or one header that matches two keys.
    """
    system = {normalize_name(name) for name in SYSTEM_FIELDS}
    keys_by_norm = {}
    for column in columns:
        keys_by_norm.setdefault(normalize_name(column["key"]), []).append(column["key"])
    candidates = []
    for name in header:
        norm = normalize_name(name)
        candidates.append([] if norm in system else keys_by_norm.get(norm, []))
    headers_by_key = {}
    for name, keys in zip(header, candidates):
        if len(keys) == 1:
            headers_by_key.setdefault(keys[0], []).append(name)
    mapping, conflicts, reported = [], [], set()
    for name, keys in zip(header, candidates):
        if len(keys) > 1:
            mapping.append(None)
            conflicts.append(f'CSV column "{name}" matches more than one column ({", ".join(keys)}).')
        elif keys and len(headers_by_key[keys[0]]) > 1:
            mapping.append(None)
            if keys[0] not in reported:
                reported.add(keys[0])
                names = " and ".join(f'"{h}"' for h in headers_by_key[keys[0]])
                conflicts.append(f'CSV columns {names} all match the column "{keys[0]}".')
        else:
            mapping.append(keys[0] if keys else None)
    return mapping, conflicts


def check_mapping(mapping: list, header: list, columns: list) -> list:
    """``mapping`` (one column key or None per header cell) checked against the table."""
    keys = {c["key"] for c in columns}
    if not isinstance(mapping, list) or len(mapping) != len(header):
        raise TableError("The mapping must name one column (or none) for every CSV header.", "invalid")
    seen = set()
    for position, key in enumerate(mapping):
        if key in (None, ""):
            continue
        if key not in keys:
            raise TableError(f'CSV column "{header[position]}" maps to "{key}", which isn\'t a column.', "invalid")
        if key in seen:
            raise TableError(f'Two CSV columns map to "{key}".', "invalid")
        seen.add(key)
    cleaned = [key or None for key in mapping]
    if not any(cleaned):
        raise TableError(
            "No CSV column matches a table column. Name the header cells after the column keys, "
            "or map them.",
            "invalid",
        )
    return cleaned


def cell_from_csv(column: dict, text):
    """The value for ``clean_row`` from one CSV cell (None: an empty cell)."""
    if text is None or text == "":
        return None
    if column["type"] == "json":
        try:
            return json.loads(text)
        except ValueError:
            shown = text if len(text) <= 60 else text[:57] + "..."
            raise TableError(f'Column "{column["key"]}" expects JSON; got {shown!r}.', "invalid") from None
    return text


def row_from_csv(columns: list, cells: dict) -> dict:
    """``{key: csv text}`` → the row's checked ``data`` (raises ``TableError``)."""
    by_key = {c["key"]: c for c in columns}
    values = {}
    for key, text in cells.items():
        if key not in by_key:
            values[key] = text  # clean_row names the unknown column
            continue
        value = cell_from_csv(by_key[key], text)
        if value is not None:
            values[key] = value
    from .service import _check_size

    data = clean_row(columns, values)
    _check_size(data)
    return data


def cell_to_csv(column_type: str, value) -> str:
    if value is None:
        return ""
    if column_type == "json":
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def header_for(columns: list) -> list:
    return ["id"] + [c["key"] for c in columns] + ["created_at", "updated_at"]


def export_values(row, columns: list) -> list:
    """One TableRow as CSV cells, in ``header_for`` order."""
    return (
        [str(row.id)]
        + [cell_to_csv(c["type"], row.data.get(c["key"])) for c in columns]
        + [_stamp(row.created_at), _stamp(row.updated_at)]
    )


def _stamp(moment) -> str:
    return moment.isoformat().replace("+00:00", "Z")


class _Echo:
    def write(self, value):
        return value


def stream_csv(table, queryset):
    """CSV lines (header first) for ``queryset``'s rows, read in chunks."""
    writer = csv.writer(_Echo())
    yield writer.writerow(header_for(table.columns))
    for row in queryset.iterator(chunk_size=1000):
        yield writer.writerow(export_values(row, table.columns))


def _reader(text_file):
    csv.field_size_limit(max(csv.field_size_limit(), FIELD_LIMIT))
    return csv.reader(text_file)


def open_text(binary_file):
    """A text view of an uploaded file (UTF-8, with or without a BOM)."""
    return io.TextIOWrapper(binary_file, encoding="utf-8-sig", newline="")


def import_csv(table, text_file, *, mapping=None, dry_run=False, user=None) -> dict:
    """Check, then (unless ``dry_run`` or something failed) insert every row of the file.

    ``mapping``: see ``resolve_mapping``. ``text_file`` must be seekable (it is
    read twice: check, then write); rows are read one at a time. Returns
    ``{"rows", "inserted", "errors", "error_count", "mapping", "dry_run"}``;
    ``errors`` lists the first ``MAX_ERRORS`` as ``{"line", "message"}``.
    """
    from .service import TableService

    if not (hasattr(text_file, "seekable") and text_file.seekable()):
        raise TableError("A CSV import reads the file twice: pass a path or a seekable file.", "invalid")
    try:
        reader = _reader(text_file)
        header = next(reader, None)
        if not header:
            raise TableError("The file is empty: the first line must name the columns.", "invalid")
        mapping = resolve_mapping(header, mapping, table.columns)
        rows, errors, error_count = 0, [], 0
        # Line numbers count CSV records, the header being line 1 (as the helper's).
        for line, cells in enumerate(reader, start=2):
            if not any(cells):
                continue
            rows += 1
            try:
                row_from_csv(table.columns, _map(cells, mapping, header))
            except TableError as exc:
                error_count += 1
                if len(errors) < MAX_ERRORS:
                    errors.append({"line": line, "message": exc.message})
    except UnicodeDecodeError:
        raise TableError("The file isn't UTF-8 text. Save it as \"CSV UTF-8\" and try again.", "invalid") from None
    except csv.Error as exc:
        raise TableError(f"The file isn't valid CSV: {exc}.", "invalid") from None

    report = {
        "rows": rows, "inserted": 0, "errors": errors, "error_count": error_count,
        "mapping": {header[i]: key for i, key in enumerate(mapping) if key}, "dry_run": bool(dry_run),
    }
    cap = settings.PYRUNNER_TABLE_ROW_CAP
    if not error_count and table.row_count + rows > cap:
        report["error_count"] = 1
        report["errors"] = [{"line": None, "message": (
            f"The table holds {table.row_count:,} rows; {rows:,} more would pass its {cap:,}-row limit."
        )}]
    if dry_run or report["error_count"] or not rows:
        return report

    text_file.seek(0)
    reader = _reader(text_file)
    next(reader)
    batch = []
    with transaction.atomic():
        for cells in reader:
            if not any(cells):
                continue
            batch.append(_map(cells, mapping, header))
            if len(batch) == _BATCH:
                report["inserted"] += _insert(TableService, table, batch, user)
                batch = []
        if batch:
            report["inserted"] += _insert(TableService, table, batch, user)
    return report


def resolve_mapping(header, mapping, columns) -> list:
    """The checked mapping for ``header``: ``mapping`` as given (a list, one
    key or None per header cell, or a ``{header: key}`` dict), else matched by
    name (``match_header``); an ambiguous match is refused, never guessed."""
    if not isinstance(header, list) or not header or not all(isinstance(h, str) for h in header):
        raise TableError("header must be the CSV's first line: a list of names.", "invalid")
    if mapping is None:
        mapping, conflicts = match_header(header, columns)
        if conflicts:
            raise TableError(
                " ".join(conflicts) + " Say which CSV column fills which table column with a column mapping.",
                "invalid",
            )
    elif isinstance(mapping, dict):
        unknown = sorted(str(k) for k in mapping if k not in header)
        if unknown:
            raise TableError(f"The mapping names CSV columns the header lacks: {', '.join(unknown)}.", "invalid")
        mapping = [mapping.get(name) for name in header]
    return check_mapping(mapping, header, columns)


def check_lines(table, header, mapping, lines, first_line=2) -> dict:
    """Check CSV lines (lists of cells) without writing; the loopback's dry run."""
    mapping = resolve_mapping(header, mapping, table.columns)
    rows, errors, error_count = 0, [], 0
    for offset, cells in enumerate(_lines(lines)):
        if not any(cells):
            continue
        rows += 1
        try:
            row_from_csv(table.columns, _map(cells, mapping, header))
        except TableError as exc:
            error_count += 1
            if len(errors) < MAX_ERRORS:
                errors.append({"line": first_line + offset, "message": exc.message})
    return {"rows": rows, "errors": errors, "error_count": error_count,
            "mapping": {header[i]: key for i, key in enumerate(mapping) if key}}


def insert_lines(table, header, mapping, lines, *, first_line=2, user=None) -> int:
    """Insert CSV lines, all or nothing; a bad line raises ``TableError`` naming it."""
    from .service import TableService

    mapping = resolve_mapping(header, mapping, table.columns)
    data = []
    for offset, cells in enumerate(_lines(lines)):
        if not any(cells):
            continue
        try:
            data.append(row_from_csv(table.columns, _map(cells, mapping, header)))
        except TableError as exc:
            raise TableError(f"Line {first_line + offset}: {exc.message}", exc.code) from None
    return len(TableService.insert(table, data, user=user)) if data else 0


def _lines(lines):
    if not isinstance(lines, list):
        raise TableError("lines must be a list of CSV lines (lists of cells).", "invalid")
    for cells in lines:
        if not isinstance(cells, list) or not all(isinstance(c, str) for c in cells):
            raise TableError("Each CSV line must be a list of text cells.", "invalid")
        yield cells


def _map(cells: list, mapping: list, header: list) -> dict:
    if len(cells) > len(header):
        raise TableError(f"This line has {len(cells)} cells; the header has {len(header)}.", "invalid")
    return {key: cells[i] if i < len(cells) else "" for i, key in enumerate(mapping) if key}


def _insert(service, table, batch, user) -> int:
    return len(service.insert(table, [row_from_csv(table.columns, cells) for cells in batch], user=user))
