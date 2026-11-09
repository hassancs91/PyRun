"""Scripts as MCP tools (2.5 Stage 2, T14).

A script with ``expose_as_tool`` on is listed by MCP ``tools/list`` under its
``tool_name``, with its inputs schema (as stored, in declared order) as the
tool's ``inputSchema``, to a token that holds ``scripts:run`` and may see the
script: the token's workspace, and its script binding. Calling the tool is
``scripts.run`` with ``wait: true`` through the registry, so the inputs check,
the 30-a-minute limit (a tool call counts as a ``scripts.run`` call), the wait
cap, maintenance windows, the concurrency policy, the audit row and the reply
are the same. A run that outlasts the wait comes back with its ``run_id`` and a
``next`` hint to poll ``runs.get``.

Script tools sit next to the registry's tools without clashing: a registry tool
is always ``<namespace>-<name>`` (it has a hyphen) and a tool name has none.
They are MCP only; REST callers use ``scripts.run`` with the script id, and
``_catalog`` lists them under ``script_tools`` with ``kind: "script"``.
"""

import copy
import re

from django.utils.text import slugify

from core.models import Script

RUN_SLUG = "scripts.run"
NAME_RE = re.compile(Script.TOOL_NAME_PATTERN)
# A script that declares no inputs takes none (scripts.run refuses any). MCP
# requires an object schema, so this, not a bare {}.
NO_INPUTS = {"type": "object", "properties": {}, "additionalProperties": False}


def run_permission():
    """The permission a script tool needs (scripts.run's), or None without it."""
    from .registry import registry

    spec = registry.get(RUN_SLUG)
    return spec.permission if spec is not None else None


def default_name(script_name):
    """A tool name made from a script name ("Send report!" -> send_report), or ""."""
    name = slugify(script_name or "").replace("-", "_")
    if name and not name[0].isalpha():
        name = f"script_{name}"
    return name[: Script.TOOL_NAME_MAX_LENGTH].rstrip("_")


def name_holder(workspace_id, name, exclude_pk=None):
    """The exposed script in this workspace that already uses ``name``, or None."""
    holders = Script.objects.filter(workspace_id=workspace_id, expose_as_tool=True, tool_name=name)
    if exclude_pk is not None:
        holders = holders.exclude(pk=exclude_pk)
    return holders.only("id", "name", "archived_at").first()


def restored_tool(data, workspace_id, taken):
    """``(expose_as_tool, tool_name)`` for a script a backup restore creates.

    Keeps what the editor would accept: a malformed name is dropped. A name
    another restored script in the same workspace already exposes leaves this
    one unexposed (the first, by creation, keeps it), since two backup
    workspaces can fold into one on restore. ``taken`` collects the
    ``(workspace_id, name)`` pairs exposed so far.
    """
    name = data.get("tool_name") or ""
    if (
        not isinstance(name, str)
        or len(name) > Script.TOOL_NAME_MAX_LENGTH
        or not NAME_RE.fullmatch(name)
    ):
        return False, ""
    expose = data.get("expose_as_tool") is True and (workspace_id, name) not in taken
    if expose:
        taken.add((workspace_id, name))
    return expose, name


def exposed(ctx, *, listed=False):
    """The exposed scripts this caller may run, by tool name.

    ``listed``: only the ones ``tools/list`` shows (enabled). A call still finds
    a disabled one, so the model is told ``script_disabled`` instead of
    "unknown tool" when a script is switched off mid-session.
    """
    from .endpoints.scripts import scripts_for

    permission = run_permission()
    if ctx.workspace is None or permission is None or permission not in ctx.permissions:
        return Script.objects.none()
    scripts = scripts_for(ctx).filter(expose_as_tool=True)
    if listed:
        scripts = scripts.filter(is_enabled=True)
    return scripts.only(
        "id", "name", "description", "inputs_schema", "tool_name", "is_enabled"
    ).order_by("tool_name")


def find(ctx, name):
    """The exposed script a ``tools/call`` names, or None."""
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        return None
    return exposed(ctx).filter(tool_name=name).first()


def input_schema(script):
    schema = script.inputs_schema if isinstance(script.inputs_schema, dict) else None
    return copy.deepcopy(schema or NO_INPUTS)


def description(script):
    return script.description.strip() or f"Runs the PyRun script “{script.name}”."


def mcp_tool(script):
    """The ``tools/list`` entry."""
    return {
        "name": script.tool_name,
        "title": script.name,
        "description": description(script),
        "inputSchema": input_schema(script),
        "annotations": {"readOnlyHint": False},
    }


def catalog_entry(script):
    """The ``_catalog`` entry: what the tool is and how REST runs the same script."""
    return {
        "kind": "script",
        "mcp_name": script.tool_name,
        "title": script.name,
        "description": description(script),
        "inputSchema": input_schema(script),
        "script": str(script.id),
        "rest": RUN_SLUG,
        "access": "run",
        "permission": run_permission(),
    }


def call(ctx, script, arguments):
    """Run the tool: ``scripts.run`` with wait. Raises ApiError like any endpoint."""
    from .registry import registry

    return registry.call(
        RUN_SLUG, ctx, {"script": str(script.id), "inputs": arguments, "wait": True}
    )
