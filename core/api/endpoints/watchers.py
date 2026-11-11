"""
``watchers.list`` (``watchers:read``): the workspace's watchers (2.5 Stage 7, T31).

A watcher's settings, state and input map are never returned: a polled URL
can carry a key in its query string. Neither is its last error text (the
console shows it), as for ``system.health``: only how many checks in a row
failed. A token bound to one script sees that script's watchers.
"""

from core.api.errors import ApiError
from core.api.registry import endpoint
from core.models import Watcher
from core.triggers import registry as adapters

from .scripts import iso, parse_id, scripts_for


def _row(watcher):
    return {
        "id": str(watcher.pk),
        "name": watcher.name,
        "adapter": {"key": watcher.adapter, "title": adapters.title(watcher.adapter)},
        "script": {"id": str(watcher.script_id), "name": watcher.script.name},
        "connection": watcher.connection.name if watcher.connection_id else None,
        "interval_seconds": watcher.interval_seconds,
        "fire_cap_per_hour": watcher.fire_cap_per_hour,
        "enabled": watcher.enabled,
        "auto_disabled_at": iso(watcher.auto_disabled_at),
        "last_checked_at": iso(watcher.last_checked_at),
        "last_event_at": iso(watcher.last_event_at),
        "next_check_at": iso(watcher.next_check_at) if watcher.enabled else None,
        "error_count": watcher.error_count,
        "owner_plugin": watcher.owner_plugin,
    }


@endpoint(
    "watchers.list",
    title="List watchers",
    description=(
        "Watchers in this workspace: triggers that check an outside source on an interval "
        "and start a run of their script per new item. Each has its adapter, script, "
        "interval, whether it is on (auto_disabled_at says it turned itself off after "
        "failing), when it last checked and last started a run, and how many checks in a "
        "row failed. Settings and error texts aren't returned."
    ),
    input={
        "type": "object",
        "properties": {
            "script": {"type": "string", "maxLength": 64, "description": "Only this script's watchers (its id)"},
            "enabled": {"type": "boolean", "description": "Only watchers that are on (or off)"},
        },
    },
    permission="watchers:read",
    pyai_tier="channel",
)
def list_watchers(ctx, params):
    rows = Watcher.objects.filter(
        workspace=ctx.workspace, script__in=scripts_for(ctx, archived=True),
    ).select_related("script", "connection").defer("script__code", "config", "state", "recent_events")
    if "script" in params:
        pk = parse_id(params["script"])
        if pk is None or not scripts_for(ctx, archived=True).filter(pk=pk).exists():
            raise ApiError("not_found", "Unknown script.", status=404)
        rows = rows.filter(script_id=pk)
    if "enabled" in params:
        rows = rows.filter(enabled=params["enabled"])
    entries = [_row(w) for w in rows.order_by("name", "created_at")]
    return {"watchers": entries, "count": len(entries)}
