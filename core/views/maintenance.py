"""
Maintenance windows in the console (2.5 Stage 1, ticket T07).

The same views serve two scopes:

- global windows, under Settings (``settings/maintenance/...``): superusers;
- one workspace's windows (``workspaces/<pk>/maintenance/...``): its Owners
  and Admins (and superusers), with the same gating as the other workspace
  management pages (a non-member gets 404, a plain member 403).

A window is looked up within its scope, so a workspace's URL can never reach
a global window or another workspace's.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.forms import MaintenanceWindowForm
from core.models import MaintenanceWindow, Workspace
from core.views.decorators import ADMIN_ONLY_MESSAGE
from core.views.workspaces import _require_manage


def _scope(request, pk):
    """The workspace the page is about (None = global), after the permission check."""
    if pk is None:
        if not request.user.is_superuser:
            raise PermissionDenied(ADMIN_ONLY_MESSAGE)
        return None
    workspace = get_object_or_404(Workspace, pk=pk)
    _require_manage(request, workspace)
    return workspace


def _url(name, workspace, **kwargs):
    if workspace is None:
        return reverse(f"cpanel:maintenance_{name}", kwargs=kwargs)
    return reverse(f"cpanel:workspace_maintenance_{name}", kwargs={"pk": workspace.pk, **kwargs})


def _window(workspace, window_id):
    return get_object_or_404(MaintenanceWindow, pk=window_id, workspace=workspace)


def _next_display(window, now=None):
    """The 'next occurrence' preview: the one going on, else the next; None if none."""
    occ = window.next_occurrence(now)
    if occ is None:
        return None
    return f"{window.format_local(occ[0])} to {window.format_local(occ[1])} ({window.timezone})"


def _annotate(windows, now):
    """Give each window what the list shows: whether it's on, its next occurrence."""
    for w in windows:
        w.is_on = w.enabled and w.occurrence_at(now) is not None
        w.next_display = _next_display(w, now)
    return windows


@login_required
def maintenance_list_view(request: HttpRequest, pk=None) -> HttpResponse:
    """List a scope's windows; a workspace page also shows the global ones."""
    workspace = _scope(request, pk)
    now = timezone.now()
    windows = _annotate(list(MaintenanceWindow.objects.filter(workspace=workspace)), now)
    for w in windows:
        w.edit_url = _url("edit", workspace, window_id=w.pk)
        w.toggle_url = _url("toggle", workspace, window_id=w.pk)
    global_windows = []
    if workspace is not None:
        # Read-only here (they are edited under Settings), but they apply.
        global_windows = _annotate(
            list(MaintenanceWindow.objects.filter(workspace__isnull=True, enabled=True)), now
        )
    return render(request, "cpanel/maintenance/list.html", {
        "workspace": workspace,
        "windows": windows,
        "global_windows": global_windows,
        "new_url": _url("create", workspace),
    })


@login_required
def maintenance_edit_view(request: HttpRequest, pk=None, window_id=None) -> HttpResponse:
    """Create (no ``window_id``) or edit a window of the scope."""
    workspace = _scope(request, pk)
    window = _window(workspace, window_id) if window_id else MaintenanceWindow(workspace=workspace)
    if request.method == "POST":
        form = MaintenanceWindowForm(request.POST, instance=window)
        if form.is_valid():
            window = form.save(commit=False)
            window.workspace = workspace
            window.save()
            messages.success(request, f'Maintenance window "{window.name}" saved.')
            return redirect(_url("list", workspace))
    else:
        form = MaintenanceWindowForm(instance=window)
    # The id is a UUID with a default, so an unsaved window already has a pk:
    # "new" is decided by the URL, not by window.pk.
    is_new = window_id is None
    return render(request, "cpanel/maintenance/form.html", {
        "workspace": workspace,
        "form": form,
        "window": window,
        "is_new": is_new,
        "next_display": None if is_new else _next_display(window),
        "list_url": _url("list", workspace),
        "delete_url": None if is_new else _url("delete", workspace, window_id=window.pk),
    })


@login_required
@require_POST
def maintenance_toggle_view(request: HttpRequest, pk=None, window_id=None) -> HttpResponse:
    """Enable or disable a window."""
    workspace = _scope(request, pk)
    window = _window(workspace, window_id)
    window.enabled = not window.enabled
    window.save(update_fields=["enabled", "updated_at"])
    state = "enabled" if window.enabled else "disabled"
    messages.success(request, f'Maintenance window "{window.name}" {state}.')
    return redirect(_url("list", workspace))


@login_required
@require_POST
def maintenance_delete_view(request: HttpRequest, pk=None, window_id=None) -> HttpResponse:
    """Delete a window."""
    workspace = _scope(request, pk)
    window = _window(workspace, window_id)
    name = window.name
    window.delete()
    messages.success(request, f'Maintenance window "{name}" deleted.')
    return redirect(_url("list", workspace))
