"""
Google Sheets for scripts: read, append, update and find rows, list a spreadsheet's sheets and create spreadsheets.

Every function takes the spreadsheet's id (the part of its URL between
``/d/`` and ``/edit``) and, where it reads or writes cells, a range in A1
notation, as Google takes it: ``"Sheet1"`` (the whole sheet),
``"Sheet1!A1:D"``, ``"Sheet1!A2:C10"``. A sheet name with spaces or
punctuation goes in single quotes: ``"'Q1 sales'!A:D"``.

Cells are plain values: text, numbers, booleans. Reads give each row as a
list (Google leaves out empty cells at the end of a row and empty rows at the
end of a range); ``find_rows`` and ``new_rows`` give rows as dicts keyed by
the range's first row (its header). Writes take a list of rows (or one row);
``append_rows`` also takes dicts keyed by the header.

The connection is a Google connection with the **Sheets** scope set (Connect
with Google, or a pasted refresh token). Its token goes only to Google's Sheets
API host, ``https://sheets.googleapis.com``.
"""

import re

from . import _client

PROVIDER = "google"
BASE_URL = "https://sheets.googleapis.com"  # Sheets' own API host (the connection's base URL is www.googleapis.com)

# A1 cell references: "A1", "A1:C10", "A:C", "2:5", "A2:C", "$A$1".
_CELLS = re.compile(r"\$?([A-Za-z]{0,3})\$?(\d*)(?::\$?([A-Za-z]{0,3})\$?(\d*))?")
_VALUE_INPUT = ("USER_ENTERED", "RAW")
_VALUE_RENDER = ("FORMATTED_VALUE", "UNFORMATTED_VALUE", "FORMULA")


def _api(connection):
    return _client.shared(connection, PROVIDER, base_url=BASE_URL)


def _values_path(spreadsheet_id, range, suffix="") -> str:
    return f"/v4/spreadsheets/{_client.quote(spreadsheet_id)}/values/{_client.quote(range)}{suffix}"


def _choice(name, value, choices) -> str:
    value = str(value).upper()
    if value not in choices:
        raise ValueError(f"{name} is one of {', '.join(choices)}, not {value!r}")
    return value


def _rows(rows, *, dicts=False) -> list:
    """A list of rows from a list of rows or one row (a list of values, or a dict when ``dicts``)."""
    if isinstance(rows, (str, bytes)):
        raise TypeError("rows is a list of rows (each a list of values), or one row")
    if isinstance(rows, dict):
        rows = [rows]
    rows = list(rows)
    if rows and not any(isinstance(row, (list, tuple, dict)) for row in rows):
        return [rows]  # one row of values
    allowed = (list, tuple, dict) if dicts else (list, tuple)
    for row in rows:
        if not isinstance(row, allowed):
            kinds = "lists of values or dicts" if dicts else "lists of values"
            raise TypeError(f"rows are {kinds}, not {type(row).__name__} (a row: [\"Ada\", 36])")
    return [row if isinstance(row, dict) else list(row) for row in rows]


def _column_number(letters) -> int:
    number = 0
    for letter in letters.upper():
        number = number * 26 + ord(letter) - 64
    return number


def _split(range):
    """``(sheet prefix with "!" or "", (first column, first row, last column, last row))`` of an A1 range."""
    sheet, bang, cells = str(range).rpartition("!")
    if not bang:
        match = _CELLS.fullmatch(cells)
        # "Sheet1" or "'Q1 sales'" is a whole sheet; "A1:C", "A:C" or "2:5" are cells of the first sheet.
        if not match or not any(match.groups()) or not (match.group(2) or match.group(3) is not None):
            return f"{cells}!", ("", "", "", "")
        return "", match.groups(default="")
    match = _CELLS.fullmatch(cells)
    if not match:
        raise ValueError(f"{range!r} isn't an A1 range (\"Sheet1!A1:D\")")
    return f"{sheet}!", match.groups(default="")


def _start_row(reply_range) -> int:
    """The first row number of the range Google names in a reply (``"Sheet1!A1:D20"`` -> 1)."""
    _sheet, (_col, row, _col2, _row2) = _split(reply_range or "A1")
    return int(row) if row else 1


def _header_range(range) -> tuple:
    """``(the header row's range, the header's first column offset in that row)`` for dict rows."""
    prefix, (col, row, col2, _row2) = _split(range)
    row = row or "1"
    if col and col2:
        return f"{prefix}{col}{row}:{col2}{row}", 0
    return f"{prefix}{row}:{row}", (_column_number(col) - 1 if col else 0)


def _match_text(value) -> str:
    """``value`` as the sheet shows it, to compare with a formatted cell."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _value_range(spreadsheet_id, range, value_render, connection) -> dict:
    return _api(connection).get(_values_path(spreadsheet_id, range), params={
        "valueRenderOption": _choice("value_render", value_render, _VALUE_RENDER),
    }) or {}


def _table(spreadsheet_id, range, value_render, connection):
    """``(header, [(row number, values dict)...])`` of a range whose first row is its header."""
    reply = _value_range(spreadsheet_id, range, value_render, connection)
    values = reply.get("values") or []
    if not values:
        return [], []
    header = [str(name) for name in values[0]]
    first = _start_row(reply.get("range"))
    rows = []
    for offset, cells in enumerate(values[1:], start=1):
        record = {}
        for name, cell in zip(header, list(cells) + [""] * (len(header) - len(cells))):
            if name and name not in record:
                record[name] = cell
        rows.append((first + offset, record, any(cell != "" for cell in cells)))
    return header, rows


def _column(header, name, range) -> str:
    if name not in header:
        raise ValueError(f"No column {name!r} in the first row of {range!r} ({', '.join(h for h in header if h)})")
    return name


def client(connection=None, *, timeout=30):
    """The shared client of ``connection``, for calls this module has no function for.

    Paths are relative to ``https://sheets.googleapis.com``. For example:

        sheets = google_sheets.client()
        sheets.post(f"/v4/spreadsheets/{spreadsheet_id}:batchUpdate", json={"requests": [...]})
    """
    return _client.Client(connection, provider=PROVIDER, timeout=timeout, base_url=BASE_URL)


def read_range(spreadsheet_id, range, *, value_render="FORMATTED_VALUE", connection=None):
    """The rows of ``range``: a list of rows, each a list of cell values.

    ``value_render`` is ``"FORMATTED_VALUE"`` (text as the sheet shows it),
    ``"UNFORMATTED_VALUE"`` (numbers as numbers) or ``"FORMULA"``. Empty cells
    at the end of a row and empty rows at the end are left out. For example:

        rows = google_sheets.read_range(spreadsheet_id, "Sheet1!A1:C")
        header, data = rows[0], rows[1:]
    """
    return _value_range(spreadsheet_id, range, value_render, connection).get("values") or []


def append_rows(spreadsheet_id, range, rows, *, value_input="USER_ENTERED", connection=None):
    """Add ``rows`` after the last row of the table in ``range``; returns Google's ``updates`` summary.

    ``rows`` is a list of rows or one row. A row is a list of values, or a dict
    keyed by the names in the range's first row (its header; a name that isn't
    there raises ValueError, a column left out stays empty). With
    ``value_input="USER_ENTERED"`` values are read as if typed (numbers, dates,
    formulas); ``"RAW"`` stores them as they are. New rows are inserted, so
    nothing below the table is overwritten. No rows: no request, None. For example:

        google_sheets.append_rows(spreadsheet_id, "Leads", [["Ada", "ada@example.com", 36]])
        google_sheets.append_rows(spreadsheet_id, "Leads", {"Name": "Grace", "Email": "grace@example.com"})
    """
    rows = _rows(rows, dicts=True)
    if not rows:
        return None
    if any(isinstance(row, dict) for row in rows):
        header_range, offset = _header_range(range)
        header = [str(name) for name in (read_range(spreadsheet_id, header_range, connection=connection) or [[]])[0]]
        header = header[offset:]
        # Each name's first column (an empty or repeated header cell gets nothing).
        columns = [name if name and name not in header[:i] else None for i, name in enumerate(header)]
        out = []
        for row in rows:
            if not isinstance(row, dict):
                out.append(row)
                continue
            row = {_column(header, str(name), range): value for name, value in row.items()}
            out.append([row.get(name) if name is not None else None for name in columns])
        rows = out
    reply = _api(connection).post(_values_path(spreadsheet_id, range, ":append"), params={
        "valueInputOption": _choice("value_input", value_input, _VALUE_INPUT),
        "insertDataOption": "INSERT_ROWS",
    }, json={"majorDimension": "ROWS", "values": rows})
    return (reply or {}).get("updates")


def update_range(spreadsheet_id, range, rows, *, value_input="USER_ENTERED", connection=None):
    """Write ``rows`` into ``range``, starting at its top-left cell; returns Google's summary.

    ``rows`` is a list of rows (each a list of values) or one row. A None
    value leaves that cell as it is; ``""`` empties it. The summary has
    ``updatedRange``, ``updatedRows``, ``updatedColumns`` and ``updatedCells``.
    For example:

        google_sheets.update_range(spreadsheet_id, "Sheet1!D2", [["Done"]])
        google_sheets.update_range(spreadsheet_id, "Sheet1!A5:C5", ["Ada", "ada@example.com", 36])
    """
    rows = _rows(rows)
    return _api(connection).put(_values_path(spreadsheet_id, range), params={
        "valueInputOption": _choice("value_input", value_input, _VALUE_INPUT),
    }, json={"majorDimension": "ROWS", "values": rows})


def find_rows(spreadsheet_id, range, match, *, value_render="FORMATTED_VALUE", limit=None, connection=None):
    """The rows of ``range`` whose cells equal every value in ``match``, as ``{"row": n, "values": {...}}``.

    The range's first row is its header: ``match`` maps header names to the
    values to look for, compared with the cells as the sheet shows them
    (``True`` matches ``TRUE``, ``36`` matches ``36``, None an empty cell).
    ``row`` is the row's number in the sheet (for ``update_range``), ``values``
    the row keyed by the header. It reads the whole range: give only the
    columns you need on a big sheet. For example:

        found = google_sheets.find_rows(spreadsheet_id, "Leads", {"Email": "ada@example.com"}, limit=1)
        if found:
            google_sheets.update_range(spreadsheet_id, f"Leads!D{found[0]['row']}", [["Contacted"]])
    """
    if not isinstance(match, dict) or not match:
        raise ValueError("find_rows() needs match=: a dict of header names and values")
    header, rows = _table(spreadsheet_id, range, value_render, connection)
    wanted = {_column(header, str(name), range): _match_text(value) for name, value in match.items()}
    found = []
    for number, record, _filled in rows:
        if limit is not None and len(found) >= limit:
            break
        if all(_match_text(record.get(name)) == text for name, text in wanted.items()):
            found.append({"row": number, "values": record})
    return found


def list_sheets(spreadsheet_id, *, connection=None):
    """The spreadsheet's sheets (tabs), in order: Google's sheet properties.

    Each has ``sheetId``, ``title``, ``index``, ``sheetType`` and
    ``gridProperties`` (``rowCount``, ``columnCount``). For example:

        for sheet in google_sheets.list_sheets(spreadsheet_id):
            print(sheet["title"], sheet["gridProperties"]["rowCount"])
    """
    reply = _api(connection).get(f"/v4/spreadsheets/{_client.quote(spreadsheet_id)}",
                                 params={"fields": "sheets.properties"}) or {}
    return [sheet.get("properties") or {} for sheet in reply.get("sheets") or []]


def create_spreadsheet(title, *, sheets=None, connection=None):
    """Create a spreadsheet in the account's Drive; returns Google's spreadsheet.

    ``sheets`` names its sheets (one name or a list; default one "Sheet1"). The
    reply has ``spreadsheetId`` and ``spreadsheetUrl``. For example:

        created = google_sheets.create_spreadsheet("Weekly report", sheets=["Summary", "Data"])
        google_sheets.append_rows(created["spreadsheetId"], "Data", [["Date", "Visits"]])
    """
    body = {"properties": {"title": str(title)}}
    if sheets:
        names = [sheets] if isinstance(sheets, str) else list(sheets)
        body["sheets"] = [{"properties": {"title": str(name)}} for name in names]
    return _api(connection).post("/v4/spreadsheets", json=body)


def new_rows(spreadsheet_id, range, state=None, *, key_column=None, limit=None, value_render="FORMATTED_VALUE",
             connection=None):
    """The rows added to ``range`` since ``state``: ``(rows, new_state)``.

    For a scheduled script that acts on new rows (Triggers' "new row" watcher
    uses it too). Rows are ``{"row": n, "values": {...}}`` as ``find_rows``
    gives them, in order. ``state`` is what the previous call returned (a small
    dict; keep it, e.g. in a datastore); without one, nothing is new yet and the
    state marks the current last row. Rows are new by position after that row, which suits
    a sheet rows are added to at the end (a form, ``append_rows``). Deleting or
    sorting rows above the end moves the mark back to the new last row,
    reporting nothing. Empty rows are skipped. With ``key_column`` (a header
    name), a row whose cell there is still empty isn't new yet: it and the rows
    after it are looked at again next time. ``limit`` returns at most that many
    (the rest come next time). For example:

        store = DataStore("sheets")             # from pyrunner_datastore import DataStore
        rows, state = google_sheets.new_rows(spreadsheet_id, "Leads", store.get("leads"), key_column="Email")
        for row in rows:
            print("new lead", row["values"]["Email"])
        store["leads"] = state
    """
    header, rows = _table(spreadsheet_id, range, value_render, connection)
    if key_column is not None:
        _column(header, str(key_column), range)
    last = rows[-1][0] if rows else 0  # the last row with data (0: none yet)
    mark = state.get("last_row") if isinstance(state, dict) else None
    if not isinstance(mark, int) or isinstance(mark, bool) or last < mark:
        # No state yet (nothing is new), or rows were removed: mark the current end.
        return [], {"last_row": last}
    out = []
    for number, record, filled in rows:
        if number <= mark:
            continue
        if limit is not None and len(out) >= limit:
            break
        if not filled:
            mark = number
            continue
        if key_column is not None and _match_text(record.get(str(key_column))) == "":
            break
        out.append({"row": number, "values": record})
        mark = number
    return out, {"last_row": mark}
