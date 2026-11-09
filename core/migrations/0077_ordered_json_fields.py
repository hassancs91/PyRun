# 2.5, T50: keep JSON key order on PostgreSQL.
#
# Script.inputs_schema and Run.inputs/result/event become OrderedJSONField,
# which is the ``json`` type on PostgreSQL (kept as written) instead of
# ``jsonb`` (keys reordered). On SQLite the column is text either way, so only
# the migration state changes there and the tables aren't rebuilt.
#
# Rows saved before this keep the order PostgreSQL gave them: jsonb didn't
# store the original order, so there is nothing to restore it from. The cast
# changes no values.

import core.models.fields
from django.db import migrations

COLUMNS = {
    "Script": ("inputs_schema",),
    "Run": ("inputs", "result", "event"),
}


def _alter(apps, schema_editor, db_type):
    if schema_editor.connection.vendor != "postgresql":
        return
    quote = schema_editor.quote_name
    for model_name, columns in COLUMNS.items():
        table = apps.get_model("core", model_name)._meta.db_table
        # One ALTER TABLE per table, so each table is rewritten once.
        changes = ", ".join(
            f"ALTER COLUMN {quote(column)} TYPE {db_type} USING {quote(column)}::{db_type}"
            for column in columns
        )
        schema_editor.execute(f"ALTER TABLE {quote(table)} {changes}")


def to_json(apps, schema_editor):
    _alter(apps, schema_editor, "json")


def to_jsonb(apps, schema_editor):
    _alter(apps, schema_editor, "jsonb")


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0076_apicall"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RunPython(to_json, to_jsonb)],
            state_operations=[
                migrations.AlterField(
                    model_name="run",
                    name="event",
                    field=core.models.fields.OrderedJSONField(
                        blank=True,
                        default=None,
                        help_text="What started the run: {kind, received_at, ...} (see core.services.run_events).",
                        null=True,
                    ),
                ),
                migrations.AlterField(
                    model_name="run",
                    name="inputs",
                    field=core.models.fields.OrderedJSONField(
                        blank=True,
                        default=None,
                        help_text="Inputs stamped at queue time (validated against the script's inputs_schema).",
                        null=True,
                    ),
                ),
                migrations.AlterField(
                    model_name="run",
                    name="result",
                    field=core.models.fields.OrderedJSONField(
                        blank=True,
                        default=None,
                        help_text="JSON object the script set with pyrunner_run.set_result() (secrets masked).",
                        null=True,
                    ),
                ),
                migrations.AlterField(
                    model_name="script",
                    name="inputs_schema",
                    field=core.models.fields.OrderedJSONField(
                        blank=True,
                        default=None,
                        help_text="JSON Schema (type object) of the inputs a run of this script takes.",
                        null=True,
                    ),
                ),
            ],
        ),
    ]
