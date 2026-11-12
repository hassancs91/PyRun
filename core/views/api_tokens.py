"""
API Token management views for the control panel.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.forms import APITokenForm
from core.models import DataStore, APIToken, Plugin, PluginPublicPage
from core.services import security_events


def _token_scope(request):
    """Q scoping tokens to the active workspace (tenancy Stage 3).

    Includes NULL-workspace tokens (legacy/un-stamped) so a single-workspace
    instance is byte-for-byte; once a token is created post-Stage-3 it carries a
    workspace and is no longer visible cross-workspace.
    """
    return Q(workspace=request.workspace) | (Q(workspace__isnull=True) & ~Q(scope="api"))


@login_required
def api_token_list_view(request: HttpRequest) -> HttpResponse:
    """List the active workspace's API tokens."""
    tokens = list(
        APIToken.objects.filter(_token_scope(request))
        .select_related("datastore", "created_by", "brain", "script", "table")
        .defer("script__code")
    )
    # An expired token is refused at auth whatever its switch says (U62).
    now = timezone.now()
    for token in tokens:
        token.expired = bool(token.expires_at and token.expires_at <= now)

    # Loaded plugin slugs so the list can badge plugin tokens whose plugin has
    # since been removed (plugin_slug is not an FK — orphans 404 harmlessly).
    from core.plugins import all_plugins

    # Oversight: every public page shared from this workspace's plugins
    # (creation happens on plugin dashboards via PublicPageAPI.share()).
    public_pages = PluginPublicPage.objects.filter(
        workspace=request.workspace
    ).select_related("created_by")

    return render(
        request,
        "cpanel/api_tokens/list.html",
        {
            "tokens": tokens,
            "loaded_plugin_slugs": {p.slug for p in all_plugins()},
            # Installed but not loaded = inactive, not "not installed" (U62).
            "installed_plugin_slugs": set(Plugin.objects.values_list("slug", flat=True)),
            "public_pages": public_pages,
        },
    )


@login_required
def api_token_create_view(request: HttpRequest) -> HttpResponse:
    """Create a new API token."""
    if request.method == "POST":
        form = APITokenForm(request.POST, workspace=request.workspace)
        if form.is_valid():
            token = form.save(commit=False)
            # Generate the token value
            raw_token = APIToken.generate_token(scope=token.scope, kind=token.kind)
            token.token = raw_token
            token.created_by = request.user
            # Stamp the active workspace (tenancy Stage 3) so the token only
            # resolves this workspace's datastores.
            token.workspace = request.workspace
            token.save()
            security_events.record(
                "token_create",
                request=request,
                user=request.user,
                detail=f'"{token.name}" ({token.get_scope_display()})'[:255],
            )

            # Store the token in session for one-time display
            request.session["new_api_token"] = raw_token
            request.session["new_api_token_id"] = str(token.id)

            messages.success(request, f'API token "{token.name}" created successfully.')
            return redirect("cpanel:api_token_created", pk=token.pk)
    else:
        form = APITokenForm(workspace=request.workspace)

    return render(
        request,
        "cpanel/api_tokens/create.html",
        {
            "form": form,
        },
    )


@login_required
def api_token_created_view(request: HttpRequest, pk) -> HttpResponse:
    """Display newly created token (one-time view)."""
    token_obj = get_object_or_404(APIToken, _token_scope(request), pk=pk)

    # Get the token value from session (one-time display)
    new_token = request.session.pop("new_api_token", None)
    new_token_id = request.session.pop("new_api_token_id", None)

    # Verify the token in session matches the requested token
    if new_token_id != str(pk):
        # Token was already shown or doesn't match, redirect to list
        messages.warning(request, "Token can only be viewed once after creation.")
        return redirect("cpanel:api_token_list")

    return render(
        request,
        "cpanel/api_tokens/created.html",
        {
            "token_obj": token_obj,
            "token_value": new_token,
        },
    )


@login_required
@require_POST
def api_token_revoke_view(request: HttpRequest, pk) -> HttpResponse:
    """Revoke (delete) an API token."""
    token = get_object_or_404(APIToken, _token_scope(request), pk=pk)
    name = token.name
    token.delete()

    messages.success(request, f'API token "{name}" has been revoked.')
    return redirect("cpanel:api_token_list")


@login_required
@require_POST
def api_token_toggle_view(request: HttpRequest, pk) -> HttpResponse:
    """Toggle an API token's active status."""
    token = get_object_or_404(APIToken, _token_scope(request), pk=pk)
    token.is_active = not token.is_active
    token.save(update_fields=["is_active"])

    status = "activated" if token.is_active else "deactivated"
    messages.success(request, f'API token "{token.name}" has been {status}.')
    return redirect("cpanel:api_token_list")


# The Activity page lists at most this many of a token's newest audit rows.
ACTIVITY_LIMIT = 200


@login_required
def api_token_activity_view(request: HttpRequest, pk) -> HttpResponse:
    """A unified API token's audited calls: runs, stops, writes and failures."""
    from core.services.api_audit import RETENTION_DAYS

    token = get_object_or_404(APIToken, _token_scope(request), pk=pk, scope=APIToken.Scope.API)
    calls = list(token.api_calls.order_by("-created_at", "-id")[:ACTIVITY_LIMIT])
    return render(
        request,
        "cpanel/api_tokens/activity.html",
        {
            "token": token,
            "calls": calls,
            "limit": ACTIVITY_LIMIT,
            "retention_days": RETENTION_DAYS,
        },
    )


@login_required
@require_POST
def public_page_revoke_view(request: HttpRequest, pk) -> HttpResponse:
    """Revoke a shared public page (the URL 404s; re-sharing rotates the token)."""
    page = get_object_or_404(PluginPublicPage, pk=pk, workspace=request.workspace)
    page.is_active = False
    page.save(update_fields=["is_active"])

    messages.success(
        request,
        f'Public page "{page.plugin_slug}: {page.page}" has been revoked. '
        "Its URL is permanently dead; re-sharing issues a new one.",
    )
    return redirect("cpanel:api_token_list")
