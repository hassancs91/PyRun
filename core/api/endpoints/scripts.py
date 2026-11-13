"""Scripts over REST and MCP (2.5 Stage 2): metadata, ``scripts.run``; code apart.

``scripts.list`` and ``scripts.get`` never return code. ``scripts.code`` (2.5
Stage 3, T18) does, under its own permission (``scripts:read_code``), with the
values of the secrets the script gets masked, and only for the console's Py AI,
never a chat channel's.

A token bound to one script (``APIToken.script``) sees that script alone, here
and in ``runs.*``. Every lookup is scoped to the token's workspace, and an id
that is unknown, in another workspace or outside the binding gets the same 404.
"""

import uuid

from django.db.models import Q

from core.api.errors import ApiError
from core.api.registry import endpoint
from core.models import Script, ScriptSchedule


def iso(value):
    return value.isoformat() if value is not None else None


def unknown_script():
    return ApiError("not_found", "Unknown script.", status=404)


def scripts_for(ctx, *, archived=False):
    """The scripts this caller may see: its workspace's, or its bound script.

    ``archived=True`` keeps archived scripts too (their runs stay readable,
    as on the console's Runs page); listings and ``scripts.get`` leave them out.
    """
    scripts = Script.objects.for_workspace(ctx.workspace).defer("code")
    if not archived:
        scripts = scripts.filter(archived_at__isnull=True)
    if ctx.script_id is not None:
        scripts = scripts.filter(pk=ctx.script_id)
    return scripts


def parse_id(value):
    """A script or run id from the caller, or None when it isn't one."""
    try:
        return uuid.UUID(value)
    except (TypeError, ValueError, AttributeError):
        return None


def resolve_script(ctx, value, *, archived=False):
    """The visible script ``value`` names; a bound token may leave it out."""
    scripts = scripts_for(ctx, archived=archived)
    if value is None:
        if ctx.script_id is None:
            raise ApiError("invalid_input", "$.script: pass the script id.", status=400)
        script = scripts.first()
    else:
        pk = parse_id(value)
        script = scripts.filter(pk=pk).first() if pk is not None else None
    if script is None:
        raise unknown_script()
    return script


def summary(script):
    schema = script.inputs_schema if isinstance(script.inputs_schema, dict) else {}
    return {
        "id": str(script.id),
        "name": script.name,
        "description": script.description,
        "enabled": script.is_enabled,
        "tags": sorted(tag.name for tag in script.tags.all()),
        "owner_plugin": script.owner_plugin or None,
        "has_inputs": bool(schema.get("properties")),
        "updated_at": iso(script.updated_at),
    }


def schedule_summary(script):
    try:
        schedule = script.schedule
    except ScriptSchedule.DoesNotExist:
        return {"mode": "manual", "active": False, "summary": "Manual", "timezone": None,
                "next_run": None}
    return {
        "mode": schedule.run_mode,
        "active": schedule.is_scheduled,
        "summary": schedule.schedule_display,
        "timezone": schedule.timezone,
        "next_run": iso(schedule.next_run) if schedule.is_scheduled else None,
    }


@endpoint(
    "scripts.list",
    title="List scripts",
    description=(
        "Scripts in this workspace (or the one script this token is bound to), "
        "by name. Archived scripts are left out. No code."
    ),
    input={
        "type": "object",
        "properties": {
            "q": {"type": "string", "maxLength": 200,
                  "description": "Text to find in the name or description"},
            "tag": {"type": "string", "maxLength": 100, "description": "Tag name"},
            "enabled": {"type": "boolean"},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        },
    },
    permission="scripts:read",
    pyai_tier="channel",
)
def list_scripts(ctx, params):
    offset, limit = params.get("offset", 0), params.get("limit", 100)
    scripts = scripts_for(ctx)
    q = params.get("q", "").strip()
    if q:
        scripts = scripts.filter(Q(name__icontains=q) | Q(description__icontains=q))
    if params.get("tag", "").strip():
        # A subquery, not a join: no duplicate rows, no DISTINCT over JSON columns.
        tagged = Script.objects.filter(tags__name__iexact=params["tag"].strip()).values("pk")
        scripts = scripts.filter(pk__in=tagged)
    if "enabled" in params:
        scripts = scripts.filter(is_enabled=params["enabled"])
    rows = list(
        scripts.order_by("name", "id").prefetch_related("tags")[offset : offset + limit + 1]
    )
    return {
        "scripts": [summary(s) for s in rows[:limit]],
        "next_offset": offset + limit if len(rows) > limit else None,
    }


@endpoint(
    "scripts.get",
    title="Get script",
    description=(
        "One script's metadata: inputs schema, schedule and last run. Never its code. "
        "A token bound to one script may leave out `script`."
    ),
    input={
        "type": "object",
        "properties": {"script": {"type": "string", "maxLength": 64, "description": "Script id"}},
    },
    permission="scripts:read",
    pyai_tier="channel",
)
def get_script(ctx, params):
    script = resolve_script(ctx, params.get("script"))
    last = script.runs.only("id", "status", "trigger_type", "created_at", "ended_at").order_by(
        "-created_at"
    ).first()
    return {
        **summary(script),
        "inputs_schema": script.inputs_schema if isinstance(script.inputs_schema, dict) else {},
        "timeout_seconds": script.timeout_seconds,
        "concurrency_policy": script.concurrency_policy,
        "schedule": schedule_summary(script),
        "last_run": None if last is None else {
            "id": str(last.id),
            "status": last.status,
            "trigger_type": last.trigger_type,
            "created_at": iso(last.created_at),
            "ended_at": iso(last.ended_at),
        },
        "created_at": iso(script.created_at),
    }


@endpoint(
    "scripts.code",
    title="Get script code",
    description=(
        "One script's current code, as saved in the editor, with the values of the "
        "secrets it gets masked as [NAME:MASKED]. A token bound to one script may "
        "leave out `script`."
    ),
    input={
        "type": "object",
        "properties": {"script": {"type": "string", "maxLength": 64, "description": "Script id"}},
    },
    permission="scripts:read_code",
)
def script_code(ctx, params):
    from core.executor import mask_run_value
    from core.models import Run
    from core.services.secret_backends import SecretResolutionError

    script = resolve_script(ctx, params.get("script"))
    code, withheld = script.code, []
    # The mask set a run of this script would get (its secrets and the AI
    # credential), through the run page's helper; nothing is saved.
    unsaved_run = Run(script=script, workspace_id=script.workspace_id)
    try:
        code = mask_run_value(unsaved_run, code)
    except SecretResolutionError:
        code = None
        withheld.append("code")
    return {
        "id": str(script.id),
        "name": script.name,
        "code": code,
        "lines": None if code is None else len(script.code.splitlines()),
        "updated_at": iso(script.updated_at),
        "withheld": withheld,
    }


@endpoint(
    "scripts.run",
    title="Run script",
    description=(
        "Start a script, with `inputs` checked against its inputs schema. Returns the run id "
        "and status at once, or with `wait` waits up to `wait_seconds` (at most 55) and returns "
        "the result when the run ends in time. A run still going comes back with "
        "is_finished false: poll runs.get with its run_id. "
        "A token bound to one script may leave out `script`."
    ),
    input={
        "type": "object",
        "properties": {
            "script": {"type": "string", "maxLength": 64, "description": "Script id"},
            "inputs": {"type": "object", "additionalProperties": True,
                       "description": "Values for the script's declared inputs"},
            "wait": {"type": "boolean", "description": "Wait for the run to end (default false)"},
            "wait_seconds": {"type": "integer", "minimum": 1, "maximum": 55,
                             "description": "How long to wait, with wait (default and most 55)"},
        },
    },
    access="run",
    permission="scripts:run",
    rate=(30, 60),
    done="is_finished",
)
def run_script(ctx, params):
    from contextlib import ExitStack

    from django.utils import timezone

    from core.models import Run
    from core.services import run_wait
    from core.services.maintenance import blocked_message
    from core.services.run_events import api_event
    from core.services.script_inputs import ScriptInputsError, prepare_inputs
    from core.tasks import queue_script_run
    from .runs import run_reply

    wait = params.get("wait", False)
    if "wait_seconds" in params and not wait:
        raise ApiError("invalid_input", "$.wait_seconds: only with wait: true.")
    script = resolve_script(ctx, params.get("script"))
    if not script.is_enabled:
        raise ApiError("script_disabled", "This script is disabled.", status=409)
    try:
        inputs = prepare_inputs(script, params.get("inputs"))
    except ScriptInputsError as exc:
        raise ApiError("invalid_input", exc.message) from None

    with ExitStack() as stack:
        waiter = None
        if wait:
            seconds = run_wait.budget(params.get("wait_seconds"), ctx.remaining_seconds())
            try:
                # Before the run exists: with no worker alive, no run is created.
                waiter = stack.enter_context(run_wait.reserve(seconds))
            except run_wait.WorkersUnavailable as exc:
                raise ApiError(
                    "workers_unavailable", exc.message, status=503, retry_after=exc.retry_after
                ) from None
        run = Run.objects.create(
            script=script,
            workspace_id=script.workspace_id,
            status=Run.Status.PENDING,
            trigger_type=Run.TriggerType.API,
            code_snapshot=script.code,
            inputs=inputs,
            event=api_event(ctx.principal_id, ctx.transport),
        )
        try:
            queue_script_run(run)
        except ScriptInputsError as exc:
            # The schema changed since the check above; the row is gone.
            raise ApiError("invalid_input", exc.message) from None
        except Exception:
            Run.objects.filter(pk=run.pk, status=Run.Status.PENDING).update(
                status=Run.Status.FAILED,
                ended_at=timezone.now(),
                stderr=Run.QUEUE_FAILED_TEXT,
            )
            raise
        if run.is_maintenance_skipped:
            # As for a webhook: 503 + Retry-After; the run is listed as skipped.
            block = run.maintenance_block
            raise ApiError(
                "maintenance",
                blocked_message(run, block),
                status=503,
                retry_after=block.retry_after(),
            )
        waited = waiter.wait(run) if waiter is not None else None
    return run_reply(run, waited)
