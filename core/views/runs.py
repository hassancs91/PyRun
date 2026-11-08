"""
Run views for the control panel.
"""
import json
import uuid

from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import BooleanField, ExpressionWrapper, Q
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse

from core.models import Run, Script
from core.services.run_reports import json_size, progress_for_display, result_text

RUNS_PER_PAGE = 25
SEARCH_MAX_LENGTH = 200


@login_required
def run_list_view(request: HttpRequest) -> HttpResponse:
    """List all runs with optional filtering."""
    # Same pattern as the packages page: viewing the list reconciles runs whose
    # worker died without finalizing them, so a stuck 'running' row heals on
    # sight even when the cluster (and its every-minute heartbeat) is down.
    Run.reconcile_stale()

    runs = (
        Run.objects.for_workspace(request.workspace)
        .select_related("script", "triggered_by")
        # The list renders stderr (the inline error expander) but never stdout,
        # which is the far larger column. Deferring it keeps a page of runs small.
        # Same for the result (up to 256 KB): the list only marks that one exists.
        .defer("stdout", "result", "progress")
        .annotate(has_result=ExpressionWrapper(Q(result__isnull=False), output_field=BooleanField()))
        .order_by("-created_at")
    )

    # Optional filtering by status
    status_filter = request.GET.get("status")
    if status_filter and status_filter in dict(Run.Status.choices):
        runs = runs.filter(status=status_filter)

    # Optional filtering by script
    script_filter = request.GET.get("script")
    if script_filter:
        try:
            runs = runs.filter(script_id=uuid.UUID(script_filter))
        except ValueError:
            runs = runs.none()  # not a script id: no runs match

    # Text search: script name + stderr by default (the "which run had this
    # error?" question), stdout only when asked — it is the big column, and
    # an icontains over it is an honest sequential scan at self-hosted scale.
    # The filter runs in SQL, so stdout stays deferred from the SELECT either
    # way. Capped so a pasted traceback can't become a multi-KB LIKE pattern.
    search_query = request.GET.get("q", "").strip()[:SEARCH_MAX_LENGTH]
    search_output = request.GET.get("deep") == "1"
    if search_query:
        match = Q(script__name__icontains=search_query) | Q(stderr__icontains=search_query)
        if search_output:
            match |= Q(stdout__icontains=search_query)
        runs = runs.filter(match)

    # Get the active workspace's scripts for the filter dropdown
    scripts = Script.objects.for_workspace(request.workspace).order_by("name")

    # Paginate
    paginator = Paginator(runs, RUNS_PER_PAGE)
    page_obj = paginator.get_page(request.GET.get("page"))
    page_range = paginator.get_elided_page_range(
        page_obj.number, on_each_side=1, on_ends=1
    )

    # Preserve active filters in pagination links (everything except `page`),
    # and in the status tabs / script picker (everything except the one being
    # changed) so switching one filter never drops the others.
    params = request.GET.copy()
    params.pop("page", None)
    querystring = params.urlencode()
    without_status = params.copy()
    without_status.pop("status", None)
    without_script = params.copy()
    without_script.pop("script", None)

    return render(request, "cpanel/runs/list.html", {
        "runs": page_obj,
        "page_obj": page_obj,
        "page_range": page_range,
        "querystring": querystring,
        "querystring_without_status": without_status.urlencode(),
        "querystring_without_script": without_script.urlencode(),
        "status_choices": Run.Status.choices,
        "status_filter": status_filter,
        "script_filter": script_filter,
        "search_query": search_query,
        "search_output": search_output,
        "scripts": scripts,
    })


@login_required
def run_detail_view(request: HttpRequest, pk) -> HttpResponse:
    """View run details including output."""
    run = get_object_or_404(
        Run.objects.select_related("script", "triggered_by"),
        pk=pk,
        workspace=request.workspace,
    )

    input_rows, inputs_hidden = run_input_rows(run)
    has_result = run.result is not None
    return render(request, "cpanel/runs/detail.html", {
        "run": run,
        "isolation_note": isolation_note(run),
        "output_truncated": "[OUTPUT TRUNCATED" in (run.stdout or ""),
        "stderr_truncated": "[OUTPUT TRUNCATED" in (run.stderr or ""),
        "input_rows": input_rows,
        "inputs_hidden": inputs_hidden,
        # Stored with secrets already masked (core.services.run_reports).
        "result_text": result_text(run.result) if has_result else "",
        "result_bytes": json_size(run.result) if has_result else 0,
        "progress": progress_for_display(run.progress),
    })


@login_required
def run_status_view(request: HttpRequest, pk) -> JsonResponse:
    """A run's status and progress, polled by its page while it is live."""
    run = get_object_or_404(
        Run.objects.only("id", "status", "progress"), pk=pk, workspace=request.workspace
    )
    return JsonResponse({
        "status": run.status,
        "is_finished": run.is_finished,
        "progress": progress_for_display(run.progress),
    })


@login_required
def run_result_download_view(request: HttpRequest, pk) -> HttpResponse:
    """The run's result as a .json file (secrets already masked)."""
    run = get_object_or_404(
        Run.objects.only("id", "result"), pk=pk, workspace=request.workspace
    )
    if run.result is None:
        raise Http404("This run has no result.")
    response = HttpResponse(result_text(run.result), content_type="application/json; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="run-{run.pk}-result.json"'
    return response


def run_input_rows(run) -> tuple[list[dict], bool]:
    """The run's inputs for its Inputs card, secrets masked: (rows, hidden).

    Inputs are stored raw because the script needs the real values, so masking
    happens here (``mask_run_inputs``), with the same secret set execution uses.
    When a secret can't be resolved the values can't be checked, so they are
    hidden, not shown. Rows follow the script's declared order; anything else
    comes after, sorted (PostgreSQL's jsonb doesn't keep key order).
    """
    if not run.inputs:
        return [], False
    from core.executor import mask_run_inputs
    from core.services.secret_backends import SecretResolutionError

    try:
        masked = mask_run_inputs(run)
    except SecretResolutionError:
        return [], True
    declared = list((run.script.inputs_schema or {}).get("properties", {}))
    names = [n for n in declared if n in masked] + sorted(n for n in masked if n not in declared)
    rows = []
    for name in names:
        value = masked[name]
        is_text = isinstance(value, str)
        rows.append({
            "name": name,
            "text": value if is_text else json.dumps(value, indent=2, ensure_ascii=False),
            "block": isinstance(value, (dict, list)) or (is_text and "\n" in value),
        })
    return rows, False


def host_sandbox_tier() -> str:
    """The host's sandbox tier: the cached probe, else this process's detection."""
    from core.models import GlobalSettings

    cap = GlobalSettings.get_settings().sandbox_capability
    if cap in ("full", "rlimits_only", "none"):
        return cap
    from core.executor_backends.sandboxed import runtime_tier

    return runtime_tier()


def isolation_note(run) -> str:
    """Say when a run's policy wants the sandbox but this host can't provide it (U77).

    Describes the host as it is now (the tier isn't stored per run); the standard
    Docker image's tier doesn't change between runs.
    """
    try:
        from core.executor import resolve_isolation

        if not resolve_isolation(run).sandbox:
            return ""
        if host_sandbox_tier() == "full":
            return ""
    except Exception:
        return ""
    return "Resource limits only — this host can't provide the sandbox the isolation policy asks for."
