"""
Settings views for the control panel.
"""
import logging
import os

from django.shortcuts import render, redirect
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone

from core.models import GlobalSettings
from core.services.schedule_service import ScheduleService
from core.services.notification_service import NotificationService
from core.services.retention_service import RetentionService
from core.services.system_info_service import SystemInfoService
from core.forms import (
    NotificationSettingsForm,
    GeneralSettingsForm,
    LogRetentionForm,
    WorkerSettingsForm,
    ExecutionIsolationForm,
    BackupCreateForm,
    BackupRestoreForm,
    S3BackupScheduleForm,
    RecaptchaSettingsForm,
)
from core.views.decorators import superuser_required

logger = logging.getLogger(__name__)


@login_required
@superuser_required
def settings_view(request: HttpRequest) -> HttpResponse:
    """Display global settings."""
    return render_settings(request)


def render_settings(request: HttpRequest, tab: str = "", **bound) -> HttpResponse:
    """The Settings page; a refused save passes its bound form (U69).

    ``tab`` opens that tab and ``bound`` replaces one form (e.g.
    ``retention_form=form``), so the errors show under their fields and the
    typed values stay. The page then renders at the POST URL, like any refused
    Django form.
    """
    from core.api.registry import registry
    from core.services.backup_schedule_service import BackupScheduleService

    settings = GlobalSettings.get_settings()
    forms = {
        "notification_form": lambda: NotificationSettingsForm(instance=settings),
        "general_form": lambda: GeneralSettingsForm(instance=settings),
        "retention_form": lambda: LogRetentionForm(instance=settings),
        "worker_form": lambda: WorkerSettingsForm(instance=settings),
        "isolation_form": lambda: ExecutionIsolationForm(instance=settings),
        "backup_create_form": BackupCreateForm,
        "backup_restore_form": BackupRestoreForm,
        "backup_schedule_form": lambda: S3BackupScheduleForm(instance=settings),
        "recaptcha_form": lambda: RecaptchaSettingsForm(instance=settings),
    }
    unknown = set(bound) - set(forms)
    if unknown:
        raise TypeError(f"Unknown settings form(s): {', '.join(sorted(unknown))}")
    context = {name: bound[name] if name in bound else make() for name, make in forms.items()}
    context.update(
        {
            "settings": settings,
            "active_tab": tab or "general",
            "backup_schedule_status": BackupScheduleService.get_schedule_status(),
            "nproc_not_enforced": _scripts_run_as_root(),
            "effective_retry": _effective_retry(settings),
            "api_write_endpoints": registry.writes(),
        }
    )
    return render(request, "cpanel/settings.html", context)


def _effective_retry(settings):
    """The retry window the running cluster booted with, when it differs (U31)."""
    from django.core.cache import cache

    from core.services.worker_config import EFFECTIVE_RETRY_CACHE_KEY

    try:
        value = cache.get(EFFECTIVE_RETRY_CACHE_KEY)
    except Exception:
        return None
    if not value or int(value) == int(settings.q_retry):
        return None
    return int(value)


def flash_form_errors(request: HttpRequest, form) -> None:
    """Flash a refused form's errors as toasts, each with its field's label.

    For forms that can't be re-rendered in place (Services cards that are
    filled by JS). Settings tabs re-render with the bound form instead (U69).
    """
    for field, errors in form.errors.items():
        label = ""
        if field != "__all__" and field in form.fields:
            label = form.fields[field].label or field.replace("_", " ").capitalize()
        for error in errors:
            messages.error(request, f"{label}: {error}" if label else str(error))


def settings_tab(tab: str) -> HttpResponse:
    """Redirect back to the Settings tab that was just saved (U9)."""
    url = reverse("cpanel:settings")
    return redirect(f"{url}#{tab}" if tab else url)


def _scripts_run_as_root() -> bool:
    """Whether scripts run as root here (they inherit the worker's uid).

    The kernel exempts root from RLIMIT_NPROC, so the Isolation tab's process
    cap does nothing in the standard image, where everything runs as uid 0.
    """
    geteuid = getattr(os, "geteuid", None)  # absent on Windows
    return bool(geteuid is not None and geteuid() == 0)


@login_required
@superuser_required
@require_POST
def toggle_global_pause_view(request: HttpRequest) -> HttpResponse:
    """Toggle global schedule pause."""
    settings = GlobalSettings.get_settings()

    if settings.schedules_paused:
        count = ScheduleService.resume_all_schedules()
        messages.success(request, f"All schedules resumed. {count} schedules reactivated.")
    else:
        count = ScheduleService.pause_all_schedules(user=request.user)
        messages.warning(request, f"All schedules paused. {count} schedules deactivated.")

    return settings_tab("schedules")


@login_required
@superuser_required
@require_POST
def notification_settings_view(request: HttpRequest) -> HttpResponse:
    """Update notification settings."""
    settings = GlobalSettings.get_settings()
    form = NotificationSettingsForm(request.POST, instance=settings)

    if form.is_valid():
        form.save(settings)
        messages.success(request, "Notification settings saved successfully.")
    else:
        return render_settings(request, "notifications", notification_form=form)

    return settings_tab("notifications")


@login_required
@require_POST
def test_email_view(request: HttpRequest) -> JsonResponse:
    """Send a test email to verify configuration."""
    # JSON endpoint (called via fetch): an in-body superuser check returns a 403
    # JSON body rather than a login redirect, matching the other settings
    # endpoints (sandbox probe, restart workers). Without it any logged-in member
    # could fire test emails at the configured admin address.
    if not request.user.is_superuser:
        return JsonResponse(
            {"success": False, "error": "Permission denied."}, status=403
        )

    settings = GlobalSettings.get_settings()

    if settings.email_backend == GlobalSettings.EmailBackend.DISABLED:
        return JsonResponse({
            "success": False,
            "error": "Email backend is disabled. Please configure an email backend first.",
        })

    recipient = settings.default_notification_email
    fallback = False
    if not recipient:
        # U10: no default yet — send to the admin who clicked.
        recipient = request.user.email
        fallback = True
    if not recipient:
        return JsonResponse({
            "success": False,
            "error": "No default notification email configured, and your account has no email address.",
        })

    try:
        NotificationService.send_test_email(recipient)
        note = " (your address — no default notification email is set)" if fallback else ""
        return JsonResponse({
            "success": True,
            "message": f"Test email sent to {recipient}{note}",
        })
    except Exception as e:
        logger.exception("Failed to send test email")
        return JsonResponse({
            "success": False,
            "error": describe_email_failure(settings, e),
        })


def describe_email_failure(settings, exc: Exception) -> str:
    """One readable line for a failed test email (U71): server, port, cause, hint."""
    if settings.email_backend == GlobalSettings.EmailBackend.RESEND:
        host, port = "smtp.resend.com", 587
    else:
        host, port = settings.smtp_host or "(no host)", settings.smtp_port
    text = str(exc) or exc.__class__.__name__
    message = f"Couldn't send through {host}:{port}: {text}."
    if port == 465:
        message += (
            " Port 465 needs implicit TLS, which PyRun doesn't support — use 587 (STARTTLS)."
        )
    elif "timed out" in text.lower():
        message += " Check the host and port, and that this server may connect out on that port."
    return message


@login_required
@superuser_required
@require_POST
def general_settings_view(request: HttpRequest) -> HttpResponse:
    """Update general settings."""
    settings = GlobalSettings.get_settings()
    form = GeneralSettingsForm(request.POST, instance=settings)

    if form.is_valid():
        previous_slug = settings.admin_url_slug
        form.save(settings)
        messages.success(request, "General settings saved successfully.")
        if settings.admin_url_slug != previous_slug:
            # urlpatterns bake the slug in at import time (pyrunner/urls.py), so
            # the new address only exists once every process has restarted.
            messages.warning(
                request,
                "The admin URL changes after a restart: URL routes are built at "
                f"startup, so /{previous_slug}/ stays live and /{settings.admin_url_slug}/ "
                "only works once PyRun has been restarted.",
            )
    else:
        return render_settings(request, "", general_form=form)

    return settings_tab("")


@login_required
@superuser_required
@require_POST
def recaptcha_settings_view(request: HttpRequest) -> HttpResponse:
    """Update Google reCAPTCHA v2 login-protection settings."""
    settings = GlobalSettings.get_settings()
    form = RecaptchaSettingsForm(request.POST, instance=settings)

    if form.is_valid():
        form.save(settings)
        messages.success(request, "reCAPTCHA settings saved successfully.")
    else:
        return render_settings(request, "security", recaptcha_form=form)

    return settings_tab("security")


@login_required
@superuser_required
@require_POST
def retention_settings_view(request: HttpRequest) -> HttpResponse:
    """Update log retention settings."""
    settings = GlobalSettings.get_settings()
    form = LogRetentionForm(request.POST, instance=settings)

    if form.is_valid():
        form.save(settings)
        messages.success(request, "Log retention settings saved successfully.")
    else:
        return render_settings(request, "retention", retention_form=form)

    return settings_tab("retention")


@login_required
@superuser_required
@require_POST
def worker_settings_view(request: HttpRequest) -> HttpResponse:
    """Update worker settings."""
    settings = GlobalSettings.get_settings()
    form = WorkerSettingsForm(request.POST, instance=settings)

    if form.is_valid():
        form.save(settings)
        messages.warning(
            request, "Worker settings saved. Restart workers for changes to take effect."
        )
    else:
        return render_settings(request, "workers", worker_form=form)

    return settings_tab("workers")


@login_required
@superuser_required
@require_POST
def execution_isolation_settings_view(request: HttpRequest) -> HttpResponse:
    """Update script-execution isolation (sandbox) settings.

    Dashboard-managed config for FOUNDATIONS Seam 2 (Stage 1): the per-run
    resource limits resolved at execution time. No restart required.
    """
    settings = GlobalSettings.get_settings()
    form = ExecutionIsolationForm(request.POST, instance=settings)

    if form.is_valid():
        form.save(settings)
        messages.success(request, "Execution & isolation settings saved successfully.")
    else:
        return render_settings(request, "isolation", isolation_form=form)

    return settings_tab("isolation")


@login_required
@superuser_required
@require_POST
def api_writes_settings_view(request: HttpRequest) -> HttpResponse:
    """Turn API writes (write-class registry endpoints, plan D8) on or off."""
    enabled = request.POST.get("enabled") == "on"
    settings = GlobalSettings.get_settings()
    settings.api_writes_enabled = enabled
    settings.save(update_fields=["api_writes_enabled", "updated_at"])
    logger.warning("api.writes %s by user=%s", "enabled" if enabled else "disabled", request.user.pk)
    messages.success(request, "API writes turned on." if enabled else "API writes turned off.")
    return settings_tab("api")


@login_required
@require_POST
def sandbox_test_view(request: HttpRequest) -> JsonResponse:
    """Probe this host's sandbox capability and cache the result.

    Backs the "Test sandbox on this host" button (sandbox Stage 2). Runs the
    same probe as ``manage.py sandbox_check`` and stores
    ``sandbox_capability`` + ``sandbox_checked_at`` on GlobalSettings. This is a
    convenience gate; the executor re-detects at run time (the real enforcement).
    """
    if not request.user.is_superuser:
        return JsonResponse(
            {"success": False, "error": "Permission denied."}, status=403
        )

    from core.services.sandbox import run_and_store_probe

    try:
        result = run_and_store_probe()
    except Exception as e:  # never let the probe 500 the settings page
        logger.exception("Sandbox capability probe failed")
        return JsonResponse({"success": False, "error": str(e)})

    settings = GlobalSettings.get_settings()
    return JsonResponse({
        "success": True,
        "capability": result.capability,
        "capability_display": settings.get_sandbox_capability_display(),
        "tool": result.tool or "",
        "detail": result.detail,
        "checked_at": settings.sandbox_checked_at.isoformat()
        if settings.sandbox_checked_at
        else "",
    })


@login_required
@require_POST
def restart_workers_view(request: HttpRequest) -> JsonResponse:
    """Trigger a worker restart via management command."""
    # Only superusers can restart workers
    if not request.user.is_superuser:
        return JsonResponse({
            "success": False,
            "error": "Permission denied. Only administrators can restart workers.",
        }, status=403)

    import subprocess
    import sys
    from django.conf import settings as django_settings

    try:
        # Run the restart_workers management command
        result = subprocess.run(
            [sys.executable, "manage.py", "restart_workers", "--timeout", "15"],
            capture_output=True,
            text=True,
            timeout=20,
            cwd=django_settings.BASE_DIR,
        )

        if result.returncode == 0:
            # Exit 0 means the command CONFIRMED a new worker PID — not just
            # that a signal was sent.
            return JsonResponse({
                "success": True,
                "message": "Workers restarted.",
                "output": result.stdout,
            })
        else:
            return JsonResponse({
                "success": False,
                "error": result.stderr or result.stdout or "Unknown error",
            })

    except subprocess.TimeoutExpired:
        return JsonResponse({
            "success": True,
            "message": "Restart initiated. Workers may still be restarting.",
        })
    except Exception as e:
        logger.exception("Failed to restart workers")
        return JsonResponse({
            "success": False,
            "error": str(e),
        })


@login_required
@superuser_required
@require_POST
def manual_cleanup_view(request: HttpRequest) -> HttpResponse:
    """Trigger manual cleanup of old runs."""
    try:
        deleted_count = RetentionService.cleanup_all_runs()

        # Update last_cleanup_at timestamp
        settings = GlobalSettings.get_settings()
        settings.last_cleanup_at = timezone.now()
        settings.save(update_fields=["last_cleanup_at"])

        if deleted_count > 0:
            messages.success(request, f"Cleanup completed. {deleted_count} runs deleted.")
        else:
            messages.info(request, "No runs to clean up based on current retention settings.")
    except Exception as e:
        logger.exception("Manual cleanup failed")
        messages.error(request, f"Cleanup failed: {e}")

    return settings_tab("retention")


@login_required
def cleanup_preview_view(request: HttpRequest) -> JsonResponse:
    """Get preview of what would be cleaned up."""
    # Instance-wide (every workspace's script names), like the rest of Settings.
    if not request.user.is_superuser:
        return JsonResponse({"success": False, "error": "Permission denied."}, status=403)
    try:
        stats = RetentionService.get_cleanup_stats()
        return JsonResponse({
            "success": True,
            "stats": stats,
        })
    except Exception as e:
        logger.exception("Failed to get cleanup preview")
        return JsonResponse({
            "success": False,
            "error": str(e),
        })


@login_required
def system_info_view(request: HttpRequest) -> JsonResponse:
    """Get system information via AJAX."""
    # Instance-wide host details, like the rest of Settings.
    if not request.user.is_superuser:
        return JsonResponse({"success": False, "error": "Permission denied."}, status=403)
    try:
        info = SystemInfoService.get_all_info()
        return JsonResponse({
            "success": True,
            "data": info,
        })
    except Exception as e:
        logger.exception("Failed to get system info")
        return JsonResponse({
            "success": False,
            "error": str(e),
        })
