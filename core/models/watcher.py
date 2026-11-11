"""
Watchers: polling triggers (2.5 Stage 7, ticket T31).

A watcher asks an outside source what is new every ``interval_seconds``
through an adapter (``core.triggers``: ``http_poll``, ``rss_new_item``, the
integrations' adapters later) and starts one run of its script per new item,
with the item in the run's event (kind ``watcher``). The minute task
``core.tasks.watchers_tick_task`` does the checking (``core.triggers.runner``).

``targets`` is a list of script ids so that one watcher starting several
scripts can be added later without a schema change; in this version it holds
exactly one id, always the same as ``script`` (the FK gives the cascade on
script delete and cheap per-script queries on both engines).

``state`` is ``{"cursor": <the adapter's own state>, "seen": [<event ids>]}``:
the cursor says where the source was when last checked (None until the first
check, which reports nothing and only marks the place); ``seen`` holds the ids
of the last ``SEEN_MAX`` events that became runs, so an event reported twice
never starts a second run.
"""

import uuid

from django.conf import settings
from django.db import models


class Watcher(models.Model):
    INTERVAL_MIN_SECONDS = 60
    INTERVAL_MAX_SECONDS = 7 * 24 * 3600
    DEFAULT_INTERVAL_SECONDS = 300
    DEFAULT_FIRE_CAP = 60
    FIRE_CAP_MAX = 3600
    # Consecutive failed checks before the watcher turns itself off.
    AUTO_DISABLE_AFTER = 10
    # Failed checks back off: the interval doubles per failure, up to this.
    BACKOFF_MAX_SECONDS = 3600
    # Event ids remembered for de-duplication (state["seen"]).
    SEEN_MAX = 5000
    # Events listed in the watcher's event log (recent_events).
    EVENT_LOG_MAX = 50

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # CASCADE like every other workspace-owned row that names a script.
    workspace = models.ForeignKey(
        "core.Workspace", on_delete=models.CASCADE, related_name="watchers",
    )
    name = models.CharField(max_length=120)
    # The script it starts (v1: exactly one). ``targets`` mirrors it.
    script = models.ForeignKey(
        "core.Script", on_delete=models.CASCADE, related_name="watchers",
    )
    targets = models.JSONField(default=list, blank=True, help_text="Script ids this watcher starts (one in this version)")
    adapter = models.CharField(max_length=80, help_text="Adapter key, e.g. http_poll")
    connection = models.ForeignKey(
        "core.Connection", on_delete=models.SET_NULL, null=True, blank=True, related_name="watchers",
        help_text="The connection the adapter checks as (adapters that need one)",
    )
    config = models.JSONField(default=dict, blank=True, help_text="Adapter settings (its config_schema)")
    input_map = models.JSONField(
        default=dict, blank=True,
        help_text='Script inputs from the event: {"input": "item.path"}',
    )
    interval_seconds = models.PositiveIntegerField(default=DEFAULT_INTERVAL_SECONDS)
    fire_cap_per_hour = models.PositiveIntegerField(default=DEFAULT_FIRE_CAP)
    enabled = models.BooleanField(default=True)

    state = models.JSONField(null=True, blank=True)
    recent_events = models.JSONField(default=list, blank=True)

    # Scheduling and health. ``next_check_at`` NULL = due now.
    next_check_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_event_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    error_count = models.PositiveIntegerField(default=0)
    auto_disabled_at = models.DateTimeField(null=True, blank=True)
    # The fire cap's current hour: when it began and how many runs it started.
    cap_window_start = models.DateTimeField(null=True, blank=True)
    cap_window_count = models.PositiveIntegerField(default=0)
    # Set while a check runs (the claim); an old value is an abandoned claim.
    checking_since = models.DateTimeField(null=True, blank=True)

    # Plugin ownership, as on Script (a slug string, not an FK).
    owner_plugin = models.CharField(max_length=100, null=True, blank=True, db_index=True)
    owner_key = models.CharField(max_length=100, null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="watchers_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "created_at"]
        indexes = [models.Index(fields=["enabled", "next_check_at"], name="core_watche_due_idx")]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.script_id:
            self.targets = [str(self.script_id)]
            fields = kwargs.get("update_fields")
            if fields is not None and "script" in fields and "targets" not in fields:
                kwargs["update_fields"] = [*fields, "targets"]
        super().save(*args, **kwargs)

    @property
    def is_auto_disabled(self) -> bool:
        return not self.enabled and self.auto_disabled_at is not None

    @property
    def cursor(self):
        return (self.state or {}).get("cursor")

    @property
    def seen(self) -> list:
        return list((self.state or {}).get("seen") or [])
