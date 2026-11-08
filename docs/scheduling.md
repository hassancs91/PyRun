# Scheduling scripts

PyRun can run a script automatically on a schedule. Every script has one
schedule, configured in the **Schedule** panel of the script edit form. There
are seven run modes:

| Mode | What it does |
|------|--------------|
| **Manual** | No automatic runs — you trigger it yourself (or via webhook). |
| **Interval** | Every N minutes, any whole number from 1 to 32767 (about 22 days). Quick-pick buttons cover the common values. |
| **Daily** | One or more `HH:MM` times, every day. |
| **Weekly** | One or more days of the week, at one or more `HH:MM` times. |
| **Monthly** | One or more days of the month, at one or more `HH:MM` times. |
| **Yearly** | One calendar date (month + day) at one `HH:MM` time. 29 February runs only in leap years. |
| **Cron expression** | Any standard 5-field cron expression. |

Under the hood all non-manual modes compile to a
[django-q2](https://django-q2.readthedocs.io/) schedule. The daily/weekly/monthly/yearly
modes build a cron expression for you; **Cron expression** mode simply lets you
type that expression directly, for schedules the guided modes can't express
(e.g. "every 15 minutes during business hours on weekdays").

---

## Cron expression mode

### Using it

1. Open a script → **Edit** → the **Schedule** panel.
2. Choose **Cron expression** as the run mode.
3. Type a standard **5-field** cron expression, for example `0 9 * * 1-5`.
4. As you type, PyRun validates the expression and shows the **next three run
   times** so you can confirm it before saving.
5. Make sure **Schedule Active** is ticked, then **Save**.

The next run time is shown on the script detail page and in the dashboard's
**Upcoming runs** list.

### Cron syntax

A cron expression has five whitespace-separated fields:

```
┌───────────── minute        (0-59)
│ ┌───────────── hour        (0-23)
│ │ ┌───────────── day of month (1-31)
│ │ │ ┌───────────── month     (1-12)
│ │ │ │ ┌───────────── day of week (0-6, 0 = Sunday)
│ │ │ │ │
* * * * *
```

Each field accepts:

- `*` — every value
- a single number — e.g. `5`
- a list — `1,15,30`
- a range — `1-5`
- a step — `*/15` (every 15), or `0-30/10` (0,10,20,30)

**Examples:**

| Expression | Meaning |
|------------|---------|
| `*/5 * * * *` | Every 5 minutes |
| `0 * * * *` | Every hour, on the hour |
| `0 9 * * 1-5` | 09:00, Monday–Friday |
| `30 8,17 * * *` | 08:30 and 17:30 every day |
| `0 0 1 * *` | Midnight on the 1st of every month |
| `0 0 * * 0` | Midnight every Sunday |
| `15 */2 * * *` | Every 2 hours, at quarter past |

Need help building one? Try [crontab.guru](https://crontab.guru) — the form
also links to it.

### What is *not* accepted

To keep behaviour predictable, PyRun intentionally rejects:

- **Named shortcuts** like `@daily`, `@hourly`, `@reboot`.
- **6-field (seconds) expressions** — use exactly five fields.
- Anything `croniter` considers invalid (bad ranges, out-of-bounds values, etc.).

You'll see the specific reason inline in the form.

### Timezone

Pick the **Timezone** next to the expression: it is written in that zone, like
the times in the daily/weekly/monthly modes. `0 9 * * 1-5` with `Asia/Tokyo`
runs at 09:00 Tokyo time on weekdays, whatever the server's clock says.

Under the hood PyRun converts the expression to UTC before handing it to
the scheduler (`0 9 * * 1-5` in Tokyo is stored as `0 0 * * 1-5`). When a time
crosses UTC midnight the day fields move with it - `30 0 * * 1-5` in Tokyo
becomes `30 15 * * 0,1,2,3,4` - and an expression with several hours can turn
into two or three scheduler entries. Daylight-saving zones are re-converted once
a day, so after a DST change a run can be off by the offset difference for at
most one day (the same as the guided modes).

Two shapes can't be shifted exactly and are rejected with a message when the
timezone isn't UTC: a `*` minute field in a zone with a fractional-hour offset
(for example `Asia/Kolkata`, +05:30), and `#` / `L` day modifiers on a time that
crosses UTC midnight. Switch the schedule to UTC for those.

The **next-run preview** in the form is computed in the selected timezone, so
what you preview is what you get.

---

## Pausing & resuming

- Untick **Schedule Active** (or use the toggle on the script detail page) to
  pause a single schedule without losing its configuration.
- Admins can pause/resume **all** schedules globally from Settings; cron
  schedules are included in the global pause/resume.

- **Archiving** a script pauses its schedule. Restoring the script does not
  resume it: tick **Schedule Active** again.

Every change is recorded in the script's **schedule history** (including the
cron expression before/after), so you can audit who changed what and when.

**Saving an Interval script restarts its interval.** An Interval schedule runs
for the first time as soon as it is created or turned on, then every N minutes.
Every **Save** of the script form re-creates the schedule, even when you only
changed the code or nothing at all, so the script runs again within about a
minute and the interval counts from then. For an hourly job, a code fix saved at
10:20 means an extra run at about 10:20 and the next ones at 11:20, 12:20, and
so on. The clock-based modes (daily, weekly, monthly, yearly, cron) are not
affected by saving.

---

## Scripts with inputs

A script can declare inputs on its **Inputs** tab. A scheduled run has nobody
to fill them in, so it gets each input's **default**. That is why a schedule
can only be turned on when every required input has a default: saving the
script form, or resuming a paused schedule, is refused with the names of the
inputs that need one. Optional inputs without a default are simply left out of
a scheduled run. Plugins get the same rule: the SDK's `ScheduleAPI.sync`
refuses to schedule such a script, and `ScriptAPI.upsert` refuses such inputs
on a script whose schedule is on (see [plugins.md](plugins.md)).

If the inputs change later in a way the schedule can't satisfy (for example a
backup restored from another instance), each scheduled time is skipped without
creating a run, and the skip is logged with the reason.

---

## When the server was down

Missed fires are not replayed one by one. If one or more scheduled times pass
while PyRun (or its worker) is stopped, the schedule runs **once**, late, at the
first scheduler check after the worker is back (within about a minute), and then
returns to its normal cadence. For example, an every-5-minutes schedule that was
down for 20 minutes runs once on return, not four times; a daily 09:00 schedule
whose server was down overnight runs once when the server comes back, at
whatever time that is.

The late run looks like any other scheduled run. If a job must not run outside
its window (a "09:00 report" that is wrong at 15:00), check the time in the
script and exit early.

---

## Backup & restore

Cron schedules — and the full weekly/monthly configuration — are included in
PyRun backups and are recreated on restore. The django-q2 schedule objects
themselves are regenerated from the stored configuration after import, so
`next_run` is recomputed on the restored instance.

---

## Scheduling from a plugin (SDK)

Plugins can schedule the scripts they provision via `ScheduleAPI.sync()` in
`core.plugins.api`:

```python
from core.plugins.api import ScheduleAPI
from core.models import ScriptSchedule

# `script` is a Script the plugin created via ScriptAPI.upsert(...)
ScheduleAPI("my-plugin").sync(
    script,
    mode=ScriptSchedule.RunMode.CRON,
    cron="0 9 * * 1-5",   # weekday mornings
)
```

An invalid `cron` value raises `ValueError` with the validation reason. The
other modes are unchanged:

```python
# interval
ScheduleAPI("my-plugin").sync(script, mode=ScriptSchedule.RunMode.INTERVAL, interval_minutes=60)
# daily
ScheduleAPI("my-plugin").sync(script, mode=ScriptSchedule.RunMode.DAILY, time_str="09:00")
```

---

## How it works (internals)

| Piece | Where |
|-------|-------|
| `ScriptSchedule.run_mode` + `cron_expression` field | `core/models/schedule.py` |
| Validation & next-run preview helpers | `ScheduleService.validate_cron_expression()` / `preview_cron_runs()` in `core/services/schedule_service.py` |
| django-q2 schedule creation | `ScheduleService._create_cron_schedule()` |
| Form field + validation | `ScheduleForm` in `core/forms.py` |
| Live preview endpoint | `cron_preview_view` → `cpanel:cron_preview` (`/cpanel/scripts/cron/preview/`) |
| Form UI + preview JS | `templates/cpanel/scripts/_form_sidebar.html`, `static/js/script_form.js` |
| Migration | `core/migrations/0039_scriptschedule_cron.py` |
| Tests | `core/test_cron_scheduling.py` |

`croniter` (already a dependency, used by django-q2) handles cron parsing and
next-run computation. When a schedule is saved, `ScheduleService.sync_schedule()`
deletes the script's old django-q2 schedules and creates a single `CRON`-type
schedule whose `cron` field is the raw expression. The django-q2 worker then
fires `core.tasks.execute_scheduled_run` at each matching time.
