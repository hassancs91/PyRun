"""Drop repeated times from stored schedules, and the extra crons they made.

Before 2.5 the console schedule form accepted a time listed twice ("09:00,
9:00"), saved both, and the scheduler made one django-q2 cron per entry, so
the script ran twice at that time. The form refuses a repeat now and the
scheduler builds one cron per distinct time, but a row saved before keeps its
repeat, and its old crons stay until the schedule is synced again: the daily
resync rebuilds non-UTC zones only, so a UTC schedule kept running twice.

This migration keeps the first entry of each repeated time in the daily,
weekly and monthly times lists (every mode's list, so a repeat in a mode the
schedule doesn't use no longer blocks a save), and among the schedule's own
django-q2 crons deletes the ones identical to a cron it keeps. A repeated
time made byte-identical crons (same function, arguments and expression), so
what is left is exactly what a sync from the cleaned row builds, in any zone.
Rows without a repeat are not touched; running it twice changes nothing.
"""

from django.db import migrations

TIMES_FIELDS = ("daily_times", "weekly_times", "monthly_times")


def _distinct(times):
    """``times`` with each wall-clock time once (first spelling kept).

    The rule of ``ScheduleService._distinct_times``, written out here because a
    migration must not import code that keeps changing after it.

    Returns None when the list has no repeat or holds something that isn't
    an "HH:MM" string; such a row is left as it is.
    """
    if not isinstance(times, list):
        return None
    seen = set()
    kept = []
    for value in times:
        try:
            hour, minute = (int(part) for part in str(value).split(":"))
        except ValueError:
            return None
        if (hour, minute) in seen:
            continue
        seen.add((hour, minute))
        kept.append(value)
    return kept if len(kept) != len(times) else None


def drop_repeated_times(apps, schema_editor):
    ScriptSchedule = apps.get_model("core", "ScriptSchedule")
    QSchedule = apps.get_model("django_q", "Schedule")

    for row in ScriptSchedule.objects.only("id", "q_schedule_ids", *TIMES_FIELDS).iterator():
        changes = {}
        for field in TIMES_FIELDS:
            kept = _distinct(getattr(row, field))
            if kept is not None:
                changes[field] = kept
        if not changes:
            continue

        ids = [i for i in (row.q_schedule_ids or []) if isinstance(i, int)]
        crons = {
            q.id: (q.func, q.args, q.schedule_type, q.cron, q.minutes)
            for q in QSchedule.objects.filter(id__in=ids)
        }
        kept_ids, seen, extra = [], set(), []
        for q_id in row.q_schedule_ids or []:
            key = crons.get(q_id)
            if key is not None and key in seen:
                extra.append(q_id)
                continue
            if key is not None:
                seen.add(key)
            kept_ids.append(q_id)
        if extra:
            QSchedule.objects.filter(id__in=extra).delete()
            changes["q_schedule_ids"] = kept_ids

        ScriptSchedule.objects.filter(pk=row.pk).update(**changes)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0085_connection_owner_unique"),
        ("django_q", "0012_auto_20200702_1608"),
    ]

    operations = [
        migrations.RunPython(drop_repeated_times, migrations.RunPython.noop),
    ]
