"""
Dashboard view for the control panel.
"""
import os

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse

from core.models import Environment, Run, Script, WorkspaceMembership
from core.services import maintenance
from core.services.dashboard_service import DashboardService
from core.services.system_info_service import SystemInfoService


def _in_container() -> bool:
    return os.path.exists("/.dockerenv") or os.environ.get("PYRUNNER_IN_CONTAINER") == "1"


def _maintenance_url(request, ws):
    """The banner's link: the workspace's windows page, for those who may manage it."""
    if ws is None:
        return reverse("cpanel:maintenance_list") if request.user.is_superuser else None
    if request.user.is_superuser or WorkspaceMembership.objects.filter(
        user=request.user, workspace=ws, role__in=WorkspaceMembership.MANAGE_ROLES
    ).exists():
        return reverse("cpanel:workspace_maintenance_list", kwargs={"pk": ws.pk})
    return None


@login_required
def dashboard_view(request):
    """Main dashboard view with overview statistics."""
    ws = request.workspace

    # Get statistics from service (scoped to the active workspace — tenancy Stage 3)
    stats = DashboardService.get_statistics(workspace=ws)

    # Active-workspace runs, reused by the legacy count cards and the pulse.
    ws_runs = Run.objects.for_workspace(ws)

    # Legacy counts (for backwards compatibility)
    runs_count = ws_runs.count()
    # Environments are SHARED infrastructure (not workspace-scoped); count global.
    environments_count = Environment.objects.filter(is_active=True).count()
    success_count = ws_runs.filter(status=Run.Status.SUCCESS).count()
    failed_count = ws_runs.filter(
        status__in=[Run.Status.FAILED, Run.Status.TIMEOUT]
    ).count()

    # Recent activity
    recent_runs = ws_runs.select_related("script", "triggered_by").order_by(
        "-created_at"
    )[:8]
    recent_scripts = Script.objects.for_workspace(ws).select_related(
        "environment"
    ).order_by("-updated_at")[:5]

    # Run pulse — last 40 runs, oldest → newest, for the activity ribbon.
    # `height` (14–100) encodes run duration so the ribbon reads like a heartbeat.
    pulse_runs = list(ws_runs.order_by("-created_at")[:40])
    pulse_runs.reverse()
    run_pulse = []
    for r in pulse_runs:
        d = r.duration
        height = 26 if d is None else int(18 + min(d, 60) / 60 * 82)
        run_pulse.append({"status": r.status, "height": height})

    # New widgets (scoped to the active workspace — tenancy Stage 3)
    recent_failures = DashboardService.get_recent_failures(workspace=ws)
    upcoming_runs = DashboardService.get_upcoming_scheduled_runs(workspace=ws)
    system_health = DashboardService.get_system_health()
    # Host-wide numbers: superusers only (F22, like Settings → System).
    system_resources = (
        SystemInfoService.get_system_resources() if request.user.is_superuser else None
    )

    context = {
        # A maintenance window that is on right now (dashboard banner)
        "maintenance": maintenance.banner(ws.id if ws else None),
        "maintenance_url": _maintenance_url(request, ws),
        # Statistics cards
        "scripts_count": stats["total_scripts"],
        "active_scripts_count": stats["active_scripts"],
        "runs_count": runs_count,
        "runs_today": stats["runs_today"],
        "runs_this_week": stats["runs_this_week"],
        "success_rate": stats["success_rate"],
        "queue_size": stats["queue_size"],
        "environments_count": environments_count,
        "success_count": success_count,
        "failed_count": failed_count,
        # Recent activity
        "recent_runs": recent_runs,
        "recent_scripts": recent_scripts,
        "run_pulse": run_pulse,
        # New widgets
        "recent_failures": recent_failures,
        "upcoming_runs": upcoming_runs,
        # System health
        "system_health": system_health,
        # System resources (CPU, RAM, Storage)
        "system_resources": system_resources,
        "in_container": _in_container(),
    }
    return render(request, "cpanel/dashboard.html", context)


@login_required
def system_resources_api(request):
    """AJAX endpoint for system resource metrics (superusers only, F22)."""
    if not request.user.is_superuser:
        return JsonResponse({"error": "Permission denied."}, status=403)
    resources = SystemInfoService.get_system_resources()
    return JsonResponse(resources)
