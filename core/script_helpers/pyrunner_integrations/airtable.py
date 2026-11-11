"""
Airtable for scripts: list, find, create, update and delete records in a base's tables.

Every record function takes the base's id (``app...``) and a table's name or id
(``tbl...``), and uses Airtable's own record shape:
``{"id": "rec...", "createdTime": "...", "fields": {"Name": "Ada", ...}}``.
Empty fields are left out of ``fields`` (Airtable's way). Lists come page by
page as you read them; ``limit`` stops early.

Writes go in batches of 10 records (Airtable's limit per request), one request
each: when a batch fails, the batches before it are already saved. Airtable
allows 5 requests a second per base and, past that, refuses every request for
30 seconds: after a 429 the call waits those 30 seconds and tries again (up to
3 times) before it raises RATE_LIMITED.

The connection is an Airtable personal access token with the scopes your
script needs (``data.records:read``, ``data.records:write``,
``schema.bases:read``) and access to the base.
"""

import json as _json

from . import _client

PROVIDER = "airtable"
BATCH = 10  # records per write request (Airtable's limit)
PAGE_SIZE = 100  # records per page (Airtable's maximum)
RATE_LIMIT_WAIT = 30.0  # seconds Airtable refuses requests after a 429 (it sends no Retry-After)


def _api(connection):
    return _client.shared(connection, PROVIDER, rate_limit_wait=RATE_LIMIT_WAIT)


def _table_path(base, table, record_id=None) -> str:
    path = f"/v0/{_client.quote(base)}/{_client.quote(table)}"
    return f"{path}/{_client.quote(record_id)}" if record_id is not None else path


def _names(value) -> list:
    """A list of names: one name (a string) or several."""
    return [value] if isinstance(value, (str, dict)) else list(value)


def _batches(items):
    if isinstance(items, dict):
        raise TypeError("Pass a list of records (for one record: create_record() or update_record()).")
    items = list(items)
    for start in range(0, len(items), BATCH):
        yield items[start:start + BATCH]


def _list_params(view, fields, sort, formula, limit) -> dict:
    params = {"pageSize": min(PAGE_SIZE, limit) if limit and limit > 0 else PAGE_SIZE}
    if view:
        params["view"] = view
    if formula:
        params["filterByFormula"] = formula
    if fields:
        params["fields[]"] = _names(fields)
    for i, rule in enumerate(_names(sort or ())):
        if isinstance(rule, dict):
            field, direction = rule.get("field"), rule.get("direction") or "asc"
        else:
            rule = str(rule)
            field, direction = (rule[1:], "desc") if rule.startswith("-") else (rule, "asc")
        params[f"sort[{i}][field]"] = field
        params[f"sort[{i}][direction]"] = direction
    return params


def _literal(value) -> str:
    """``value`` as an Airtable formula literal."""
    if value is None:
        return "BLANK()"
    if isinstance(value, bool):
        return "TRUE()" if value else "FALSE()"
    if isinstance(value, (int, float)):
        return _json.dumps(value, allow_nan=False)
    if isinstance(value, str):
        return _json.dumps(value, ensure_ascii=False)  # double quotes, \" and \\ escaped
    raise ValueError(f"find_records() matches text, numbers, booleans and None, not {type(value).__name__}")


def _match_formulas(match) -> list:
    """One ``{Field}=value`` formula per item of ``match``."""
    parts = []
    for field, value in match.items():
        field = str(field)
        if "{" in field or "}" in field:
            raise ValueError(f"Field name {field!r} has a curly brace: pass formula= instead of match=")
        parts.append(f"{{{field}}}={_literal(value)}")
    return parts


def client(connection=None, *, timeout=30):
    """The shared client of ``connection``, for calls this module has no function for.

    Paths are relative to ``https://api.airtable.com``. For example:

        comments = airtable.client().get(f"/v0/{base}/{table}/{record_id}/comments")["comments"]
    """
    return _client.Client(connection, provider=PROVIDER, timeout=timeout, rate_limit_wait=RATE_LIMIT_WAIT)


def get_record(base, table, record_id, *, connection=None):
    """One record by its id (``rec...``); a record that doesn't exist raises NOT_FOUND.

    For example:

        record = airtable.get_record("appXXXXXXXXXXXXXX", "Contacts", "recXXXXXXXXXXXXXX")
        print(record["fields"].get("Email"))
    """
    return _api(connection).get(_table_path(base, table, record_id))


def list_records(base, table, *, view=None, fields=None, sort=None, formula=None, limit=None, connection=None):
    """Yield the table's records, fetching pages as it goes.

    ``view`` (a view's name or id) gives its records in its order and filters.
    ``fields`` limits each record to those fields (one name or a list). ``sort``
    is a field name or a list of them, ``"-Name"`` for descending (or Airtable's
    ``{"field", "direction"}``).
    ``formula`` is an Airtable formula a record must make true
    (``filterByFormula``). ``limit`` stops after that many records. For example:

        for record in airtable.list_records(base, "Contacts", sort=["-Created"], limit=50):
            print(record["id"], record["fields"].get("Name"))
    """
    return _api(connection).paginate(
        _table_path(base, table), params=_list_params(view, fields, sort, formula, limit),
        items="records", cursor="offset", limit=limit)


def find_records(base, table, match=None, *, formula=None, view=None, fields=None, limit=None,
                 connection=None):
    """A list of the records whose fields equal every value in ``match``.

    ``match`` maps field names to text, numbers, booleans (a checkbox) or None
    (an empty field). ``formula`` adds an Airtable formula of your own (or
    replaces ``match``). ``limit`` stops after that many. For example:

        found = airtable.find_records(base, "Contacts", {"Email": "ada@example.com"}, limit=1)
        contact = found[0] if found else None
    """
    parts = _match_formulas(match or {})
    if formula:
        parts.append(formula)
    if not parts:
        raise ValueError("find_records() needs match= (field values) or formula=")
    combined = parts[0] if len(parts) == 1 else f"AND({','.join(parts)})"
    return list(list_records(base, table, view=view, fields=fields, formula=combined, limit=limit,
                             connection=connection))


def create_records(base, table, records, *, typecast=False, connection=None):
    """Create a record for each dict of field values in ``records``; returns the new records, in order.

    ``typecast=True`` lets Airtable convert values (text to a select option, a
    date string to a date) instead of refusing them. For example:

        created = airtable.create_records(base, "Contacts", [
            {"Name": "Ada", "Email": "ada@example.com"},
            {"Name": "Grace", "Email": "grace@example.com"},
        ])
    """
    api = _api(connection)
    out = []
    for batch in _batches(records):
        body = {"records": [{"fields": dict(fields)} for fields in batch]}
        if typecast:
            body["typecast"] = True
        out += api.post(_table_path(base, table), json=body)["records"]
    return out


def create_record(base, table, fields, *, typecast=False, connection=None):
    """Create one record from a dict of field values and return it (with its new ``id``).

    For example:

        record = airtable.create_record(base, "Contacts", {"Name": "Ada", "Email": "ada@example.com"})
    """
    return create_records(base, table, [fields], typecast=typecast, connection=connection)[0]


def update_records(base, table, records, *, replace=False, typecast=False, upsert_on=None, connection=None):
    """Update records given as ``{"id": "rec...", "fields": {...}}``; returns them as saved.

    Only the given fields change; ``replace=True`` empties every field not
    given. ``upsert_on`` (a list of field names) updates the record whose
    fields match those values, or creates one: the records then need no
    ``id``. For example:

        airtable.update_records(base, "Contacts", [{"id": "recXXXXXXXXXXXXXX", "fields": {"Status": "Done"}}])
        airtable.update_records(base, "Contacts", [{"fields": {"Email": "ada@example.com", "Score": 9}}],
                                upsert_on=["Email"])
    """
    api = _api(connection)
    out = []
    for batch in _batches(records):
        body = {"records": [
            {**({"id": r["id"]} if r.get("id") else {}), "fields": dict(r.get("fields") or {})} for r in batch
        ]}
        if typecast:
            body["typecast"] = True
        if upsert_on:
            body["performUpsert"] = {"fieldsToMergeOn": _names(upsert_on)}
        # PUT replaces the record and is safe to repeat; PATCH only merges.
        reply = api.call("PUT" if replace else "PATCH", _table_path(base, table), json=body)
        out += reply["records"]
    return out


def update_record(base, table, record_id, fields, *, replace=False, typecast=False, connection=None):
    """Update one record's fields and return it as saved (``replace=True`` empties the fields not given).

    For example:

        airtable.update_record(base, "Contacts", "recXXXXXXXXXXXXXX", {"Status": "Done"})
    """
    return update_records(base, table, [{"id": record_id, "fields": fields}], replace=replace,
                          typecast=typecast, connection=connection)[0]


def delete_records(base, table, record_ids, *, connection=None):
    """Delete the records with these ids (one id or a list); returns ``[{"id": ..., "deleted": True}, ...]``.

    For example:

        airtable.delete_records(base, "Contacts", ["recXXXXXXXXXXXXXX", "recYYYYYYYYYYYYYY"])
    """
    if isinstance(record_ids, str):
        record_ids = [record_ids]
    api = _api(connection)
    out = []
    for batch in _batches(record_ids):
        out += api.delete(_table_path(base, table), params={"records[]": list(batch)})["records"]
    return out


def list_bases(*, limit=None, connection=None):
    """Yield the bases the token can reach: ``{"id": "app...", "name": ..., "permissionLevel": ...}``.

    Needs the ``schema.bases:read`` scope. For example:

        for base in airtable.list_bases():
            print(base["id"], base["name"])
    """
    return _api(connection).paginate(
        "/v0/meta/bases", items="bases", cursor="offset", limit=limit)


def list_tables(base, *, connection=None):
    """The base's tables, each with its ``id``, ``name``, ``fields`` (id, name, type) and ``views``.

    Needs the ``schema.bases:read`` scope. For example:

        for table in airtable.list_tables(base):
            print(table["name"], [field["name"] for field in table["fields"]])
    """
    return _api(connection).get(f"/v0/meta/bases/{_client.quote(base)}/tables")["tables"]
