"""
Plugin management views (control panel, superuser only).

Thin HTTP layer over PluginService. The heavy lifting (zip-slip-safe install,
isolated preflight, controlled restart) lives in the service; these views just
gate on superuser, call the service, set messages, and redirect.
"""

import logging
import math
import time
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.forms import PluginUploadForm
from core.models import GlobalSettings, Plugin
from core.services import PluginInstallError, PluginService, plugin_updates
from core.services.plugin_connections import states_for
from core.views.decorators import superuser_required

logger = logging.getLogger(__name__)

#: Update history rows shown on a plugin's page.
UPDATE_HISTORY_ROWS = 20

# Content types for bundled plugin icons (kept in sync with the doctor's
# ALLOWED_ICON_EXTS). SVG is served only as an <img> source, never inlined.
_ICON_CONTENT_TYPES = {
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


@login_required
@superuser_required
def plugin_list_view(request: HttpRequest) -> HttpResponse:
    """List installed plugins with status, errors, and a restart banner."""
    plugins = list(Plugin.objects.all())
    # Attach the delete-confirm preview string (Plugin Platform v2): what a
    # delete with "also delete data" would really drop.
    for p in plugins:
        p.owned_summary = PluginService.owned_resource_summary(p.slug)
    context = {
        "plugins": plugins,
        "pending_restart": PluginService.pending_restart(),
        # Plugins that failed the guarded boot loader in settings (rare; the
        # preflight-on-boot normally quarantines first). Surfaced for visibility.
        "load_errors": getattr(settings, "PLUGIN_LOAD_ERRORS", {}),
        # Signing (2.5 T43): the trust policy form.
        "trust_policy": GlobalSettings.get_settings().plugin_trust_policy,
        "trust_policies": GlobalSettings.PluginTrustPolicy.choices,
        "unsigned_count": sum(1 for p in plugins if p.trust != Plugin.Trust.VERIFIED),
    }
    return render(request, "cpanel/plugins/list.html", context)


@login_required
@superuser_required
@require_POST
def plugin_trust_policy_view(request: HttpRequest) -> HttpResponse:
    """Set the plugin trust policy: allow unsigned plugins, or signed only (plan D10)."""
    policy = request.POST.get("policy", "")
    if policy not in GlobalSettings.PluginTrustPolicy.values:
        messages.error(request, "Unknown trust policy.")
        return redirect("cpanel:plugin_list")
    global_settings = GlobalSettings.get_settings()
    global_settings.plugin_trust_policy = policy
    global_settings.save(update_fields=["plugin_trust_policy", "updated_at"])
    logger.warning("plugins.trust_policy %s by user=%s", policy, request.user.pk)
    if policy == GlobalSettings.PluginTrustPolicy.SIGNED_ONLY:
        messages.success(
            request,
            "Only signed plugins can now be installed, updated or activated. "
            "Plugins already active keep running.",
        )
    else:
        messages.success(request, "Unsigned plugins can be installed again.")
    return redirect("cpanel:plugin_list")


@login_required
@superuser_required
def plugin_detail_view(request: HttpRequest, slug: str) -> HttpResponse:
    """Plugin detail page — the local analogue of a marketplace listing.

    Surfaces the packaged metadata (icon, author, license, links, categories,
    description, declared provisions) plus lifecycle actions. Reads from disk via
    the manifest accessors, so it works for INSTALLED-but-not-active plugins too.
    """
    plugin = get_object_or_404(Plugin, slug=slug)
    plugin.owned_summary = PluginService.owned_resource_summary(plugin.slug)
    context = {
        "plugin": plugin,
        "pending_restart": PluginService.pending_restart(),
        # In-place updates (2.5 T42): history, newest first, and the rollback.
        "updates": list(plugin.updates.select_related("user")[:UPDATE_HISTORY_ROWS]),
        "rollback_problem": plugin_updates.rollback_problem(plugin) if plugin.previous_version else None,
        # requires_connections (T25), with their status in this workspace.
        "connection_requirements": states_for(request.workspace, plugin.slug, plugin.manifest),
    }
    return render(request, "cpanel/plugins/detail.html", context)


@login_required
@superuser_required
def plugin_icon_view(request: HttpRequest, slug: str) -> HttpResponse:
    """Serve a plugin's bundled icon straight from PLUGINS_DIR.

    Reads the file from disk (not staticfiles), so an INSTALLED-but-not-active
    plugin's icon still renders and there is no network dependency. The icon path
    comes from the manifest and is re-validated here to stay inside the plugin
    folder (defense-in-depth alongside the doctor's static check).
    """
    plugin = get_object_or_404(Plugin, slug=slug)
    icon_rel = plugin.manifest_value("icon")
    if not icon_rel:
        raise Http404("Plugin has no icon.")

    folder = (Path(settings.PLUGINS_DIR) / plugin.slug).resolve()
    target = (folder / str(icon_rel)).resolve()
    # Path-traversal guard: the resolved file must live under the plugin folder.
    if folder != target and folder not in target.parents:
        raise Http404("Icon path escapes the plugin folder.")
    ext = target.suffix.lower()
    if ext not in _ICON_CONTENT_TYPES or not target.is_file():
        raise Http404("Icon not found.")

    response = FileResponse(open(target, "rb"), content_type=_ICON_CONTENT_TYPES[ext])
    response["Cache-Control"] = "private, max-age=300"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
@superuser_required
def plugin_upload_view(request: HttpRequest) -> HttpResponse:
    """Upload + install a plugin .zip (does not load/activate it)."""
    if request.method == "POST":
        form = PluginUploadForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                plugin, update, warnings = PluginService.upload(
                    form.cleaned_data["plugin_file"], user=request.user
                )
            except PluginInstallError as exc:
                messages.error(request, f"Upload failed: {exc}")
                return render(request, "cpanel/plugins/upload.html", {"form": form})
            except Exception:
                logger.exception("Unexpected plugin install error")
                messages.error(request, "Upload failed unexpectedly. Check the logs.")
                return render(request, "cpanel/plugins/upload.html", {"form": form})

            if update is not None:
                messages.success(request, _updated_message(plugin, update))
                if warnings.strip():
                    messages.warning(request, f'Doctor warnings for "{plugin.name}":\n{warnings}')
                return redirect("cpanel:plugin_detail", slug=plugin.slug)

            provisions = plugin.provisions_summary
            provision_note = f" It will create {provisions}." if provisions else ""
            messages.success(
                request,
                f'Plugin "{plugin.name}" installed.{provision_note} '
                "Click Activate to validate and enable it.",
            )
            return redirect("cpanel:plugin_detail", slug=plugin.slug)
    else:
        form = PluginUploadForm()
    return render(request, "cpanel/plugins/upload.html", {"form": form})


@login_required
@superuser_required
@require_POST
def plugin_activate_view(request: HttpRequest, pk) -> HttpResponse:
    """Validate the plugin in isolation; on success mark it ACTIVE."""
    plugin = get_object_or_404(Plugin, pk=pk)
    ok, output = PluginService.activate(plugin)
    if ok:
        messages.success(
            request,
            f'Plugin "{plugin.name}" passed preflight and is now active. '
            "Restart to apply.",
        )
        if output.strip():  # advisory doctor warnings (non-blocking)
            messages.warning(request, f"Doctor warnings for \"{plugin.name}\":\n{output}")
    else:
        first = (output or "").strip().splitlines()
        detail = first[-1] if first else "see plugin error for details"
        messages.error(request, f'Activation failed for "{plugin.name}": {detail}')
    return redirect("cpanel:plugin_list")


@login_required
@superuser_required
@require_POST
def plugin_deactivate_view(request: HttpRequest, pk) -> HttpResponse:
    """Deactivate a plugin (data preserved)."""
    plugin = get_object_or_404(Plugin, pk=pk)
    PluginService.deactivate(plugin)
    messages.success(
        request, f'Plugin "{plugin.name}" deactivated. Restart to apply.'
    )
    return redirect("cpanel:plugin_list")


@login_required
@superuser_required
@require_POST
def plugin_delete_view(request: HttpRequest, pk) -> HttpResponse:
    """Delete a plugin's files + row (optionally its data)."""
    plugin = get_object_or_404(Plugin, pk=pk)
    name = plugin.name
    remove_data = request.POST.get("remove_data") == "on"
    warnings = PluginService.delete(plugin, remove_data=remove_data)
    for w in warnings:
        messages.warning(request, w)
    messages.success(request, f'Plugin "{name}" deleted. Restart to apply.')
    return redirect("cpanel:plugin_list")


def _updated_message(plugin, update) -> str:
    text = f'Plugin "{plugin.name}" updated from {update.from_version} to {update.to_version}.'
    if plugin.status == Plugin.Status.ACTIVE:
        return text + " Restart to load the new version; its update steps run after the restart."
    return text + " Its update steps run after the next restart while it is active."


@login_required
@superuser_required
@require_POST
def plugin_rollback_view(request: HttpRequest, pk) -> HttpResponse:
    """Put the previous version of a plugin back (2.5 T42)."""
    plugin = get_object_or_404(Plugin, pk=pk)
    try:
        row = PluginService.rollback(plugin, user=request.user)
    except PluginInstallError as exc:
        messages.error(request, f"Rollback failed: {exc}")
        return redirect("cpanel:plugin_detail", slug=plugin.slug)
    note = " Restart to load it." if plugin.status == Plugin.Status.ACTIVE else ""
    messages.success(
        request, f'Plugin "{plugin.name}" rolled back from {row.from_version} to {row.to_version}.{note}'
    )
    return redirect("cpanel:plugin_detail", slug=plugin.slug)


@login_required
@superuser_required
@require_POST
def plugin_restart_view(request: HttpRequest) -> HttpResponse:
    """Trigger a controlled restart, then redirect to the interstitial.

    Post/Redirect/Get: the 'restarting…' page lives at a GET URL, so a manual
    refresh during the wait can never re-POST and re-trigger another restart.
    ``since`` (the request time) travels in the URL so the page — and a refresh
    of it — waits for a process that started after this restart was asked for.
    """
    requested_at = time.time()
    PluginService.trigger_restart()
    return redirect(f"{reverse('cpanel:plugin_restarting')}?since={requested_at:.3f}")


def _since(request: HttpRequest) -> float:
    """The ``since`` query value as a finite float; 0.0 when absent or bad."""
    try:
        value = float(request.GET.get("since", ""))
    except ValueError:
        return 0.0
    return value if math.isfinite(value) else 0.0


@login_required
@superuser_required
def plugin_restarting_view(request: HttpRequest) -> HttpResponse:
    """The 'restarting…' interstitial (GET → safe to refresh)."""
    return render(request, "cpanel/plugins/restarting.html", {"since": f"{_since(request):.3f}"})


@login_required
def plugin_restart_status_view(request: HttpRequest) -> JsonResponse:
    """JSON for the 'restarting…' page: has the restarted process answered?

    The restart is a SIGTERM sent a few seconds after the request, and the old
    process keeps serving until then, so "the server answered" is not enough:
    ``restarted`` is true only when the answering process loaded its plugins
    after ``since``.
    """
    if not request.user.is_superuser:
        return JsonResponse({"error": "Superuser access required."}, status=403)
    loaded_at = float(getattr(settings, "PLUGINS_LOADED_AT", 0.0))
    response = JsonResponse({
        "restarted": loaded_at > _since(request),
        "pending_restart": PluginService.pending_restart(),
    })
    response["Cache-Control"] = "no-store"
    return response
