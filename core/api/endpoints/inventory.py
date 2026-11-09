"""The rest of a workspace over REST, MCP and Py AI (2.5 Stage 3, T18).

Seven reads, each with its own permission:

- ``providers.list`` (``providers:read``): AI and embedding providers, which
  AI provider is the default, and the AI calls this workspace made today;
- ``secrets.names`` (``secrets:read_names``): secret names and the scripts that
  get each one, or the names one script gets;
- ``channels.list`` (``channels:read``): chat channels, their inbound handler
  and member counts;
- ``plugins.list`` (``plugins:read``), ``environments.list``
  (``environments:read``, with one environment's installed packages),
  ``libraries.list`` (``libraries:read``) and ``schedules.list``
  (``schedules:read``).

None returns a secret value, a provider key or base URL, a channel token or a
library's code. Providers, plugins and environments belong to the instance, not
a workspace, so every workspace lists the same ones; usage, scripts, channels,
libraries and schedules are the caller's workspace's (and, for a token bound to
one script, that script's). Py AI on a chat channel gets plugins, environments,
libraries and schedules; providers, secret names and channels stay on the
console (plan D9).
"""

import datetime
import logging

from django.db.models import Count, Q, Sum, Value
from django.db.models.functions import Coalesce

from core.api.errors import ApiError
from core.api.registry import endpoint
from core.models import (
    AIProvider,
    Brain,
    Channel,
    ChannelMember,
    ClaudeUsage,
    EmbeddingProvider,
    Environment,
    GlobalSettings,
    Library,
    Plugin,
    Script,
    ScriptLibrary,
    ScriptSchedule,
    Secret,
)
from .scripts import iso, parse_id, resolve_script, schedule_summary, scripts_for

logger = logging.getLogger(__name__)

EMPTY = {"type": "object", "properties": {}}
# Scripts named per secret; script_count always has the total.
MAX_SCRIPTS_PER_SECRET = 50


def script_ref(script):
    return {"id": str(script.pk), "name": script.name}


# --------------------------------------------------------------------------- providers


def usage_today(workspace, settings):
    """AI calls made today (instance timezone) by ``workspace``'s scripts and Brains.

    Py AI's own calls carry no workspace, so they aren't counted here.
    """
    from core.tz import safe_zoneinfo

    zone = safe_zoneinfo(settings.timezone, context="instance timezone")
    since = datetime.datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = ClaudeUsage.objects.filter(created_at__gte=since).filter(
        Q(script_id__in=Script.objects.filter(workspace=workspace).values("pk"))
        | Q(brain_id__in=Brain.objects.filter(workspace=workspace).values("pk"))
    )
    by_model = [
        {
            "provider": row["served_by"] or None,
            "model": row["model"] or None,
            "calls": row["calls"],
            "input_tokens": row["input"] or 0,
            "output_tokens": row["output"] or 0,
        }
        # provider is NULL on rows older than the column, "" on some others.
        for row in rows.annotate(served_by=Coalesce("provider", Value("")))
        .values("served_by", "model")
        .annotate(calls=Count("id"), input=Sum("input_tokens"), output=Sum("output_tokens"))
        .order_by("served_by", "model")
    ]
    return {
        "since": since.isoformat(),
        "calls": sum(m["calls"] for m in by_model),
        "input_tokens": sum(m["input_tokens"] for m in by_model),
        "output_tokens": sum(m["output_tokens"] for m in by_model),
        "by_model": by_model,
    }


@endpoint(
    "providers.list",
    title="List AI providers",
    description=(
        "The instance's AI providers (used by scripts, Py AI and Brains) and embedding "
        "providers: names, types and models, which AI provider is the default, and the "
        "AI calls this workspace's scripts and Brains made today. Never keys or URLs."
    ),
    input=EMPTY,
    permission="providers:read",
)
def list_providers(ctx, params):
    settings = GlobalSettings.get_settings()
    active = settings.active_ai_provider_id
    ai = [
        {
            "name": p.name,
            "type": p.provider_type,
            "model": p.default_model or None,
            "default": p.pk == active,
            "tested_at": iso(p.last_tested_at),
        }
        for p in AIProvider.objects.order_by("name")
    ]
    embedding = [
        {
            "name": p.name,
            "type": p.provider_type,
            "model": p.model,
            "dimension": p.dimension,
            "enabled": p.enabled,
            "tested_at": iso(p.last_tested_at),
        }
        for p in EmbeddingProvider.objects.order_by("name")
    ]
    return {
        "ai": {
            "enabled": settings.claude_enabled,
            "default": next((p["name"] for p in ai if p["default"]), None),
            "providers": ai,
            "usage_today": usage_today(ctx.workspace, settings),
        },
        "embedding": {"providers": embedding},
    }


# --------------------------------------------------------------------------- secrets


def secrets_by_script(workspace_id, scripts):
    """{script pk: the Secret rows a run of it gets}, the run resolver's selection.

    Every script in *All* mode gets the same set, so it is read once; a script in
    *Selected* mode gets its own (its plugin's secrets and its attached ones).
    """
    from core.executor import secrets_in_scope
    from core.services import EncryptionService

    if not EncryptionService.is_configured():
        return {s.pk: [] for s in scripts}  # nothing is injected without encryption
    everyone = None
    got = {}
    for script in scripts:
        if script.injection_mode == Script.InjectionMode.SELECTED:
            got[script.pk] = secrets_in_scope(
                workspace_id,
                mode="selected",
                owner_plugin=script.owner_plugin,
                script_id=script.pk,
            )
        else:
            if everyone is None:
                everyone = secrets_in_scope(workspace_id)
            got[script.pk] = everyone
    return got


@endpoint(
    "secrets.names",
    title="List secret names",
    description=(
        "Secret names in this workspace, never their values: who owns each (a plugin "
        "or nobody), where its value comes from, and the scripts that get it as an "
        "environment variable. Pass script (its id) for the secrets that one script gets."
    ),
    input={
        "type": "object",
        "properties": {
            "script": {"type": "string", "maxLength": 64,
                       "description": "A script id: only the secrets it gets"},
        },
    },
    permission="secrets:read_names",
)
def secret_names(ctx, params):
    target = resolve_script(ctx, params["script"]) if "script" in params else None
    scripts = list(
        scripts_for(ctx).only("id", "name", "injection_mode", "owner_plugin").order_by("name", "id")
    )
    got = secrets_by_script(ctx.workspace.pk, scripts)
    users = {}
    for script in scripts:
        for secret in got[script.pk]:
            users.setdefault(secret.pk, []).append(script)
    # The rows a script here can get: this workspace's, and any that belong to no
    # workspace (made before workspaces; the run resolver injects them too).
    rows = Secret.objects.filter(Q(workspace=ctx.workspace) | Q(workspace__isnull=True))
    if target is not None:
        rows = rows.filter(pk__in=[s.pk for s in got[target.pk]])
    entries = []
    for secret in sorted(rows, key=lambda s: (s.key, s.owner_plugin or "")):
        using = users.get(secret.pk, [])
        entries.append({
            "name": secret.get_clean_name(),
            "owner_plugin": secret.owner_plugin or None,
            "source": secret.source,
            "description": secret.description,
            "scripts": [script_ref(s) for s in using[:MAX_SCRIPTS_PER_SECRET]],
            "script_count": len(using),
        })
    result = {"secrets": entries, "count": len(entries)}
    if target is not None:
        result["script"] = {**script_ref(target), "injection_mode": target.injection_mode}
    return result


# --------------------------------------------------------------------------- channels


def channel_error(channel):
    """``last_error`` with the channel's own credentials masked; None if they can't be read."""
    from core.executor import mask_secrets_in_value

    if not channel.last_error:
        return None
    try:
        creds = channel.get_credentials()
    except Exception:
        logger.warning("channels api: credentials of channel %s can't be read", channel.pk)
        return None
    values = {k: v for k, v in creds.items() if isinstance(v, str)}
    return mask_secrets_in_value(channel.last_error, values)


@endpoint(
    "channels.list",
    title="List chat channels",
    description=(
        "Chat channels (Telegram) in this workspace: whether each is on and configured, "
        "what answers its inbound messages (a script or Py AI), its members by status, "
        "its daily reply cap and its last error. Never tokens."
    ),
    input=EMPTY,
    permission="channels:read",
)
def list_channels(ctx, params):
    channels = list(Channel.objects.for_workspace(ctx.workspace).order_by("name"))
    members = {}
    for row in (
        ChannelMember.objects.filter(channel__in=channels)
        .values("channel_id", "status")
        .annotate(n=Count("id"))
        .order_by()
    ):
        members.setdefault(row["channel_id"], {})[row["status"]] = row["n"]
    targets = {
        s.pk: s
        for s in scripts_for(ctx).filter(
            pk__in=[c.inbound_target_id for c in channels if c.inbound_target_id]
        ).only("id", "name")
    }
    entries = []
    for c in channels:
        counts = members.get(c.pk, {})
        script = targets.get(c.inbound_target_id)
        entries.append({
            "name": c.name,
            "provider": c.provider,
            "enabled": c.enabled,
            "configured": c.is_configured,
            "inbound": {
                "enabled": c.inbound_enabled,
                "handler": c.inbound_handler or None,
                "script": script_ref(script) if c.inbound_handler == "script" and script else None,
                "access": c.inbound_access,
            },
            "members": {
                status: counts.get(status, 0) for status in ChannelMember.Status.values
            },
            "daily_reply_cap": c.daily_reply_cap or None,
            "daily_cap_reached": c.daily_cap_reached(),
            "last_error": channel_error(c),
            "last_tested_at": iso(c.last_tested_at),
            "last_inbound_at": iso(c.last_inbound_at),
        })
    return {"channels": entries, "count": len(entries)}


# --------------------------------------------------------------------------- plugins


@endpoint(
    "plugins.list",
    title="List plugins",
    description=(
        "Plugins installed on this instance: name, version, status (active, installed, "
        "disabled or errored), source, summary and author."
    ),
    input=EMPTY,
    permission="plugins:read",
    pyai_tier="channel",
)
def list_plugins(ctx, params):
    plugins = list(Plugin.objects.order_by("name"))
    return {
        "plugins": [
            {
                "slug": p.slug,
                "name": p.name,
                "version": p.version or None,
                "status": p.status,
                "source": p.source,
                "summary": p.summary,
                "author": p.author,
                "installed_at": iso(p.installed_at),
                "activated_at": iso(p.activated_at),
            }
            for p in plugins
        ],
        "count": len(plugins),
    }


# --------------------------------------------------------------------------- environments


def requirement_lines(text):
    """The lines of an environment's saved requirements (no blanks or comments).

    PyRun saves the environment's ``pip freeze`` there after every package
    change, so this is what was installed then; ``packages`` is what is now.
    """
    lines = (line.strip() for line in (text or "").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def resolve_environment(name_or_id):
    pk = parse_id(name_or_id)
    matches = list(
        Environment.objects.filter(pk=pk) if pk is not None
        else Environment.objects.filter(name=name_or_id)
    )
    if not matches:
        raise ApiError("not_found", f"Environment '{name_or_id}' not found", status=404)
    if len(matches) > 1:
        raise ApiError(
            "invalid_input",
            f"$.environment: several environments are named '{name_or_id}'; pass its id.",
        )
    return matches[0]


@endpoint(
    "environments.list",
    title="List environments",
    description=(
        "Python environments on this instance (shared by every workspace): Python "
        "version, default, saved requirements, and how many of this workspace's "
        "scripts use each. Pass environment (a name or id) for that one environment "
        "with its installed packages, read from the environment itself."
    ),
    input={
        "type": "object",
        "properties": {
            "environment": {"type": "string", "maxLength": 100,
                            "description": "An environment's name or id: list its packages"},
        },
    },
    permission="environments:read",
    # With environment, a call runs pip list in that environment (seconds).
    rate=(30, 60),
    pyai_tier="channel",
)
def list_environments(ctx, params):
    from core.services.environment_service import EnvironmentService

    if "environment" in params:
        environments = [resolve_environment(params["environment"])]
    else:
        environments = list(Environment.objects.order_by("-is_default", "name"))
    used = dict(
        scripts_for(ctx)
        .filter(environment__in=environments)
        .values_list("environment_id")
        .annotate(n=Count("id"))
        .order_by()
    )
    entries = []
    for env in environments:
        entry = {
            "id": str(env.pk),
            "name": env.name,
            "description": env.description,
            "python_version": env.python_version or None,
            "default": env.is_default,
            "active": env.is_active,
            "requirements": requirement_lines(env.requirements),
            "scripts": used.get(env.pk, 0),
        }
        if "environment" in params:
            packages = EnvironmentService.get_installed_packages(env)
            entry["packages"] = sorted(packages, key=lambda p: p["name"].lower())
        entries.append(entry)
    return {"environments": entries, "count": len(entries)}


# --------------------------------------------------------------------------- libraries


@endpoint(
    "libraries.list",
    title="List libraries",
    description=(
        "Script libraries in this workspace: import name (key), current version, module "
        "names and the scripts they are attached to. Never their code."
    ),
    input=EMPTY,
    permission="libraries:read",
    pyai_tier="channel",
)
def list_libraries(ctx, params):
    libraries = list(Library.objects.for_workspace(ctx.workspace).order_by("key"))
    attached = {}
    for row in (
        ScriptLibrary.objects.filter(library__in=libraries, script__in=scripts_for(ctx))
        .values("library_id", "script_id", "script__name")
        .order_by("script__name", "script_id")
    ):
        attached.setdefault(row["library_id"], []).append(
            {"id": str(row["script_id"]), "name": row["script__name"]}
        )
    entries = []
    for lib in libraries:
        head = lib.head
        entries.append({
            "key": lib.key,
            "name": lib.name,
            "description": lib.description,
            "owner_plugin": lib.owner_plugin or None,
            "version": lib.current_version,
            "modules": sorted(head.modules) if head is not None else [],
            "scripts": attached.get(lib.pk, []),
            "updated_at": iso(lib.updated_at),
        })
    return {"libraries": entries, "count": len(entries)}


# --------------------------------------------------------------------------- schedules


@endpoint(
    "schedules.list",
    title="List schedules",
    description=(
        "Scheduled scripts in this workspace: mode, a readable summary, timezone, next "
        "and last scheduled run, and whether each is on. paused says the instance's "
        "Pause all switch is on: then no schedule starts runs."
    ),
    input={
        "type": "object",
        "properties": {
            "active": {"type": "boolean", "description": "Only schedules that are on (or off)"},
        },
    },
    permission="schedules:read",
    pyai_tier="channel",
)
def list_schedules(ctx, params):
    settings = GlobalSettings.get_settings()
    rows = (
        ScriptSchedule.objects.filter(script__in=scripts_for(ctx))
        .exclude(run_mode=ScriptSchedule.RunMode.MANUAL)
        .select_related("script")
        .defer("script__code")
        .order_by("script__name", "script_id")
    )
    entries = []
    for schedule in rows:
        summary = schedule_summary(schedule.script)
        if "active" in params and summary["active"] != params["active"]:
            continue
        entries.append({
            "script": {**script_ref(schedule.script), "enabled": schedule.script.is_enabled},
            **summary,
            "last_run_at": iso(schedule.last_scheduled_run),
        })
    return {
        "schedules": entries,
        "count": len(entries),
        "paused": settings.schedules_paused,
        "paused_at": iso(settings.schedules_paused_at) if settings.schedules_paused else None,
    }
