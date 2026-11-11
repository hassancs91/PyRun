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

In the daily, weekly and monthly modes you type the times separated by commas
(`09:00, 18:00`). Each time can be listed once: `09:00, 9:00` is refused with
"09:00 is listed twice.", since both mean 09:00. A schedule saved with a
repeated time before PyRun refused one is cleaned up when you upgrade to 2.5:
the repeat is dropped from its times (in every mode's list, the first entry
kept) and so is the extra cron it made, so the script runs once at that time,
whatever the time zone. A repeat that comes back later (a restored older
backup) still makes one cron per distinct time, and only the chosen mode's
times are checked for a repeat when you save: one left in the times of a mode
the schedule no longer uses is dropped.

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
- For a pause you can plan (an upgrade tonight, a database backup every
  Sunday), use a [maintenance window](#maintenance-windows) instead.

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

## When the script is still running

A schedule can fire while the script's previous run is still going: a slow
run, an every-minute schedule, or a Run now that happened to overlap. The
script's **If it's already running** setting (the script editor's Execution
box) decides what happens. It applies to every way a script starts (Run now,
Re-run, schedules, webhooks, chat channels and plugins), not just schedules.

| Setting | What a new trigger does while a run is in flight |
|---|---|
| **Allow** (default) | Starts another run alongside it. This is how every script behaved before PyRun 2.5. |
| **Skip** | Creates the run and marks it cancelled at once, with a **skipped: already running** chip on the runs list and the run page. Nothing is executed and no notification is sent. A webhook call still gets a 200, with `"status": "skipped"`, so the sender doesn't retry. A chat message gets a reply saying it was skipped. |
| **Queue** | Keeps the run pending with a **waiting its turn** chip, and starts it when the run ahead of it ends. Queued runs start one at a time, in the order they came in, each with the inputs, webhook body or chat message it arrived with. |

"In flight" means running, or pending and about to run (queued for a worker,
or waiting its turn). Finished runs never block.

A queued run normally starts within a second or two of the run ahead of it
ending: when that run finishes, is stopped, or is marked failed because its
worker died. The worker's once-a-minute heartbeat also checks for queued runs
whose turn has come, so nothing waits longer than about a minute after the
script is free. A queued run doesn't count as lost while it waits, however
long the runs ahead of it take. To drop a queued run, open it and press
**Cancel**.

A pending run that never got queued for a worker, for example because the
process creating it crashed, stops blocking the script after five minutes.

---

## When a run fails: retries

A script can try again on its own when a run fails. The settings are in the
script editor's Execution box, and like the concurrency setting they apply to
every way a script starts, not just schedules:

| Setting | Default | What it does |
|---|---|---|
| **Retries on failure** | 0 (never) | How many more times a failed run is tried, up to 10. |
| **Delay before a retry** | 60 seconds | How long the first retry waits, up to a day. |
| **Backoff multiplier** | 1 | Each retry waits this many times longer than the one before. With a 60-second delay, 1 gives 60 s, 60 s, 60 s; 2 gives 60 s, 120 s, 240 s. A retry never waits more than a day. |
| **Retry timeouts too** | off | Whether a run that hit its timeout is retried as well. |

A run counts as failed when the script exits with an error, or PyRun couldn't
start it (a missing environment, a secret that couldn't be resolved). When it
fails, PyRun creates the next attempt right away as a pending run with a
**retry at HH:MM** chip. It has the same inputs, and for a webhook or chat
trigger the same request data (`WEBHOOK_*`, `INBOUND_*`, the webhook body file,
`pyrunner_run.event()`).
It runs the script's code as it is when the retry starts, so a fix you save
in the meantime is picked up.

**Timing.** The worker checks for due retries once a minute, on its
heartbeat. A retry therefore starts up to about a minute after its delay ends,
and delays under a minute act like about a minute.

**Timeouts and the stop grace period.** A run that hits its timeout first
gets SIGTERM and the script's **Stop grace period** (10 seconds by default) to
shut down, then it is killed: the same graceful stop as the Stop button (see
[stopping a run](run-context.md#stopping-a-run)). A run can therefore take its
timeout plus the grace period before its process is gone.

**What never retries:** a successful run, a run you stopped, a run the
concurrency policy skipped, a run the platform marked failed because its
worker died, and a timeout unless **Retry timeouts too** is on. When the
retry is due PyRun checks again: if the script has been disabled or archived,
or its retries were turned down below this attempt, the retry is cancelled
without running.

**Concurrency.** A retry obeys the script's **If it's already running**
setting when it starts, lined up behind any run that was triggered while it
waited: with **Skip** it is skipped if another run is going, with **Queue** it
waits its turn. A retry that is still waiting for its delay doesn't count as
running, so it never holds up other runs.

**Notifications** go out once, for the outcome: the first success, or the
last failed attempt. A failed attempt with a retry after it sends nothing. If
the retry then can't start (skipped, cancelled because the script was
disabled, or its inputs no longer fit the script), the failed attempt before
it sends its notification at that point. Cancelling a retry yourself sends
nothing.

**On the run page** a retry shows **attempt 2 of 3 · retry of #…** with a link
to the first attempt, and the failed attempt links the retry that followed it.
To drop a retry that is still waiting, open it and press **Cancel**. Scripts
can read their attempt number from `run.json` (`run.attempt`, see
[run context](run-context.md)).

---

## Maintenance windows

A maintenance window is a planned pause: while it is on, the kinds of trigger
it blocks don't start runs. Instance admins set **global** windows (they apply
to every workspace) under **Settings → Schedules → Manage maintenance
windows**; a workspace's Owners and Admins set that workspace's own windows
from **Workspaces → Maintenance**, where the global ones are listed too.

A window is either:

- **One-off**: from one moment to another, for example 1 December 02:00 to
  04:30;
- **Weekly**: from one time of day to another on the days you tick, for
  example every Sunday 02:00 to 04:00. When the end time is at or before the
  start time the window ends the next day, so 23:00 to 01:00 crosses
  midnight and 00:00 to 00:00 is the whole day.

The times are in the window's own time zone, and they stay put across
daylight saving changes: a Sunday 01:00 to 05:00 window in New York is three
hours long on the day the clocks go forward and five on the day they go
back. The window's page shows its next occurrence.

Pick what it blocks (scheduled runs, webhook calls, chat messages and
watchers by default):

| Trigger | Inside the window |
|---|---|
| Scheduled runs | skipped; the run is not made up afterwards |
| Webhook calls | `503` with a `Retry-After` header (seconds until the window ends) and `{"status": "maintenance", "until": ..., "run_id": ...}`; a sender that retries delivers it again after the window |
| Chat messages | the bot replies that the script isn't started, and until when |
| Watchers | not checked; new items start runs at the first check after the window ([Watchers](#watchers)) |
| Manual runs (optional) | Run now, Re-run and plugin buttons show the same message |
| API runs (optional) | not started: any run with trigger type API that isn't a webhook call or a chat message |

Every refused trigger is still listed: the run is `cancelled` with a
**skipped: maintenance window** chip, and its output names the window and
when it ends. Such a run sends no notification and is never retried. A retry
that comes due inside a window counts as the trigger of its first attempt;
when that is blocked the retry is dropped and the failed attempt before it
sends its notification.

Runs that are already going when a window starts are never stopped, and a
run waiting its turn under the [Queue policy](#when-the-script-is-still-running)
still starts when the run ahead of it ends. While a window is on, the
dashboard shows **Maintenance until ...**. Disabled windows block nothing.
**Pause all** under Settings → Schedules stays the instant switch for
schedules.

Maintenance windows are included in backups. Restoring a backup made before
they existed leaves the instance's windows as they are.

## Watchers

A watcher starts a script when something new shows up somewhere else: an
entry in a JSON list behind a URL, an item in an RSS or Atom feed. It checks
the source on an interval and starts **one run per new item**, with the item
in the run's [event](run-context.md#the-run-event) (kind `watcher`).

Add one from the script's **Triggers** page (the script page links it under
Schedule): pick what to watch, fill in its settings, and press **Test the
source** to see what it finds without saving anything. The workspace's
watchers are all listed under **Triggers** in the sidebar; a watcher's page
has **Check now** and an event log of the last 50 events, each linking to its
run.

| Source | Settings | What counts as new |
|---|---|---|
| New entry in a JSON list (HTTP) | URL, list path (dotted, empty = the whole response), id path, newest first, optional since parameter | an entry whose id it hasn't seen |
| New item in an RSS or Atom feed | feed URL | an item whose id (its `guid` or Atom `id`, else its link) it hasn't seen |

The URL must answer without signing in. For a source that needs a key, a
schedule plus `pyrunner_integrations.http` and a Connection is the way for now.

**The first check only marks where the source is**: what is already there
never starts runs. After that, each check starts a run per new item, oldest
first. **Script inputs from the item** maps the script's declared inputs to
paths in the event (`{"title": "item.title"}`); without it, read
`pyrunner_run.event()["item"]` in the script.

**Exactly once.** A watcher remembers the ids of the last 5,000 items that
started runs, and saves where the source is only after every run of a check
is queued. An item reported twice, a check cut short or a worker that dies
mid-check never starts a second run for the same item.

**Limits and failures.**

- **Interval**: at least 60 seconds (default 5 minutes). The checks run on
  their own one-minute task, apart from the schedules.
- **Most runs an hour** (default 60): new items past it wait for the next
  hour; none are lost.
- A source that doesn't answer within 20 seconds counts as a failed check.
- After a failed check the watcher waits twice as long each time (the
  interval doubled per failure, up to an hour). After **10 failures in a row**
  it turns itself off and its creator gets an email. Fix the cause, then turn
  it on again: that clears the failures, and the next check is within a minute.
- Changing what a watcher watches (its source, settings or connection) starts
  it over: the next check only marks.
- Each run goes through the script's
  [concurrency policy](#when-the-script-is-still-running) and retries like any
  other run. A disabled or archived script's watchers aren't checked.
- A [maintenance window](#maintenance-windows) that blocks watcher runs (new
  windows do by default) skips the checks while it is on, without losing
  anything: new items start runs at the first check after it. **Pause all**
  stops the watchers too.

Watchers are included in backups with where each source was, so a restored
watcher doesn't start runs for items it already handled. One whose connection
didn't come back is restored turned off. API and MCP callers list watchers
with `watchers.list` (never their settings, which can hold a key in a URL).

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
`core.plugins.api`. It takes the same choices as the schedule form in the
script editor and saves the same schedule, so a plugin can do anything the
form can, including several times a day:

```python
from core.plugins.api import ScheduleAPI

api = ScheduleAPI("my-plugin")
# `script` is a Script the plugin created via ScriptAPI.upsert(...)

# interval
api.sync(script, mode="interval", interval_minutes=60)
# daily, 9am and 6pm in one call
api.sync(script, mode="daily", times=["09:00", "18:00"], tz="Europe/Berlin")
# weekly: Monday, Wednesday and Friday (0 = Monday ... 6 = Sunday)
api.sync(script, mode="weekly", weekdays=[0, 2, 4], times=["07:30"])
# monthly: the 1st and the 15th, morning and evening
api.sync(script, mode="monthly", month_days=[1, 15], times=["08:00", "20:00"])
# yearly: one date, one time
api.sync(script, mode="yearly", month=6, day=15, times=["02:00"])
# cron: any 5-field expression, written in tz
api.sync(script, mode="cron", cron="0 9 * * 1-5", tz="Asia/Tokyo")
# off
api.sync(script, mode="manual")
```

Times are `HH:MM`, 24-hour, in `tz` (default `UTC`), and are stored as
`HH:MM` (`"9:05"` becomes `"09:05"`) in the order you give them. Days are
stored sorted. A month day the month doesn't have (31 in April) is skipped
that month, as in the form.

The single-value arguments from before still work: `time_str="09:00"` is
`times=["09:00"]` and `weekday=0` is `weekdays=[0]`. Passing both forms of one
of them is an error.

`sync` raises `ValueError`, and saves nothing, when:

- the mode is unknown, or a mode's required argument is missing;
- a list is empty, isn't a list or tuple, repeats a value, or holds a time or
  day that isn't valid (the message names it, e.g. `times[1] '25:00' is not a
  valid time`);
- a list doesn't apply to the mode (`month_days` with `mode="weekly"`, or any
  list with `mode="manual"`);
- yearly mode gets more than one time;
- the `cron` expression is invalid (the message gives the reason);
- the script has a required input with no default (see
  [Scripts with inputs](#scripts-with-inputs)).

An existing schedule is left as it was when a call is refused.

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
