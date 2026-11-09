"""
JSON key order on both engines (2.5, ticket T50).

PostgreSQL's ``jsonb`` keeps object keys shortest first, then bytewise, so a
script's declared inputs and a run's inputs, result and event used to come
back reordered there. ``Script.inputs_schema`` and ``Run.inputs/result/event``
are now ``OrderedJSONField`` (the ``json`` type on PostgreSQL). What this pins:

- Every way a value is written or read (save, UPDATE, bulk_create, values(),
  raw SQL) keeps the order, nested objects included, and DISTINCT, ``isnull``
  and key text lookups still work.
- The Run-now form, the script editor, ``ScriptAPI``, ``prepare_inputs`` and a
  backup restore keep the declared input order; the run page, the result and
  event downloads, ``RunView``, notifications and the read API keep the order
  a run's values were written in.
- The migration turns the columns into ``json`` on PostgreSQL (a no-op on
  SQLite) and keeps the values of rows saved before it.

Every expected order here differs from jsonb's, so these tests fail on
PostgreSQL without the fix; SQLite always kept the order, so run them on
PostgreSQL too (DATABASE_URL); the PostgreSQL-only checks skip on SQLite.
"""

import importlib
import json
import uuid
from types import SimpleNamespace
from unittest import mock

from django.apps import apps
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection
from django.test import TestCase
from django.urls import reverse

from core.forms import ScriptForm, ScriptInputsForm
from core.models import APIToken, Environment, Run, Script, User, Workspace, WorkspaceMembership
from core.plugins.api import ScriptAPI
from core.services.backup_service import BackupService
from core.services.notification_service import NotificationService
from core.services.run_reports import result_excerpt
from core.services.script_inputs import input_fields, prepare_inputs
from core.test_run_reports import _setup_wizard_off

MIGRATION = importlib.import_module("core.migrations.0077_ordered_json_fields")

# jsonb would list these as to, body, subject.
DECLARED = ["subject", "to", "body"]
SCHEMA = {
    "type": "object",
    "properties": {
        "subject": {"type": "string", "default": "Hello"},
        "to": {"type": "string", "default": "a@example.com"},
        "body": {"type": "string", "default": "Hi"},
    },
    "required": ["subject"],
}
# jsonb would give mid, zeta, alpha at the top and reorder the nested objects.
RESULT = {
    "zeta": 1,
    "alpha": {"yy": True, "b": [{"later": 1, "a": 2}]},
    "mid": "x",
}
EVENT = {"kind": "manual", "received_at": "2026-11-09T10:00:00+00:00", "user": {"id": 7, "email": "o@example.com"}}
INPUTS = {"subject": "Hello", "to": "a@example.com", "body": "Hi"}


def _dumps(value):
    return json.dumps(value, ensure_ascii=False)


class _Fixture:
    def setUp(self):
        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(name="t50env", path=f"t50env{uuid.uuid4().hex[:6]}")

    def _script(self, schema=SCHEMA, **kwargs):
        kwargs.setdefault("code", "pass")
        return Script.objects.create(
            name=f"s{uuid.uuid4().hex[:6]}", environment=self.env, workspace=self.ws,
            inputs_schema=schema, **kwargs,
        )

    def _run(self, script=None, **kwargs):
        kwargs.setdefault("status", Run.Status.SUCCESS)
        return Run.objects.create(script=script or self._script(), workspace=self.ws, **kwargs)

    def assertSameOrder(self, actual, expected):
        # Same values AND the same key order at every level.
        self.assertEqual(actual, expected)
        self.assertEqual(_dumps(actual), _dumps(expected))


# --------------------------------------------------------------------------- #
# The field
# --------------------------------------------------------------------------- #


class FieldTests(_Fixture, TestCase):
    def test_the_columns_are_json_on_postgresql(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL only: SQLite stores JSON as text")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "WHERE table_name IN ('scripts', 'runs') "
                "AND column_name IN ('inputs_schema', 'inputs', 'result', 'event', 'progress')"
            )
            types = {(table, column): kind for table, column, kind in cursor.fetchall()}
        self.assertEqual(types, {
            ("scripts", "inputs_schema"): "json",
            ("runs", "inputs"): "json",
            ("runs", "result"): "json",
            ("runs", "event"): "json",
            ("runs", "progress"): "jsonb",  # unchanged: {pct, message, updated_at}
        })

    def test_save_and_load_keep_the_order(self):
        script = self._script()
        run = self._run(script, inputs=INPUTS, result=RESULT, event=EVENT)
        self.assertEqual(list(Script.objects.get(pk=script.pk).inputs_schema["properties"]), DECLARED)
        loaded = Run.objects.get(pk=run.pk)
        self.assertSameOrder(loaded.inputs, INPUTS)
        self.assertSameOrder(loaded.result, RESULT)
        self.assertSameOrder(loaded.event, EVENT)

    def test_update_keeps_the_order(self):
        # run_reports writes the result with a conditional UPDATE.
        run = self._run()
        Run.objects.filter(pk=run.pk).update(result=RESULT)
        self.assertSameOrder(Run.objects.get(pk=run.pk).result, RESULT)
        run.result = {"b": 1, "a": 2}
        run.save(update_fields=["result"])
        self.assertSameOrder(Run.objects.get(pk=run.pk).result, {"b": 1, "a": 2})

    def test_bulk_create_keeps_the_order(self):
        # More than one row: PostgreSQL's bulk insert goes through UNNEST.
        script = self._script()
        runs = Run.objects.bulk_create([
            Run(script=script, workspace=self.ws, status=Run.Status.SUCCESS, result=RESULT, event=EVENT),
            Run(script=script, workspace=self.ws, status=Run.Status.SUCCESS, result={"b": 1, "a": 2}),
        ])
        self.assertSameOrder(Run.objects.get(pk=runs[0].pk).result, RESULT)
        self.assertSameOrder(Run.objects.get(pk=runs[0].pk).event, EVENT)
        self.assertSameOrder(Run.objects.get(pk=runs[1].pk).result, {"b": 1, "a": 2})

    def test_values_and_raw_queries_keep_the_order(self):
        run = self._run(result=RESULT)
        self.assertSameOrder(Run.objects.filter(pk=run.pk).values_list("result", flat=True).get(), RESULT)
        self.assertSameOrder(Run.objects.filter(pk=run.pk).values("result").get()["result"], RESULT)
        table = Run._meta.db_table
        # A raw query skips select_format; the field decodes what the driver returns.
        key = Run._meta.pk.get_db_prep_value(run.pk, connection)
        raw = list(Run.objects.raw(f"SELECT id, result FROM {table} WHERE id = %s", [key]))
        self.assertEqual(len(raw), 1)
        self.assertSameOrder(raw[0].result, RESULT)

    def test_null_stays_sql_null_and_isnull_works(self):
        empty = self._run()
        full = self._run(result=RESULT)
        self.assertIsNone(Run.objects.get(pk=empty.pk).result)
        self.assertEqual(
            set(Run.objects.filter(result__isnull=True, pk__in=[empty.pk, full.pk]).values_list("pk", flat=True)),
            {empty.pk},
        )

    def test_distinct_and_key_text_lookups_work(self):
        script = self._script()
        run = self._run(script, result=RESULT, event=EVENT)
        # Whole rows, json columns included (json has no equality operator).
        self.assertEqual(Script.objects.filter(pk=script.pk).distinct().count(), 1)
        self.assertEqual(list(Run.objects.filter(pk=run.pk).distinct()), [run])
        self.assertEqual(list(Run.objects.filter(pk=run.pk).order_by().union(Run.objects.filter(pk=run.pk).order_by())), [run])
        self.assertTrue(Run.objects.filter(pk=run.pk, event__kind__startswith="man").exists())
        self.assertFalse(Run.objects.filter(pk=run.pk, event__kind__startswith="web").exists())


# --------------------------------------------------------------------------- #
# Declared input order: every way a schema comes in, every place it's listed
# --------------------------------------------------------------------------- #


class SchemaOrderTests(_Fixture, TestCase):
    def test_run_now_form_and_prepare_inputs_follow_the_declared_order(self):
        script = Script.objects.get(pk=self._script().pk)
        self.assertEqual([spec.name for spec in input_fields(script.inputs_schema)], DECLARED)
        self.assertEqual(list(ScriptInputsForm(script).fields), DECLARED)
        prepared = prepare_inputs(script, {"body": "B", "to": "t@example.com", "subject": "S"})
        self.assertEqual(list(prepared), DECLARED)

    def test_the_editor_stores_the_declared_order(self):
        script = self._script(schema=None)
        data = {
            "name": script.name, "description": "", "code": "pass", "environment": str(self.env.pk),
            "timeout_seconds": "60", "is_enabled": "on", "isolation_mode": "inherit",
            "injection_mode": "all", "notify_on": "never", "inputs_schema": json.dumps(SCHEMA),
        }
        form = ScriptForm(data=data, instance=script, workspace=self.ws)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(list(Script.objects.get(pk=script.pk).inputs_schema["properties"]), DECLARED)

    def test_the_sdk_stores_and_returns_the_declared_order(self):
        api = ScriptAPI("t50_plugin")
        with mock.patch("core.services.schedule_service.ScheduleService.sync_schedule"):
            api.upsert(key="mail", code="pass", environment=self.env, inputs_schema=SCHEMA)
        self.assertEqual(list(api.get("mail").inputs_schema["properties"]), DECLARED)

    def test_a_backup_restore_keeps_the_declared_order(self):
        user = User.objects.create(email="t50-restore@example.com", username="t50-restore")
        script = self._script()
        run = self._run(script, inputs=INPUTS, result=RESULT, event=EVENT)
        # Through JSON text, as a backup file is written and read.
        data = json.loads(json.dumps(BackupService.create_backup(include_runs=True), cls=DjangoJSONEncoder))
        result = BackupService.restore_backup(data, restore_runs=True, current_user=user)
        self.assertTrue(result["success"], result)
        restored = Script.objects.get(name=script.name)
        self.assertEqual(list(restored.inputs_schema["properties"]), DECLARED)
        run = Run.objects.get(pk=run.pk)
        self.assertSameOrder(run.inputs, INPUTS)
        self.assertSameOrder(run.result, RESULT)
        self.assertSameOrder(run.event, EVENT)


# --------------------------------------------------------------------------- #
# A run's values, in the order they were written
# --------------------------------------------------------------------------- #


class RunOrderTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        _setup_wizard_off(self)
        self.user = User.objects.create(email="t50@example.com", username="t50")
        WorkspaceMembership.objects.update_or_create(
            user=self.user, workspace=self.ws, defaults={"role": WorkspaceMembership.ROLE_OWNER},
        )
        self.client.force_login(self.user)
        # Inputs with an undeclared extra, written after the declared ones.
        self.inputs = {**INPUTS, "zz_extra": 1, "a_extra": 2}
        self.run = self._run(inputs=self.inputs, result=RESULT, event=EVENT)

    def test_run_page_lists_inputs_result_and_event_in_order(self):
        resp = self.client.get(reverse("cpanel:run_detail", args=[self.run.pk]))
        self.assertEqual(resp.status_code, 200)
        names = [row["name"] for row in resp.context["input_rows"]]
        self.assertEqual(names, DECLARED + ["zz_extra", "a_extra"])
        self.assertEqual(resp.context["result_text"], json.dumps(RESULT, indent=2, ensure_ascii=False))
        self.assertEqual(resp.context["event"]["text"], json.dumps(EVENT, indent=2, ensure_ascii=False))

    def test_downloads_keep_the_order(self):
        result = self.client.get(reverse("cpanel:run_result_download", args=[self.run.pk]))
        self.assertEqual(result.content.decode(), json.dumps(RESULT, indent=2, ensure_ascii=False))
        event = self.client.get(reverse("cpanel:run_event_download", args=[self.run.pk]))
        self.assertEqual(event.content.decode(), json.dumps(EVENT, indent=2, ensure_ascii=False))

    def test_sdk_and_notifications_keep_the_order(self):
        api = ScriptAPI("t50_plugin")
        with mock.patch("core.services.schedule_service.ScheduleService.sync_schedule"):
            api.upsert(key="mail", code="pass", environment=self.env, inputs_schema=SCHEMA)
        script = api.get("mail")
        self._run(script, inputs=self.inputs, result=RESULT)
        view = api.latest_run("mail")
        self.assertSameOrder(view.inputs, self.inputs)
        self.assertSameOrder(view.result, RESULT)
        run = Run.objects.get(pk=self.run.pk)
        self.assertEqual(result_excerpt(run, 10_000), _dumps(RESULT))
        self.assertSameOrder(NotificationService._build_webhook_payload(run)["result"], RESULT)

    def test_the_read_api_keeps_the_order(self):
        raw = APIToken.generate_token(scope="api", kind="bearer")
        APIToken.objects.create(
            name="t50", scope="api", token=raw, workspace=self.ws,
            permissions=["scripts:read", "runs:read"],
        )
        auth = {"HTTP_AUTHORIZATION": "Bearer " + raw}
        script = self.client.get("/api/v1/scripts.get", {"script": str(self.run.script_id)}, **auth)
        self.assertEqual(script.status_code, 200, script.content)
        self.assertEqual(list(script.json()["data"]["inputs_schema"]["properties"]), DECLARED)
        run = self.client.get("/api/v1/runs.get", {"run": str(self.run.pk)}, **auth)
        self.assertEqual(run.status_code, 200, run.content)
        data = run.json()["data"]
        self.assertSameOrder(data["inputs"], self.inputs)
        self.assertSameOrder(data["result"], RESULT)
        self.assertEqual(list(data["event"]), list(EVENT))


# --------------------------------------------------------------------------- #
# The migration
# --------------------------------------------------------------------------- #


class MigrationTests(_Fixture, TestCase):
    def test_it_does_nothing_on_sqlite(self):
        editor = SimpleNamespace(connection=SimpleNamespace(vendor="sqlite"), execute=mock.Mock())
        MIGRATION.to_json(apps, editor)
        MIGRATION.to_jsonb(apps, editor)
        editor.execute.assert_not_called()

    def test_existing_rows_keep_their_values_on_postgresql(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL only: the column type changes there")
        with connection.cursor() as cursor:
            # Rows made in this test transaction have deferred foreign-key
            # checks pending, and ALTER TABLE refuses to run while they are.
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        with connection.schema_editor() as editor:
            MIGRATION.to_jsonb(apps, editor)
        script = self._script()
        run = self._run(script, inputs=INPUTS, result=RESULT, event=EVENT)
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        with connection.schema_editor() as editor:
            MIGRATION.to_json(apps, editor)
        # Same values; the order is whatever jsonb kept (it can't be recovered).
        self.assertEqual(Script.objects.get(pk=script.pk).inputs_schema, SCHEMA)
        loaded = Run.objects.get(pk=run.pk)
        self.assertEqual((loaded.inputs, loaded.result, loaded.event), (INPUTS, RESULT, EVENT))
        # New writes keep their order again.
        Run.objects.filter(pk=run.pk).update(result=RESULT)
        self.assertSameOrder(Run.objects.get(pk=run.pk).result, RESULT)
