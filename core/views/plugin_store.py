"""
Plugin Store views (control panel, superuser only; 2.5 T44).

Thin HTTP layer over core/services/plugin_store.py: the Store tab (browse,
search, categories, updates available), an entry's page, Refresh, Store
settings (the index URL), Install / Update from the store, and Install from
URL. Pages only read the cached, verified index; the network is used on
Refresh, on saving a new index URL, and when installing.
"""

import logging
from urllib.parse import urlsplit

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from core.models import GlobalSettings
from core.netguard import is_safe_endpoint_url
from core.plugins.trust.index import SHA256_RE
from core.services import PluginInstallError, PluginService, plugin_store
from core.views.decorators import superuser_required
from core.views.plugins import installed_message, updated_message

logger = logging.getLogger(__name__)

_URL_MAX = 500


def _matches(item, q, category) -> bool:
    entry = item.entry
    if category and category not in entry.categories:
        return False
    if not q:
        return True
    haystack = " ".join((entry.name, entry.slug, entry.summary, entry.description, " ".join(entry.categories)))
    return q.lower() in haystack.lower()


@login_required
@superuser_required
def plugin_store_view(request: HttpRequest) -> HttpResponse:
    """The Store tab: what the signed index offers, next to what is installed."""
    state = plugin_store.state()
    all_items = plugin_store.items(state.index) if state.index else []
    q = request.GET.get("q", "").strip()[:100]
    category = request.GET.get("category", "").strip()[:50]
    context = {
        "state": state,
        "items": [i for i in all_items if _matches(i, q, category)],
        "updates": [i for i in all_items if i.action == plugin_store.StoreItem.UPDATE],
        "store_updates": sum(1 for i in all_items if i.action == plugin_store.StoreItem.UPDATE),
        "total": len(all_items),
        "categories": state.index.categories if state.index else [],
        "q": q,
        "category": category,
        "configured_url": GlobalSettings.get_settings().plugin_store_url,
        "official_url": plugin_store.OFFICIAL_INDEX_URL,
        "pending_restart": PluginService.pending_restart(),
    }
    return render(request, "cpanel/plugins/store.html", context)


@login_required
@superuser_required
def plugin_store_detail_view(request: HttpRequest, slug: str) -> HttpResponse:
    """One store entry: description, what it creates, changelog link, Install / Update."""
    state = plugin_store.state()
    entry = state.index.get(slug) if state.index else None
    if entry is None:
        messages.error(request, f"'{slug}' is not in the store index.")
        return redirect("cpanel:plugin_store")
    from core.models import Plugin

    item = plugin_store.item_for(entry, Plugin.objects.filter(slug=slug).first())
    return render(request, "cpanel/plugins/store_detail.html", {"state": state, "item": item})


@login_required
@superuser_required
@require_POST
def plugin_store_refresh_view(request: HttpRequest) -> HttpResponse:
    """Fetch the index now instead of waiting for the 12-hour refresh."""
    try:
        index = plugin_store.refresh()
    except plugin_store.StoreError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Store index loaded: {len(index.entries)} plugins.")
    return redirect("cpanel:plugin_store")


def _http_url_problem(url) -> str:
    """'' for an http(s) URL with a host, else what to fix.

    A single-label host (``http://store:8080/index.json``, a Docker network
    name) is fine here (2.5 T52); whether the address may be reached at all
    is the SSRF guard's call (``is_safe_endpoint_url``), as for any URL.
    """
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError:
        return "Enter an http:// or https:// URL."
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname or any(c.isspace() for c in url):
        return "Enter an http:// or https:// URL."
    return ""


@login_required
@superuser_required
@require_POST
def plugin_store_settings_view(request: HttpRequest) -> HttpResponse:
    """Set the store index URL (blank: PyRun's official index), then load it."""
    url = request.POST.get("store_url", "").strip()
    if url:
        problem = ""
        if len(url) > _URL_MAX:
            problem = f"The URL is longer than {_URL_MAX} characters."
        else:
            problem = _http_url_problem(url)
        if not problem:
            ok, why = is_safe_endpoint_url(url)
            problem = "" if ok else why
        if problem:
            messages.error(request, f"Store index URL not saved: {problem}")
            return redirect("cpanel:plugin_store")
    settings_obj = GlobalSettings.get_settings()
    settings_obj.plugin_store_url = url
    settings_obj.save(update_fields=["plugin_store_url", "updated_at"])
    logger.warning("plugins.store_url %s by user=%s", url or "(official)", request.user.pk)
    if not plugin_store.index_url():
        messages.success(request, "Store index URL cleared.")
        return redirect("cpanel:plugin_store")
    try:
        index = plugin_store.refresh()
    except plugin_store.StoreError as exc:
        messages.warning(request, f"Store index URL saved. {exc}")
    else:
        messages.success(request, f"Store index URL saved: {len(index.entries)} plugins.")
    return redirect("cpanel:plugin_store")


def _done(request, plugin, update, warnings):
    if update is not None:
        messages.success(request, updated_message(plugin, update))
        if warnings.strip():
            messages.warning(request, f'Doctor warnings for "{plugin.name}":\n{warnings}')
    else:
        messages.success(request, installed_message(plugin))
    return redirect("cpanel:plugin_detail", slug=plugin.slug)


@login_required
@superuser_required
@require_POST
def plugin_store_install_view(request: HttpRequest, slug: str) -> HttpResponse:
    """Install a store plugin, or update the installed one to the store's version."""
    try:
        plugin, update, warnings = plugin_store.install_from_store(slug, user=request.user)
    except PluginInstallError as exc:
        messages.error(request, f"Install failed: {exc}")
        return redirect("cpanel:plugin_store_detail", slug=slug)
    except Exception:
        logger.exception("Unexpected plugin store install error")
        messages.error(request, "Install failed unexpectedly. Check the logs.")
        return redirect("cpanel:plugin_store_detail", slug=slug)
    return _done(request, plugin, update, warnings)


@login_required
@superuser_required
@require_POST
def plugin_install_url_view(request: HttpRequest) -> HttpResponse:
    """Install (or update) a plugin from a zip URL: SSRF guard, 50 MB cap, optional sha256."""
    url = request.POST.get("url", "").strip()
    sha256 = request.POST.get("sha256", "").strip()
    problem = ""
    if not url:
        problem = "Enter the zip's URL."
    elif len(url) > 2000:
        problem = "The URL is too long."
    elif sha256 and not SHA256_RE.match(sha256.lower()):
        problem = "The sha256 must be 64 hexadecimal characters (or leave it empty)."
    if problem:
        messages.error(request, f"Install failed: {problem}")
        return redirect("cpanel:plugin_upload")
    try:
        plugin, update, warnings = plugin_store.install_from_url(url, sha256=sha256, user=request.user)
    except PluginInstallError as exc:
        messages.error(request, f"Install failed: {exc}")
        return redirect("cpanel:plugin_upload")
    except Exception:
        logger.exception("Unexpected plugin install-from-URL error")
        messages.error(request, "Install failed unexpectedly. Check the logs.")
        return redirect("cpanel:plugin_upload")
    return _done(request, plugin, update, warnings)
