"""Model fields shared by the core models."""

from django.db import models


class OrderedJSONField(models.JSONField):
    """A JSONField that keeps object key order on PostgreSQL as well (2.5, T50).

    Django stores a JSONField as ``jsonb`` on PostgreSQL, which keeps object
    keys shortest first, then bytewise, not in the order they were written.
    SQLite stores the JSON text, so the order survives there. This field uses
    PostgreSQL's ``json`` type instead, which stores the text as sent, so a
    script's declared inputs and a run's inputs, result and event come back in
    the order they were written on both engines.

    Values are sent as ``json`` (not ``jsonb``, which would reorder them before
    the column sees them) and read as text, then decoded by the field, as
    Django does for ``jsonb``. Reading as text also lets DISTINCT and UNION
    compare whole rows.

    What ``json`` gives up on PostgreSQL: it has no equality or containment
    operators, so the lookups that compare JSON values or test for a key
    (``exact``/``in`` on the field or on a key, ``contains``, ``has_key``,
    ``isnull`` on a key) fail there. ``isnull`` on the field itself and the
    text lookups on a key (``->>``, e.g. ``event__kind__startswith``) work.
    Use it for documents that are stored and shown, not searched.
    """

    def db_type(self, connection):
        if connection.vendor == "postgresql":
            return "json"
        return super().db_type(connection)

    def get_db_prep_value(self, value, connection, prepared=False):
        if connection.vendor != "postgresql":
            return super().get_db_prep_value(value, connection, prepared)
        from django.db.backends.postgresql.operations import get_json_dumps
        from psycopg.types.json import Json

        if not prepared:
            value = self.get_prep_value(value)
        return Json(value, dumps=get_json_dumps(self.encoder))

    def select_format(self, compiler, sql, params):
        if compiler.connection.vendor == "postgresql":
            return f"({sql})::text", params
        return super().select_format(compiler, sql, params)

    def from_db_value(self, value, expression, connection):
        # A raw SQL query skips select_format, and the driver then hands over
        # the json column already decoded.
        if value is not None and not isinstance(value, (str, bytes, bytearray)):
            return value
        return super().from_db_value(value, expression, connection)
