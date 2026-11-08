"""Workspace connector instructions; credentials are only revealed at creation."""

import uuid

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.api.context import token_context
from core.api.registry import registry
from core.models import APIToken, GlobalSettings
from core.views.decorators import superuser_required


@login_required
def connect_view(request):
    tokens = APIToken.objects.filter(workspace=request.workspace, scope="api").order_by("name")
    selected = None
    endpoints = []
    if request.GET.get("token"):
        try:
            token_id = uuid.UUID(request.GET["token"])
        except ValueError as exc:
            raise Http404("Unknown token.") from exc
        selected = get_object_or_404(tokens, pk=token_id)
        endpoints = registry.visible(token_context(selected))
    global_settings = GlobalSettings.get_settings()
    return render(
        request,
        "cpanel/connect.html",
        {
            "mcp_url": request.build_absolute_uri("/mcp"),
            "api_url": request.build_absolute_uri("/api/v1"),
            "url_tokens_enabled": global_settings.mcp_url_tokens_enabled,
            "writes_enabled": global_settings.api_writes_enabled,
            "parallel_calls": settings.MCP_PARALLEL_CALLS,
            "budget": settings.MCP_CALL_BUDGET_SECONDS,
            "tokens": tokens,
            "selected": selected,
            "endpoints": endpoints,
        },
    )


@login_required
@superuser_required
@require_POST
def url_tokens_toggle_view(request):
    enabled = request.POST.get("enabled") == "on"
    GlobalSettings.objects.filter(pk=GlobalSettings.get_settings().pk).update(
        mcp_url_tokens_enabled=enabled
    )
    messages.success(request, "MCP URL tokens enabled." if enabled else "MCP URL tokens disabled.")
    return redirect("cpanel:connect")
