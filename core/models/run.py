"""
Run model for tracking script execution history.
"""

import datetime
import logging
import uuid

from django.conf import settings
from django.db import models

from .fields import OrderedJSONField
from .script import Script
from .workspace import WorkspaceScopedManager


logger = logging.getLogger(__name__)


class Run(models.Model):
    """
    Represents a single execution of a script.
    Tracks timing, output, and status of each run.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"
        TIMEOUT = "timeout", "Timeout"
        CANCELLED = "cancelled", "Cancelled"

    # The statuses a run ends in (``is_finished``).
    FINISHED_STATUSES = (Status.SUCCESS, Status.FAILED, Status.TIMEOUT, Status.CANCELLED)

    # What a run's stderr says when PyRun itself failed, not the script. Anyone
    # who can see the run reads it, so the exception and traceback go to the
    # server log only (UI shows no internals).
    INTERNAL_ERROR_TEXT = (
        "This run failed because of an internal PyRun error, not your script. "
        "An administrator can find the details in the server log."
    )
    QUEUE_FAILED_TEXT = "Failed to queue task. The server log has the details."

    class TriggerType(models.TextChoices):
        MANUAL = "manual", "Manual"
        SCHEDULED = "scheduled", "Scheduled"
        API = "api", "API"
        WATCHER = "watcher", "Watcher"

    # Why a CANCELLED run was cancelled, when the platform did it rather than
    # someone pressing Stop. Blank = an ordinary cancel.
    class CancelReason(models.TextChoices):
        CONCURRENCY_SKIP = "concurrency_skip", "Skipped: already running"
        MAINTENANCE = "maintenance", "Skipped: maintenance window"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    script = models.ForeignKey(
        Script,
        on_delete=models.CASCADE,
        related_name="runs",
    )

    # Tenancy: nullable + backfilled to the default workspace for upgrade-safety;
    # queries scope through WorkspaceScopedManager (.for_workspace).
    workspace = models.ForeignKey(
        "core.Workspace",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_index=True,
        related_name="runs",
        help_text="Workspace this run belongs to (tenancy seam; nullable).",
    )

    objects = WorkspaceScopedManager()

    # Execution status
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    exit_code = models.IntegerField(
        null=True,
        blank=True,
        help_text="Process exit code (0 = success)",
    )

    # Output capture
    stdout = models.TextField(
        blank=True,
        help_text="Standard output from script execution",
    )
    stderr = models.TextField(
        blank=True,
        help_text="Standard error from script execution",
    )

    # Timing
    started_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When execution started",
    )
    ended_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When execution ended",
    )

    # Snapshot of script code at execution time (for audit trail)
    code_snapshot = models.TextField(
        blank=True,
        help_text="Copy of script code at time of execution",
    )

    # Which Library revision each attached library was pinned to, stamped at
    # QUEUE time: {library_key: version}. code_snapshot's counterpart for shared
    # code — the executor materializes these exact revisions, so editing a
    # library after queueing cannot change what an already-queued run imports.
    # A tiny int map rather than a content copy: storage scales with edits, not
    # runs. NULL (old runs, scripts with no libraries) => nothing to materialize.
    library_versions = models.JSONField(
        null=True,
        blank=True,
        default=None,
        help_text="Library revisions pinned at queue time: {library_key: version}.",
    )

    # The inputs this run started with, validated against the script's
    # inputs_schema (defaults filled in) and stamped at QUEUE time by
    # queue_script_run, so a duplicate task delivery reads the same values.
    # Stored raw, because the script needs the real values: anything that
    # displays them masks secrets first. NULL = the script declared no inputs.
    # inputs, result and event are OrderedJSONFields: their keys come back in
    # the order they were written, on PostgreSQL too.
    inputs = OrderedJSONField(
        null=True,
        blank=True,
        default=None,
        help_text="Inputs stamped at queue time (validated against the script's inputs_schema).",
    )

    # What the script reported through pyrunner_run while it ran (2.5, T02).
    # Both are written only by the internal loopback endpoints, only while the
    # run is RUNNING, and with secret values already masked, so every reader
    # (run page, RunView, notifications, backups) gets the masked copy.
    # ``result``: the JSON object the script passed to set_result(), capped at
    # core.services.run_reports.RESULT_MAX_BYTES. ``progress``: the latest
    # {"pct", "message", "updated_at"}. NULL = the script never set one.
    result = OrderedJSONField(
        null=True,
        blank=True,
        default=None,
        help_text="JSON object the script set with pyrunner_run.set_result() (secrets masked).",
    )
    progress = models.JSONField(
        null=True,
        blank=True,
        default=None,
        help_text="Latest pyrunner_run.progress() update: {pct, message, updated_at}.",
    )

    # What started the run (2.5, T03): one envelope for every trigger,
    # {"kind", "received_at", ...kind-specific keys}, built by
    # core.services.run_events and capped there at EVENT_MAX_BYTES. Set when
    # the run is created (queue_script_run fills in a minimal one for any
    # caller that didn't). Stored raw, like inputs, because the script reads
    # it (pyrunner_run.event()): the run page masks secrets before showing it.
    # NULL = a run from before 2.5.
    event = OrderedJSONField(
        null=True,
        blank=True,
        default=None,
        help_text="What started the run: {kind, received_at, ...} (see core.services.run_events).",
    )

    # Concurrency policy (2.5, T05; core.services.run_concurrency).
    # ``cancel_reason``: set with CANCELLED when the platform cancelled the run
    # (a Skip-policy script that was already running).
    # ``queued_behind``: on a Queue-policy script, the run this one waited
    # behind. Set while the row is held (PENDING, no task yet) and kept after
    # it starts, as history. A plain id, not a foreign key: deleting the run
    # ahead must not clear the marker that says this one is waiting.
    # ``queued_payload``: the task's request data (webhook body, chat message)
    # as JSON text while the run is held, cleared when it starts. Text, not
    # JSON, so the WEBHOOK_* values come out byte for byte (PostgreSQL's jsonb
    # would reorder keys).
    cancel_reason = models.CharField(
        max_length=32,
        choices=CancelReason.choices,
        blank=True,
        default="",
        help_text="Why the platform cancelled the run (blank = an ordinary cancel).",
    )
    queued_behind = models.UUIDField(
        null=True,
        blank=True,
        default=None,
        help_text="The run this one waited behind (Queue concurrency policy).",
    )
    queued_payload = models.TextField(
        blank=True,
        default="",
        help_text="Request data for a run waiting its turn (cleared when it starts).",
    )

    # Retry on failure (2.5, T06; core.services.run_retries).
    # ``attempt``: 1 for a run a trigger started, n for its (n - 1)-th retry.
    # ``retry_of``: on a retry, the first attempt (the run the trigger started).
    # ``next_attempt_at``: on a retry waiting for its delay, when it is due.
    # The worker heartbeat starts it then: it clears this, resets created_at
    # to that moment (so the concurrency policy lines it up behind any run
    # triggered while it waited) and queues it like any other run. A waiting
    # retry keeps its webhook or chat data in ``queued_payload`` meanwhile.
    attempt = models.PositiveSmallIntegerField(
        default=1,
        help_text="1 for a run a trigger started, 2 for its first retry, and so on.",
    )
    retry_of = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="retries",
        help_text="On a retry: the first attempt, the run the trigger started.",
    )
    next_attempt_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="On a retry waiting for its delay: when it starts.",
    )

    # Graceful stop (2.5, T08). Set when someone stops a RUNNING run (Stop
    # button, SDK). The run stays RUNNING while its script shuts down; the
    # worker kills it once the script's stop_grace_seconds are up and records
    # the run as CANCELLED when the process has ended.
    stop_requested_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When a stop was requested for this run.",
    )

    # Webhook v2 (2.5, T20): the HTTP status the webhook call that created
    # this run was answered with (200 queued or skipped, 503 maintenance, 500
    # failed to queue; a sync call (T22): its reply's status, 202 when the
    # wait ran out, 409 skipped). NULL for other triggers, for retries (no
    # call made them) and for webhook runs from before 2.5. The script page's
    # Recent deliveries shows it.
    response_code = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        default=None,
        help_text="HTTP status the webhook call that created this run got (webhook runs only).",
    )

    # Who triggered the run
    triggered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="triggered_runs",
    )

    # django-q2 task tracking
    task_id = models.CharField(
        max_length=100,
        blank=True,
        db_index=True,
        help_text="django-q2 task ID for tracking async execution",
    )

    # OS process tracking (for force-kill). Set while the script subprocess is
    # alive, cleared on completion. Used to kill the job's process tree without
    # touching the django-q worker that spawned it.
    pid = models.IntegerField(
        null=True,
        blank=True,
        help_text="OS PID of the running script subprocess (for kill).",
    )

    # How this run was triggered
    trigger_type = models.CharField(
        max_length=20,
        choices=TriggerType.choices,
        default=TriggerType.MANUAL,
        help_text="How this run was triggered",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "runs"
        verbose_name = "run"
        verbose_name_plural = "runs"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["script", "-created_at"]),
            models.Index(fields=["status", "-created_at"]),
        ]

    def __str__(self):
        return f"Run {self.id} - {self.script.name} ({self.status})"

    def save(self, *args, **kwargs):
        # A run always belongs to its script's workspace. Code that creates a
        # Run without one (a plugin using the models directly) would otherwise
        # execute the script unscoped: the default workspace's data stores and
        # every workspace's secrets (F82).
        if self.workspace_id is None and self.script_id is not None:
            self.workspace_id = (
                Script.objects.filter(pk=self.script_id)
                .values_list("workspace_id", flat=True)
                .first()
            )
        super().save(*args, **kwargs)

    # Grace beyond a run's own timeout before a still-'running' row is declared
    # dead. A live worker HARD-kills the subprocess at timeout_seconds and
    # finalizes within seconds (the per-task cushion is +60s), so five minutes
    # past the deadline nothing legitimate can still be executing.
    RECONCILE_GRACE_SECONDS = 300
    # How old a PENDING run must be before it counts as lost — but only while
    # workers are demonstrably alive: a live cluster re-delivers unacknowledged
    # tasks within its `retry` window, so a day without pickup means the queued
    # task no longer exists (e.g. a worker was killed between queue and ack).
    RECONCILE_PENDING_AFTER = datetime.timedelta(hours=24)

    @classmethod
    def reconcile_stale(cls) -> int:
        """Fail runs whose worker died without finalizing them.

        A worker killed while busy (container stop SIGKILLs workers by design —
        see entrypoint.sh — plus OOM kills and crashes) leaves its Run at
        'running' forever: stdout is never captured, force-stop has no live pid,
        and anything gating on "a run is in flight" (plugin concurrency guards,
        queue checks) stays blocked. Nothing in-process can catch that, so this
        reconciler runs out-of-band: every minute on the worker heartbeat and on
        the runs list page. It is the Run counterpart of
        ``PackageOperation.reconcile_stale``.

        Status-only by design: it never kills OS processes. A detached script
        may genuinely still be running, but its recorded pid may also have been
        recycled to an innocent process — reporting honestly beats killing
        blind.

        Returns the number of runs reconciled.
        """
        from django.utils import timezone

        from core.models.settings import GlobalSettings

        now = timezone.now()
        reconciled = 0
        notify_pks: list = []

        # RUNNING: dead once well past started_at + the script's own timeout +
        # grace. The deadline is per-run, and the candidate set is tiny by
        # construction, so iterate instead of fighting cross-database duration
        # arithmetic. The conditional UPDATE re-checks status so a worker
        # finalizing concurrently (or an external cancel) always wins.
        grace = datetime.timedelta(seconds=cls.RECONCILE_GRACE_SECONDS)
        candidates = cls.objects.filter(
            status=cls.Status.RUNNING,
            started_at__isnull=False,
            started_at__lt=now - grace,
        ).select_related("script")
        for run in candidates:
            deadline = (
                run.started_at
                + datetime.timedelta(seconds=run.script.timeout_seconds)
                + grace
            )
            if now < deadline:
                continue
            marker = (
                "\n[RECONCILED: the worker executing this run stopped before "
                "finalizing it (e.g. a worker restart or kill mid-run), so its "
                "output was never captured. Marked failed "
                f"{cls.RECONCILE_GRACE_SECONDS}s past the script's "
                f"{run.script.timeout_seconds}s timeout. A detached script "
                "process may have kept running after the worker died.]"
            )
            flipped = cls.objects.filter(
                pk=run.pk, status=cls.Status.RUNNING
            ).update(
                status=cls.Status.FAILED,
                exit_code=-1,
                stderr=(run.stderr or "") + marker,
                ended_at=now,
                pid=None,
            )
            reconciled += flipped
            if flipped:
                notify_pks.append(run.pk)

        # PENDING: only reconciled while workers are alive — with the cluster
        # down, "queued" is still the truth and these runs will execute when it
        # returns.
        # A run held by the Queue concurrency policy has no task yet by
        # design: it is waiting its turn, not lost, however long the runs ahead
        # of it take (they are reconciled on their own; that releases it).
        # Nor is a retry waiting for its delay (up to a day): it counts as
        # lost only once it has been due for as long as any pending run.
        if GlobalSettings.get_settings().worker_is_alive():
            stale_pending = list(
                cls.objects.filter(
                    status=cls.Status.PENDING,
                    created_at__lt=now - cls.RECONCILE_PENDING_AFTER,
                )
                .exclude(task_id="", queued_behind__isnull=False)
                .exclude(
                    models.Q(task_id="")
                    & models.Q(next_attempt_at__isnull=False)
                    & models.Q(next_attempt_at__gt=now - cls.RECONCILE_PENDING_AFTER)
                )
                .values_list("pk", flat=True)
            )
            flipped = cls.objects.filter(
                pk__in=stale_pending, status=cls.Status.PENDING
            ).update(
                status=cls.Status.FAILED,
                exit_code=-1,
                stderr=(
                    "[RECONCILED: queued for over 24 hours while workers were "
                    "alive — the queued task was lost before pickup (e.g. a "
                    "worker restart between queue and execution). Re-run the "
                    "script.]"
                ),
                ended_at=now,
            )
            reconciled += flipped
            if flipped:
                notify_pks.extend(
                    cls.objects.filter(
                        pk__in=stale_pending, status=cls.Status.FAILED
                    ).values_list("pk", flat=True)
                )

        cls._notify_reconciled(notify_pks)
        cls._release_queues(notify_pks)
        return reconciled

    @classmethod
    def reconcile_orphans_at_worker_start(cls, worker_started_at: float) -> int:
        """Fail RUNNING runs whose script process is gone, as a worker starts.

        Called by ``pyrunner_qcluster --reconcile-orphans``, which the standard
        all-in-one container (entrypoint.sh) passes: there the worker and every
        script it started share one process namespace, so after a container
        restart (or a worker restart) a run still marked RUNNING whose process
        no longer exists here can never finish. Without this it showed "running"
        until ``reconcile_stale``'s deadline (started + timeout + 5 min).

        A worker started on its own (split worker containers, several workers on
        one database) doesn't pass the flag — a run's pid there may belong to
        another container — and keeps the deadline rule.

        Conservative by construction: only runs that started before this worker
        and have a recorded pid; the pid counts as gone if no such process
        exists, it is a zombie, or the process holding that pid now was created
        after this worker started (the pid was reused). A live process — or one
        that can't be inspected — is left alone, and runs with no pid yet are
        left to the deadline rule. Status-only, like ``reconcile_stale``.

        Returns the number of runs reconciled.
        """
        import psutil
        from django.utils import timezone

        started_cutoff = datetime.datetime.fromtimestamp(
            worker_started_at, tz=datetime.timezone.utc
        )
        now = timezone.now()
        reconciled = 0
        notify_pks: list = []
        candidates = cls.objects.filter(
            status=cls.Status.RUNNING,
            pid__isnull=False,
            started_at__isnull=False,
            started_at__lt=started_cutoff,
        )
        for run in candidates:
            try:
                proc = psutil.Process(run.pid)
                gone = (
                    proc.status() == psutil.STATUS_ZOMBIE
                    or proc.create_time() >= worker_started_at
                )
            except psutil.NoSuchProcess:
                gone = True
            except psutil.Error:
                gone = False  # can't tell (e.g. access denied): keep the deadline rule
            if not gone:
                continue
            marker = (
                "\n[RECONCILED: the worker restarted while this run was executing "
                "and the run's process is gone, so its output was never captured. "
                "Marked failed when the worker started again.]"
            )
            flipped = cls.objects.filter(
                pk=run.pk, status=cls.Status.RUNNING, pid=run.pid
            ).update(
                status=cls.Status.FAILED,
                exit_code=-1,
                stderr=(run.stderr or "") + marker,
                ended_at=now,
                pid=None,
            )
            reconciled += flipped
            if flipped:
                notify_pks.append(run.pk)

        cls._notify_reconciled(notify_pks)
        cls._release_queues(notify_pks)
        return reconciled

    @classmethod
    def _release_queues(cls, pks) -> None:
        """Start the next waiting run of each script whose run was reconciled.

        A reconciled run ends without passing through the executor's finalize,
        which is where a Queue-policy script normally starts its next run. The
        heartbeat would catch it within a minute; this does it right away.
        Best effort, like the notifications.
        """
        if not pks:
            return
        from core.services.run_concurrency import try_release

        for script_id in set(
            cls.objects.filter(pk__in=pks).values_list("script_id", flat=True)
        ):
            try_release(script_id)

    @classmethod
    def _notify_reconciled(cls, pks) -> None:
        """Send the normal failure notifications for reconciled runs.

        A run the reconciler flips to FAILED never went through
        ``execute_run_task``'s finalize path, so without this the one failure
        class the platform most needs to report - "your worker died mid-run" -
        was the one it stayed silent about. Best-effort per run: a broken
        channel must not stop the reconciler (which is the heartbeat).
        """
        if not pks:
            return
        from core.services.notification_service import NotificationService

        for run in cls.objects.filter(pk__in=pks).select_related("script"):
            try:
                NotificationService.send_notification(run)
            except Exception:
                logger.exception(f"Notification for reconciled run {run.pk} failed")

    @property
    def duration(self) -> float | None:
        """Return the duration in seconds, or None if not completed."""
        if self.started_at and self.ended_at:
            return (self.ended_at - self.started_at).total_seconds()
        return None

    @property
    def duration_display(self) -> str:
        """Return a human-readable duration string."""
        from core.formatting import format_duration

        return format_duration(self.duration)

    @property
    def is_finished(self) -> bool:
        """Check if the run has completed (successfully or not)."""
        return self.status in self.FINISHED_STATUSES

    @property
    def is_held(self) -> bool:
        """Waiting for the runs ahead of it (Queue concurrency policy)."""
        return (
            self.status == self.Status.PENDING
            and not self.task_id
            and self.queued_behind is not None
        )

    @property
    def is_waiting_retry(self) -> bool:
        """A retry waiting for its delay (the heartbeat starts it when due)."""
        return (
            self.status == self.Status.PENDING
            and not self.task_id
            and self.queued_behind is None
            and self.next_attempt_at is not None
        )

    @property
    def is_stopping(self) -> bool:
        """A stop was requested and the script is still shutting down."""
        return self.status == self.Status.RUNNING and self.stop_requested_at is not None

    @property
    def stop_deadline(self):
        """When a stopping run is killed if its script hasn't exited (or None)."""
        if not self.is_stopping:
            return None
        return self.stop_requested_at + datetime.timedelta(
            seconds=self.script.stop_grace_seconds
        )

    @property
    def is_skipped(self) -> bool:
        """Not started: the script was already running (Skip concurrency policy)."""
        return self.cancel_reason == self.CancelReason.CONCURRENCY_SKIP

    @property
    def is_maintenance_skipped(self) -> bool:
        """Not started: a maintenance window blocks its kind of trigger."""
        return self.cancel_reason == self.CancelReason.MAINTENANCE

    @property
    def is_successful(self) -> bool:
        """Check if the run completed successfully."""
        return self.status == self.Status.SUCCESS

    @property
    def has_output(self) -> bool:
        """Check if there is any output (stdout or stderr)."""
        return bool(self.stdout or self.stderr)

    def get_stdout_preview(self, max_lines: int = 10) -> str:
        """Return a preview of stdout (last N lines)."""
        if not self.stdout:
            return ""
        lines = self.stdout.split("\n")
        if len(lines) <= max_lines:
            return self.stdout
        return "...\n" + "\n".join(lines[-max_lines:])

    def get_stderr_preview(self, max_lines: int = 10) -> str:
        """Return a preview of stderr (last N lines)."""
        if not self.stderr:
            return ""
        lines = self.stderr.split("\n")
        if len(lines) <= max_lines:
            return self.stderr
        return "...\n" + "\n".join(lines[-max_lines:])
