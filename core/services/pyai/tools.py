"""
Workspace introspection and granted Brain tools for Py AI's in-process MCP server.

Introspection wraps existing workspace-scoped reads. Brain tools recheck grants;
remember only captures a proposal for human approval. No tool runs scripts,
reveals secrets or applies note changes. ``get_run_error`` (the end of a failed
run's stderr, already secret-masked when the run was stored) is given to the
console Py AI only, which is limited to workspace owners/admins — not to chat
channels, whose approved senders need not be either. ORM access is wrapped in
``sync_to_async`` because handlers run inside the SDK's event loop.

The runtime adds the registry read endpoints (``scripts-get``, ``runs-output``,
``datastores-entries``...) through ``core.api.tools``, the bridge that tiers
them the same way: console Py AI gets them all, chat channels the safe subset.
"""

import json

from asgiref.sync import sync_to_async
from claude_agent_sdk import tool

SERVER_NAME = "pyai"


def _ok(data: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(data, default=str)}]}


# --- sync ORM readers (workspace-scoped) ------------------------------------


def _count_scripts(ws) -> dict:
    from core.models import Script

    return {"count": Script.objects.for_workspace(ws).filter(archived_at__isnull=True).count()}


def _list_scripts(ws) -> dict:
    from core.models import Script

    rows = (
        Script.objects.for_workspace(ws)
        .filter(archived_at__isnull=True)
        .order_by("name")[:100]
    )
    scripts = []
    for s in rows:
        last = s.runs.order_by("-created_at").first()
        scripts.append(
            {"name": s.name, "enabled": s.is_enabled,
             "last_run_status": last.status if last else None}
        )
    return {"scripts": scripts, "count": len(scripts)}


def _get_script(ws, name: str) -> dict:
    from core.models import Script

    qs = Script.objects.for_workspace(ws).filter(archived_at__isnull=True)
    s = qs.filter(name__iexact=name).first() or qs.filter(name__icontains=name).first()
    if s is None:
        return {"found": False, "name": name}
    last = s.runs.order_by("-created_at").first()
    return {
        "found": True,
        "name": s.name,
        "enabled": s.is_enabled,
        "description": s.description,
        "run_count": s.runs.count(),
        "last_run": (
            {"status": last.status, "at": last.created_at, "duration_s": last.duration}
            if last else None
        ),
    }


def _recent_runs(ws, limit) -> dict:
    from core.models import Run

    limit = max(1, min(int(limit or 10), 50))
    rows = (
        Run.objects.for_workspace(ws)
        .select_related("script")
        .order_by("-created_at")[:limit]
    )
    return {
        "runs": [
            {"script": r.script.name, "status": r.status, "trigger": r.trigger_type,
             "at": r.created_at, "duration_s": r.duration}
            for r in rows
        ]
    }


# How much of a failed run's stderr Py AI sees: the end, where the traceback is.
RUN_ERROR_TAIL_CHARS = 2000


def _get_run_error(ws, name: str) -> dict:
    """The most recent failed (or timed-out) run of one script: exit code + stderr tail.

    Workspace-scoped like ``_get_script``. The stored stderr was masked for the
    run's injected secrets and AI credential before it was saved, so this
    returns nothing the run page doesn't already show an owner/admin.
    """
    from core.models import Run, Script

    qs = Script.objects.for_workspace(ws).filter(archived_at__isnull=True)
    s = qs.filter(name__iexact=name).first() or qs.filter(name__icontains=name).first()
    if s is None:
        return {"found": False, "name": name}
    run = (
        s.runs.filter(status__in=[Run.Status.FAILED, Run.Status.TIMEOUT])
        .order_by("-created_at")
        .first()
    )
    if run is None:
        return {"found": True, "script": s.name, "failed_run": None}
    stderr = run.stderr or ""
    return {
        "found": True,
        "script": s.name,
        "failed_run": {
            "status": run.status,
            "at": run.created_at,
            "exit_code": run.exit_code,
            "stderr_tail": stderr[-RUN_ERROR_TAIL_CHARS:],
            "stderr_truncated": len(stderr) > RUN_ERROR_TAIL_CHARS,
        },
    }


def _list_schedules(ws) -> dict:
    from core.models import ScriptSchedule

    rows = ScriptSchedule.objects.filter(
        script__workspace=ws, is_active=True
    ).select_related("script")
    return {"schedules": [{"script": s.script.name, "mode": s.run_mode} for s in rows]}


def _list_datastores(ws) -> dict:
    from core.models import DataStore

    rows = DataStore.objects.for_workspace(ws).order_by("name")
    return {"datastores": [{"name": d.name, "entry_count": d.entry_count} for d in rows]}


def _query_datastore(ws, name: str, key) -> dict:
    from core.models import DataStore

    d = DataStore.objects.for_workspace(ws).filter(name=name).first()
    if d is None:
        return {"found": False, "name": name}
    if key:
        e = d.entries.filter(key=key).first()
        return {"found": True, "key": key, "exists": e is not None,
                "value": e.get_value() if e else None}
    entries = {e.key: e.get_value() for e in d.entries.order_by("key")[:100]}
    return {"found": True, "count": d.entries.count(), "entries": entries}


# --- tool builders (closures capture the workspace) -------------------------

_OPTIONAL_LIMIT = {"type": "object", "properties": {"limit": {"type": "integer"}}, "required": []}
_QUERY_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "key": {"type": "string"}},
    "required": ["name"],
}


def build_tools(workspace, *, agent="pyai"):
    """Return introspection tools plus the agent's currently granted Brain tools."""
    from core.brain.tools import build_tools as brain_tools

    @tool("count_scripts", "Count the scripts in this workspace.", {})
    async def count_scripts(args):
        return _ok(await sync_to_async(_count_scripts)(workspace))

    @tool("list_scripts", "List scripts with enabled state and last run status.", {})
    async def list_scripts(args):
        return _ok(await sync_to_async(_list_scripts)(workspace))

    @tool("get_script", "Get details and the last run for one script by name.", {"name": str})
    async def get_script(args):
        return _ok(await sync_to_async(_get_script)(workspace, args.get("name", "")))

    @tool("recent_runs", "List the most recent script runs (status, when, duration).", _OPTIONAL_LIMIT)
    async def recent_runs(args):
        return _ok(await sync_to_async(_recent_runs)(workspace, args.get("limit", 10)))

    @tool("list_schedules", "List active scheduled scripts and their schedule mode.", {})
    async def list_schedules(args):
        return _ok(await sync_to_async(_list_schedules)(workspace))

    @tool("list_datastores", "List datastores with their entry counts.", {})
    async def list_datastores(args):
        return _ok(await sync_to_async(_list_datastores)(workspace))

    @tool("query_datastore", "Read a datastore: all entries, or one value by key.", _QUERY_SCHEMA)
    async def query_datastore(args):
        return _ok(
            await sync_to_async(_query_datastore)(
                workspace, args.get("name", ""), args.get("key") or None
            )
        )

    tools = [count_scripts, list_scripts, get_script, recent_runs,
             list_schedules, list_datastores, query_datastore]

    if agent == "pyai":
        # Console Py AI only (owners/admins): say WHY a script failed.
        @tool(
            "get_run_error",
            "Get why a script failed: exit code and the end of stderr (the "
            "traceback) of its most recent failed or timed-out run, by script name.",
            {"name": str},
        )
        async def get_run_error(args):
            return _ok(await sync_to_async(_get_run_error)(workspace, args.get("name", "")))

        tools.append(get_run_error)

    return tools + brain_tools(workspace, agent=agent)


TOOL_NAMES = [
    "count_scripts", "list_scripts", "get_script", "recent_runs",
    "list_schedules", "list_datastores", "query_datastore",
    "get_run_error",  # console Py AI only (see build_tools)
]
ALLOWED_TOOLS = [f"mcp__{SERVER_NAME}__{n}" for n in TOOL_NAMES]
