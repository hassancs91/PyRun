"""Instance health over REST, MCP and Py AI (2.5 Stage 3, T18): ``system.health``.

One read (``system:read``) that answers "is everything OK?": whether a worker is
alive and how many tasks wait for one, whether schedules are paused, the
maintenance windows on now, the last scheduled backup, the running version and
whether a newer one is out, plugins that failed to load, and this workspace's
runs (going, waiting, failed in the last 24 hours). ``issues`` names what needs
attention in plain sentences; ``ok`` is true when there is none.

The worker, queue, schedules switch, backup, version and plugins belong to the
instance; maintenance windows (the workspace's and the global ones) and runs
are the caller's workspace's. No error text is returned: the console's pages
have it. Py AI on a chat channel may call it (plan D9).
"""

import datetime

from django.utils import timezone

from core.api.registry import endpoint
from core.models import GlobalSettings, Plugin, Run

from .scripts import iso

FAILED = (Run.Status.FAILED, Run.Status.TIMEOUT)


def worker_health(settings):
    from core.services.system_info_service import SystemInfoService

    status = SystemInfoService.get_worker_status()
    return {
        "alive": settings.worker_is_alive(),
        "status": status["status"],
        "configured_workers": status["configured_workers"],
        "heartbeat_at": iso(status["heartbeat_at"]),
        "tasks_waiting": status["queued_tasks"],
    }


def maintenance_health(workspace):
    from core.services.maintenance import banner

    on = banner(workspace.pk)
    if on is None:
        return {"active": False, "windows": [], "until": None, "until_local": None,
                "blocks": None}
    first = on.windows[0]
    return {
        "active": True,
        "windows": [w.name for w in on.windows],
        "until": iso(on.until),
        # As the dashboard banner and a blocked run's message say it.
        "until_local": f"{first.format_local(on.until)} ({first.timezone})",
        "blocks": on.blocks,
    }


def backup_health():
    """The last scheduled backup (a backup downloaded from the console isn't recorded)."""
    from core.services.backup_schedule_service import BackupScheduleService

    status = BackupScheduleService.get_schedule_status()
    return {
        "scheduled": status["enabled"],
        "schedule": status["schedule"] if status["enabled"] else None,
        "next_run": iso(status["next_run"]) if status["enabled"] else None,
        "last_run_at": iso(status["last_run"]),
        "last_status": status["last_status"] or None,
        "last_size_bytes": status["last_size"],
    }


def version_health(settings):
    from core.services.update_service import UpdateService
    from pyrunner.version import __version__

    # The daily update check stores the newest release it saw; the comparison
    # is the console's (the header's "update available").
    return {
        "running": __version__,
        "latest": settings.update_latest_version.strip() or None,
        "update_available": UpdateService.get_update_context()["update_available"],
        "checked_at": iso(settings.update_checked_at),
    }


def runs_health(workspace, now):
    runs = Run.objects.filter(workspace=workspace)
    return {
        "running": runs.filter(status=Run.Status.RUNNING).count(),
        "waiting": runs.filter(status=Run.Status.PENDING).count(),
        "failed_last_24h": runs.filter(
            status__in=FAILED, created_at__gte=now - datetime.timedelta(hours=24)
        ).count(),
    }


def issues_for(health):
    issues = []
    worker = health["worker"]
    if not worker["alive"]:
        issues.append(
            "No worker is running, so runs and schedules don't start."
            if worker["heartbeat_at"]
            else "No worker has started since this instance began: runs and schedules don't start."
        )
    if health["schedules"]["paused"]:
        issues.append("Schedules are paused (Pause all): no schedule starts runs.")
    maintenance = health["maintenance"]
    if maintenance["active"]:
        issues.append(
            f"Maintenance window {', '.join(maintenance['windows'])} is on until "
            f"{maintenance['until_local']}: it blocks {maintenance['blocks']}."
        )
    if health["backup"]["scheduled"] and health["backup"]["last_status"] == "failed":
        issues.append("The last scheduled backup failed (see Settings → Backup).")
    if health["plugins"]["errored"]:
        issues.append(f"Plugins that failed to load: {', '.join(health['plugins']['errored'])}.")
    failed = health["runs"]["failed_last_24h"]
    if failed:
        issues.append(
            f"{failed} run{'s' if failed != 1 else ''} failed or timed out in this workspace "
            "in the last 24 hours."
        )
    return issues


@endpoint(
    "system.health",
    title="System health",
    description=(
        "Is everything OK? Worker liveness and tasks waiting, whether schedules are "
        "paused, maintenance windows on now, the last scheduled backup, the running "
        "version and whether an update is out, plugins that failed to load, and this "
        "workspace's runs going, waiting and failed in the last 24 hours. issues names "
        "what needs attention; ok is true when there is nothing."
    ),
    input={"type": "object", "properties": {}},
    permission="system:read",
    pyai_tier="channel",
)
def health(ctx, params):
    settings = GlobalSettings.get_settings()
    now = timezone.now()
    report = {
        "worker": worker_health(settings),
        "schedules": {
            "paused": settings.schedules_paused,
            "paused_at": iso(settings.schedules_paused_at) if settings.schedules_paused else None,
        },
        "maintenance": maintenance_health(ctx.workspace),
        "backup": backup_health(),
        "version": version_health(settings),
        "plugins": {
            "errored": list(
                Plugin.objects.filter(status=Plugin.Status.ERRORED)
                .order_by("name")
                .values_list("name", flat=True)
            ),
        },
        "runs": runs_health(ctx.workspace, now),
        "checked_at": now.isoformat(),
    }
    issues = issues_for(report)
    return {"ok": not issues, "issues": issues, **report}
