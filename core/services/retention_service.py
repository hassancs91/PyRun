"""
Log retention service for managing run cleanup.

The policy, per script (the script's overrides, else the global settings):

- Only FINISHED runs are ever deleted. A run that is pending (queued, held by
  the Queue concurrency policy, a retry waiting for its time) or running is
  kept whatever the days/count settings, by every cleanup path: the daily
  sweep, **Run cleanup now**, and the preview counts the same way.
- A finished run is kept while it is newer than the retention days (0 = no
  age rule) or among the script's newest N finished runs that actually ran
  (0 = no count rule). Both 0 keeps everything.
- Runs refused before they started (``cancel_reason`` concurrency skip or
  maintenance) don't take those N places: they get their own newest N, so a
  burst of them (a sender retrying a blocked webhook) can't push real runs
  out of the history, and can't grow without limit under a count-only policy.
"""

import logging
from datetime import timedelta

from django.db.models import Q, QuerySet
from django.utils import timezone

# The name lives in schedule_service's infra-schedule registry so global
# pause/resume can never delete or forget this schedule.
from core.services.schedule_service import CLEANUP_SCHEDULE_NAME

logger = logging.getLogger(__name__)


class RetentionService:
    """
    Service for managing log retention and cleanup of old runs.
    """

    @classmethod
    def get_effective_retention(cls, script) -> tuple[int, int]:
        """
        Get the effective retention settings for a script.
        Per-script overrides take precedence over global settings.

        Args:
            script: Script model instance

        Returns:
            tuple: (retention_days, retention_count)
        """
        from core.models import GlobalSettings

        settings = GlobalSettings.get_settings()

        # Use script override if set, otherwise use global
        retention_days = (
            script.retention_days_override
            if script.retention_days_override is not None
            else settings.retention_days
        )
        retention_count = (
            script.retention_count_override
            if script.retention_count_override is not None
            else settings.retention_count
        )

        return retention_days, retention_count

    @classmethod
    def get_runs_to_delete_for_script(
        cls, script, days: int | None = None, count: int | None = None
    ) -> QuerySet:
        """
        Get queryset of runs that would be deleted for a specific script
        (finished runs only; see the module docstring for the policy).

        Args:
            script: Script model instance
            days: Override retention days (None = use effective setting)
            count: Override retention count (None = use effective setting)

        Returns:
            QuerySet of Run objects to delete
        """
        from core.models import Run

        if days is None or count is None:
            effective_days, effective_count = cls.get_effective_retention(script)
            if days is None:
                days = effective_days
            if count is None:
                count = effective_count

        # If both are 0, keep everything (no cleanup)
        if days == 0 and count == 0:
            return Run.objects.none()

        # Only finished runs can go: pending (queued, held, a retry waiting
        # for its time) and running ones are never deleted. The status is
        # part of the DELETE's own query, so a run that is still in flight
        # when the cleanup runs is kept even if the preview was taken earlier.
        finished = Run.objects.filter(
            script=script, status__in=Run.FINISHED_STATUSES
        )
        to_delete = finished

        # Keep runs newer than retention_days
        if days > 0:
            cutoff_date = timezone.now() - timedelta(days=days)
            to_delete = to_delete.filter(created_at__lt=cutoff_date)

        # Keep the newest N runs that actually ran, and separately the newest
        # N refused ones (skipped by the concurrency policy or blocked by a
        # maintenance window), so refused runs can't crowd out real ones.
        if count > 0:
            refused = Q(cancel_reason__in=cls.refused_reasons())
            for kept in (finished.exclude(refused), finished.filter(refused)):
                newest = kept.order_by("-created_at", "-id").values("id")[:count]
                to_delete = to_delete.exclude(id__in=newest)

        return to_delete

    @staticmethod
    def refused_reasons() -> tuple[str, ...]:
        """``cancel_reason`` values of runs refused before they started."""
        from core.models import Run

        return (Run.CancelReason.CONCURRENCY_SKIP, Run.CancelReason.MAINTENANCE)

    @classmethod
    def cleanup_runs_for_script(
        cls, script, days: int | None = None, count: int | None = None
    ) -> int:
        """
        Delete old runs for a specific script based on retention policy.

        Args:
            script: Script model instance
            days: Override retention days (None = use effective setting)
            count: Override retention count (None = use effective setting)

        Returns:
            int: Number of runs deleted
        """
        runs_to_delete = cls.get_runs_to_delete_for_script(script, days, count)
        # delete()'s total also counts rows removed with the runs (their mask
        # rows); report runs only, the number the preview shows.
        _, per_model = runs_to_delete.delete()
        deleted_count = per_model.get(runs_to_delete.model._meta.label, 0)

        if deleted_count > 0:
            logger.info(
                f"Deleted {deleted_count} runs for script '{script.name}' (id={script.id})"
            )

        return deleted_count

    @classmethod
    def cleanup_all_runs(cls) -> int:
        """
        Run cleanup for all scripts using global/per-script settings.

        Returns:
            int: Total number of runs deleted
        """
        from core.models import Script

        total_deleted = 0
        scripts = Script.objects.all()

        for script in scripts:
            try:
                deleted = cls.cleanup_runs_for_script(script)
                total_deleted += deleted
            except Exception:
                logger.exception(
                    f"Failed to cleanup runs for script '{script.name}'"
                )

        logger.info(f"Total cleanup: {total_deleted} runs deleted across all scripts")
        return total_deleted

    @classmethod
    def get_cleanup_stats(cls) -> dict:
        """
        Get statistics about what would be cleaned up.

        Returns:
            dict: Stats including total_runs, runs_to_delete, runs_to_keep,
            runs_in_progress, per_script breakdown. Counted with the same
            query the cleanup deletes with.
        """
        from core.models import Script, Run

        total_runs = Run.objects.count()
        total_to_delete = 0
        per_script = []

        for script in Script.objects.all():
            runs_count = script.runs.count()
            to_delete_count = cls.get_runs_to_delete_for_script(script).count()
            total_to_delete += to_delete_count

            if runs_count > 0:
                per_script.append({
                    "script_id": str(script.id),
                    "script_name": script.name,
                    "total_runs": runs_count,
                    "to_delete": to_delete_count,
                    "to_keep": runs_count - to_delete_count,
                })

        return {
            "total_runs": total_runs,
            "runs_to_delete": total_to_delete,
            "runs_to_keep": total_runs - total_to_delete,
            # Pending or running now: never deleted (part of runs_to_keep).
            "runs_in_progress": Run.objects.exclude(
                status__in=Run.FINISHED_STATUSES
            ).count(),
            "per_script": per_script,
        }

    @classmethod
    def enable_auto_cleanup(cls) -> None:
        """
        Create django-q2 schedule for daily cleanup at 2 AM.
        """
        from django_q.models import Schedule
        from datetime import time

        # Calculate next run at 2 AM
        now = timezone.now()
        next_run = now.replace(hour=2, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)

        Schedule.objects.update_or_create(
            name=CLEANUP_SCHEDULE_NAME,
            defaults={
                "func": "core.tasks.cleanup_old_runs_task",
                "schedule_type": Schedule.DAILY,
                "next_run": next_run,
                "repeats": -1,  # Repeat forever
            },
        )

        logger.info(f"Auto-cleanup schedule enabled, next run at {next_run}")

    @classmethod
    def disable_auto_cleanup(cls) -> None:
        """
        Remove auto-cleanup schedule.
        """
        from django_q.models import Schedule

        deleted, _ = Schedule.objects.filter(name=CLEANUP_SCHEDULE_NAME).delete()

        if deleted:
            logger.info("Auto-cleanup schedule disabled")

    @classmethod
    def is_auto_cleanup_scheduled(cls) -> bool:
        """
        Check if auto-cleanup is currently scheduled.
        """
        from django_q.models import Schedule

        return Schedule.objects.filter(name=CLEANUP_SCHEDULE_NAME).exists()
