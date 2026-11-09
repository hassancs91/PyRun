"""
SDK schedules: time lists and monthly mode (2.5 Stage 1, ticket T10).

What this pins, roughly in order of how much it would hurt to get wrong:

- ``ScheduleAPI.sync(times=[...], weekdays=[...], month_days=[...])`` and
  ``mode="monthly"`` save the same ScriptSchedule row the console's schedule
  form saves for the same choices, and the same django-q2 crons (checked
  against a real form save in a zone that shifts the day).
- The single-value kwargs (``time_str``, ``weekday``) keep working and mean
  the one-entry list.
- A bad list, a list the mode doesn't use, both forms of one kwarg or an
  unknown mode is a ValueError that names the problem, and nothing is saved:
  no new row, an existing schedule untouched, django-q2 never called.
- A repeated time ("09:00, 9:00") is refused by the form and the SDK alike
  (finding F2.5b-02 of the 2.5 test pass), and a row the form saved with one
  before that still gets one cron per time, so the script runs once.
"""

import uuid
from unittest import mock

from django.test import TestCase
from django_q.models import Schedule as QSchedule

from core.forms import ScheduleForm
from core.models import Environment, Script, ScriptSchedule, Workspace
from core.plugins.api import ScheduleAPI, ScriptAPI
from core.services.schedule_service import ScheduleService

OWNER = "sched_lists"
Mode = ScriptSchedule.RunMode

# Every field the form and the SDK both write.
ROW_FIELDS = (
    "run_mode", "timezone", "is_active", "interval_minutes", "daily_times",
    "weekly_days", "weekly_times", "monthly_days", "monthly_times",
    "cron_expression", "yearly_month", "yearly_day", "yearly_time",
)

# What the editor posts when only one mode's fields are filled in.
BLANK_POST = {
    "interval_minutes": "", "daily_times_input": "", "weekly_times_input": "",
    "monthly_times_input": "", "cron_expression": "", "yearly_month": "",
    "yearly_day": "", "yearly_time": "", "is_active": "on",
}


class _Fixture:
    def setUp(self):
        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(name="sl", path=f"sl{uuid.uuid4().hex[:6]}")

    def _script(self, key=None):
        return ScriptAPI(OWNER).upsert(
            key=key or uuid.uuid4().hex[:8], code="print(1)", environment=self.env
        )


class FormParityTests(_Fixture, TestCase):
    """The SDK and the form produce the same row and the same django-q2 crons."""

    TZ = "Asia/Tokyo"  # +09:00, no DST: early-morning times move to the previous UTC day

    def _form_row(self, **post):
        script = Script.objects.create(
            name=f"form-{uuid.uuid4().hex[:6]}", code="print(1)", environment=self.env
        )
        form = ScheduleForm(
            {**BLANK_POST, "timezone": self.TZ, **post},
            instance=ScriptSchedule(script=script, workspace=self.ws),
        )
        self.assertTrue(form.is_valid(), form.errors)
        sched = form.save()
        ScheduleService.sync_schedule(sched)  # what the editor view does next
        return ScriptSchedule.objects.get(pk=sched.pk)

    def _sdk_row(self, **kwargs):
        sched = ScheduleAPI(OWNER).sync(self._script(), tz=self.TZ, **kwargs)
        return ScriptSchedule.objects.get(pk=sched.pk)

    def assertSameRow(self, form_row, sdk_row):
        for field in ROW_FIELDS:
            with self.subTest(field=field):
                self.assertEqual(getattr(sdk_row, field), getattr(form_row, field))
        self.assertEqual(sdk_row.next_run, form_row.next_run)
        crons = [
            sorted(QSchedule.objects.filter(id__in=row.q_schedule_ids).values_list("cron", flat=True))
            for row in (form_row, sdk_row)
        ]
        self.assertTrue(crons[0], "the form row created no django-q2 schedules")
        self.assertEqual(crons[1], crons[0])

    def test_daily_two_times_in_one_call(self):
        form_row = self._form_row(run_mode="daily", daily_times_input="09:00, 18:00")
        sdk_row = self._sdk_row(mode="daily", times=["09:00", "18:00"])
        self.assertSameRow(form_row, sdk_row)
        self.assertEqual(sdk_row.daily_times, ["09:00", "18:00"])
        self.assertEqual(len(sdk_row.q_schedule_ids), 2)

    def test_weekly_days_and_times(self):
        form_row = self._form_row(
            run_mode="weekly", weekly_days_input=["0", "2", "4"],
            weekly_times_input="06:30, 18:00",
        )
        sdk_row = self._sdk_row(mode="weekly", weekdays=[4, 0, 2], times=["06:30", "18:00"])
        self.assertSameRow(form_row, sdk_row)
        self.assertEqual(sdk_row.weekly_days, [0, 2, 4])

    def test_monthly_days_and_times(self):
        form_row = self._form_row(
            run_mode="monthly", monthly_days_input=["1", "15", "31"],
            monthly_times_input="08:00, 20:30",
        )
        sdk_row = self._sdk_row(mode="monthly", month_days=[15, 1, 31], times=["08:00", "20:30"])
        self.assertSameRow(form_row, sdk_row)
        self.assertEqual(sdk_row.monthly_days, [1, 15, 31])
        self.assertEqual(sdk_row.monthly_times, ["08:00", "20:30"])

    def test_times_normalized_like_the_form(self):
        form_row = self._form_row(run_mode="daily", daily_times_input="9:05, 18:0")
        sdk_row = self._sdk_row(mode="daily", times=["9:05", " 18:0 "])
        self.assertSameRow(form_row, sdk_row)
        self.assertEqual(sdk_row.daily_times, ["09:05", "18:00"])

    def test_yearly_takes_a_one_entry_times_list(self):
        form_row = self._form_row(
            run_mode="yearly", yearly_month="6", yearly_day="15", yearly_time="02:00"
        )
        sdk_row = self._sdk_row(mode="yearly", month=6, day=15, times=["02:00"])
        self.assertSameRow(form_row, sdk_row)

    def test_a_repeated_time_is_refused_by_both(self):
        # Compared after normalizing: "9:00" repeats "09:00". The form used to
        # save the repeat, and each time became a cron, so 09:00 ran twice.
        script = Script.objects.create(name="form-repeat", code="print(1)", environment=self.env)
        cases = [
            ("daily_times_input", {"run_mode": "daily"}, {"mode": "daily"}),
            ("weekly_times_input", {"run_mode": "weekly", "weekly_days_input": ["0"]},
             {"mode": "weekly", "weekdays": [0]}),
            ("monthly_times_input", {"run_mode": "monthly", "monthly_days_input": ["1"]},
             {"mode": "monthly", "month_days": [1]}),
        ]
        for field, post, kwargs in cases:
            with self.subTest(field=field):
                form = ScheduleForm(
                    {**BLANK_POST, "timezone": self.TZ, **post, field: "09:00, 18:00, 9:00"},
                    instance=ScriptSchedule(script=script, workspace=self.ws),
                )
                self.assertFalse(form.is_valid())
                self.assertEqual(form.errors[field], ["09:00 is listed twice."])

                sdk_script = self._script()
                with self.assertRaises(ValueError) as cm:
                    ScheduleAPI(OWNER).sync(
                        sdk_script, tz=self.TZ, times=["09:00", "18:00", "9:00"], **kwargs
                    )
                self.assertIn("times lists '09:00' twice", str(cm.exception))
                self.assertFalse(ScriptSchedule.objects.filter(script=sdk_script).exists())
        self.assertFalse(ScriptSchedule.objects.filter(script=script).exists())

    def test_switching_mode_clears_the_old_mode_fields(self):
        script = self._script()
        api = ScheduleAPI(OWNER)
        api.sync(script, mode="weekly", weekdays=[1], times=["09:00"])
        sched = api.sync(script, mode="monthly", month_days=[10], times=["07:00"])
        sched.refresh_from_db()
        self.assertEqual((sched.weekly_days, sched.weekly_times), ([], []))
        self.assertEqual((sched.monthly_days, sched.monthly_times), ([10], ["07:00"]))
        self.assertEqual(len(sched.q_schedule_ids), 1)
        # tz defaults to UTC, so the cron is the local time as given.
        self.assertEqual(QSchedule.objects.get(id=sched.q_schedule_ids[0]).cron, "0 7 10 * *")
        self.assertFalse(QSchedule.objects.filter(cron__endswith="* * 2").exists())


class StoredRepeatedTimeTests(_Fixture, TestCase):
    """A row the form saved with a repeated time, before it refused one, gets
    one django-q2 cron per distinct time; the row itself is left as it was."""

    def _synced_row(self, **fields):
        script = Script.objects.create(
            name=f"stored-{uuid.uuid4().hex[:6]}", code="print(1)", environment=self.env
        )
        sched = ScriptSchedule.objects.create(
            script=script, workspace=self.ws, timezone="Asia/Tokyo", is_active=True, **fields
        )
        ScheduleService.sync_schedule(sched)
        return ScriptSchedule.objects.get(pk=sched.pk)

    def test_one_cron_per_distinct_time(self):
        cases = {
            "daily": {"run_mode": Mode.DAILY},
            "weekly": {"run_mode": Mode.WEEKLY, "weekly_days": [0, 2]},
            "monthly": {"run_mode": Mode.MONTHLY, "monthly_days": [1, 15]},
        }
        for mode, fields in cases.items():
            with self.subTest(mode=mode):
                times_field = f"{mode}_times"
                repeated = self._synced_row(**fields, **{times_field: ["09:00", "18:00", "09:00"]})
                clean = self._synced_row(**fields, **{times_field: ["09:00", "18:00"]})
                crons = [
                    sorted(QSchedule.objects.filter(id__in=row.q_schedule_ids).values_list("cron", flat=True))
                    for row in (repeated, clean)
                ]
                self.assertEqual(len(repeated.q_schedule_ids), 2)
                self.assertEqual(crons[0], crons[1])
                self.assertEqual(repeated.next_run, clean.next_run)
                self.assertEqual(getattr(repeated, times_field), ["09:00", "18:00", "09:00"])


class SingleValueKwargsTests(_Fixture, TestCase):
    """time_str / weekday keep working and mean the one-entry list."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch("core.services.schedule_service.ScheduleService.sync_schedule")
        self.sync_schedule = patcher.start()
        self.addCleanup(patcher.stop)

    def _sync(self, **kwargs):
        return ScheduleAPI(OWNER).sync(self._script(), **kwargs)

    def test_daily_time_str(self):
        old = self._sync(mode="daily", time_str="09:00")
        new = self._sync(mode="daily", times=["09:00"])
        self.assertEqual(old.daily_times, ["09:00"])
        self.assertEqual(old.daily_times, new.daily_times)
        self.assertTrue(old.is_active)

    def test_weekly_weekday_and_time_str(self):
        old = self._sync(mode="weekly", weekday=3, time_str="08:00")
        new = self._sync(mode="weekly", weekdays=[3], times=["08:00"])
        self.assertEqual((old.weekly_days, old.weekly_times), ([3], ["08:00"]))
        self.assertEqual((old.weekly_days, old.weekly_times), (new.weekly_days, new.weekly_times))

    def test_weekday_as_a_digit_string(self):
        sched = self._sync(mode="weekly", weekday="5", time_str="08:00")
        self.assertEqual(sched.weekly_days, [5])

    def test_time_str_is_normalized(self):
        self.assertEqual(self._sync(mode="daily", time_str="9:05").daily_times, ["09:05"])

    def test_monthly_with_time_str(self):
        sched = self._sync(mode="monthly", month_days=[1], time_str="06:00")
        self.assertEqual((sched.monthly_days, sched.monthly_times), ([1], ["06:00"]))

    def test_mode_enum_and_list_tuples(self):
        sched = self._sync(mode=Mode.WEEKLY, weekdays=(6, 5), times=("21:00",))
        self.assertEqual(sched.run_mode, "weekly")
        self.assertEqual((sched.weekly_days, sched.weekly_times), ([5, 6], ["21:00"]))

    def test_unused_single_value_kwargs_are_still_ignored(self):
        # Unused single-value kwargs were always ignored; they still are.
        sched = self._sync(mode="interval", interval_minutes=30, time_str="09:00", weekday=1)
        self.assertEqual(sched.interval_minutes, 30)
        self.assertEqual((sched.daily_times, sched.weekly_days), ([], []))


class InvalidListsTests(_Fixture, TestCase):
    """Every refusal is a ValueError naming the problem; nothing is written."""

    CASES = [
        # (kwargs, text the message must contain)
        ({"mode": "daily", "times": "09:00"}, "times must be a list"),
        ({"mode": "daily", "times": []}, "times is empty"),
        ({"mode": "daily", "times": ["25:00"]}, "times[0] '25:00' is not a valid time"),
        ({"mode": "daily", "times": ["09:00", 900]}, "times[1] 900 is not a valid time"),
        ({"mode": "daily", "times": ["09:00", "9:60"]}, "times[1] '9:60'"),
        ({"mode": "daily", "times": ["09:00:00"]}, "HH:MM"),
        ({"mode": "daily", "times": ["09:00", "9:00"]}, "times lists '09:00' twice"),
        ({"mode": "daily", "time_str": "noon"}, "time_str 'noon' is not a valid time"),
        ({"mode": "daily"}, "daily mode requires times"),
        ({"mode": "weekly", "weekdays": [7], "times": ["09:00"]}, "weekdays[0] 7 is not a weekday"),
        ({"mode": "weekly", "weekdays": [True], "times": ["09:00"]}, "weekdays[0] True"),
        ({"mode": "weekly", "weekdays": [1.0], "times": ["09:00"]}, "weekdays[0] 1.0"),
        ({"mode": "weekly", "weekdays": ["mon"], "times": ["09:00"]}, "weekdays[0] 'mon'"),
        ({"mode": "weekly", "weekdays": [0, 0], "times": ["09:00"]}, "weekdays lists 0 twice"),
        ({"mode": "weekly", "weekdays": {0, 1}, "times": ["09:00"]}, "weekdays must be a list"),
        ({"mode": "weekly", "weekday": -1, "time_str": "09:00"}, "weekday -1 is not a weekday"),
        ({"mode": "weekly", "times": ["09:00"]}, "weekly mode requires weekdays"),
        ({"mode": "weekly", "weekdays": [1]}, "weekly mode requires times"),
        ({"mode": "monthly", "month_days": [0], "times": ["09:00"]}, "month_days[0] 0 is not a day"),
        ({"mode": "monthly", "month_days": [32], "times": ["09:00"]}, "month_days[0] 32"),
        ({"mode": "monthly", "month_days": [], "times": ["09:00"]}, "month_days is empty"),
        ({"mode": "monthly", "times": ["09:00"]}, "monthly mode requires month_days"),
        ({"mode": "monthly", "day": 15, "times": ["09:00"]}, "monthly mode requires month_days"),
        ({"mode": "monthly", "month_days": [1]}, "monthly mode requires times"),
        ({"mode": "yearly", "month": 6, "day": 15, "times": ["09:00", "18:00"]}, "give one time"),
        ({"mode": "weekly", "month_days": [1], "weekdays": [1], "times": ["09:00"]},
         "month_days doesn't apply to weekly mode"),
        ({"mode": "daily", "weekdays": [1], "times": ["09:00"]}, "weekdays doesn't apply to daily mode"),
        ({"mode": "interval", "interval_minutes": 5, "times": ["09:00"]},
         "times doesn't apply to interval mode"),
        ({"mode": "manual", "times": ["09:00"]}, "times doesn't apply to manual mode"),
        ({"mode": "cron", "cron": "0 9 * * *", "month_days": [1]}, "month_days doesn't apply"),
        ({"mode": "daily", "time_str": "09:00", "times": ["10:00"]}, "pass time_str or times, not both"),
        ({"mode": "weekly", "weekday": 1, "weekdays": [2], "times": ["09:00"]},
         "pass weekday or weekdays, not both"),
        ({"mode": "fortnightly"}, "Unknown mode 'fortnightly'"),
        ({"mode": None}, "Unknown mode None"),
    ]

    def setUp(self):
        super().setUp()
        patcher = mock.patch("core.services.schedule_service.ScheduleService.sync_schedule")
        self.sync_schedule = patcher.start()
        self.addCleanup(patcher.stop)

    def test_each_case_is_refused_with_a_clear_message_and_nothing_saved(self):
        script = self._script()
        for kwargs, expected in self.CASES:
            with self.subTest(**{k: repr(v) for k, v in kwargs.items()}):
                with self.assertRaises(ValueError) as cm:
                    ScheduleAPI(OWNER).sync(script, **kwargs)
                self.assertIn(expected, str(cm.exception))
        self.assertFalse(ScriptSchedule.objects.filter(script=script).exists())
        self.sync_schedule.assert_not_called()

    def test_an_existing_schedule_is_left_as_it_was(self):
        script = self._script()
        ScheduleAPI(OWNER).sync(script, mode="monthly", month_days=[1, 15], times=["09:00"])
        self.sync_schedule.reset_mock()
        with self.assertRaises(ValueError):
            ScheduleAPI(OWNER).sync(script, mode="monthly", month_days=[1, 40], times=["09:00"])
        sched = ScriptSchedule.objects.get(script=script)
        self.assertEqual(
            (sched.run_mode, sched.monthly_days, sched.monthly_times, sched.is_active),
            ("monthly", [1, 15], ["09:00"], True),
        )
        self.sync_schedule.assert_not_called()


class MonthlyRequiredInputsTests(_Fixture, TestCase):
    """The required-input rule covers the new monthly path too."""

    def test_monthly_refused_while_a_required_input_has_no_default(self):
        script = ScriptAPI(OWNER).upsert(
            key="needs-input", code="print(1)", environment=self.env,
            inputs_schema={
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        )
        with mock.patch("core.services.schedule_service.ScheduleService.sync_schedule") as sync:
            with self.assertRaises(ValueError) as cm:
                ScheduleAPI(OWNER).sync(script, mode="monthly", month_days=[1], times=["09:00"])
        self.assertIn("Schedule not saved", str(cm.exception))
        self.assertFalse(ScriptSchedule.objects.filter(script=script).exists())
        sync.assert_not_called()
