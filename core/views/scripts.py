"""
Script views for the control panel.
"""
import logging
import re

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import CharField, Q
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast
from django.views.decorators.http import require_http_methods, require_POST
from django.http import HttpRequest, HttpResponse, JsonResponse

from core.models import Script, Run, ScriptSchedule, ScheduleHistory, Library, ScriptLibrary, Secret, SecretGrant, Tag
from core.charts import DURATION_CHART_MAX_BARS, duration_chart
from core.forms import ScriptForm, ScheduleForm, ScriptInputsForm, WebhookSettingsForm
from core.services import connections as connection_service
from core.services.maintenance import blocked_message
from core.services.run_events import KIND_WEBHOOK, manual_event
from core.services.webhook_signatures import header_for as signature_header_for
from core.services.webhook_signatures import preset_headers as signature_preset_headers
from core.tasks import queue_script_run
from core.services.schedule_service import ScheduleService
from core.services.script_env import editor_secret_names, env_var_collisions
from core.services.script_inputs import ScriptInputsError, prepare_inputs, schedule_conflict
from core.views.ownership import owned_block_message, owned_delete_blocked

logger = logging.getLogger(__name__)

# Matches os.environ['X'] / os.environ.get('X') / os.getenv('X') (single/double quotes).
_ENV_REF_RE = re.compile(
    r"""os\.(?:environ\s*\[|environ\.get\s*\(|getenv\s*\()\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]"""
)


SCRIPTS_PER_PAGE = 25
# The script page's Webhook card lists this many of the latest webhook calls.
WEBHOOK_DELIVERIES_SHOWN = 20


def recent_webhook_deliveries(script):
    """The script's latest runs created by a webhook call, newest first.

    Webhook runs are API runs whose event kind is ``webhook`` (``scripts.run``
    runs are ``api``); retries are left out, a call made only the first
    attempt. The event's kind is read as text (``->>``) and compared as text:
    Run.event is ``json`` on PostgreSQL, which has no JSON equality, and a
    key's own ``exact`` lookup compares JSON values.
    """
    return list(
        script.runs.filter(trigger_type=Run.TriggerType.API, attempt=1)
        .alias(event_kind=Cast(KeyTextTransform("kind", "event"), CharField()))
        .filter(event_kind=KIND_WEBHOOK)
        .defer("stdout", "stderr", "code_snapshot", "event", "inputs", "result", "queued_payload")
        .order_by("-created_at")[:WEBHOOK_DELIVERIES_SHOWN]
    )


def _reconcile_grants(script, secret_ids, workspace) -> None:
    """Make the script's SecretGrant set exactly match ``secret_ids`` (Selected mode).

    Only secrets in the active workspace are grantable. Adds missing grants,
    removes deselected ones; never touches grants for secrets outside the
    workspace's reach.
    """
    valid = {
        str(pk)
        for pk in Secret.objects.for_workspace(workspace)
        .filter(pk__in=[s for s in secret_ids if s])
        .values_list("pk", flat=True)
    }
    existing = {str(g.secret_id): g for g in SecretGrant.objects.filter(script=script)}
    for sid in valid - existing.keys():
        SecretGrant.objects.create(script=script, secret_id=sid, active=True)
    for sid in existing.keys() - valid:
        existing[sid].delete()


def _reconcile_connections(request, script) -> None:
    """Save the Connections box: only when the form had it (a post without it keeps the grants)."""
    if "connections_section" in request.POST:
        connection_service.set_grants(
            script, request.POST.getlist("granted_connection_ids"), request.workspace
        )


def _connection_choices(workspace, script, creator) -> list:
    """The workspace's connections for the script form's Connections box.

    ``creator`` is the script's (the signed-in user for a new script): a
    connection granted to all its creator's scripts reaches it too.
    """
    from core.integrations.presets import get_preset
    from core.models import Connection, ConnectionGrant

    granted = set()
    if script is not None:
        granted = {
            str(pk) for pk in ConnectionGrant.objects.filter(script=script, active=True)
            .values_list("connection_id", flat=True)
        }
    user_script = script is None or script.owner_plugin is None
    choices = []
    for c in Connection.objects.filter(workspace=workspace).order_by("name"):
        preset = get_preset(c.provider)
        choices.append({
            "id": str(c.pk),
            "name": c.name,
            "provider_label": preset.label if preset else c.provider,
            "granted": str(c.pk) in granted,
            "by_wildcard": bool(
                c.grant_all_scripts and user_script and creator is not None
                and c.created_by_id is not None and c.created_by_id == creator.pk
            ),
        })
    return choices


def _reconcile_libraries(script, library_ids, workspace) -> None:
    """Make the script's attached-library set exactly match ``library_ids``.

    Mirrors ``_reconcile_grants``: only libraries in the active workspace are
    attachable (the workspace filter IS the tenancy guard here — a posted id from
    another workspace is simply not in ``valid``, so it is ignored rather than
    trusted), and detach is a row delete. Unlike secrets there is no
    injection-mode gate: libraries are explicit-only, always.
    """
    valid = {
        str(pk)
        for pk in Library.objects.for_workspace(workspace)
        .filter(pk__in=[lid for lid in library_ids if lid])
        .values_list("pk", flat=True)
    }
    existing = {str(a.library_id): a for a in ScriptLibrary.objects.filter(script=script)}
    for lid in valid - existing.keys():
        ScriptLibrary.objects.create(script=script, library_id=lid)
    for lid in existing.keys() - valid:
        existing[lid].delete()


@login_required
@require_POST
def scan_env_refs_view(request: HttpRequest) -> HttpResponse:
    """Scan a posted script body for os.environ references; return the keys it uses
    and which already exist as secrets in the active workspace (for one-click attach)."""
    code = request.POST.get("code", "")
    keys = sorted(set(_ENV_REF_RE.findall(code)))
    existing = {
        s.key: str(s.id)
        for s in Secret.objects.for_workspace(request.workspace).filter(key__in=keys)
    }
    refs = [{"key": k, "secret_id": existing.get(k)} for k in keys]
    return JsonResponse({"refs": refs})


# Starter code templates available via ?template=<key> on the create page.
SCRIPT_TEMPLATES = {
    "ai": {
        "name": "AI Web Research",
        "description": "Uses Claude (web search + fetch) to research a topic.",
        "code": '''"""
AI Web Research example.

Requires:
  - AI enabled in PyRun (Services -> AI Provider); web search needs an
    Anthropic provider (it is served by Anthropic's API)
  - 'claude-agent-sdk' installed in this script's Environment (Environments -> Packages)
"""
from pyrunner_ai import ask_claude

TOPIC = "the latest stable Python release and its headline features"

# Web search + web fetch are enabled by default.
answer = ask_claude(
    f"Search the web and give me a short, up-to-date briefing on {TOPIC}. "
    "Include the version number and 3 bullet points."
)

print(answer)

# Want details (tools used, cost, turns)? Use raw=True:
# result = ask_claude("...", raw=True)
# print(result.text, result.tools_used, result.cost_usd)
''',
    },
}


@login_required
def script_list_view(request: HttpRequest) -> HttpResponse:
    """List scripts: status/tag/owner filters, a name + description search
    and pagination, all carried in the URL so they combine and can be shared."""
    scripts = (
        Script.objects.for_workspace(request.workspace)
        .select_related("environment", "created_by")
        .prefetch_related("tags")
        .order_by("-updated_at")
    )

    # Optional filtering by status
    status_filter = request.GET.get("status")
    if status_filter not in ("enabled", "disabled", "archived"):
        status_filter = None
    if status_filter == "enabled":
        scripts = scripts.filter(is_enabled=True, archived_at__isnull=True)
    elif status_filter == "disabled":
        scripts = scripts.filter(is_enabled=False, archived_at__isnull=True)
    elif status_filter == "archived":
        scripts = scripts.filter(archived_at__isnull=False)
    else:
        # Default "All" excludes archived scripts
        scripts = scripts.filter(archived_at__isnull=True)

    # Filter by tag
    tag_filter = request.GET.get("tag")
    selected_tag = None
    if tag_filter:
        try:
            selected_tag = Tag.objects.get(pk=tag_filter)
            scripts = scripts.filter(tags=selected_tag)
        except (Tag.DoesNotExist, ValueError, ValidationError):
            pass

    # Filter by owning plugin (Plugin Platform v2). The owner list is computed
    # from the whole workspace so the dropdown always offers every owner.
    owners = list(
        Script.objects.for_workspace(request.workspace)
        .exclude(owner_plugin__isnull=True)
        .exclude(owner_plugin="")
        .order_by("owner_plugin")
        .values_list("owner_plugin", flat=True)
        .distinct()
    )
    owner_filter = request.GET.get("owner_plugin")
    if owner_filter:
        scripts = scripts.filter(owner_plugin=owner_filter)

    # Text search on name and description (server-side, so it covers every page).
    search_query = (request.GET.get("q") or "").strip()[:200]
    if search_query:
        scripts = scripts.filter(
            Q(name__icontains=search_query) | Q(description__icontains=search_query)
        )

    paginator = Paginator(scripts, SCRIPTS_PER_PAGE)
    page_obj = paginator.get_page(request.GET.get("page"))
    page_range = paginator.get_elided_page_range(page_obj.number, on_each_side=1, on_ends=1)

    # Every link keeps the other filters; changing one filter drops `page`.
    params = request.GET.copy()
    params.pop("page", None)

    def _without(*keys):
        q = params.copy()
        for k in keys:
            q.pop(k, None)
        return q.urlencode()

    # Get all tags for filter dropdown
    all_tags = Tag.objects.all().order_by("name")

    return render(request, "cpanel/scripts/list.html", {
        "scripts": page_obj,
        "page_obj": page_obj,
        "page_range": page_range,
        "querystring": params.urlencode(),
        "querystring_without_status": _without("status"),
        "querystring_without_tag": _without("tag"),
        "querystring_without_q": _without("q"),
        "status_filter": status_filter,
        "all_tags": all_tags,
        "selected_tag": selected_tag,
        "owners": owners,
        "selected_owner": owner_filter or "",
        "search_query": search_query,
        "is_filtered": bool(status_filter or selected_tag or owner_filter or search_query),
    })


def _warn_if_timeout_outruns_workers(request, script) -> None:
    """Non-blocking heads-up when a saved timeout exceeds the worker window.

    The cluster floors its broker re-delivery window (`retry`) to the fleet's
    largest script timeout at start (pyrunner_qcluster), so a timeout raised
    PAST what the running cluster booted with is not covered until the next
    worker restart. The running cluster's window is read from the cache stamp
    the worker heartbeat leaves (this web process's settings.Q_CLUSTER only
    holds env defaults). Advisory only: duplicate deliveries are no-ops
    (execute_run's claim), so the exposure is noise, not data loss.
    """
    from django.conf import settings as django_settings
    from django.core.cache import cache

    from core.services.worker_config import EFFECTIVE_RETRY_CACHE_KEY

    window = (
        cache.get(EFFECTIVE_RETRY_CACHE_KEY)
        or django_settings.Q_CLUSTER.get("retry")
        or 0
    )
    # +60 = the per-task cushion queue_script_run adds; the task is always
    # acknowledged within max_run_seconds (timeout + stop grace) + 60, so
    # only a run longer than window - 60 can still be in flight when
    # re-delivery fires.
    if script.max_run_seconds + 60 > window:
        messages.warning(
            request,
            f'"{script.name}" can run for {script.max_run_seconds}s (its timeout '
            f"plus its stop grace period) but the running workers' task-recovery "
            f"window is {window}s. Restart workers "
            "(Settings → Workers) so long runs are fully covered.",
        )


def _warn_on_env_var_collisions(request, script):
    """Name the env vars a secret this script gets replaces at run time."""
    clashes = env_var_collisions(script)
    if clashes:
        listed = ", ".join(clashes)
        messages.warning(
            request,
            f"A secret this script gets has the same name as {listed}, so the "
            "secret's value is used. Rename the env var to keep both.",
        )


@login_required
def script_create_view(request: HttpRequest) -> HttpResponse:
    """Create a new script."""
    if request.method == "POST":
        form = ScriptForm(request.POST, workspace=request.workspace)
        if form.is_valid():
            script = form.save(commit=False)
            script.created_by = request.user
            # Stamp the active workspace (tenancy Stage 3) so the new script is
            # owned by — and visible in — the workspace it was created in.
            script.workspace = request.workspace
            script.save()
            form.save_m2m()  # Save M2M relationships (tags)
            # Reconcile per-script secret grants when in Selected injection mode.
            if script.injection_mode == Script.InjectionMode.SELECTED:
                _reconcile_grants(script, request.POST.getlist("granted_secret_ids"), request.workspace)
            _reconcile_libraries(script, request.POST.getlist("library_ids"), request.workspace)
            _reconcile_connections(request, script)
            messages.success(request, f'Script "{script.name}" created successfully.')
            _warn_if_timeout_outruns_workers(request, script)
            _warn_on_env_var_collisions(request, script)
            return redirect("cpanel:script_detail", pk=script.pk)
    else:
        # Optionally pre-fill from a starter template (e.g. ?template=ai).
        template = SCRIPT_TEMPLATES.get(request.GET.get("template", ""))
        form = (
            ScriptForm(initial=template, workspace=request.workspace)
            if template
            else ScriptForm(workspace=request.workspace)
        )

    available_tags = Tag.objects.all().order_by("name")
    return render(request, "cpanel/scripts/create.html", {
        "form": form,
        "available_tags": available_tags,
        "selected_tag_ids": [],
        "granted_secrets": [],
        "connection_choices": _connection_choices(request.workspace, None, request.user),
        "available_libraries": Library.objects.for_workspace(request.workspace).order_by("key"),
        "attached_library_ids": [],
        "env_secrets": editor_secret_names(None, request.workspace.pk if request.workspace else None),
    })


@login_required
def script_detail_view(request: HttpRequest, pk) -> HttpResponse:
    """View script details and recent runs."""
    script = get_object_or_404(
        Script.objects.select_related("environment", "created_by").prefetch_related("tags"),
        pk=pk,
        workspace=request.workspace,
    )
    recent_runs = script.runs.select_related("triggered_by").order_by("-created_at")[:10]
    webhook_deliveries = recent_webhook_deliveries(script)

    # Duration chart: the last N finished runs, oldest → newest. Only the two
    # timestamps are needed, so skip the output columns entirely.
    chart_runs = list(
        script.runs.filter(started_at__isnull=False, ended_at__isnull=False)
        .only("id", "status", "started_at", "ended_at")
        .order_by("-created_at")[:DURATION_CHART_MAX_BARS]
    )
    chart_runs.reverse()

    # Ensure schedule exists for this script
    schedule, _ = ScriptSchedule.objects.get_or_create(
        script=script,
        defaults={"created_by": request.user, "workspace": script.workspace},
    )

    from core.executor import script_workdir
    from core.models import GlobalSettings

    return render(request, "cpanel/scripts/detail.html", {
        "script": script,
        "recent_runs": recent_runs,
        "webhook_deliveries": webhook_deliveries,
        "webhook_signature_header": signature_header_for(script),
        "schedule": schedule,
        "duration_chart": duration_chart(chart_runs),
        "working_folder": str(script_workdir(script.pk)),
        "schedules_globally_paused": GlobalSettings.get_settings().schedules_paused,
    })


@login_required
def script_edit_view(request: HttpRequest, pk) -> HttpResponse:
    """Edit an existing script and its schedule."""
    script = get_object_or_404(Script, pk=pk, workspace=request.workspace)

    # Get or create schedule for this script
    schedule, created = ScriptSchedule.objects.get_or_create(
        script=script,
        defaults={"created_by": request.user, "workspace": script.workspace},
    )

    if request.method == "POST":
        # Snapshot BEFORE the form touches the instance. ScheduleForm is bound to
        # this very object, and validation's construct_instance() already writes
        # every Meta.fields value (run_mode/interval_minutes/timezone/is_active)
        # onto it — snapshotting after is_valid() compared those four against
        # themselves, so a mode/timezone/enabled-only edit wrote no history row
        # at all, and a mixed edit recorded the NEW value as the previous one.
        previous_config = schedule.config_snapshot()

        form = ScriptForm(request.POST, instance=script, workspace=request.workspace)
        schedule_form = ScheduleForm(request.POST, instance=schedule)

        valid = form.is_valid() and schedule_form.is_valid()
        # A scheduled run has nobody to fill in its inputs. Checked across both
        # forms, so neither a new required input nor turning a schedule on can
        # leave a schedule that could never start a run.
        if valid and schedule_form.instance.is_scheduled:
            conflict = schedule_conflict(form.cleaned_data.get("inputs_schema"))
            if conflict:
                schedule_form.add_error(None, conflict)
                valid = False

        if valid:
            script = form.save(commit=False)
            script.save()
            form.save_m2m()
            if script.injection_mode == Script.InjectionMode.SELECTED:
                _reconcile_grants(script, request.POST.getlist("granted_secret_ids"), request.workspace)
            _reconcile_libraries(script, request.POST.getlist("library_ids"), request.workspace)
            _reconcile_connections(request, script)
            schedule = schedule_form.save()

            # Capture new config
            new_config = schedule.config_snapshot()

            # Create history entry if changed
            if previous_config != new_config:
                change_type = (
                    ScheduleHistory.ChangeType.CREATED
                    if created
                    else ScheduleHistory.ChangeType.UPDATED
                )
                ScheduleHistory.objects.create(
                    schedule=schedule,
                    change_type=change_type,
                    previous_config=previous_config if not created else None,
                    new_config=new_config,
                    changed_by=request.user,
                )

            # Sync with django-q2
            ScheduleService.sync_schedule(schedule)

            messages.success(request, f'Script "{script.name}" updated successfully.')
            _warn_if_timeout_outruns_workers(request, script)
            _warn_on_env_var_collisions(request, script)
            return redirect("cpanel:script_detail", pk=script.pk)
    else:
        form = ScriptForm(instance=script, workspace=request.workspace)
        schedule_form = ScheduleForm(instance=schedule)

    available_tags = Tag.objects.all().order_by("name")
    selected_tag_ids = list(script.tags.values_list("pk", flat=True))
    granted_secrets = [
        {"id": str(g.secret_id), "key": g.secret.key, "owner_plugin": g.secret.owner_plugin or ""}
        for g in script.secret_grants.select_related("secret").filter(active=True)
    ]
    return render(request, "cpanel/scripts/edit.html", {
        "form": form,
        "schedule_form": schedule_form,
        "script": script,
        "available_tags": available_tags,
        "selected_tag_ids": selected_tag_ids,
        "granted_secrets": granted_secrets,
        "connection_choices": _connection_choices(request.workspace, script, script.created_by),
        "available_libraries": Library.objects.for_workspace(request.workspace).order_by("key"),
        "attached_library_ids": [str(pk) for pk in script.libraries.values_list("pk", flat=True)],
        # Names only, for the saved script: a refused POST has written the
        # posted values onto `script` in memory.
        "env_secrets": editor_secret_names(Script.objects.get(pk=script.pk), script.workspace_id),
    })


def _start_manual_run(request: HttpRequest, script, inputs, rerun_of=None) -> HttpResponse:
    """Create and queue a manual run with already-validated inputs; go to it.

    ``rerun_of``: the id of the run a Re-run copies, recorded in the event.
    """
    # Stamp the run's workspace from its script (tenancy Stage 1) so the
    # executor scopes secrets to it.
    run = Run.objects.create(
        script=script,
        workspace_id=script.workspace_id,
        status=Run.Status.PENDING,
        triggered_by=request.user,
        code_snapshot=script.code,
        inputs=inputs,
        event=manual_event(request.user, rerun_of=rerun_of),
    )

    # Queue for async execution via django-q2
    try:
        queue_script_run(run)
        if run.is_maintenance_skipped:
            messages.warning(request, blocked_message(run, run.maintenance_block))
        elif run.is_skipped:
            messages.warning(
                request,
                f'Not started: "{script.name}" is already running, and its '
                "concurrency policy is Skip.",
            )
        elif run.is_held:
            messages.info(
                request,
                f'Script "{script.name}" is already running. This run is queued '
                "and starts when the run ahead of it ends.",
            )
        else:
            messages.info(request, f'Script "{script.name}" has been queued for execution.')
    except ScriptInputsError as e:
        # The schema changed after validation; queue_script_run deleted the
        # row, so there is no run to show (and none to save back as failed).
        messages.error(request, f"Not run. {e.message}")
        return redirect("cpanel:script_detail", pk=script.pk)
    except Exception:
        logger.exception("Script %s: the run couldn't be queued", script.pk)
        run.status = Run.Status.FAILED
        run.stderr = Run.QUEUE_FAILED_TEXT
        run.save()
        messages.error(request, "Failed to queue script. The server log has the details.")

    return redirect("cpanel:run_detail", pk=run.pk)


def _refuse_unrunnable(request: HttpRequest, script) -> HttpResponse | None:
    if script.can_run:
        return None
    if script.is_archived:
        messages.error(request, "Cannot run an archived script.")
    else:
        messages.error(request, "Cannot run a disabled script.")
    return redirect("cpanel:script_detail", pk=script.pk)


@login_required
@require_http_methods(["GET", "POST"])
def script_run_view(request: HttpRequest, pk) -> HttpResponse:
    """Trigger a manual script run.

    A script without declared inputs runs straight from the POST, as always. One
    with an inputs schema shows its form on GET; the POST is validated before
    any Run row exists, so bad inputs come back as field errors, never a run.
    """
    script = get_object_or_404(Script, pk=pk, workspace=request.workspace)

    refused = _refuse_unrunnable(request, script)
    if refused:
        return refused

    if not script.inputs_schema:
        if request.method == "GET":
            return redirect("cpanel:script_detail", pk=pk)
        return _start_manual_run(request, script, None)

    if request.method == "POST":
        form = ScriptInputsForm(script, request.POST)
        if form.is_valid():
            return _start_manual_run(request, script, form.inputs)
    else:
        form = ScriptInputsForm(script)
    return render(request, "cpanel/scripts/run.html", {"script": script, "form": form})


@login_required
@require_POST
def run_rerun_view(request: HttpRequest, pk) -> HttpResponse:
    """Start a new run of a run's script with that run's exact inputs."""
    source = get_object_or_404(
        Run.objects.select_related("script"), pk=pk, workspace=request.workspace
    )
    script = source.script

    refused = _refuse_unrunnable(request, script)
    if refused:
        return refused

    # Re-checked against the script as it is now: the schema may have changed
    # since the original run, and a re-run must not bypass it.
    try:
        inputs = prepare_inputs(script, source.inputs)
    except ScriptInputsError as e:
        messages.error(request, f"Can't re-run with the same inputs. {e.message}")
        return redirect("cpanel:run_detail", pk=source.pk)
    return _start_manual_run(request, script, inputs, rerun_of=source.pk)


@login_required
@require_POST
def script_toggle_view(request: HttpRequest, pk) -> HttpResponse:
    """Toggle script enabled/disabled state."""
    script = get_object_or_404(Script, pk=pk, workspace=request.workspace)
    script.is_enabled = not script.is_enabled
    script.save(update_fields=["is_enabled", "updated_at"])

    status = "enabled" if script.is_enabled else "disabled"
    messages.success(request, f'Script "{script.name}" is now {status}.')
    return redirect("cpanel:script_detail", pk=pk)


@login_required
def cron_preview_view(request: HttpRequest) -> JsonResponse:
    """
    Validate a cron expression and return its next few run times.

    Used by the schedule form for live feedback. Query params: ``expression``
    and ``timezone`` (IANA name; unknown values fall back to UTC).
    """
    from core.tz import safe_zoneinfo

    expression = request.GET.get("expression", "")
    tz_name = getattr(
        safe_zoneinfo(request.GET.get("timezone") or "UTC", context="cron preview"), "key", "UTC"
    )
    is_valid, error = ScheduleService.validate_cron_expression(expression, tz_name)
    if not is_valid:
        return JsonResponse({"valid": False, "error": error, "runs": [], "timezone": tz_name})

    runs = ScheduleService.preview_cron_runs(expression, count=3, timezone_name=tz_name)
    return JsonResponse(
        {
            "valid": True,
            "error": None,
            "runs": [dt.strftime("%a %d %b %Y, %H:%M") for dt in runs],
            "timezone": tz_name,
        }
    )


@login_required
@require_POST
def schedule_toggle_view(request: HttpRequest, pk) -> HttpResponse:
    """Toggle schedule active/inactive state."""
    script = get_object_or_404(Script, pk=pk, workspace=request.workspace)

    try:
        schedule = script.schedule
    except ScriptSchedule.DoesNotExist:
        messages.error(request, "No schedule configured for this script.")
        return redirect("cpanel:script_detail", pk=pk)

    if not schedule.is_active and schedule.run_mode != ScriptSchedule.RunMode.MANUAL:
        conflict = schedule_conflict(script.inputs_schema)
        if conflict:
            messages.error(request, f"Schedule not resumed. {conflict}")
            return redirect("cpanel:script_detail", pk=pk)

    previous_active = schedule.is_active
    schedule.is_active = not schedule.is_active
    schedule.save(update_fields=["is_active", "updated_at"])

    # Record history
    ScheduleHistory.objects.create(
        schedule=schedule,
        change_type=(
            ScheduleHistory.ChangeType.ENABLED
            if schedule.is_active
            else ScheduleHistory.ChangeType.DISABLED
        ),
        previous_config={"is_active": previous_active},
        new_config={"is_active": schedule.is_active},
        changed_by=request.user,
    )

    # Sync with django-q2
    ScheduleService.sync_schedule(schedule)

    status = "enabled" if schedule.is_active else "paused"
    messages.success(request, f'Schedule for "{script.name}" is now {status}.')
    return redirect("cpanel:script_detail", pk=pk)


@login_required
def schedule_history_view(request: HttpRequest, pk) -> HttpResponse:
    """View schedule change history."""
    script = get_object_or_404(Script, pk=pk, workspace=request.workspace)

    try:
        schedule = script.schedule
        history = schedule.history.select_related("changed_by").order_by("-created_at")
    except ScriptSchedule.DoesNotExist:
        history = []
        schedule = None

    return render(request, "cpanel/scripts/schedule_history.html", {
        "script": script,
        "schedule": schedule,
        "history": history,
    })


@login_required
@require_POST
def webhook_enable_view(request: HttpRequest, pk) -> HttpResponse:
    """Enable webhook for a script (creates token if not exists)."""
    script = get_object_or_404(Script, pk=pk, workspace=request.workspace)

    if not script.webhook_token:
        script.create_webhook_token()
        messages.success(request, f'Webhook enabled for "{script.name}".')
    else:
        messages.info(request, "Webhook is already enabled.")

    return redirect("cpanel:script_detail", pk=pk)


@login_required
@require_POST
def webhook_disable_view(request: HttpRequest, pk) -> HttpResponse:
    """Disable webhook for a script (removes token)."""
    script = get_object_or_404(Script, pk=pk, workspace=request.workspace)

    if script.webhook_token:
        script.clear_webhook_token()
        messages.success(request, f'Webhook disabled for "{script.name}".')
    else:
        messages.info(request, "Webhook is already disabled.")

    return redirect("cpanel:script_detail", pk=pk)


@login_required
@require_POST
def webhook_regenerate_view(request: HttpRequest, pk) -> HttpResponse:
    """Regenerate webhook token (invalidates old URL)."""
    script = get_object_or_404(Script, pk=pk, workspace=request.workspace)

    script.regenerate_webhook_token()
    messages.success(request, f'Webhook URL regenerated for "{script.name}". The old URL is now invalid.')

    return redirect("cpanel:script_detail", pk=pk)


@login_required
@require_http_methods(["GET", "POST"])
def webhook_settings_view(request: HttpRequest, pk) -> HttpResponse:
    """The webhook's signature (preset + secret) and rate limits."""
    script = get_object_or_404(Script, pk=pk, workspace=request.workspace)
    if request.method == "POST":
        form = WebhookSettingsForm(request.POST, instance=script)
        if form.is_valid():
            form.save()
            messages.success(request, f'Webhook settings saved for "{script.name}".')
            return redirect("cpanel:script_detail", pk=pk)
        # A rejected form has written its values onto ``script``: reload so
        # the page shows what is stored.
        script.refresh_from_db()
    else:
        form = WebhookSettingsForm(instance=script)
    return render(request, "cpanel/scripts/webhook_settings.html", {
        "script": script,
        "form": form,
        "secret_saved": bool(script.webhook_signature_secret_encrypted),
        "preset_headers": signature_preset_headers(),
    })


@login_required
@require_POST
def script_fork_view(request: HttpRequest, pk) -> HttpResponse:
    """Duplicate a script into the active workspace and open the copy for editing.

    Plugin-owned scripts fork too — the copy is a plain user script, which is
    the supported way to customize a plugin-provided script without editing
    the plugin's own row out from under it.
    """
    from core.services.script_service import fork_script

    source = get_object_or_404(Script, pk=pk, workspace=request.workspace)
    fork = fork_script(source, user=request.user)
    messages.success(
        request,
        f'Forked "{source.name}" as "{fork.name}". The copy has no webhook and '
        "its schedule is paused — review it below, then enable what you need.",
    )
    return redirect("cpanel:script_edit", pk=fork.pk)


@login_required
@require_POST
def script_archive_view(request: HttpRequest, pk) -> HttpResponse:
    """Archive a script (soft delete)."""
    from django.utils import timezone

    script = get_object_or_404(Script, pk=pk, workspace=request.workspace)

    if script.is_archived:
        messages.info(request, f'Script "{script.name}" is already archived.')
        return redirect("cpanel:script_detail", pk=pk)

    # Archive the script
    script.archived_at = timezone.now()
    script.archived_by = request.user
    script.save(update_fields=["archived_at", "archived_by", "updated_at"])

    # Pause the schedule if it exists and is active
    try:
        schedule = script.schedule
        if schedule.is_active:
            schedule.is_active = False
            schedule.save(update_fields=["is_active", "updated_at"])
            ScheduleService.sync_schedule(schedule)
    except ScriptSchedule.DoesNotExist:
        pass

    messages.success(request, f'Script "{script.name}" has been archived.')
    return redirect("cpanel:script_list")


@login_required
@require_POST
def script_restore_view(request: HttpRequest, pk) -> HttpResponse:
    """Restore an archived script."""
    script = get_object_or_404(Script, pk=pk, workspace=request.workspace)

    if not script.is_archived:
        messages.info(request, f'Script "{script.name}" is not archived.')
        return redirect("cpanel:script_detail", pk=pk)

    # Restore the script
    script.archived_at = None
    script.archived_by = None
    script.save(update_fields=["archived_at", "archived_by", "updated_at"])

    messages.success(request, f'Script "{script.name}" has been restored.')
    return redirect("cpanel:script_detail", pk=pk)


@login_required
@require_POST
def script_delete_view(request: HttpRequest, pk) -> HttpResponse:
    """Permanently delete an archived script."""
    script = get_object_or_404(Script, pk=pk, workspace=request.workspace)

    if not script.is_archived:
        messages.error(request, "Only archived scripts can be permanently deleted.")
        return redirect("cpanel:script_detail", pk=pk)

    if owned_delete_blocked(request, script):
        messages.error(request, owned_block_message(script, "script"))
        return redirect("cpanel:script_detail", pk=pk)

    name = script.name
    script.delete()  # CASCADE will handle runs and schedule

    messages.success(request, f'Script "{name}" has been permanently deleted.')
    return redirect("cpanel:script_list")
