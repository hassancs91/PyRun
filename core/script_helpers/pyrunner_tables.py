"""
PyRun Tables for scripts.

Read and write the rows of a table made on the **Tables** page. Values are
checked and converted against the table's columns on the server, so a number
column always holds numbers and a date column ``YYYY-MM-DD`` text. Standard
library only: the helper talks to PyRun's internal loopback API with the run's
signed token, on SQLite and Postgres installs alike.

Usage:
    from pyrunner_tables import table

    leads = table("leads")                       # the table's slug (or its name)
    row = leads.insert({"email": "ana@example.com", "score": 7})
    ids = leads.insert([{"email": "b@example.com"}, {"email": "c@example.com"}])
    for lead in leads.rows(where={"score": {"$gte": 5}}, order="-created_at", limit=50):
        print(lead["id"], lead["email"], lead["score"])
    leads.get(row["id"])                         # the row, or None
    leads.update(row["id"], {"score": 9})        # None empties a cell
    leads.upsert("email", {"email": "ana@example.com", "score": 10})
    leads.count(where={"status": "new"})
    leads.delete(row["id"])                      # True if it existed

Rows are dicts with ``id``, every column, ``created_at`` and ``updated_at``
(UTC, ISO 8601). ``where`` holds conditions that must all match:
``{"field": value}`` (None: empty), or ``{"field": {"$op": value}}`` with
``$eq``, ``$ne``, ``$gt``, ``$gte``, ``$lt``, ``$lte``, ``$in`` (a list),
``$contains`` and ``$prefix`` (text, ignoring case) and ``$empty`` (true or
false). ``order`` is a field name, ``-field`` for descending, or a list.

A table holds at most 100 000 rows by default (the operator's
``PYRUNNER_TABLE_ROW_CAP``); past it ``insert`` raises ``TableError`` with code
``row_cap``. Bigger data belongs in a managed Database (``pyrunner_db``).
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

# Rows per insert request (the server takes at most 1 000).
INSERT_BATCH = 500


class TableError(Exception):
    """A table operation failed. ``code`` is stable: ``invalid``,
    ``unsupported_filter``, ``row_cap``, ``too_large``, ``not_found`` ..."""

    def __init__(self, message, code="error", status=None):
        super().__init__(message)
        self.code = code
        self.status = status


class TableNotFound(TableError, LookupError):
    """The table does not exist, or this script may not use it."""


class RowNotFound(TableError, LookupError):
    """No row with that id in the table."""


def _request(name: str, op: str, payload: dict) -> dict:
    base = os.environ.get("PYRUNNER_INTERNAL_URL")
    token = os.environ.get("PYRUNNER_INTERNAL_TOKEN")
    if not base or not token:
        raise TableError("PyRun tables are not available in this run context.")
    url = f"{base.rstrip('/')}/internal/tables/{urllib.parse.quote(name, safe='')}/{op}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        code, message = "error", raw.decode("utf-8", "ignore")
        try:
            err = (json.loads(raw) if raw else {}).get("error") or {}
            code = err.get("code") or code
            message = err.get("message") or message
        except ValueError:
            pass
        if e.code == 404 and code == "not_found":
            raise TableNotFound(message, code, e.code) from None
        if code == "row_not_found":
            raise RowNotFound(message, code, e.code) from None
        raise TableError(f"{op} failed (HTTP {e.code}): {message}", code, e.code) from None
    except urllib.error.URLError as e:
        raise TableError(f"Could not reach PyRun: {e}") from None


class Table:
    """One table. Get it with ``table(name)``."""

    def __init__(self, name: str, info: dict):
        self.name = name
        self.title = info.get("name") or name
        self.slug = info.get("slug") or name
        self.columns = [
            {"key": c["key"], "type": c["type"], "required": c.get("required", False), "default": c.get("default")}
            for c in info.get("columns") or []
        ]

    def __repr__(self):
        return f"<Table {self.slug!r} columns={[c['key'] for c in self.columns]}>"

    def _call(self, op, **payload):
        return _request(self.slug, op, payload)

    def insert(self, rows):
        """Insert one row (a dict; returns the stored row) or many (a list; returns their ids).

        A list goes in batches of ``INSERT_BATCH``; each batch is all or
        nothing, so on an error the earlier batches are already stored.
        """
        if isinstance(rows, dict):
            return self._call("insert", rows=[rows])["rows"][0]
        ids = []
        rows = list(rows)
        for start in range(0, len(rows), INSERT_BATCH):
            ids += self._call("insert", rows=rows[start:start + INSERT_BATCH])["ids"]
        return ids

    def rows(self, where=None, order=None, limit=100, offset=0) -> list:
        """Matching rows, ``limit`` (1-1000) at a time from ``offset``."""
        return self._call("rows", where=where, order=order, limit=limit, offset=offset)["rows"]

    def get(self, row_id):
        """The row with this id, or None."""
        return self._call("get", id=row_id)["row"]

    def update(self, row_id, values: dict) -> dict:
        """Change some cells of one row; returns the row. Raises ``RowNotFound``."""
        return self._call("update", id=row_id, values=values)["row"]

    def upsert(self, key: str, values: dict) -> dict:
        """Update the first row whose ``key`` column equals ``values[key]``, else insert ``values``."""
        return self._call("upsert", key=key, values=values)["row"]

    def delete(self, row_id) -> bool:
        """Delete one row; True if it existed."""
        return self._call("delete", ids=[row_id])["deleted"] == 1

    def count(self, where=None) -> int:
        return self._call("count", where=where)["count"]


def table(name: str) -> Table:
    """The table with this slug (or name) in the run's workspace. Raises ``TableNotFound``."""
    return Table(name, _request(name, "info", {})["table"])
