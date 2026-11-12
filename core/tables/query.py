"""
Table filters and sort orders, compiled to Django lookups on ``TableRow.data``.

One condition is ``(field, op, value)``: ``field`` a column key or ``id`` /
``created_at`` / ``updated_at``; ``op`` one of ``OPS``. The console grid's
filter chips and the helper's ``where`` dicts both end up as a list of
conditions (AND). They compile to JSONField key lookups, which Django emits
for SQLite (``JSON_EXTRACT``) and Postgres (``jsonb ->``) alike. Values are
coerced through the column type first, so ``{"score": "5"}`` matches the
number 5 and a date filter compares the stored ``YYYY-MM-DD`` text.

Text matching (``contains``, ``prefix``) ignores case on both engines.
"""

from django.db.models import F, Q
from django.db.models.fields.json import KeyTransform

from .columns import SYSTEM_FIELDS, coerce, parse_datetime
from .errors import TableError

OPS = ("eq", "ne", "gt", "gte", "lt", "lte", "contains", "prefix", "in", "empty", "not_empty")
OP_LABELS = {
    "eq": "is",
    "ne": "is not",
    "gt": ">",
    "gte": "≥",
    "lt": "<",
    "lte": "≤",
    "contains": "contains",
    "prefix": "starts with",
    "in": "is one of",
    "empty": "is empty",
    "not_empty": "is not empty",
}
_RANGE = ("gt", "gte", "lt", "lte")
OPS_BY_TYPE = {
    "text": ("eq", "ne", "contains", "prefix", "in", "empty", "not_empty"),
    "link": ("eq", "ne", "contains", "prefix", "empty", "not_empty"),
    "number": ("eq", "ne", *_RANGE, "in", "empty", "not_empty"),
    "date": ("eq", "ne", *_RANGE, "empty", "not_empty"),
    "datetime": ("eq", "ne", *_RANGE, "empty", "not_empty"),
    "boolean": ("eq", "ne", "empty", "not_empty"),
    "json": ("empty", "not_empty"),
    # System fields.
    "id": ("eq", "ne", *_RANGE, "in"),
    "timestamp": _RANGE,
}
# Ops that take no value.
VALUELESS = ("empty", "not_empty")
# The helper's where-dict spelling of each op.
DOLLAR = {f"${op}": op for op in OPS}
MAX_IN = 100
# A whole number in a filter must fit 64 bits: neither database binds a
# bigger one (SQLite raises OverflowError, Postgres "bigint out of range").
INT_MIN, INT_MAX = -(2**63), 2**63 - 1


def field_type(columns: list, field: str) -> str:
    if field == "id":
        return "id"
    if field in ("created_at", "updated_at"):
        return "timestamp"
    for column in columns:
        if column["key"] == field:
            return column["type"]
    known = ", ".join([c["key"] for c in columns] + list(SYSTEM_FIELDS))
    raise TableError(f'Unknown field "{field}" (fields: {known}).', "unsupported_filter")


def _value(columns, field, kind, op, value):
    """The condition's value coerced for comparison against the stored form."""
    if op == "in":
        if not isinstance(value, (list, tuple)) or not value:
            raise TableError(f'"{field}" in needs a non-empty list.', "unsupported_filter")
        if len(value) > MAX_IN:
            raise TableError(f'"{field}" in takes at most {MAX_IN} values.', "unsupported_filter")
        return [_value(columns, field, kind, "eq", v) for v in value]
    if op in ("contains", "prefix"):
        if not isinstance(value, (str, int, float)) or isinstance(value, bool) or value == "":
            raise TableError(f'"{field}" {OP_LABELS[op]} needs some text.', "unsupported_filter")
        return str(value)
    if kind == "id":
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = None
        if number is None or not INT_MIN <= number <= INT_MAX:
            raise TableError(f'"id" compares with a whole number; got {value!r}.', "unsupported_filter")
        return number
    if kind == "timestamp":
        moment = parse_datetime(value)
        if moment is None:
            raise TableError(f'"{field}" compares with a date and time; got {value!r}.', "unsupported_filter")
        return moment
    column = next(c for c in columns if c["key"] == field)
    try:
        coerced = coerce(column, value)
    except TableError as exc:
        raise TableError(exc.message, "unsupported_filter") from None
    if coerced is None:
        raise TableError(f'"{field}" {OP_LABELS[op]} needs a value.', "unsupported_filter")
    if isinstance(coerced, int) and not isinstance(coerced, bool) and not INT_MIN <= coerced <= INT_MAX:
        raise TableError(
            f'"{field}" compares with whole numbers from {INT_MIN} to {INT_MAX}; got {value!r}.',
            "unsupported_filter",
        )
    return coerced


def condition_q(columns: list, field: str, op: str, value=None) -> Q:
    """One condition as a ``Q`` on TableRow."""
    if op not in OPS:
        raise TableError(f'Unknown operator "{op}" (operators: {", ".join(OPS)}).', "unsupported_filter")
    kind = field_type(columns, field)
    if op not in OPS_BY_TYPE[kind]:
        raise TableError(
            f'"{field}" ({kind}) can\'t be filtered with "{op}"; it takes {", ".join(OPS_BY_TYPE[kind])}.',
            "unsupported_filter",
        )
    if kind in ("id", "timestamp"):
        value = _value(columns, field, kind, op, value)
        if op == "ne":
            return ~Q(**{field: value})
        return Q(**{field if op == "eq" else f"{field}__{op}": value})
    # Every lookup is spelled out (``__exact`` too): a column key that is also a
    # lookup name ("contains", "gt", "in", "isnull") would otherwise be read as
    # that lookup on the whole ``data`` instead of as the key.
    path = f"data__{field}"
    empty = Q(**{f"{path}__isnull": True}) | Q(**{f"{path}__exact": None})
    if op == "empty":
        return empty
    if op == "not_empty":
        return ~empty
    value = _value(columns, field, kind, op, value)
    if op == "eq":
        return Q(**{f"{path}__exact": value})
    if op == "ne":
        # SQL's NOT on a missing key is NULL (no match); an empty cell is "not v".
        return ~Q(**{f"{path}__exact": value}) | empty
    if op == "contains":
        return Q(**{f"{path}__icontains": value})
    if op == "prefix":
        return Q(**{f"{path}__istartswith": value})
    return Q(**{f"{path}__{op}": value})


def where_conditions(where) -> list:
    """The helper's ``where`` dict as ``[(field, op, value)]``.

    ``{"field": value}`` is equality (None: empty); ``{"field": {"$gt": 1,
    "$lt": 9}}`` applies each operator; ``{"field": {"$empty": true}}``. All
    conditions must hold.
    """
    if where in (None, {}):
        return []
    if not isinstance(where, dict):
        raise TableError("where must be an object of field: condition.", "unsupported_filter")
    conditions = []
    for field, spec in where.items():
        if isinstance(spec, dict) and spec and all(isinstance(k, str) and k.startswith("$") for k in spec):
            for dollar, value in spec.items():
                if dollar == "$empty":
                    conditions.append((field, "empty" if value else "not_empty", None))
                    continue
                if dollar not in DOLLAR:
                    raise TableError(
                        f'Unknown operator "{dollar}" (operators: {", ".join(sorted(DOLLAR))}, $empty).',
                        "unsupported_filter",
                    )
                conditions.append((field, DOLLAR[dollar], value))
        elif spec is None:
            conditions.append((field, "empty", None))
        else:
            conditions.append((field, "eq", spec))
    return conditions


def filter_rows(queryset, columns: list, conditions: list):
    query = Q()
    for field, op, value in conditions:
        query &= condition_q(columns, field, op, value)
    return queryset.filter(query)


def order_by(columns: list, order) -> list:
    """Django ``order_by`` expressions for ``"field"`` / ``"-field"`` (or a list of them); id breaks ties."""
    if order in (None, "", []):
        return ["id"]
    items = [order] if isinstance(order, str) else order
    if not isinstance(items, (list, tuple)):
        raise TableError("order must be a field name or a list of them.", "unsupported_filter")
    expressions = []
    for item in items:
        if not isinstance(item, str) or not item.lstrip("-"):
            raise TableError("order must be a field name or a list of them.", "unsupported_filter")
        descending = item.startswith("-")
        field = item.lstrip("-")
        kind = field_type(columns, field)
        if kind == "json":
            raise TableError(f'"{field}" is a JSON column and can\'t be sorted.', "unsupported_filter")
        target = F(field) if kind in ("id", "timestamp") else KeyTransform(field, "data")
        expressions.append(target.desc(nulls_last=True) if descending else target.asc(nulls_last=True))
    if not any(isinstance(i, str) and i.lstrip("-") == "id" for i in items):
        expressions.append(F("id").asc())
    return expressions
