"""
Watcher adapters of the google_sheets integration (see ``framework.Trigger``).

``sheets_new_row``: a run per row added to a sheet, through
``google_sheets.new_rows``. Triggers (2.5 Stage 7) runs it; until then
nothing does.
"""

import hashlib
import json

from core.integrations.framework import Trigger

CONFIG_SCHEMA = {
    "type": "object",
    "required": ["spreadsheet_id", "range"],
    "additionalProperties": False,
    "properties": {
        "spreadsheet_id": {
            "type": "string", "minLength": 1, "title": "Spreadsheet id",
            "description": "From the spreadsheet's URL: /spreadsheets/d/<id>/edit.",
        },
        "range": {
            "type": "string", "minLength": 1, "title": "Sheet or range",
            "description": "A sheet's name (\"Leads\") or an A1 range (\"Leads!A1:F\"); its first row is the header.",
        },
        "key_column": {
            "type": "string", "title": "Key column",
            "description": "A header name: a row counts once this cell is filled in, and its value is the "
                           "row's id. Empty: rows are new by position.",
        },
    },
}


def _event_id(row, key_column) -> str:
    if key_column:
        return f"key:{row['values'].get(key_column, '')}"
    # By position: the row number plus its content, so a row added where one was deleted is new again.
    digest = hashlib.sha256(json.dumps(row["values"], sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return f"row:{row['row']}:{digest[:12]}"


def check_new_rows(module, connection, config, state, limit=None):
    key_column = config.get("key_column") or None
    rows, new_state = module.new_rows(config["spreadsheet_id"], config["range"], state, key_column=key_column,
                                      limit=limit, connection=connection)
    events = [{"id": _event_id(row, key_column),
               "item": {"spreadsheet_id": config["spreadsheet_id"], "range": config["range"], **row}}
              for row in rows]
    return events, new_state


TRIGGERS = (
    Trigger(key="sheets_new_row", title="New row in a Google Sheet", config_schema=CONFIG_SCHEMA,
            check=check_new_rows),
)
