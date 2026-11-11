"""
Triggers in the console (2.5 Stage 7, ticket T31).

- A script's Triggers page (``scripts/<pk>/triggers/``): everything that
  starts it, in one place: its schedule, its webhook, its watchers and the
  channels whose messages run it.
- The workspace's Triggers list (``triggers/``): every watcher.
- A watcher's page (``triggers/<id>/``) with its event log, and the form
  (``triggers/new/?script=<pk>&adapter=<key>``, ``triggers/<id>/edit/``)
  whose adapter settings come from the adapter's ``config_schema``. The form's
  Test button asks the source without saving or starting anything.
- Check now, on/off and delete (POST).

Every lookup is scoped to ``request.workspace`` like the script pages.
"""

import datetime
import json
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods, require_POST

from core.models import Channel, Run, Script, ScriptSchedule, Watcher
from core.triggers import registry, runner
from core.views.ownership import owned_block_message, owned_delete_blocked
from core.watcher_forms import CONFIG_PREFIX, WatcherForm

# Check now outcomes shown as a warning rather than a success.
_WARN_OUTCOMES = {runner.FAILED, runner.LOST, runner.NOT_FOUND}
_INFO_OUTCOMES = {runner.BUSY, runner.MAINTENANCE, runner.CAPPED, runner.DISABLED, runner.SCRIPT_UNAVAILABLE}


# Run columns the event log never shows.
_RUN_HEAVY_FIELDS = ("stdout", "stderr", "code_snapshot", "event", "inputs", "result", "queued_payload")


def _parse_time(text):
    try:
        return datetime.datetime.fromisoformat(text) if text else None
    except (TypeError, ValueError):
        return None


def _watcher(request, watcher_id):
    return get_object_or_404(
        Watcher.objects.select_related("script", "connection"), pk=watcher_id, workspace=request.workspace,
    )


def _annotate(watchers):
    """Give each watcher what a list row shows: its adapter's title and its status."""
    for w in watchers:
        w.adapter_title = registry.title(w.adapter)
        if w.is_auto_disabled:
            w.status, w.status_class = "Turned off after errors", "bg-fail/10 text-fail"
        elif not w.enabled:
            w.status, w.status_class = "Off", "bg-panel-hi text-faint"
        elif w.error_count:
            w.status, w.status_class = f"Failing ({w.error_count})", "bg-warn/10 text-warn"
        elif w.state is None:
            w.status, w.status_class = "Not checked yet", "bg-panel-hi text-muted"
        else:
            w.status, w.status_class = "On", "bg-ok/10 text-ok"
    return watchers


def _back(request, fallback):
    """The page a POST came from (``next``), if it is one of ours, else ``fallback``."""
    target = request.POST.get("next", "")
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()},
                                                  require_https=request.is_secure()):
        return redirect(target)
    return redirect(fallback)


@login_required
def script_triggers_view(request: HttpRequest, pk) -> HttpResponse:
    """Everything that starts this script: schedule, webhook, watchers, channels."""
    script = get_object_or_404(Script, pk=pk, workspace=request.workspace)
    schedule = ScriptSchedule.objects.filter(script=script).first()
    watchers = _annotate(list(script.watchers.select_related("connection")))
    channels = list(
        Channel.objects.filter(
            workspace=request.workspace, inbound_handler="script", inbound_target_id=script.pk,
        ).order_by("name")
    )
    from core.models import GlobalSettings

    return render(request, "cpanel/watchers/script_triggers.html", {
        "script": script,
        "schedule": schedule,
        "watchers": watchers,
        "channels": channels,
        "adapters": list(registry.adapters().values()),
        "schedules_globally_paused": GlobalSettings.get_settings().schedules_paused,
    })


@login_required
def watcher_list_view(request: HttpRequest) -> HttpResponse:
    """Every watcher of the workspace."""
    watchers = _annotate(list(
        Watcher.objects.filter(workspace=request.workspace).select_related("script", "connection")
    ))
    return render(request, "cpanel/watchers/list.html", {"watchers": watchers})


@login_required
def watcher_detail_view(request: HttpRequest, watcher_id) -> HttpResponse:
    """One watcher: its settings, health and event log (the last 50 events)."""
    watcher = _watcher(request, watcher_id)
    _annotate([watcher])
    log = list(watcher.recent_events or [])
    run_ids = [entry.get("run") for entry in log if entry.get("run")]
    runs = {
        str(run.pk): run
        for run in Run.objects.filter(pk__in=run_ids, workspace=request.workspace).defer(*_RUN_HEAVY_FIELDS)
    }
    events = [
        {"id": entry.get("id"), "at": _parse_time(entry.get("at")), "run_id": entry.get("run"),
         "run": runs.get(entry.get("run"))}
        for entry in log
    ]
    return render(request, "cpanel/watchers/detail.html", {
        "watcher": watcher,
        "events": events,
        "event_log_max": Watcher.EVENT_LOG_MAX,
    })


@login_required
@require_http_methods(["GET", "POST"])
def watcher_edit_view(request: HttpRequest, watcher_id=None) -> HttpResponse:
    """Create (``?script=&adapter=``) or edit a watcher; the Test button asks the source."""
    watcher = _watcher(request, watcher_id) if watcher_id else None
    script = watcher.script if watcher else None
    if watcher is None:
        script_id = request.GET.get("script", "")
        if _is_uuid(script_id):
            script = Script.objects.filter(pk=script_id, workspace=request.workspace).first()
    key = request.GET.get("adapter") or (watcher.adapter if watcher else "")
    adapter = registry.get(key)
    if adapter is None:
        # Step one: pick what to watch.
        return render(request, "cpanel/watchers/pick.html", {
            "watcher": watcher,
            "script": script,
            "adapters": list(registry.adapters().values()),
            "unknown_adapter": key if watcher else "",
        })

    test_result = None
    if request.method == "POST":
        form = WatcherForm(request.workspace, adapter, request.POST, instance=watcher)
        valid = form.is_valid()
        if request.POST.get("action") == "test":
            # A test never saves: the form shows again with what was typed.
            if any(name.startswith(CONFIG_PREFIX) or name == "connection" for name in form.errors):
                test_result = {"ok": False, "message": "Fix the settings first.", "items": []}
            else:
                ok, message, items = runner.test_adapter(
                    adapter, form.cleaned_data["config"], form.cleaned_data.get("connection"),
                )
                test_result = {
                    "ok": ok, "message": message,
                    "items": [json.dumps(item, indent=2, ensure_ascii=False, default=str) for item in items],
                }
        elif valid:
            watcher = form.save(created_by=request.user)
            messages.success(request, f'Watcher "{watcher.name}" saved.')
            return redirect("cpanel:watcher_detail", watcher_id=watcher.pk)
    else:
        form = WatcherForm(request.workspace, adapter, instance=watcher,
                           initial={"script": script.pk} if script and not watcher else None)

    return render(request, "cpanel/watchers/form.html", {
        "form": form,
        "watcher": watcher,
        "adapter": adapter,
        "script": script,
        "test_result": test_result,
        "adapter_changed": watcher is not None and watcher.adapter != adapter.key,
    })


def _is_uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


@login_required
@require_POST
def watcher_check_view(request: HttpRequest, watcher_id) -> HttpResponse:
    """Check now: one check of the watcher, here and now."""
    watcher = _watcher(request, watcher_id)
    result = runner.check_watcher(watcher.pk)
    if result.outcome in _WARN_OUTCOMES:
        messages.error(request, f'"{watcher.name}": {result.message}')
    elif result.outcome in _INFO_OUTCOMES:
        messages.warning(request, f'"{watcher.name}": {result.message}')
    else:
        messages.success(request, f'"{watcher.name}": {result.message}')
    return _back(request, reverse("cpanel:watcher_detail", kwargs={"watcher_id": watcher.pk}))


@login_required
@require_POST
def watcher_toggle_view(request: HttpRequest, watcher_id) -> HttpResponse:
    """Turn a watcher on or off. On again clears its failures and checks it soon."""
    watcher = _watcher(request, watcher_id)
    if watcher.enabled:
        watcher.enabled = False
        watcher.save(update_fields=["enabled", "updated_at"])
        messages.success(request, f'Watcher "{watcher.name}" turned off.')
    else:
        watcher.enabled = True
        watcher.auto_disabled_at = None
        watcher.error_count = 0
        watcher.next_check_at = None
        watcher.save(update_fields=["enabled", "auto_disabled_at", "error_count", "next_check_at", "updated_at"])
        messages.success(request, f'Watcher "{watcher.name}" turned on.')
    return _back(request, reverse("cpanel:watcher_detail", kwargs={"watcher_id": watcher.pk}))


@login_required
@require_POST
def watcher_delete_view(request: HttpRequest, watcher_id) -> HttpResponse:
    """Delete a watcher. The runs it started stay."""
    watcher = _watcher(request, watcher_id)
    if owned_delete_blocked(request, watcher):
        messages.error(request, owned_block_message(watcher, "watcher"))
        return redirect("cpanel:watcher_detail", watcher_id=watcher.pk)
    script_pk, name = watcher.script_id, watcher.name
    watcher.delete()
    messages.success(request, f'Watcher "{name}" deleted. The runs it started stay.')
    return redirect("cpanel:script_triggers", pk=script_pk)
