"""
Declared script inputs (2.5 Stage 0, ticket T01).

What this pins, roughly in order of how much it would hurt to get wrong:

- Bad inputs never become a run: the Run-now form rejects them with a field
  error before any row exists, and ``queue_script_run`` (which every trigger
  goes through) deletes the pending row and raises for any caller that skipped
  the check. Never a FAILED run.
- Inputs are stamped on the row BEFORE the task is queued, so a duplicate task
  delivery reads the same values (the task carries only the run id).
- Scalars reach a real subprocess as ``INPUT_<NAME>``; precedence is system
  environment < inputs < secrets.
- The run page's Inputs card masks secret values, and hides them when a secret
  can't be resolved.
- Re-run reuses the exact inputs; schedules refuse required inputs without a
  default (editor, resume toggle, and the scheduled tick itself).
- Backups carry both new fields.
"""

import json
import os
import sys
import uuid
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.executor import _build_script_environment, execute_run, mask_secrets_in_value
from core.forms import ScriptForm, ScriptInputsForm
from core.models import (
    Environment,
    Run,
    Script,
    ScriptSchedule,
    Workspace,
    WorkspaceMembership,
)
from core.services.backup_service import BackupService
from core.services.script_inputs import (
    INPUTS_MAX_BYTES,
    ScriptInputsError,
    check_inputs_schema,
    input_env,
    prepare_inputs,
    required_without_default,
    schedule_conflict,
)
from core.services.script_service import fork_script
from core.services.secret_backends import SecretResolutionError
from core.tasks import execute_scheduled_run, queue_script_run

User = get_user_model()

SCHEMA = {
    "type": "object",
    "properties": {
        "city": {"type": "string", "title": "City", "maxLength": 40},
        "days": {"type": "integer", "minimum": 1, "maximum": 14, "default": 3},
        "metric": {"type": "boolean", "default": True},
        "unit": {"type": "string", "enum": ["C", "F"], "default": "C"},
        "options": {"type": "object"},
    },
    "required": ["city"],
    "additionalProperties": False,
}


def _schema(**overrides):
    schema = json.loads(json.dumps(SCHEMA))
    schema.update(overrides)
    return schema


def _setup_wizard_off(test):
    for target in (
        "core.services.setup_service.SetupService.is_setup_needed",
        "core.services.setup_service.SetupService.needs_admin_setup",
    ):
        p = mock.patch(target, return_value=False)
        p.start()
        test.addCleanup(p.stop)


class _Fixture:
    def setUp(self):
        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(name="ienv", path=f"ienv{uuid.uuid4().hex[:6]}")

    def _script(self, schema=SCHEMA, **kwargs):
        kwargs.setdefault("code", "pass")
        return Script.objects.create(
            name=f"s{uuid.uuid4().hex[:6]}",
            environment=self.env,
            workspace=self.ws,
            inputs_schema=schema,
            **kwargs,
        )

    def _run(self, script, inputs=None, **kwargs):
        kwargs.setdefault("status", Run.Status.PENDING)
        return Run.objects.create(script=script, workspace=self.ws, inputs=inputs, **kwargs)


# --------------------------------------------------------------------------- #
# The schema
# --------------------------------------------------------------------------- #


class CheckInputsSchemaTests(TestCase):
    def test_a_valid_schema_is_returned_unchanged(self):
        self.assertEqual(check_inputs_schema(_schema()), SCHEMA)

    def test_nothing_declared_is_none(self):
        for empty in (None, "", {}, {"type": "object"}, {"type": "object", "properties": {}}):
            with self.subTest(empty=empty):
                self.assertIsNone(check_inputs_schema(empty))

    def test_refusals(self):
        cases = {
            "not an object": ["a"],
            "not type object": {"type": "array", "items": {"type": "string"}},
            "unsupported keyword": {"type": "object", "properties": {"a": {"type": "string", "pattern": "x"}}},
            "property without a type": {"type": "object", "properties": {"a": {}}},
            "required names an undeclared property": {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["b"]},
            "name with a dash": {"type": "object", "properties": {"my-city": {"type": "string"}}},
            "name starting with a digit": {"type": "object", "properties": {"1st": {"type": "string"}}},
            "name over 64 characters": {"type": "object", "properties": {"a" * 65: {"type": "string"}}},
            "names that clash ignoring case": {"type": "object", "properties": {"city": {"type": "string"}, "CITY": {"type": "string"}}},
            "default of the wrong type": {"type": "object", "properties": {"n": {"type": "integer", "default": "3"}}},
            "default outside the choices": {"type": "object", "properties": {"u": {"type": "string", "enum": ["C"], "default": "K"}}},
            "NaN": {"type": "object", "properties": {"n": {"type": "number", "minimum": float("nan")}}},
            "NUL in a title": {"type": "object", "properties": {"a": {"type": "string", "title": "x\x00"}}},
            "too big": {"type": "object", "properties": {"a": {"type": "string", "description": "x" * 40_000}}},
        }
        for label, schema in cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                check_inputs_schema(schema)

    def test_clash_message_names_the_variable(self):
        with self.assertRaisesMessage(ValueError, "INPUT_CITY"):
            check_inputs_schema(
                {"type": "object", "properties": {"city": {"type": "string"}, "City": {"type": "string"}}}
            )

    def test_required_without_default_and_schedule_conflict(self):
        self.assertEqual(required_without_default(SCHEMA), ["city"])
        self.assertIn('"city"', schedule_conflict(SCHEMA))
        with_default = _schema()
        with_default["properties"]["city"]["default"] = "Beirut"
        self.assertEqual(required_without_default(with_default), [])
        self.assertEqual(schedule_conflict(with_default), "")
        self.assertEqual(schedule_conflict(None), "")


# --------------------------------------------------------------------------- #
# Validating a run's inputs
# --------------------------------------------------------------------------- #


class PrepareInputsTests(TestCase):
    def _script(self, schema=SCHEMA):
        return SimpleNamespace(inputs_schema=schema)

    def test_defaults_fill_in_and_order_follows_the_schema(self):
        inputs = prepare_inputs(self._script(), {"unit": "F", "city": "Paris"})
        self.assertEqual(
            inputs, {"city": "Paris", "days": 3, "metric": True, "unit": "F"}
        )
        self.assertEqual(list(inputs), ["city", "days", "metric", "unit"])

    def test_a_default_is_copied_not_shared(self):
        schema = _schema()
        schema["properties"]["options"]["default"] = {"a": [1]}
        inputs = prepare_inputs(self._script(schema), {"city": "x"})
        inputs["options"]["a"].append(2)
        self.assertEqual(schema["properties"]["options"]["default"], {"a": [1]})

    def test_each_bad_input_is_named(self):
        with self.assertRaises(ScriptInputsError) as ctx:
            prepare_inputs(self._script(), {"days": 99, "unit": "K", "extra": 1})
        errors = ctx.exception.field_errors
        self.assertEqual(errors["city"], "This input is required.")
        self.assertIn("range", errors["days"])
        self.assertIn("choices", errors["unit"])
        self.assertIn("doesn't declare", errors["extra"])
        self.assertIn("city", ctx.exception.message)

    def test_type_mismatches(self):
        for name, value in (("city", 5), ("days", 2.5), ("days", True), ("metric", "yes"), ("options", [])):
            with self.subTest(name=name, value=value), self.assertRaises(ScriptInputsError) as ctx:
                prepare_inputs(self._script(), {"city": "x", name: value})
            self.assertIn(name, ctx.exception.field_errors)

    def test_no_schema(self):
        self.assertIsNone(prepare_inputs(self._script(None), None))
        self.assertIsNone(prepare_inputs(self._script(None), {}))
        with self.assertRaises(ScriptInputsError):
            prepare_inputs(self._script(None), {"city": "x"})

    def test_text_both_engines_can_store(self):
        for value in ("a\x00b", "\ud800"):
            with self.subTest(value=repr(value)), self.assertRaises(ScriptInputsError):
                prepare_inputs(self._script(), {"city": value})
        with self.assertRaises(ScriptInputsError):
            prepare_inputs(self._script(), {"city": "x", "options": {"k": "a\x00"}})

    def test_size_cap(self):
        schema = {"type": "object", "properties": {"blob": {"type": "string"}}}
        with self.assertRaisesMessage(ScriptInputsError, "KB limit"):
            prepare_inputs(self._script(schema), {"blob": "x" * INPUTS_MAX_BYTES})

    def test_undeclared_extras_when_allowed_still_need_env_safe_names(self):
        schema = {"type": "object", "properties": {"a": {"type": "string"}}}
        self.assertEqual(prepare_inputs(self._script(schema), {"a": "1", "b": 2}), {"a": "1", "b": 2})
        with self.assertRaises(ScriptInputsError):
            prepare_inputs(self._script(schema), {"bad-name": 1})
        with self.assertRaises(ScriptInputsError):
            prepare_inputs(self._script(schema), {"a": "1", "A": "2"})

    def test_non_object_inputs(self):
        with self.assertRaises(ScriptInputsError):
            prepare_inputs(self._script(), ["city"])


class InputEnvTests(TestCase):
    def test_scalars_are_exported_objects_are_not(self):
        env = input_env(
            {"city": "Paris", "days": 3, "ratio": 2.5, "metric": True, "off": False,
             "options": {"a": 1}, "tags": ["x"], "nothing": None}
        )
        self.assertEqual(
            env,
            {"INPUT_CITY": "Paris", "INPUT_DAYS": "3", "INPUT_RATIO": "2.5",
             "INPUT_METRIC": "true", "INPUT_OFF": "false"},
        )

    def test_empty(self):
        self.assertEqual(input_env(None), {})
        self.assertEqual(input_env({}), {})


class MaskSecretsInValueTests(TestCase):
    def test_strings_keys_and_numbers_are_masked(self):
        secrets = {"API_KEY": "s3cr3t-value", "PIN": "987654"}
        masked = mask_secrets_in_value(
            {"token": "Bearer s3cr3t-value", "s3cr3t-value": 1, "n": 987654, "ok": True,
             "list": ["x s3cr3t-value"], "none": None},
            secrets,
        )
        self.assertEqual(masked["token"], "Bearer [API_KEY:MASKED]")
        self.assertIn("[API_KEY:MASKED]", masked)
        self.assertEqual(masked["n"], "[PIN:MASKED]")
        self.assertEqual(masked["list"], ["x [API_KEY:MASKED]"])
        self.assertIs(masked["ok"], True)
        self.assertIsNone(masked["none"])
        self.assertNotIn("s3cr3t-value", json.dumps(masked))

    def test_no_secrets_is_identity(self):
        value = {"a": "b"}
        self.assertIs(mask_secrets_in_value(value, {}), value)


# --------------------------------------------------------------------------- #
# Queueing: validate + stamp before the task exists
# --------------------------------------------------------------------------- #


class QueueStampTests(_Fixture, TestCase):
    def test_inputs_are_on_the_row_before_the_task_is_queued(self):
        script = self._script()
        run = self._run(script, inputs={"city": "Paris"})
        seen = {}

        def fake_async_task(func, run_id, webhook_data, **kwargs):
            # What a worker (or a duplicate delivery) would read at pickup.
            seen["inputs"] = Run.objects.get(pk=run_id).inputs
            seen["args"] = (run_id, webhook_data)
            return "task-1"

        with mock.patch("core.tasks.async_task", side_effect=fake_async_task):
            queue_script_run(run)
        expected = {"city": "Paris", "days": 3, "metric": True, "unit": "C"}
        self.assertEqual(seen["inputs"], expected)
        # The task carries only the run id: inputs come from the row.
        self.assertEqual(seen["args"], (str(run.id), None))
        run.refresh_from_db()
        self.assertEqual(run.inputs, expected)

    def test_bad_inputs_delete_the_pending_row_and_never_queue(self):
        script = self._script()
        run = self._run(script, inputs={"days": 3})
        with mock.patch("core.tasks.async_task") as task, self.assertRaises(ScriptInputsError):
            queue_script_run(run)
        task.assert_not_called()
        self.assertFalse(Run.objects.filter(pk=run.pk).exists())

    def test_a_script_without_inputs_is_unchanged(self):
        script = self._script(schema=None)
        run = self._run(script)
        with mock.patch("core.tasks.async_task", return_value="t"):
            queue_script_run(run)
        run.refresh_from_db()
        self.assertIsNone(run.inputs)
        self.assertEqual(run.task_id, "t")

    def test_duplicate_delivery_is_a_no_op_and_keeps_the_inputs(self):
        script = self._script()
        run = self._run(script, inputs={"city": "Paris"}, status=Run.Status.RUNNING)
        before = Run.objects.get(pk=run.pk).inputs
        execute_run(run)  # not PENDING: the claim fails, nothing is touched
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.RUNNING)
        self.assertEqual(run.inputs, before)


# --------------------------------------------------------------------------- #
# Executor: INPUT_<NAME> and precedence
# --------------------------------------------------------------------------- #


class InputEnvironmentTests(_Fixture, TestCase):
    def test_precedence_system_then_inputs_then_secrets(self):
        script = self._script()
        run = self._run(script, inputs={"city": "Paris", "days": 3, "metric": True, "unit": "C"})
        with mock.patch.dict(os.environ, {"INPUT_CITY": "from-system", "INPUT_DAYS": "from-system"}):
            env = _build_script_environment(run=run, secrets={"INPUT_DAYS": "from-secret"})
        self.assertEqual(env["INPUT_CITY"], "Paris")  # inputs beat the system env
        self.assertEqual(env["INPUT_DAYS"], "from-secret")  # secrets beat inputs
        self.assertEqual(env["INPUT_METRIC"], "true")
        self.assertEqual(env["INPUT_UNIT"], "C")

    def test_a_run_without_inputs_gets_no_input_vars(self):
        run = self._run(self._script(schema=None))
        env = _build_script_environment(run=run, secrets={})
        self.assertFalse([k for k in env if k.startswith("INPUT_") and k not in os.environ])

    @mock.patch("core.executor._validate_environment", return_value=sys.executable)
    def test_a_real_subprocess_reads_its_inputs(self, _val):
        code = (
            "import os, sys\n"
            "sys.stdout.reconfigure(encoding='utf-8')  # a Windows child writes cp1252 otherwise\n"
            "print('city=' + os.environ['INPUT_CITY'])\n"
            "print('days=' + os.environ['INPUT_DAYS'])\n"
            "print('metric=' + os.environ['INPUT_METRIC'])\n"
            "print('options' , 'INPUT_OPTIONS' in os.environ)\n"
        )
        script = self._script(code=code)
        run = self._run(script, inputs={"city": "Zürich", "days": 5, "metric": False, "unit": "C",
                                        "options": {"a": 1}})
        execute_run(run)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.SUCCESS, run.stderr)
        self.assertIn("city=Zürich", run.stdout)
        self.assertIn("days=5", run.stdout)
        self.assertIn("metric=false", run.stdout)
        self.assertIn("options False", run.stdout)


# --------------------------------------------------------------------------- #
# Console: Run now form, run page, re-run
# --------------------------------------------------------------------------- #


class _ConsoleFixture(_Fixture):
    def setUp(self):
        super().setUp()
        _setup_wizard_off(self)
        self.user = User.objects.create(email="inputs@example.com")
        WorkspaceMembership.objects.update_or_create(
            user=self.user, workspace=self.ws,
            defaults={"role": WorkspaceMembership.ROLE_OWNER},
        )
        self.client.force_login(self.user)


class RunNowFormTests(_ConsoleFixture, TestCase):
    def _url(self, script):
        return reverse("cpanel:script_run", args=[script.pk])

    def test_get_renders_one_field_per_input(self):
        script = self._script()
        resp = self.client.get(self._url(script))
        self.assertEqual(resp.status_code, 200)
        for name in SCHEMA["properties"]:
            self.assertContains(resp, f'name="input-{name}"')
        self.assertContains(resp, 'value="3"')  # the default is prefilled

    def test_detail_page_links_to_the_form(self):
        script = self._script()
        resp = self.client.get(reverse("cpanel:script_detail", args=[script.pk]))
        self.assertContains(resp, f'href="{self._url(script)}"')

    def test_form_page_highlights_only_scripts_in_the_sidebar(self):
        # Found in the 2.5 test pass: "script_run" contains "run", so the
        # sidebar lit up Runs as well as Scripts on this page.
        marker = "absolute left-0 top-1.5 bottom-1.5 w-[3px] rounded-full bg-ok"
        script = self._script()
        detail = self.client.get(reverse("cpanel:script_detail", args=[script.pk])).content.decode()
        form = self.client.get(self._url(script)).content.decode()
        self.assertGreater(detail.count(marker), 0)
        self.assertEqual(form.count(marker), detail.count(marker))

    def test_get_without_a_schema_goes_back_to_the_script(self):
        script = self._script(schema=None)
        resp = self.client.get(self._url(script))
        self.assertRedirects(
            resp, reverse("cpanel:script_detail", args=[script.pk]), fetch_redirect_response=False
        )

    @mock.patch("core.views.scripts.queue_script_run")
    def test_missing_required_input_is_a_field_error_and_no_run(self, queue):
        script = self._script()
        resp = self.client.post(self._url(script), {"input-days": "3", "input-unit": "0"})
        self.assertEqual(resp.status_code, 200)
        self.assertFormError(resp.context["form"], "city", "This field is required.")
        self.assertEqual(Run.objects.count(), 0)
        queue.assert_not_called()

    @mock.patch("core.views.scripts.queue_script_run")
    def test_out_of_range_input_is_a_field_error_and_no_run(self, queue):
        script = self._script()
        resp = self.client.post(self._url(script), {"input-city": "Paris", "input-days": "40"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("days", resp.context["form"].errors)
        self.assertEqual(Run.objects.count(), 0)
        queue.assert_not_called()

    @mock.patch("core.views.scripts.queue_script_run")
    def test_unparseable_json_input_is_a_field_error(self, queue):
        script = self._script()
        resp = self.client.post(self._url(script), {"input-city": "x", "input-options": "{nope"})
        self.assertFormError(resp.context["form"], "options", "Enter valid JSON.")
        self.assertEqual(Run.objects.count(), 0)

    @mock.patch("core.views.scripts.queue_script_run")
    def test_valid_inputs_create_a_run_with_them(self, queue):
        script = self._script()
        resp = self.client.post(
            self._url(script),
            {"input-city": "Paris", "input-days": "7", "input-unit": "1", "input-options": '{"a": 1}'},
        )
        run = Run.objects.get()
        self.assertRedirects(
            resp, reverse("cpanel:run_detail", args=[run.pk]), fetch_redirect_response=False
        )
        # metric's box was left unticked: an explicit false, not the default.
        self.assertEqual(
            run.inputs,
            {"city": "Paris", "days": 7, "metric": False, "unit": "F", "options": {"a": 1}},
        )
        self.assertEqual(run.triggered_by, self.user)
        queue.assert_called_once()

    @mock.patch("core.views.scripts.queue_script_run")
    def test_blank_optional_inputs_take_their_default(self, queue):
        script = self._script()
        self.client.post(self._url(script), {"input-city": "Paris", "input-days": "", "input-unit": ""})
        self.assertEqual(Run.objects.get().inputs["days"], 3)
        self.assertEqual(Run.objects.get().inputs["unit"], "C")
        self.assertNotIn("options", Run.objects.get().inputs)

    @mock.patch("core.views.scripts.queue_script_run")
    def test_a_script_without_inputs_still_runs_from_one_post(self, queue):
        script = self._script(schema=None)
        resp = self.client.post(self._url(script))
        run = Run.objects.get()
        self.assertRedirects(
            resp, reverse("cpanel:run_detail", args=[run.pk]), fetch_redirect_response=False
        )
        self.assertIsNone(run.inputs)

    def test_queue_backstop_never_leaves_a_failed_run(self):
        # The schema got stricter between the form check and the queue.
        script = self._script()

        def stricter(run, webhook_data=None):
            Script.objects.filter(pk=script.pk).update(
                inputs_schema=_schema(required=["city", "options"])
            )
            run.script.refresh_from_db()
            return queue_script_run(run, webhook_data)

        with mock.patch("core.views.scripts.queue_script_run", side_effect=stricter), \
                mock.patch("core.tasks.async_task") as task:
            resp = self.client.post(self._url(script), {"input-city": "Paris"})
        task.assert_not_called()
        self.assertEqual(Run.objects.count(), 0)
        self.assertRedirects(
            resp, reverse("cpanel:script_detail", args=[script.pk]), fetch_redirect_response=False
        )

    def test_other_workspace_script_is_404(self):
        other = Workspace.objects.create(name="Other")
        foreign = Script.objects.create(
            name="Foreign", code="pass", environment=self.env, workspace=other, inputs_schema=SCHEMA
        )
        self.assertEqual(self.client.get(self._url(foreign)).status_code, 404)


class ScriptInputsFormTests(_Fixture, TestCase):
    def test_number_input_keeps_whole_numbers_whole(self):
        schema = {"type": "object", "properties": {"x": {"type": "number"}}}
        script = self._script(schema=schema)
        for raw, expected in (("3", 3), ("2.5", 2.5), ("-1", -1)):
            with self.subTest(raw=raw):
                form = ScriptInputsForm(script, {"input-x": raw})
                self.assertTrue(form.is_valid(), form.errors)
                self.assertEqual(form.inputs, {"x": expected})
                self.assertIs(type(form.inputs["x"]), type(expected))
        for raw in ("abc", "inf", "nan"):
            with self.subTest(raw=raw):
                self.assertFalse(ScriptInputsForm(script, {"input-x": raw}).is_valid())

    def test_json_with_nan_or_infinity_is_refused(self):
        # Neither engine stores NaN/Infinity as JSON, so both JSON fields refuse it.
        script = self._script(schema={"type": "object", "properties": {"o": {"type": "object"}}})
        form = ScriptInputsForm(script, {"input-o": '{"a": NaN}'})
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["o"], ["Enter valid JSON."])

        schema_form = ScriptForm(
            {"name": "n", "code": "pass", "environment": str(self.env.pk), "timeout_seconds": "60",
             "isolation_mode": "inherit", "notify_on": "never",
             "inputs_schema": '{"type": "object", "properties": {"n": {"type": "number", "default": Infinity}}}'},
            workspace=self.ws,
        )
        self.assertFalse(schema_form.is_valid())
        self.assertEqual(schema_form.errors["inputs_schema"], ["The inputs schema isn't valid JSON."])


class RunPageTests(_ConsoleFixture, TestCase):
    def _detail(self, run):
        return self.client.get(reverse("cpanel:run_detail", args=[run.pk]))

    def test_inputs_card_shows_the_values(self):
        run = self._run(self._script(), inputs={"city": "Paris", "days": 3, "options": {"a": 1}})
        resp = self._detail(run)
        self.assertContains(resp, 'id="run-inputs-heading"')
        self.assertContains(resp, "Paris")
        self.assertContains(resp, "&quot;a&quot;: 1")
        self.assertContains(resp, reverse("cpanel:run_rerun", args=[run.pk]))

    def test_secret_values_are_masked(self):
        run = self._run(self._script(), inputs={"city": "token-abc123xyz"})
        with mock.patch("core.executor.resolve_secrets_for_run", return_value={"API_KEY": "abc123xyz"}):
            resp = self._detail(run)
        self.assertContains(resp, "token-[API_KEY:MASKED]")
        self.assertNotContains(resp, "abc123xyz")

    def test_values_are_hidden_when_a_secret_cant_be_resolved(self):
        run = self._run(self._script(), inputs={"city": "abc123xyz"})
        with mock.patch(
            "core.executor.resolve_secrets_for_run", side_effect=SecretResolutionError("vault down")
        ):
            resp = self._detail(run)
        self.assertContains(resp, "Hidden: a secret couldn")
        self.assertNotContains(resp, "abc123xyz")

    def test_no_card_and_no_rerun_for_a_run_without_inputs(self):
        run = self._run(self._script(schema=None))
        resp = self._detail(run)
        self.assertNotContains(resp, 'id="run-inputs-heading"')
        self.assertNotContains(resp, reverse("cpanel:run_rerun", args=[run.pk]))


class RerunTests(_ConsoleFixture, TestCase):
    def _rerun(self, run):
        return self.client.post(reverse("cpanel:run_rerun", args=[run.pk]))

    @mock.patch("core.views.scripts.queue_script_run")
    def test_rerun_creates_a_new_run_with_identical_inputs(self, queue):
        inputs = {"city": "Paris", "days": 5, "metric": False, "unit": "F", "options": {"a": [1, 2]}}
        source = self._run(self._script(), inputs=inputs, status=Run.Status.SUCCESS)
        resp = self._rerun(source)
        new = Run.objects.exclude(pk=source.pk).get()
        self.assertRedirects(
            resp, reverse("cpanel:run_detail", args=[new.pk]), fetch_redirect_response=False
        )
        self.assertEqual(new.inputs, inputs)
        self.assertEqual(new.status, Run.Status.PENDING)
        self.assertEqual(new.trigger_type, Run.TriggerType.MANUAL)
        self.assertEqual(new.triggered_by, self.user)
        queue.assert_called_once()

    @mock.patch("core.views.scripts.queue_script_run")
    def test_rerun_rechecks_against_the_current_schema(self, queue):
        script = self._script()
        source = self._run(script, inputs={"city": "Paris"}, status=Run.Status.SUCCESS)
        script.inputs_schema = _schema(required=["city", "options"])
        script.save()
        resp = self._rerun(source)
        self.assertRedirects(
            resp, reverse("cpanel:run_detail", args=[source.pk]), fetch_redirect_response=False
        )
        self.assertEqual(Run.objects.count(), 1)
        queue.assert_not_called()

    def test_get_is_not_allowed(self):
        source = self._run(self._script(), inputs={"city": "x"})
        resp = self.client.get(reverse("cpanel:run_rerun", args=[source.pk]))
        self.assertEqual(resp.status_code, 405)

    def test_other_workspace_run_is_404(self):
        other = Workspace.objects.create(name="Other")
        foreign_script = Script.objects.create(
            name="Foreign", code="pass", environment=self.env, workspace=other
        )
        foreign = Run.objects.create(script=foreign_script, workspace=other, inputs={})
        self.assertEqual(self._rerun(foreign).status_code, 404)


# --------------------------------------------------------------------------- #
# The editor's Inputs tab and the schedule rule
# --------------------------------------------------------------------------- #


class EditorTests(_ConsoleFixture, TestCase):
    def _payload(self, script, schema, **overrides):
        data = {
            "name": script.name,
            "description": "",
            "code": "pass",
            "inputs_schema": json.dumps(schema) if isinstance(schema, dict) else schema,
            "environment": str(self.env.pk),
            "timeout_seconds": "3600",
            "is_enabled": "on",
            "isolation_mode": "inherit",
            "injection_mode": "all",
            "notify_on": "never",
            "run_mode": "manual",
            "timezone": "UTC",
        }
        data.update(overrides)
        return data

    def _post(self, script, schema, **overrides):
        return self.client.post(
            reverse("cpanel:script_edit", args=[script.pk]), self._payload(script, schema, **overrides)
        )

    def test_the_editor_renders_the_inputs_tab(self):
        script = self._script()
        resp = self.client.get(reverse("cpanel:script_edit", args=[script.pk]))
        self.assertContains(resp, 'id="editor-tab-inputs"')
        self.assertContains(resp, 'name="inputs_schema"')
        self.assertContains(resp, "&quot;city&quot;")  # the saved schema, as JSON text
        self.assertContains(resp, "js/script_inputs")

    def test_saving_a_schema(self):
        script = self._script(schema=None)
        resp = self._post(script, SCHEMA)
        self.assertEqual(resp.status_code, 302)
        script.refresh_from_db()
        self.assertEqual(script.inputs_schema, SCHEMA)

    def test_blank_clears_the_schema(self):
        script = self._script()
        self.assertEqual(self._post(script, "").status_code, 302)
        script.refresh_from_db()
        self.assertIsNone(script.inputs_schema)

    def test_an_invalid_schema_is_refused_on_the_field(self):
        script = self._script(schema=None)
        for bad in ("{not json", json.dumps({"type": "object", "properties": {"my-city": {"type": "string"}}})):
            with self.subTest(bad=bad):
                resp = self._post(script, bad)
                self.assertEqual(resp.status_code, 200)
                self.assertIn("inputs_schema", resp.context["form"].errors)
                script.refresh_from_db()
                self.assertIsNone(script.inputs_schema)

    def test_create_form_saves_a_schema(self):
        form = ScriptForm(
            {
                "name": "new", "code": "pass", "environment": str(self.env.pk),
                "timeout_seconds": "60", "isolation_mode": "inherit", "notify_on": "never",
                "inputs_schema": json.dumps(SCHEMA),
            },
            workspace=self.ws,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["inputs_schema"], SCHEMA)

    def test_turning_on_a_schedule_with_required_inputs_without_default_fails(self):
        script = self._script()
        resp = self._post(script, SCHEMA, run_mode="interval", interval_minutes="60", is_active="on")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("required and has no default", " ".join(resp.context["schedule_form"].non_field_errors()))
        self.assertContains(resp, "Schedule: Input")
        self.assertEqual(script.schedule.run_mode, ScriptSchedule.RunMode.MANUAL)

    def test_adding_a_required_input_to_a_scheduled_script_fails(self):
        script = self._script(schema=None)
        ScriptSchedule.objects.create(
            script=script, workspace=self.ws, run_mode="interval", interval_minutes=60, is_active=True
        )
        resp = self._post(script, SCHEMA, run_mode="interval", interval_minutes="60", is_active="on")
        self.assertEqual(resp.status_code, 200)
        script.refresh_from_db()
        self.assertIsNone(script.inputs_schema)

    def test_a_schedule_is_fine_when_every_required_input_has_a_default(self):
        schema = _schema()
        schema["properties"]["city"]["default"] = "Beirut"
        script = self._script(schema=None)
        resp = self._post(script, schema, run_mode="interval", interval_minutes="60", is_active="on")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(ScriptSchedule.objects.get(script=script).is_scheduled)

    def test_a_paused_schedule_can_keep_required_inputs(self):
        script = self._script(schema=None)
        resp = self._post(script, SCHEMA, run_mode="interval", interval_minutes="60")
        self.assertEqual(resp.status_code, 302)

    def test_resuming_a_paused_schedule_is_refused(self):
        script = self._script()
        schedule = ScriptSchedule.objects.create(
            script=script, workspace=self.ws, run_mode="interval", interval_minutes=60, is_active=False
        )
        self.client.post(reverse("cpanel:schedule_toggle", args=[script.pk]))
        schedule.refresh_from_db()
        self.assertFalse(schedule.is_active)

    def test_pausing_is_always_allowed(self):
        script = self._script()
        schedule = ScriptSchedule.objects.create(
            script=script, workspace=self.ws, run_mode="interval", interval_minutes=60, is_active=True
        )
        self.client.post(reverse("cpanel:schedule_toggle", args=[script.pk]))
        schedule.refresh_from_db()
        self.assertFalse(schedule.is_active)


# --------------------------------------------------------------------------- #
# Other triggers: schedule tick, webhook, chat channel
# --------------------------------------------------------------------------- #


class OtherTriggerTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        _setup_wizard_off(self)

    def test_scheduled_tick_skips_without_creating_a_run(self):
        script = self._script()
        ScriptSchedule.objects.create(
            script=script, workspace=self.ws, run_mode="interval", interval_minutes=60, is_active=True
        )
        with mock.patch("core.tasks.async_task") as task:
            result = execute_scheduled_run(str(script.id))
        self.assertFalse(result["success"])
        self.assertIn("city", result["error"])
        self.assertEqual(Run.objects.count(), 0)
        task.assert_not_called()

    def test_scheduled_tick_uses_defaults(self):
        schema = _schema()
        schema["properties"]["city"]["default"] = "Beirut"
        script = self._script(schema=schema)
        ScriptSchedule.objects.create(
            script=script, workspace=self.ws, run_mode="interval", interval_minutes=60, is_active=True
        )
        with mock.patch("core.tasks.async_task", return_value="t"):
            result = execute_scheduled_run(str(script.id))
        self.assertTrue(result["success"], result)
        self.assertEqual(Run.objects.get().inputs["city"], "Beirut")

    def _webhook(self, script):
        script.webhook_token = "tok_" + uuid.uuid4().hex
        script.save(update_fields=["webhook_token"])
        return self.client.post(
            reverse("webhook_trigger", args=[script.webhook_token]), data="{}", content_type="application/json"
        )

    @mock.patch("core.views.webhooks.queue_script_run")
    def test_webhook_refuses_unfillable_required_inputs(self, queue):
        resp = self._webhook(self._script())
        self.assertEqual(resp.status_code, 400)
        self.assertIn("city", resp.json()["inputs"])
        self.assertEqual(Run.objects.count(), 0)
        queue.assert_not_called()

    @mock.patch("core.views.webhooks.queue_script_run")
    def test_webhook_run_gets_defaults(self, queue):
        schema = _schema()
        schema["properties"]["city"]["default"] = "Beirut"
        resp = self._webhook(self._script(schema=schema))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Run.objects.get().inputs["city"], "Beirut")

    def test_chat_message_refused_without_a_run(self):
        from core.services.channels.handlers import _script_handler

        script = self._script(is_enabled=True)
        channel = SimpleNamespace(inbound_target_id=script.id, workspace_id=self.ws.id, name="Bot")
        msg = SimpleNamespace(reply_ref={"chat_id": "1"}, text="hi", sender={})
        with mock.patch("core.tasks.queue_script_run") as queue:
            out = _script_handler(channel, msg)
        self.assertIn("can't run from chat", out.text)
        self.assertEqual(Run.objects.count(), 0)
        queue.assert_not_called()


# --------------------------------------------------------------------------- #
# Backup and fork carry the new fields
# --------------------------------------------------------------------------- #


class BackupAndForkTests(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create(email="admin-inputs@example.com", is_superuser=True)

    def test_backup_round_trips_schema_and_inputs(self):
        script = self._script()
        inputs = {"city": "Paris", "days": 3, "metric": True, "unit": "C", "options": {"a": 1}}
        Run.objects.create(script=script, workspace=self.ws, status="success", inputs=inputs)
        plain = self._script(schema=None)
        Run.objects.create(script=plain, workspace=self.ws, status="success")

        data = BackupService.create_backup(include_runs=True)
        exported = {s["id"]: s for s in data["scripts"]}
        self.assertEqual(exported[str(script.id)]["inputs_schema"], SCHEMA)
        self.assertIsNone(exported[str(plain.id)]["inputs_schema"])

        result = BackupService.restore_backup(data, restore_runs=True, current_user=self.user)
        self.assertTrue(result["success"], result)
        self.assertEqual(Script.objects.get(pk=script.pk).inputs_schema, SCHEMA)
        self.assertIsNone(Script.objects.get(pk=plain.pk).inputs_schema)
        self.assertEqual(Run.objects.get(script_id=script.pk).inputs, inputs)
        self.assertIsNone(Run.objects.get(script_id=plain.pk).inputs)

    def test_a_backup_without_the_fields_restores_them_as_null(self):
        script = self._script()
        Run.objects.create(script=script, workspace=self.ws, status="success", inputs={"city": "x"})
        data = BackupService.create_backup(include_runs=True)
        for row in data["scripts"]:
            row.pop("inputs_schema")
        for row in data["runs"]:
            row.pop("inputs")
        result = BackupService.restore_backup(data, restore_runs=True, current_user=self.user)
        self.assertTrue(result["success"], result)
        self.assertIsNone(Script.objects.get(pk=script.pk).inputs_schema)
        self.assertIsNone(Run.objects.get().inputs)

    def test_fork_copies_the_schema(self):
        fork = fork_script(self._script(), user=self.user)
        self.assertEqual(fork.inputs_schema, SCHEMA)
