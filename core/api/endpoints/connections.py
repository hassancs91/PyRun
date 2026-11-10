"""Connections over REST, MCP and Py AI (2.5 Stage 5, T23).

``connections.list`` (``connections:read``): the workspace's connections and
the scripts each one is granted to, never a key, token or password (a failed
test's error is masked with the connection's own values). Console Py AI gets it;
chat channels don't (plan D9 keeps credentials metadata, like secret names, on
the console). Like ``secrets.names``, a token bound to one script narrows only
the scripts named, not the connections.
"""

from core.api.registry import endpoint
from core.integrations.presets import get_preset
from core.models import Connection
from core.services.connections import granted_connections, masks_for, scripts_using

from .inventory import MAX_SCRIPTS_PER_SECRET, script_ref
from .scripts import iso, resolve_script, scripts_for


def _error(connection):
    from core.executor import mask_secrets_in_value

    if not connection.last_error:
        return None
    return mask_secrets_in_value(connection.last_error, masks_for(connection))


@endpoint(
    "connections.list",
    title="List connections",
    description=(
        "Connections in this workspace (accounts on outside services scripts call through "
        "pyrunner_connections), never their keys, tokens or passwords: provider, sign-in "
        "kind, account, status and last test, and the scripts each is granted to. Pass "
        "script (its id) for the connections that one script may use."
    ),
    input={
        "type": "object",
        "properties": {
            "script": {"type": "string", "maxLength": 64,
                       "description": "A script id: only the connections it may use"},
        },
    },
    permission="connections:read",
)
def list_connections(ctx, params):
    target = resolve_script(ctx, params["script"]) if "script" in params else None
    visible = set(scripts_for(ctx).values_list("pk", flat=True))
    rows = (
        granted_connections(target) if target is not None
        else Connection.objects.filter(workspace=ctx.workspace).order_by("name")
    )
    entries = []
    for connection in rows:
        preset = get_preset(connection.provider)
        using = [s for s in scripts_using(connection) if s.pk in visible]
        entries.append({
            "name": connection.name,
            "provider": connection.provider,
            "provider_label": preset.label if preset else connection.provider,
            "auth_kind": connection.auth_kind,
            "status": connection.status,
            "account_label": connection.account_label or None,
            "last_tested_at": iso(connection.last_tested_at),
            "last_error": _error(connection),
            "grant_all_scripts": connection.grant_all_scripts,
            "owner_plugin": connection.owner_plugin or None,
            "scripts": [script_ref(s) for s in using[:MAX_SCRIPTS_PER_SECRET]],
            "script_count": len(using),
        })
    result = {"connections": entries, "count": len(entries)}
    if target is not None:
        result["script"] = script_ref(target)
    return result
