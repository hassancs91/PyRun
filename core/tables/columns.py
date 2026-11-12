"""
Table columns: the types, the column-list check and value coercion.

A column is ``{"key", "type", "required", "default"}``. Every value written to
a row goes through ``coerce`` for its column, so a row's ``data`` only ever
holds values of the column's type (or no key at all for an empty cell). The
stored forms are chosen so that sorting and range filters compare correctly on
SQLite and Postgres alike: dates are ``YYYY-MM-DD`` and datetimes are UTC
``YYYY-MM-DDTHH:MM:SSZ`` (fixed width, to the second).
"""

import json
import math
import re
from datetime import date, datetime, timezone
from urllib.parse import urlparse

from .errors import TableError

TYPES = ("text", "number", "boolean", "date", "datetime", "json", "link")
TYPE_LABELS = {
    "text": "Text",
    "number": "Number",
    "boolean": "Yes / no",
    "date": "Date",
    "datetime": "Date and time",
    "json": "JSON",
    "link": "Link",
}
# Every row has these; a column can't use the names.
SYSTEM_FIELDS = ("id", "created_at", "updated_at")
KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
MAX_COLUMNS = 100
MAX_LINK_CHARS = 2048
_TRUE = {"true", "yes", "y", "1", "on"}
_FALSE = {"false", "no", "n", "0", "off"}


def clean_columns(columns) -> list:
    """The column list in its stored form, or ``TableError("invalid")``.

    Accepts the dicts the console builder, the helper and (later) the SDK
    send; drops any other keys; checks the defaults against the types.
    """
    if not isinstance(columns, list) or not columns:
        raise TableError("A table needs at least one column.", "invalid")
    if len(columns) > MAX_COLUMNS:
        raise TableError(f"A table can have at most {MAX_COLUMNS} columns.", "invalid")
    seen = set()
    cleaned = []
    for position, column in enumerate(columns, start=1):
        if not isinstance(column, dict):
            raise TableError(f"Column {position} must be an object with a key and a type.", "invalid")
        key = column.get("key")
        if not isinstance(key, str) or not KEY_RE.match(key):
            raise TableError(
                f"Column {position}: the key must start with a letter or _ and use only letters, "
                "digits and _ (64 characters at most).",
                "invalid",
            )
        if key in SYSTEM_FIELDS:
            raise TableError(f'Column {position}: "{key}" is used by every row already; pick another key.', "invalid")
        if key.lower() in seen:
            raise TableError(f'Column "{key}" appears twice (keys are not case-sensitive here).', "invalid")
        seen.add(key.lower())
        kind = column.get("type", "text")
        if kind not in TYPES:
            raise TableError(f'Column "{key}": type must be one of {", ".join(TYPES)}.', "invalid")
        required = column.get("required", False)
        if not isinstance(required, bool):
            raise TableError(f'Column "{key}": required must be true or false.', "invalid")
        entry = {"key": key, "type": kind, "required": required, "default": None}
        if column.get("default") not in (None, ""):
            try:
                entry["default"] = coerce(entry, column["default"])
            except TableError as exc:
                raise TableError(f"The default {exc.message[0].lower()}{exc.message[1:]}", "invalid") from None
        cleaned.append(entry)
    return cleaned


def _invalid(column, value, expected):
    shown = repr(value)
    if len(shown) > 60:
        shown = shown[:57] + "..."
    return TableError(f'Column "{column["key"]}" expects {expected}; got {shown}.', "invalid")


def coerce(column: dict, value):
    """``value`` as stored for ``column``; None (or "" for a non-text column) is empty."""
    kind = column["type"]
    if value is None:
        return None
    if isinstance(value, str) and value == "" and kind != "text":
        return None
    if kind == "text":
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str):
            return value
        raise _invalid(column, value, "text")
    if kind == "number":
        return _number(column, value)
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str) and value.strip().lower() in _TRUE | _FALSE:
            return value.strip().lower() in _TRUE
        raise _invalid(column, value, "true or false")
    if kind == "date":
        return _date(column, value)
    if kind == "datetime":
        return _datetime(column, value)
    if kind == "json":
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError):
            raise _invalid(column, value, "a JSON value") from None
        return value
    if kind == "link":
        if not isinstance(value, str):
            raise _invalid(column, value, "an http(s) link")
        link = value.strip()
        parsed = urlparse(link)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or len(link) > MAX_LINK_CHARS:
            raise _invalid(column, value, "an http(s) link")
        return link
    raise TableError(f'Column "{column["key"]}" has an unknown type "{kind}".', "invalid")  # pragma: no cover


def _number(column, value):
    if isinstance(value, bool):
        raise _invalid(column, value, "a number")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _invalid(column, value, "a finite number")
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text)
        except ValueError:
            pass
        try:
            number = float(text)
        except ValueError:
            raise _invalid(column, value, "a number") from None
        if not math.isfinite(number):
            raise _invalid(column, value, "a finite number")
        return number
    raise _invalid(column, value, "a number")


def _date(column, value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        text = value.strip()
        if len(text) > 10 and text[10] in "T ":
            text = text[:10]  # a datetime string: its own (local) date
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            pass
    raise _invalid(column, value, "a date (YYYY-MM-DD)")


def parse_datetime(value):
    """An aware UTC datetime from an ISO string, a date string, a datetime or epoch seconds; else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, date):
        moment = datetime(value.year, value.month, value.day)
    elif isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc).replace(microsecond=0)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    else:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).replace(microsecond=0)


def _datetime(column, value):
    moment = parse_datetime(value)
    if moment is None:
        raise _invalid(column, value, "a date and time (ISO 8601, e.g. 2026-11-12T09:30:00Z)")
    # isoformat, not strftime: it pads the year to four digits on every platform.
    return moment.replace(tzinfo=None).isoformat(timespec="seconds") + "Z"


def clean_row(columns: list, data, *, existing: dict | None = None) -> dict:
    """The row's stored ``data`` after writing ``data`` over ``existing``.

    ``existing`` None means a new row: missing columns get their default. Keys
    that aren't columns are refused (a typo would otherwise vanish silently);
    a None value empties the cell; a required column must end up with a value.
    """
    if not isinstance(data, dict):
        raise TableError("A row must be an object of column: value.", "invalid")
    by_key = {c["key"]: c for c in columns}
    unknown = sorted(str(k) for k in data if k not in by_key)
    if unknown:
        raise TableError(
            f"Unknown column{'s' if len(unknown) > 1 else ''}: {', '.join(unknown)} "
            f"(this table has {', '.join(by_key)}).",
            "invalid",
        )
    if existing is None:
        merged = {c["key"]: c["default"] for c in columns if c.get("default") is not None}
    else:
        merged = {k: v for k, v in existing.items() if k in by_key}
    for key, value in data.items():
        merged[key] = coerce(by_key[key], value)
    missing = [c["key"] for c in columns if c.get("required") and merged.get(c["key"]) is None]
    if missing:
        raise TableError(f"Required column{'s' if len(missing) > 1 else ''} without a value: {', '.join(missing)}.", "invalid")
    return {k: v for k, v in merged.items() if v is not None}
