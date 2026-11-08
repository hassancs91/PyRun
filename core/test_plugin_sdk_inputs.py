"""
Inputs and results in the plugin SDK (2.5 Stage 0, ticket T04).

What this pins, roughly in order of how much it would hurt to get wrong:

- ``ScriptAPI.queue_run(inputs=)`` checks the values before a run exists:
  bad ones raise ``InputsError`` (a ValueError with a reason per input) and
  leave no Run row and no queued task, also when the schema changed between
  the SDK's check and the queue's own.
- Good inputs are stamped on the run with defaults filled in, next to the
  plugin event, and come back masked through ``RunView``.
- ``ScheduleAPI.sync`` refuses every mode but manual while a required input
  has no default, before writing anything, reading the stored schema; and
  ``ScriptAPI.upsert`` refuses such a schema while the script's schedule is on.
- ``ScriptAPI.upsert(inputs_schema=)`` stores a checked schema; ``{}`` clears
  it, None keeps it, and a bad one raises before anything is saved.

The editor and queue rules these reuse are in core/test_script_inputs.py.
"""

import uuid
from unittest import mock

from django.test import TestCase

from core.models import Environment, Run, Script, ScriptSchedule, Workspace
from core.plugins.api import InputsError, ScheduleAPI, ScriptAPI

OWNER = "inputs_plugin"
SCHEMA = {
    "type": "object",
    "properties": {
        "city": {"type": "string", "title": "City", "maxLength": 40},
        "days": {"type": "integer", "minimum": 1, "maximum": 14, "default": 3},
        "metric": {"type": "boolean", "default": True},
    },
    "required": ["city"],
    "additionalProperties": False,
}
# Same inputs, every required one with a default: a schedule can run it.
SCHEDULABLE = {
    **SCHEMA,
    "properties": {**SCHEMA["properties"], "city": {"type": "string", "default": "Paris"}},
}


class _Fixture:
    def setUp(self):
        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(name="sdkin", path=f"sdkin{uuid.uuid4().hex[:6]}")
        self.api = ScriptAPI(OWNER)
        sync = mock.patch("core.services.schedule_service.ScheduleService.sync_schedule")
        self.sync_schedule = sync.start()
        self.addCleanup(sync.stop)

    def _upsert(self, key="report", **kwargs):
        kwargs.setdefault("code", "print(1)")
        kwargs.setdefault("environment", self.env)
        return self.api.upsert(key=key, **kwargs)

    def _queue(self, key="report", **kwargs):
        with mock.patch("core.tasks.async_task", return_value="task-1") as task:
            run = self.api.queue_run(key, **kwargs)
        return run, task


# --------------------------------------------------------------------------- #
# ScriptAPI.upsert(inputs_schema=)
# --------------------------------------------------------------------------- #

class UpsertSchemaTests(_Fixture, TestCase):
    def test_schema_is_stored_on_create_and_update(self):
        script = self._upsert(inputs_schema=SCHEMA)
        script.refresh_from_db()
        self.assertEqual(script.inputs_schema, SCHEMA)
        self._upsert(inputs_schema=SCHEDULABLE)
        script.refresh_from_db()
        self.assertEqual(script.inputs_schema, SCHEDULABLE)

    def test_none_keeps_the_schema_and_empty_dict_clears_it(self):
        script = self._upsert(inputs_schema=SCHEMA)
        self._upsert(code="print(2)")  # inputs_schema left out
        script.refresh_from_db()
        self.assertEqual(script.inputs_schema, SCHEMA)
        self.assertEqual(script.code, "print(2)")
        self._upsert(inputs_schema={})
        script.refresh_from_db()
        self.assertIsNone(script.inputs_schema)

    def test_a_schema_without_properties_declares_nothing(self):
        script = self._upsert(inputs_schema={"type": "object", "properties": {}})
        script.refresh_from_db()
        self.assertIsNone(script.inputs_schema)

    def test_bad_schema_raises_before_anything_is_saved(self):
        bad = [
            ({"type": "array"}, '"type": "object"'),
            ({"type": "object", "properties": {"x": {"type": "string", "pattern": "a"}}},
             "Invalid inputs schema"),
            ({"type": "object", "properties": {"1x": {"type": "string"}}}, "Input names"),
            ({"type": "object", "properties": {"n": {"type": "integer", "maximum": 2,
                                                     "default": 5}}}, 'default for "n"'),
            ({"type": "object", "properties": {"a": {"type": "string"},
                                               "A": {"type": "string"}}}, "INPUT_A"),
            ("not a dict", "JSON object"),
        ]
        for schema, needle in bad:
            with self.subTest(needle=needle):
                with self.assertRaises(ValueError) as cm:
                    self._upsert(key="new", inputs_schema=schema)
                self.assertIn(needle, str(cm.exception))
                self.assertFalse(Script.objects.filter(owner_plugin=OWNER, owner_key="new").exists())

        script = self._upsert(inputs_schema=SCHEMA, code="print(1)")
        with self.assertRaises(ValueError):
            self._upsert(inputs_schema={"type": "array"}, code="print(3)")
        script.refresh_from_db()
        self.assertEqual((script.inputs_schema, script.code), (SCHEMA, "print(1)"))

    def test_legacy_lane_takes_a_schema_too(self):
        script = ScriptAPI().upsert(name="legacy", code="x", environment=self.env,
                                    inputs_schema=SCHEMA)
        script.refresh_from_db()
        self.assertIsNone(script.owner_plugin)
        self.assertEqual(script.inputs_schema, SCHEMA)


class UpsertScheduleConflictTests(_Fixture, TestCase):
    def _scheduled(self, *, active=True, schema=SCHEDULABLE):
        script = self._upsert(inputs_schema=schema)
        ScriptSchedule.objects.create(
            script=script, workspace=self.ws, run_mode=ScriptSchedule.RunMode.INTERVAL,
            interval_minutes=60, is_active=active,
        )
        return script

    def test_refused_while_the_schedule_is_on(self):
        script = self._scheduled()
        with self.assertRaises(ValueError) as cm:
            self._upsert(inputs_schema=SCHEMA, code="print(9)")
        self.assertIn("active schedule", str(cm.exception))
        self.assertIn('"city"', str(cm.exception))
        script.refresh_from_db()
        self.assertEqual((script.inputs_schema, script.code), (SCHEDULABLE, "print(1)"))

    def test_allowed_when_paused_or_manual_or_every_required_input_has_a_default(self):
        script = self._scheduled(active=False)
        self._upsert(inputs_schema=SCHEMA)
        script.refresh_from_db()
        self.assertEqual(script.inputs_schema, SCHEMA)

        ScriptSchedule.objects.filter(script=script).update(
            run_mode=ScriptSchedule.RunMode.MANUAL, is_active=True
        )
        self._upsert(inputs_schema=SCHEMA)

        ScriptSchedule.objects.filter(script=script).update(
            run_mode=ScriptSchedule.RunMode.INTERVAL, is_active=True
        )
        self._upsert(inputs_schema=SCHEDULABLE)
        script.refresh_from_db()
        self.assertEqual(script.inputs_schema, SCHEDULABLE)

    def test_other_edits_are_not_blocked_by_the_rule(self):
        # The check runs only when a schema is passed: an existing conflict (a
        # restore, an edit in the console) doesn't lock the plugin out.
        script = self._scheduled()
        Script.objects.filter(pk=script.pk).update(inputs_schema=SCHEMA)
        self._upsert(code="print(5)")
        script.refresh_from_db()
        self.assertEqual(script.code, "print(5)")


# --------------------------------------------------------------------------- #
# ScriptAPI.queue_run(inputs=)
# --------------------------------------------------------------------------- #

class QueueRunInputsTests(_Fixture, TestCase):
    def test_inputs_are_stamped_with_defaults_next_to_the_plugin_event(self):
        self._upsert(inputs_schema=SCHEMA)
        run, task = self._queue(inputs={"city": "Oslo"})
        run.refresh_from_db()
        self.assertEqual(run.inputs, {"city": "Oslo", "days": 3, "metric": True})
        self.assertEqual(run.event["kind"], "plugin")
        self.assertEqual(run.event["script_key"], "report")
        self.assertEqual(run.status, Run.Status.PENDING)
        self.assertEqual(run.task_id, "task-1")
        task.assert_called_once()

    def test_no_inputs_uses_the_defaults(self):
        self._upsert(inputs_schema=SCHEDULABLE)
        run, _task = self._queue()
        run.refresh_from_db()
        self.assertEqual(run.inputs, {"city": "Paris", "days": 3, "metric": True})

    def test_script_without_inputs_queues_as_before(self):
        self._upsert()
        run, task = self._queue()
        run.refresh_from_db()
        self.assertIsNone(run.inputs)
        task.assert_called_once()

    def test_bad_inputs_raise_inputs_error_and_create_no_run(self):
        self._upsert(inputs_schema=SCHEMA)
        cases = [
            ({"city": "Oslo", "days": 99}, {"days"}),
            ({"days": 2}, {"city"}),
            ({"city": "Oslo", "metric": "yes"}, {"metric"}),
            ({"city": "Oslo", "extra": 1}, {"extra"}),
            ({"city": "x" * 41, "days": 0}, {"city", "days"}),
        ]
        for inputs, names in cases:
            with self.subTest(inputs=inputs):
                with mock.patch("core.tasks.async_task") as task, \
                        self.assertRaises(InputsError) as cm:
                    self.api.queue_run("report", inputs=inputs)
                self.assertEqual(set(cm.exception.field_errors), names)
                for name in names:
                    self.assertIn(name, cm.exception.message)
                task.assert_not_called()
        self.assertFalse(Run.objects.exists())

    def test_inputs_error_is_a_value_error_with_readable_reasons(self):
        self._upsert(inputs_schema=SCHEMA)
        with self.assertRaises(ValueError) as cm:
            self.api.queue_run("report", inputs={"city": "Oslo", "days": 99})
        self.assertIsInstance(cm.exception, InputsError)
        self.assertEqual(
            cm.exception.field_errors, {"days": "Number is outside the allowed range."}
        )
        self.assertEqual(str(cm.exception), cm.exception.message)

    def test_inputs_for_a_script_that_declares_none_are_refused(self):
        self._upsert()
        with self.assertRaises(InputsError) as cm:
            self.api.queue_run("report", inputs={"city": "Oslo"})
        self.assertIn("doesn't declare any inputs", cm.exception.message)
        self.assertFalse(Run.objects.exists())

    def test_inputs_must_be_a_dict(self):
        self._upsert(inputs_schema=SCHEMA)
        with self.assertRaises(InputsError):
            self.api.queue_run("report", inputs=["Oslo"])
        self.assertFalse(Run.objects.exists())

    def test_schema_change_between_the_checks_still_creates_no_run(self):
        # The SDK checks first; queue_script_run checks again against the schema
        # as it is then and deletes the pending row. Both end in InputsError.
        self._upsert(inputs_schema=SCHEMA)
        with mock.patch("core.services.script_inputs.prepare_inputs",
                        return_value={"town": "Oslo"}), \
                mock.patch("core.tasks.async_task") as task, \
                self.assertRaises(InputsError) as cm:
            self.api.queue_run("report", inputs={"town": "Oslo"})
        self.assertIn("city", cm.exception.field_errors)
        task.assert_not_called()
        self.assertFalse(Run.objects.exists())

    def test_unknown_key_is_still_a_plain_value_error(self):
        with self.assertRaises(ValueError) as cm:
            self.api.queue_run("ghost", inputs={"city": "Oslo"})
        self.assertNotIsInstance(cm.exception, InputsError)

    def test_run_view_shows_inputs_result_and_progress_masked(self):
        self._upsert(inputs_schema=SCHEMA)
        run, _task = self._queue(inputs={"city": "s3cr3t-city"})
        Run.objects.filter(pk=run.pk).update(
            result={"forecast": "sunny"},
            progress={"pct": 50, "message": "Half way", "updated_at": "2026-11-08T10:00:00+00:00"},
        )
        with mock.patch("core.executor.resolve_secrets_for_run",
                        return_value={"CITY_TOKEN": "s3cr3t-city"}):
            view = self.api.latest_run("report")
        self.assertEqual(view.inputs["city"], "[CITY_TOKEN:MASKED]")
        self.assertEqual(view.inputs["days"], 3)
        self.assertEqual(view.result, {"forecast": "sunny"})
        self.assertEqual(view.progress["pct"], 50)
        self.assertEqual(view.as_dict()["inputs"], view.inputs)


# --------------------------------------------------------------------------- #
# ScheduleAPI.sync
# --------------------------------------------------------------------------- #

class ScheduleSyncInputsTests(_Fixture, TestCase):
    MODES = [
        {"mode": ScriptSchedule.RunMode.INTERVAL, "interval_minutes": 60},
        {"mode": ScriptSchedule.RunMode.DAILY, "time_str": "09:00"},
        {"mode": ScriptSchedule.RunMode.WEEKLY, "weekday": 0, "time_str": "09:00"},
        {"mode": ScriptSchedule.RunMode.CRON, "cron": "0 9 * * 1-5"},
        {"mode": ScriptSchedule.RunMode.YEARLY, "month": 6, "day": 15, "time_str": "02:00"},
    ]

    def test_refused_for_every_mode_but_manual_and_nothing_is_written(self):
        script = self._upsert(inputs_schema=SCHEMA)
        for kwargs in self.MODES:
            with self.subTest(mode=kwargs["mode"]):
                with self.assertRaises(ValueError) as cm:
                    ScheduleAPI(OWNER).sync(script, **kwargs)
                self.assertIn("Schedule not saved", str(cm.exception))
                self.assertIn('"city"', str(cm.exception))
        self.assertFalse(ScriptSchedule.objects.filter(script=script).exists())
        self.sync_schedule.assert_not_called()

    def test_an_existing_schedule_is_left_as_it_was(self):
        script = self._upsert()
        ScheduleAPI(OWNER).sync(script, mode=ScriptSchedule.RunMode.MANUAL)
        self._upsert(inputs_schema=SCHEMA)  # allowed: the schedule is manual
        with self.assertRaises(ValueError):
            ScheduleAPI(OWNER).sync(script, mode=ScriptSchedule.RunMode.INTERVAL,
                                    interval_minutes=30)
        sched = ScriptSchedule.objects.get(script=script)
        self.assertEqual((sched.run_mode, sched.is_active, sched.interval_minutes),
                         (ScriptSchedule.RunMode.MANUAL, False, None))

    def test_manual_is_always_allowed(self):
        script = self._upsert(inputs_schema=SCHEMA)
        sched = ScheduleAPI(OWNER).sync(script, mode=ScriptSchedule.RunMode.MANUAL)
        self.assertFalse(sched.is_active)

    def test_allowed_when_every_required_input_has_a_default(self):
        script = self._upsert(inputs_schema=SCHEDULABLE)
        sched = ScheduleAPI(OWNER).sync(script, mode=ScriptSchedule.RunMode.INTERVAL,
                                        interval_minutes=60)
        self.assertTrue(sched.is_active)
        self.sync_schedule.assert_called_once()

    def test_reads_the_stored_schema_not_the_callers_copy(self):
        stale = self._upsert(inputs_schema=SCHEDULABLE)
        Script.objects.filter(pk=stale.pk).update(inputs_schema=SCHEMA)
        with self.assertRaises(ValueError):
            ScheduleAPI(OWNER).sync(stale, mode=ScriptSchedule.RunMode.DAILY, time_str="09:00")

        fresh = self._upsert(key="other", inputs_schema=SCHEMA)
        Script.objects.filter(pk=fresh.pk).update(inputs_schema=SCHEDULABLE)
        sched = ScheduleAPI(OWNER).sync(fresh, mode=ScriptSchedule.RunMode.DAILY, time_str="09:00")
        self.assertTrue(sched.is_active)
