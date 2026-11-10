"""
A plugin page's connection buttons (2.5 T25): Connect, use an existing connection, stop using it.

A plugin declares the accounts it needs in ``plugin.json``
(``requires_connections``) and its page includes
``cpanel/plugins/_connections.html``; the core plugin page shows the same card.
Its forms post here. Any member of the workspace may use them, as on the
Connections page, and only a requirement the installed plugin's manifest
declares can be acted on (the manifest is the truth, as with ``provides``).
"""

import logging
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.views.decorators.http import require_POST

from core.integrations.presets import OAUTH2
from core.models import Connection, Plugin
from core.services import EncryptionService, oauth
from core.services import plugin_connections as pc
from core.views.connections import local_url

logger = logging.getLogger(__name__)


def _requirement(slug, key):
    requirement = pc.declared_requirement(slug, key)
    if requirement is None:
        raise Http404("No such plugin connection")
    return requirement


def _back(request):
    """Back to the page the form was on (a local path), else the Connections page."""
    next_url = request.POST.get("next", "")
    if local_url(request, next_url):
        return redirect(next_url)
    return redirect("cpanel:connection_list")


def _plugin_name(slug):
    plugin = Plugin.objects.filter(slug=slug).first()
    return plugin.name if plugin and plugin.name else slug


def _merged_config(connection, requirement) -> dict:
    """The connection's scope choices plus the ones the requirement asks for."""
    config = dict(connection.config or {})
    sets, extra = requirement.split_scopes()
    config["scope_sets"] = list(dict.fromkeys(list(config.get("scope_sets") or []) + sets))
    have = str(config.get("extra_scopes") or "").replace(",", " ").split()
    config["extra_scopes"] = " ".join(dict.fromkeys(have + extra))
    return config


@login_required
@require_POST
def plugin_connection_connect_view(request: HttpRequest, slug, key) -> HttpResponse:
    """Connect (or reconnect) the account a plugin requirement needs, with OAuth."""
    requirement = _requirement(slug, key)
    workspace = request.workspace
    if workspace is None:
        messages.error(request, "Join a workspace first: connections belong to one.")
        return _back(request)
    if not EncryptionService.is_configured():
        messages.error(request, "Set ENCRYPTION_KEY before adding connections: their credentials are encrypted.")
        return _back(request)
    preset = requirement.preset
    if not requirement.is_oauth:
        messages.error(
            request,
            f"{preset.label} connections are added on the Connections page; then pick it here.",
        )
        return _back(request)
    client = oauth.client_for(workspace, preset.key)
    if client is None:
        messages.error(
            request,
            f"Add a {preset.label} OAuth app first (Connections → OAuth apps), or paste a token "
            "on the Connections page and pick that connection here.",
        )
        return _back(request)
    connection = pc.connection_for(workspace, slug, key)
    if connection is not None and connection.auth_kind != OAUTH2:
        messages.error(
            request,
            f'"{connection.name}" doesn\'t sign in with OAuth: change its credentials on its Connections page.',
        )
        return _back(request)
    if connection is None:
        name = pc.new_connection_name(workspace, slug, key)
        config = requirement.connect_config()
    else:
        name = connection.name
        config = _merged_config(connection, requirement)
        if config != connection.config:
            # Saved first, as the connection page's Reconnect saves its form.
            Connection.objects.filter(pk=connection.pk).update(config=config)
            connection.config = config
    next_url = request.POST.get("next", "")
    url = oauth.start(
        request,
        preset=preset,
        client=client,
        name=name,
        config=config,
        scopes=preset.oauth.scopes_for(config["scope_sets"], config["extra_scopes"]),
        connection=connection,
        owner=(slug, key),
        next_url=next_url if local_url(request, next_url) else "",
    )
    return redirect(url)


@login_required
@require_POST
def plugin_connection_use_view(request: HttpRequest, slug, key) -> HttpResponse:
    """Let the plugin use one of the workspace's own connections for a requirement."""
    requirement = _requirement(slug, key)
    try:
        pk = uuid.UUID(str(request.POST.get("connection", "")))
    except ValueError:
        messages.error(request, "Pick a connection.")
        return _back(request)
    connection = Connection.objects.filter(
        pk=pk, workspace=request.workspace, provider=requirement.provider
    ).first()
    if connection is None or connection.status == Connection.Status.REVOKED:
        messages.error(request, "That connection isn't available here any more.")
        return _back(request)
    try:
        pc.adopt(connection, slug, key)
    except ValueError as e:
        messages.error(request, str(e))
        return _back(request)
    messages.success(request, f'{_plugin_name(slug)} now uses "{connection.name}".')
    return _back(request)


@login_required
@require_POST
def plugin_connection_release_view(request: HttpRequest, slug, key) -> HttpResponse:
    """Stop the plugin using its connection for a requirement (the connection stays)."""
    _requirement(slug, key)
    connection = pc.release(request.workspace, slug, key)
    if connection is not None:
        messages.success(
            request,
            f'{_plugin_name(slug)} no longer uses "{connection.name}". It is still in Connections: '
            "revoke or delete it there if you don't need it.",
        )
    return _back(request)
