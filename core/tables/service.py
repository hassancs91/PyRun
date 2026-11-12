"""
TableService: every read and write on Tables, for every door.

The console views, the run loopback (``pyrunner_tables``) and, later, the
registry endpoints and the plugin SDK all call these functions, so validation,
coercion, the row cap and ``Table.row_count`` behave the same everywhere.

Row cap: an insert reserves its rows with one conditional UPDATE
(``row_count <= cap - n``) inside the transaction that creates them, so two
concurrent inserts can't both pass the cap, nothing is locked, and a failed
insert gives the reservation back with its rollback. Deletes subtract what
they removed. Every write path goes through here, so the count stays exact.
"""

import json
import logging
import re

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F, Value
from django.db.models.functions import Greatest
from django.utils import timezone
from django.utils.text import slugify

from core.models import Table, TableRow

from . import query as table_query
from .columns import TYPE_LABELS, clean_columns, clean_row, coerce
from .errors import TableError

logger = logging.getLogger(__name__)

# Largest stored row (its data as JSON). Bigger values belong in Storage.
MAX_ROW_BYTES = 256 * 1024
MAX_PAGE = 1000
DEFAULT_PAGE = 100
SLUG_MAX = 100
# The handle scripts pass to table(); the console form checks the same pattern.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_BATCH = 500


def slug_for(name: str) -> str:
    return slugify(name or "")[:SLUG_MAX].strip("-") or "table"


def _check_name_and_slug(name: str, slug: str) -> None:
    if not name:
        raise TableError("A table needs a name.", "invalid")
    if len(name) > 100:
        raise TableError("A table name can be at most 100 characters.", "invalid")
    if len(slug) > SLUG_MAX or not SLUG_RE.match(slug):
        raise TableError(
            f'The slug "{slug}" must use lowercase letters, digits, - and _ (starting with a letter or digit, '
            f"{SLUG_MAX} characters at most).",
            "invalid",
        )


def _check_size(data: dict) -> None:
    if len(json.dumps(data, separators=(",", ":"))) > MAX_ROW_BYTES:
        raise TableError(
            f"A row can hold at most {MAX_ROW_BYTES // 1024} KB; keep big values in Storage and link to them.",
            "too_large",
        )


def serialize_row(row: TableRow, columns: list) -> dict:
    """``{"id", <every column>, "created_at", "updated_at"}``; an empty cell is None."""
    out = {"id": row.id}
    for column in columns:
        out[column["key"]] = row.data.get(column["key"])
    out["created_at"] = row.created_at.isoformat().replace("+00:00", "Z")
    out["updated_at"] = row.updated_at.isoformat().replace("+00:00", "Z")
    return out


class TableService:
    # ------------------------------------------------------------------ tables

    @staticmethod
    def create_table(*, workspace, name, columns, slug="", description="", created_by=None,
                     owner_plugin=None, owner_key=None) -> Table:
        name = (name or "").strip()
        slug = (slug or "").strip() or slug_for(name)
        _check_name_and_slug(name, slug)
        try:
            with transaction.atomic():
                return Table.objects.create(
                    workspace=workspace, name=name, slug=slug, columns=clean_columns(columns),
                    description=description or "", created_by=created_by,
                    owner_plugin=owner_plugin, owner_key=owner_key,
                )
        except IntegrityError:
            raise TableError(f'This workspace already has a table named "{name}" or with the slug "{slug}".',
                             "invalid") from None

    @classmethod
    def update_table(cls, table: Table, *, name=None, slug=None, description=None, columns=None) -> Table:
        """Rename, describe or change the columns of ``table``.

        Columns are matched by key. A removed column's values are deleted from
        every row; a column whose type changed has every value converted, and
        the change is refused (nothing written) if any value doesn't convert or
        a newly required column is empty somewhere.
        """
        fields = []
        if name is not None:
            table.name = name.strip()
            fields.append("name")
        if slug is not None:
            table.slug = slug.strip() or slug_for(table.name)
            fields.append("slug")
        if description is not None:
            table.description = description
            fields.append("description")
        _check_name_and_slug(table.name, table.slug)
        with transaction.atomic():
            if columns is not None:
                new_columns = clean_columns(columns)
                cls._migrate_rows(table, table.columns, new_columns)
                table.columns = new_columns
                fields.append("columns")
            try:
                with transaction.atomic():
                    table.save(update_fields=fields + ["updated_at"])
            except IntegrityError:
                raise TableError(
                    f'This workspace already has a table named "{table.name}" or with the slug "{table.slug}".',
                    "invalid",
                ) from None
        return table

    @staticmethod
    def _migrate_rows(table: Table, old: list, new: list) -> None:
        old_by_key = {c["key"]: c for c in old}
        new_by_key = {c["key"]: c for c in new}
        removed = [k for k in old_by_key if k not in new_by_key]
        retyped = [k for k, c in new_by_key.items() if k in old_by_key and old_by_key[k]["type"] != c["type"]]
        required = [k for k, c in new_by_key.items() if c["required"]]
        if not (removed or retyped or required):
            return
        if required:
            rows = TableRow.objects.filter(table=table)
            for key in required:
                missing = rows.filter(table_query.condition_q(new, key, "empty")).count()
                if missing:
                    raise TableError(
                        f'Column "{key}" can\'t be required: {missing} row{"s" if missing > 1 else ""} '
                        "have no value in it. Fill them in first.",
                        "invalid",
                    )
        if not (removed or retyped):
            return
        failures, changed = [], []
        for row in TableRow.objects.filter(table=table).only("id", "data").iterator(chunk_size=1000):
            data = dict(row.data)
            touched = False
            for key in removed:
                if key in data:
                    data.pop(key)
                    touched = True
            for key in retyped:
                if key not in data:
                    continue
                try:
                    value = coerce(new_by_key[key], data[key])
                except TableError:
                    failures.append((row.id, key))
                    continue
                if value is None:
                    data.pop(key)
                else:
                    data[key] = value
                touched = True
            if touched:
                row.data = data
                changed.append(row)
        if failures:
            sample = ", ".join(f"row {rid} ({key})" for rid, key in failures[:5])
            more = f" and {len(failures) - 5} more" if len(failures) > 5 else ""
            raise TableError(
                f"{len(failures)} value{'s' if len(failures) > 1 else ''} can't be converted to the new "
                f"type: {sample}{more}. Fix or clear them first; nothing was changed.",
                "invalid",
            )
        now = timezone.now()
        for row in changed:
            row.updated_at = now
        TableRow.objects.bulk_update(changed, ["data", "updated_at"], batch_size=_BATCH)

    @staticmethod
    def delete_table(table: Table) -> None:
        table.delete()

    # -------------------------------------------------------------------- reads

    @staticmethod
    def queryset(table: Table, *, where=None, conditions=None, order=None):
        conds = list(conditions or []) + table_query.where_conditions(where)
        qs = table_query.filter_rows(TableRow.objects.filter(table=table), table.columns, conds)
        return qs.order_by(*table_query.order_by(table.columns, order))

    @classmethod
    def rows(cls, table: Table, *, where=None, order=None, limit=DEFAULT_PAGE, offset=0) -> list:
        limit, offset = _int(limit, "limit", DEFAULT_PAGE), _int(offset, "offset", 0)
        if not 1 <= limit <= MAX_PAGE:
            raise TableError(f"limit must be between 1 and {MAX_PAGE}.", "invalid")
        if offset < 0:
            raise TableError("offset can't be negative.", "invalid")
        qs = cls.queryset(table, where=where, order=order)[offset:offset + limit]
        return [serialize_row(r, table.columns) for r in qs]

    @classmethod
    def count(cls, table: Table, *, where=None) -> int:
        if not where:
            return TableRow.objects.filter(table=table).count()
        return cls.queryset(table, where=where).order_by().count()

    @staticmethod
    def get_row(table: Table, row_id) -> TableRow:
        try:
            return TableRow.objects.get(table=table, pk=_int(row_id, "id", None))
        except TableRow.DoesNotExist:
            raise TableError(f"Row {row_id} doesn't exist in this table.", "row_not_found") from None

    # ------------------------------------------------------------------- writes

    @staticmethod
    def insert(table: Table, items: list, *, user=None) -> list:
        """Insert rows (validated first, all or nothing); returns the created TableRows."""
        if not isinstance(items, list) or not items:
            raise TableError("Nothing to insert: pass a row or a list of rows.", "invalid")
        now = timezone.now()
        objs = []
        for position, item in enumerate(items, start=1):
            try:
                data = clean_row(table.columns, item)
                _check_size(data)
            except TableError as exc:
                prefix = f"Row {position}: " if len(items) > 1 else ""
                raise TableError(prefix + exc.message, exc.code) from None
            objs.append(TableRow(table=table, data=data, created_at=now, updated_at=now, created_by=user))
        n, cap = len(objs), settings.PYRUNNER_TABLE_ROW_CAP
        with transaction.atomic():
            reserved = Table.objects.filter(pk=table.pk, row_count__lte=cap - n).update(
                row_count=F("row_count") + n, updated_at=now,
            )
            if not reserved:
                current = Table.objects.filter(pk=table.pk).values_list("row_count", flat=True).first() or 0
                raise TableError(
                    f'Table "{table.name}" holds {current:,} of its {cap:,}-row limit, so {n:,} more '
                    f"row{'s' if n > 1 else ''} won't fit. For bigger data use a managed Database.",
                    "row_cap",
                )
            created = TableRow.objects.bulk_create(objs, batch_size=_BATCH)
        table.row_count = (table.row_count or 0) + n
        return created

    @staticmethod
    def update_row(table: Table, row_id, changes: dict, *, row: TableRow | None = None) -> TableRow:
        """Write ``changes`` (column: value; None empties a cell) over one row.

        The row is read again under a row lock (``select_for_update``; Postgres
        holds it, SQLite serializes writes anyway), so two writers changing
        different cells of one row both keep their change.
        """
        pk = row.pk if row is not None else _int(row_id, "id", None)
        with transaction.atomic():
            row = TableRow.objects.select_for_update().filter(table=table, pk=pk).first()
            if row is None:
                raise TableError(f"Row {pk} doesn't exist in this table.", "row_not_found")
            data = clean_row(table.columns, changes, existing=row.data)
            _check_size(data)
            row.data = data
            row.updated_at = timezone.now()
            row.save(update_fields=["data", "updated_at"])
        return row

    @classmethod
    def upsert(cls, table: Table, key_field: str, item: dict, *, user=None) -> tuple:
        """Update the first row whose ``key_field`` equals the item's, else insert it.

        Returns ``(row, created)``. Two writers upserting the same new key at
        the same moment can both insert (no row locks, both engines).
        """
        if not isinstance(item, dict):
            raise TableError("upsert takes one row (an object).", "invalid")
        column = next((c for c in table.columns if c["key"] == key_field), None)
        if column is None:
            raise TableError(f'"{key_field}" isn\'t a column of this table.', "invalid")
        if column["type"] == "json":
            raise TableError("A JSON column can't be the upsert key.", "invalid")
        if item.get(key_field) in (None, ""):
            raise TableError(f'The row needs a value for "{key_field}" to upsert by it.', "invalid")
        key_value = coerce(column, item[key_field])
        existing = (
            TableRow.objects.filter(table=table)
            .filter(table_query.condition_q(table.columns, key_field, "eq", key_value))
            .order_by("id").first()
        )
        if existing is not None:
            return cls.update_row(table, existing.id, item, row=existing), False
        return cls.insert(table, [item], user=user)[0], True

    @staticmethod
    def delete_rows(table: Table, ids) -> int:
        ids = [_int(i, "id", None) for i in (ids if isinstance(ids, (list, tuple)) else [ids])]
        if not ids:
            return 0
        with transaction.atomic():
            _, per_model = TableRow.objects.filter(table=table, pk__in=ids).delete()
            deleted = per_model.get(TableRow._meta.label, 0)
            if deleted:
                Table.objects.filter(pk=table.pk).update(
                    row_count=Greatest(F("row_count") - deleted, Value(0)), updated_at=timezone.now(),
                )
        table.row_count = max(0, (table.row_count or 0) - deleted)
        return deleted

    @staticmethod
    def recount(table: Table) -> int:
        """Set ``row_count`` from the rows (after a restore)."""
        n = TableRow.objects.filter(table=table).count()
        Table.objects.filter(pk=table.pk).update(row_count=n)
        table.row_count = n
        return n

    @staticmethod
    def describe(table: Table) -> dict:
        return {
            "name": table.name,
            "slug": table.slug,
            "description": table.description,
            "columns": [dict(c, type_label=TYPE_LABELS[c["type"]]) for c in table.columns],
            "row_count": table.row_count,
            "row_cap": settings.PYRUNNER_TABLE_ROW_CAP,
            "owner_plugin": table.owner_plugin,
        }


def _int(value, name, default):
    if value is None and default is not None:
        return default
    if isinstance(value, bool):
        raise TableError(f"{name} must be a whole number.", "invalid")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise TableError(f"{name} must be a whole number.", "invalid") from None
