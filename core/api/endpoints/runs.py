"""Runs over REST and MCP (2.5 Stage 2): status, result, masked output; ``runs.stop``.

What a token sees of a run is what the console shows, with two differences:
no code snapshot, and the person behind a manual run (or a plugin run a user
started) is ``user: {"id"}`` only. The console keeps showing the email; API
and MCP callers get the id (Hasan's decision on #12: a token, possibly bound to
one script, must not learn the emails of the people who ran it).

Inputs and the event are stored raw (the script needs the real values) and are
masked here, with the run page's helpers; when a secret can't be resolved they
are withheld and named in ``withheld``. The result and progress are stored
masked. Output goes through ``core.services.run_output``, the run page's helper.
"""

from datetime import timezone as dt_timezone

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core.api.errors import ApiError
from core.api.registry import endpoint
from core.models import Run
from core.services import run_output
from .scripts import iso, parse_id, resolve_script

RUN_FIELDS = ("id", "script_id", "status", "trigger_type", "created_at", "started_at",
              "ended_at", "exit_code", "attempt", "cancel_reason")


def runs_for(ctx):
    """The runs this caller may see: its workspace's, or its bound script's."""
    runs = Run.objects.for_workspace(ctx.workspace)
    if ctx.script_id is not None:
        runs = runs.filter(script_id=ctx.script_id)
    return runs


# Never sent to a token: the code a run executed and a held run's trigger data.
NEVER = ("code_snapshot", "queued_payload", "script__code")


def resolve_run(ctx, value, *skip):
    """The visible run ``value`` names; ``skip``: more columns not to load."""
    pk = parse_id(value)
    run = None
    if pk is not None:
        run = (
            runs_for(ctx).select_related("script").defer(*NEVER, *skip).filter(pk=pk).first()
        )
    if run is None:
        raise ApiError("not_found", "Unknown run.", status=404)
    return run


def run_summary(run):
    return {
        "id": str(run.id),
        "script": {"id": str(run.script_id), "name": run.script.name},
        "status": run.status,
        "trigger_type": run.trigger_type,
        "created_at": iso(run.created_at),
        "started_at": iso(run.started_at),
        "ended_at": iso(run.ended_at),
        "duration": run.duration,
        "exit_code": run.exit_code,
        "attempt": run.attempt,
        "cancel_reason": run.cancel_reason or "",
    }


def event_for(ctx, event):
    """The event as this caller may see it: a token gets the user's id, no email."""
    if ctx.principal == "user" or not isinstance(event, dict):
        return event
    user = event.get("user")
    if isinstance(user, dict):
        event = {**event, "user": {"id": user.get("id")}}
    return event


@endpoint(
    "runs.list",
    title="List runs",
    description=(
        "Recent runs, newest first, in this workspace (or of the script this token is "
        "bound to). Filter by script, status and a start time."
    ),
    input={
        "type": "object",
        "properties": {
            "script": {"type": "string", "maxLength": 64, "description": "Script id"},
            "status": {"type": "string", "enum": list(Run.Status.values)},
            "since": {"type": "string", "maxLength": 64,
                      "description": "ISO 8601 date-time; runs created at or after it (UTC if no zone)"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
    },
    permission="runs:read",
)
def list_runs(ctx, params):
    runs = runs_for(ctx)
    if "script" in params:
        runs = runs.filter(script=resolve_script(ctx, params["script"], archived=True))
    if "status" in params:
        runs = runs.filter(status=params["status"])
    if "since" in params:
        try:
            since = parse_datetime(params["since"].strip())
        except ValueError:  # well formed but not a real date-time (month 13)
            since = None
        if since is None:
            raise ApiError("invalid_input", "$.since: expected an ISO 8601 date-time.")
        if timezone.is_naive(since):
            since = since.replace(tzinfo=dt_timezone.utc)
        runs = runs.filter(created_at__gte=since)
    rows = (
        runs.select_related("script").only(*RUN_FIELDS, "script__id", "script__name")
        .order_by("-created_at", "-id")[: params.get("limit", 20)]
    )
    return {"runs": [run_summary(run) for run in rows]}


@endpoint(
    "runs.get",
    title="Get run",
    description=(
        "One run: status, timing, inputs, result, progress and event, secrets masked. "
        "Output is at runs.output."
    ),
    input={
        "type": "object",
        "properties": {"run": {"type": "string", "maxLength": 64, "description": "Run id"}},
        "required": ["run"],
    },
    permission="runs:read",
)
def get_run(ctx, params):
    from core.executor import mask_run_inputs, mask_run_value
    from core.services.secret_backends import SecretResolutionError

    run = resolve_run(ctx, params["run"])
    mask_sets, withheld = {}, []
    try:
        inputs = mask_run_inputs(run, mask_sets)
    except SecretResolutionError:
        inputs = None
        withheld.append("inputs")
    event = None
    if run.event is not None:
        try:
            event = mask_run_value(run, event_for(ctx, run.event), mask_sets)
        except SecretResolutionError:
            withheld.append("event")
    stdout, stderr = run_output.output_text(run, "stdout"), run_output.output_text(run, "stderr")
    return {
        **run_summary(run),
        "is_finished": run.is_finished,
        "is_held": run.is_held,
        "retry_of": str(run.retry_of_id) if run.retry_of_id else None,
        "next_attempt_at": iso(run.next_attempt_at),
        "stop_requested_at": iso(run.stop_requested_at),
        "inputs": inputs,
        "result": run.result,
        "progress": run.progress,
        "event": event,
        "withheld": withheld,
        "output": {
            name: {"chars": len(text), "truncated": run_output.is_truncated(text)}
            for name, text in (("stdout", stdout), ("stderr", stderr))
        },
    }


@endpoint(
    "runs.output",
    title="Get run output",
    description=(
        "A run's stdout or stderr, secrets masked, exactly as the run page shows it. "
        "`tail` keeps only the last bytes. Written when the run ends. "
        "Over REST the reply is text/plain."
    ),
    input={
        "type": "object",
        "properties": {
            "run": {"type": "string", "maxLength": 64, "description": "Run id"},
            "stream": {"type": "string", "enum": list(run_output.STREAMS)},
            "tail": {"type": "integer", "minimum": 1, "maximum": 1_000_000,
                     "description": "Return only the last this many bytes (UTF-8)"},
        },
        "required": ["run"],
    },
    permission="runs:output",
    text="text",
)
def get_output(ctx, params):
    stream = params.get("stream", "stdout")
    other = "stderr" if stream == "stdout" else "stdout"
    run = resolve_run(ctx, params["run"], other, "inputs", "result", "progress", "event")
    text = run_output.output_text(run, stream)
    truncated = run_output.is_truncated(text)
    cut = False
    if "tail" in params:
        text, cut = run_output.tail(text, params["tail"])
    return {
        "run": str(run.id),
        "status": run.status,
        "stream": stream,
        "text": text,
        "truncated": truncated,
        "tail_cut": cut,
    }


POLL_HINT = "The run hasn't ended yet: poll runs.get with this run_id until is_finished is true."


def run_reply(run, waited=None):
    """What ``scripts.run`` answers: the run's summary and, once it ended, its result.

    ``waited``: ``None`` without ``wait``, else what ``run_wait`` reports
    (``finished``, ``timeout``, or ``busy`` when every wait slot was taken).
    The result is stored masked, as ``runs.get`` returns it.
    """
    data = {
        "run_id": str(run.id),
        **{key: value for key, value in run_summary(run).items() if key != "id"},
        "is_finished": run.is_finished,
        "is_held": run.is_held,
        "result": run.result,
        "wait": waited,
    }
    if not run.is_finished:
        data["next"] = POLL_HINT
    return data


@endpoint(
    "runs.stop",
    title="Stop run",
    description=(
        "Stop a run. A running script gets the stop signal and its grace period to shut down: "
        "the run stays running, with stop_requested_at set, until it has ended. Calling again "
        "kills it at once. A pending run is cancelled. A run that has ended is left as it is "
        "(changed: false). In 2.5 this reaches runs on this PyRun container's workers only."
    ),
    input={
        "type": "object",
        "properties": {"run": {"type": "string", "maxLength": 64, "description": "Run id"}},
        "required": ["run"],
    },
    access="run",
    permission="runs:stop",
    rate=(30, 60),
)
def stop_run(ctx, params):
    from core.services.task_service import TaskService

    run = resolve_run(ctx, params["run"])
    # The run page's Stop and Kill now: the first call on a running run is the
    # graceful stop, a second one kills it.
    changed, message = TaskService.force_stop_run(run)
    run.refresh_from_db()
    return {
        "run_id": str(run.id),
        "changed": changed,
        "message": message,
        "status": run.status,
        "is_stopping": run.is_stopping,
        "stop_requested_at": iso(run.stop_requested_at),
        "is_finished": run.is_finished,
    }
