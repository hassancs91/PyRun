"""
Maintenance windows: planned periods when chosen kinds of triggers don't start
runs (2.5 Stage 1, ticket T07).

A window is either one-off (a start and an end moment) or weekly (a time of day
on chosen weekdays, in its time zone). While it is on, the run funnel
(``core.tasks.queue_script_run``, through ``core.services.maintenance.admit``)
marks every new run of a blocked trigger class CANCELLED with
``cancel_reason = maintenance``. Runs already going are never touched.

A window with no workspace is global: it applies to every workspace.
"""

import datetime
import uuid
from zoneinfo import ZoneInfo

from django.db import models
from django.utils import timezone


def _default_blocks():
    return list(MaintenanceWindow.DEFAULT_BLOCKS)


def describe_blocks(blocks) -> str:
    """Trigger classes in words, for a sentence: ``scheduled runs and webhook calls``."""
    names = [
        MaintenanceWindow.BLOCK_PHRASES[b]
        for b in MaintenanceWindow.Block.values
        if b in (blocks or ())
    ]
    if not names:
        return "nothing"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


class MaintenanceWindow(models.Model):
    class Kind(models.TextChoices):
        ONE_OFF = "one_off", "One-off"
        WEEKLY = "weekly", "Weekly"

    # The trigger classes a window can block. Every run belongs to one
    # (core.services.maintenance.trigger_class reads it from the run's event).
    class Block(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled runs"
        WEBHOOK = "webhook", "Webhook calls"
        CHANNEL = "channel", "Chat messages"
        MANUAL = "manual", "Manual runs"
        API = "api", "API runs"

    DEFAULT_BLOCKS = [Block.SCHEDULED, Block.WEBHOOK, Block.CHANNEL]
    # Each class mid-sentence ("blocks scheduled runs until ...").
    BLOCK_PHRASES = {
        Block.SCHEDULED: "scheduled runs",
        Block.WEBHOOK: "webhook calls",
        Block.CHANNEL: "chat messages",
        Block.MANUAL: "manual runs",
        Block.API: "API runs",
    }
    DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # NULL = global. CASCADE, not SET_NULL: a workspace's window must never
    # turn into a global one when its workspace goes.
    workspace = models.ForeignKey(
        "core.Workspace",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="maintenance_windows",
        help_text="The workspace this window applies to; empty = every workspace.",
    )
    name = models.CharField(max_length=100)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.WEEKLY)

    # One-off: the two moments (stored UTC, entered in ``timezone``).
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)

    # Weekly: a time of day on each of ``weekdays`` (0 = Monday), in
    # ``timezone``. An end at or before the start means the next day, so
    # 23:00-01:00 crosses midnight and equal times are a whole day.
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    weekdays = models.JSONField(
        default=list,
        blank=True,
        help_text="Days the window starts on, 0 = Monday ... 6 = Sunday",
    )

    timezone = models.CharField(max_length=50, default="UTC")
    enabled = models.BooleanField(default=True)
    blocks = models.JSONField(
        default=_default_blocks,
        help_text="Trigger classes blocked while the window is on",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "created_at"]

    def __str__(self):
        return self.name

    @property
    def tzinfo(self):
        try:
            return ZoneInfo(self.timezone or "UTC")
        except (ValueError, LookupError):
            # The form only offers known zones, and a restore maps an unknown
            # one to UTC with a warning, so this is a last resort: UTC rather
            # than a window that never matches.
            return datetime.timezone.utc

    def blocks_class(self, trigger_class: str) -> bool:
        return trigger_class in (self.blocks or [])

    def _weekly_occurrence(self, day: datetime.date):
        """The occurrence starting on local date ``day``, as aware UTC datetimes."""
        if self.start_time is None or self.end_time is None or day.weekday() not in self.weekdays:
            return None
        tz = self.tzinfo
        end_day = day if self.end_time > self.start_time else day + datetime.timedelta(days=1)
        start = datetime.datetime.combine(day, self.start_time, tzinfo=tz)
        end = datetime.datetime.combine(end_day, self.end_time, tzinfo=tz)
        start, end = start.astimezone(datetime.timezone.utc), end.astimezone(datetime.timezone.utc)
        return (start, end) if end > start else None

    def occurrence_at(self, when=None):
        """The (start, end) occurrence that covers ``when``, or None.

        Ignores ``enabled``: the caller decides whether a disabled window
        counts (enforcement doesn't, the preview shows when it would run).
        """
        when = when or timezone.now()
        if self.kind == self.Kind.ONE_OFF:
            if self.starts_at and self.ends_at and self.starts_at <= when < self.ends_at:
                return (self.starts_at, self.ends_at)
            return None
        today = when.astimezone(self.tzinfo).date()
        # An occurrence is at most a day long (25 hours across a DST change),
        # so the one covering ``when`` started today or yesterday, local time.
        for day in (today - datetime.timedelta(days=1), today):
            occ = self._weekly_occurrence(day)
            if occ and occ[0] <= when < occ[1]:
                return occ
        return None

    def next_occurrence(self, when=None):
        """The occurrence going on at ``when``, else the next one; None if none."""
        when = when or timezone.now()
        current = self.occurrence_at(when)
        if current:
            return current
        if self.kind == self.Kind.ONE_OFF:
            if self.starts_at and self.ends_at and self.starts_at > when:
                return (self.starts_at, self.ends_at)
            return None
        today = when.astimezone(self.tzinfo).date()
        for offset in range(8):
            occ = self._weekly_occurrence(today + datetime.timedelta(days=offset))
            if occ and occ[0] > when:
                return occ
        return None

    def format_local(self, moment) -> str:
        """``moment`` in the window's own time zone, e.g. ``Sat 9 Nov 2026, 02:00``."""
        local = moment.astimezone(self.tzinfo)
        return f"{local:%a} {local.day} {local:%b %Y, %H:%M}"

    @property
    def schedule_display(self) -> str:
        """When the window is on, in words."""
        if self.kind == self.Kind.ONE_OFF:
            if not (self.starts_at and self.ends_at):
                return "Once (no dates set)"
            return f"Once: {self.format_local(self.starts_at)} to {self.format_local(self.ends_at)} ({self.timezone})"
        days = ", ".join(self.DAY_NAMES[d] for d in sorted(self.weekdays)) or "no days set"
        start = self.start_time.strftime("%H:%M") if self.start_time else "--:--"
        end = self.end_time.strftime("%H:%M") if self.end_time else "--:--"
        next_day = " (next day)" if self.start_time and self.end_time and self.end_time <= self.start_time else ""
        return f"Weekly on {days}, {start} to {end}{next_day} ({self.timezone})"

    @property
    def blocks_display(self) -> str:
        """The blocked trigger classes in words (``describe_blocks``)."""
        return describe_blocks(self.blocks)
