"""
core.plugins.api — the stable, versioned SDK facade for PyRun plugins (WS2).

A plugin orchestrates PyRun primitives (scripts, secrets, datastores,
schedules, environments, runs) THROUGH this module instead of importing
``core.models`` / ``core.tasks`` / ``core.services`` directly. The facade:

  * auto-stamps ownership — every resource a plugin creates carries the plugin's
    ``owner_plugin`` slug AND a ``workspace`` (the two scoping axes from the
    foundations), so it groups, filters, cleans up, and is delete-guarded;
  * is idempotent — ``upsert(...)`` keyed on ``(owner_plugin, owner_key)`` updates
    the same row instead of spawning duplicates on re-provision (the thing the
    qdrant reference plugin hand-rolled via a stored ``script_id``);
  * auto-names DataStores ``"<owner>:<key>"`` so two plugins never collide on the
    globally-/per-workspace-unique ``name`` while referring to a store by a short
    key, and injects owner-scoped secrets under their CLEAN name;
  * never bypasses the run/sandbox seams — runs go through ``queue_script_run``
    (→ RunBackend + ``resolve_isolation``); plugin scripts default to
    ``isolation_mode='inherit'`` so the DB isolation policy decides.

CONTRACT NOTES
- ``owner=None`` selects the LEGACY global/user lane (no ownership stamping), so a
  ported ``upsert_secret(key=...)`` keeps working and old plugins are never forced
  into ``injection_mode='selected'``.
- ``workspace=None`` resolves to the default workspace (the only authoritative,
  request-free source — same rule the scheduler uses).
- Every core import is LAZY (inside a function body), mirroring
  ``run_in_environment``, so importing this module never needs the app registry —
  the light-import boot guard stays intact and a plugin's ``apps.py`` can import
  the SDK without pulling in ``core.models``.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

# Import-light exception modules (no models or app registry), re-exported so
# plugins can catch service errors through the SDK.
from core.vectors.errors import VectorError  # noqa: F401 — part of the SDK surface
from core.api.exceptions import ApiError as BrainError  # noqa: F401 — SDK surface

# Plugins declare the SDK version they target in plugin.json (e.g. "2.0"); bump
# on a breaking change to the facade. The wrapped CORE services stay stable.
# 2.1 (additive): ScriptAPI run-lifecycle surface (latest_run/runs/
# cancel_latest_run + the RunView read-model).
# 2.2 (additive): DatabaseAPI — owner-scoped managed Postgres databases
# (provision/get/list/grant/dsn) on the attached data server.
# 2.3 (additive): external-API surface — @resource handler marker, APIRequest,
# APIError (docs/plugins.md) — plus read-only workspace-scoped
# ChannelAPI.list(), and public pages (@page, PageRequest, PublicPageAPI).
# 2.4 (additive): LibraryAPI — shared multi-module code attached to scripts and
# materialized onto a run's PYTHONPATH, pinned to the revision stamped at queue
# time.
# 2.5 (additive): StorageAPI — owner-scoped object storage on the instance's
# assets StorageConnection, every key forced under apps/<owner>/; worker scripts
# reach the same prefix through the pyrunner_storage helper.
# 2.6 (additive): VectorAPI — owner-scoped vector collections (embeddings +
# similarity search with a portable payload filter) on the Local store or
# Qdrant; worker scripts reach the same collections through the
# pyrunner_vectors helper.
# 2.7 (additive): BrainAPI — owned brains and grant-checked shared read handlers.
# PyRun 2.5 adds, before the one version bump at the end of its train:
# ConnectionAPI (+ ConnectionView, ConnectionRequirement, ConnectionUnavailable)
# and plugin.json ``requires_connections``; WatcherAPI, TriggerAdapter,
# TriggerContext, TriggerError and the plugin's ``triggers.py`` adapters.
API_VERSION = "2.7"


class AIProviderAPI:
    """Bounded, tool-free text calls through configured API-key profiles.

    Credentials stay in core. Subscription profiles require the agent runtime
    and are deliberately excluded from this direct Messages API surface.
    """

    @staticmethod
    def list():
        from core.models import AIProvider

        return list(AIProvider.objects.exclude(auth_method="subscription").order_by("name").values_list("name", flat=True))

    @staticmethod
    def complete(provider_name, *, system, prompt, timeout=25):
        from core.plugins.completion import complete

        return complete(provider_name, system=system, prompt=prompt, timeout=timeout)


# --------------------------------------------------------------------------- #
# Shared helpers (all lazy)
# --------------------------------------------------------------------------- #

def _resolve_workspace(workspace):
    """Return ``workspace`` as-is, or the default workspace when None."""
    if workspace is not None:
        return workspace
    from core.models import Workspace

    return Workspace.get_default()


def _resolve_environment(environment):
    """Accept an Environment instance or a name string; return the instance/None."""
    if environment is None or not isinstance(environment, str):
        return environment
    from core.models import Environment

    return Environment.objects.filter(name=environment).first()


# --------------------------------------------------------------------------- #
# Environments — SELECTED, never owned or created by plugins
# --------------------------------------------------------------------------- #

class EnvironmentAPI:
    """Read-only view of the shared Environments. Plugins SELECT one; they never
    build venvs or pip-install (that work belongs to PyRun's environment UI)."""

    def list(self):
        from core.models import Environment

        return list(Environment.objects.all().order_by("name"))

    def get(self, name):
        from core.models import Environment

        return Environment.objects.filter(name=name).first()


# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #

class SecretAPI:
    """Owner-scoped encrypted secrets. ``owner=None`` ⇒ legacy global/user lane."""

    def __init__(self, owner=None, workspace=None):
        self.owner = owner
        self._workspace = workspace

    def _ws(self):
        return _resolve_workspace(self._workspace)

    def _qs(self):
        from core.models import Secret

        ws = self._ws()
        qs = Secret.objects.filter(workspace=ws)
        if self.owner:
            return qs.filter(owner_plugin=self.owner)
        return qs.filter(owner_plugin__isnull=True)

    def upsert(self, key, value, *, description=None, owner_key=None):
        """Create or update a secret (idempotent on (owner_plugin, key, workspace)).

        The value is always (re-)encrypted. ``key`` is the clean env-var name the
        secret injects under; for an owned secret it is also the idempotency handle.
        """
        from core.models import Secret

        secret = self._qs().filter(key=key).first()
        if secret is None:
            secret = Secret(key=key, workspace=self._ws(), owner_plugin=self.owner)
        secret.owner_key = owner_key if owner_key is not None else (key if self.owner else None)
        if description is not None:
            secret.description = description
        secret.set_value(value)
        secret.save()
        return secret

    def get(self, key):
        return self._qs().filter(key=key).first()

    def list(self):
        return list(self._qs().order_by("key"))

    def grant(self, script, secret, *, active=True):
        """Attach ``secret`` to ``script`` for selected-mode injection (idempotent)."""
        from core.models import SecretGrant

        grant, created = SecretGrant.objects.get_or_create(
            script=script, secret=secret, defaults={"active": active}
        )
        if not created and grant.active != active:
            grant.active = active
            grant.save(update_fields=["active"])
        return grant


# --------------------------------------------------------------------------- #
# Connections — accounts on outside services (PyRun 2.5, T25)
# --------------------------------------------------------------------------- #

class ConnectionUnavailable(Exception):
    """A connection the plugin can't use right now.

    ``code`` says why: CONNECTION_NOT_FOUND (nothing connected for that key),
    CONNECTION_EXPIRED / CONNECTION_REVOKED (reconnect it), CONNECTION_REFRESH_FAILED
    (the provider is unreachable: try again later), CONNECTION_UNREADABLE,
    CONNECTION_UNSUPPORTED.
    """

    def __init__(self, message, code="CONNECTION_UNAVAILABLE"):
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass(frozen=True)
class ConnectionView:
    """What plugin code may know about a connection: never a credential."""

    id: str
    name: str
    provider: str
    auth_kind: str
    status: str
    account_label: str
    scopes: tuple
    key: Optional[str]  # the requirement key it serves for this plugin

    @classmethod
    def _from(cls, connection):
        return cls(
            id=str(connection.pk),
            name=connection.name,
            provider=connection.provider,
            auth_kind=connection.auth_kind,
            status=connection.status,
            account_label=connection.account_label,
            scopes=tuple(connection.scopes or ()),
            key=connection.owner_key,
        )


@dataclass(frozen=True)
class ConnectionRequirement:
    """A requirement and how it stands.

    ``status`` is missing, ok, missing_scopes, expired, revoked or error;
    ``missing_scopes`` lists the asked-for scopes the account didn't grant.
    """

    key: str
    provider: str
    scopes: tuple
    label: str
    status: str
    connection: Optional[ConnectionView]
    missing_scopes: tuple = ()

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class ConnectionAPI:
    """The accounts a plugin signs in to (Google, GitHub, an SMTP login...).

    A plugin declares what it needs in ``plugin.json`` (``requires_connections``);
    the user connects it, or picks one of the workspace's connections, on the
    plugin page. The connection serving requirement ``key`` is the one this
    plugin holds for it in the workspace. Plugin scripts get it through
    ``grant()`` and read it with ``pyrunner_connections``, like any script;
    plugin code in the web process reads it with ``credentials()``. Refresh
    tokens and client secrets never leave PyRun.
    """

    def __init__(self, owner, workspace=None):
        if not owner:
            raise ValueError("ConnectionAPI needs the plugin's slug: connections are held per plugin.")
        self.owner = owner
        self._workspace = workspace

    def _ws(self):
        return _resolve_workspace(self._workspace)

    def _connection(self, key):
        from core.services.plugin_connections import connection_for

        return connection_for(self._ws(), self.owner, key)

    def require(self, provider, scopes=(), label="", *, key=None):
        """How the requirement (``provider``, ``scopes``, ``label``) stands: a ConnectionRequirement.

        ``key`` defaults to ``provider``. Raises ValueError for an unknown
        provider, a bad key or bad scopes, as the doctor does for the manifest.
        Saves nothing: connecting happens on the plugin page, which offers the
        manifest's entries.
        """
        from core.services.plugin_connections import parse_requirement, status_of

        entry = {"provider": provider, "scopes": list(scopes or ()), "label": label or ""}
        if key is not None:
            entry["key"] = key
        requirement = parse_requirement(entry)
        connection = self._connection(requirement.key)
        status, missing = status_of(requirement, connection)
        return ConnectionRequirement(
            key=requirement.key,
            provider=requirement.provider,
            scopes=requirement.scopes,
            label=requirement.label,
            status=status,
            connection=ConnectionView._from(connection) if connection is not None else None,
            missing_scopes=tuple(missing),
        )

    def requirements(self):
        """The manifest's ``requires_connections`` with their state in the workspace.

        Pass it to the ``cpanel/plugins/_connections.html`` include on the
        plugin page (Connect, pick a connection, stop using it).
        """
        from core.models import Plugin
        from core.services.plugin_connections import states_for

        plugin = Plugin.objects.filter(slug=self.owner).first()
        return states_for(self._ws(), self.owner, plugin.manifest if plugin else {})

    def get(self, key):
        """The connection serving requirement ``key`` (a ConnectionView), or None."""
        connection = self._connection(key)
        return ConnectionView._from(connection) if connection is not None else None

    def list(self):
        """Every connection this plugin holds in the workspace, by name."""
        from core.models import Connection

        return [
            ConnectionView._from(c)
            for c in Connection.objects.filter(workspace=self._ws(), owner_plugin=self.owner).order_by("name")
        ]

    def grant(self, script, connection, *, active=True):
        """Grant ``connection`` (a key, a ConnectionView or a Connection) to ``script``.

        Idempotent, like ``SecretAPI.grant``; ``active=False`` keeps the grant
        but gives nothing. Raises ConnectionUnavailable when nothing serves the
        key, ValueError when the script is in another workspace.
        """
        from core.models import Connection, ConnectionGrant

        if isinstance(connection, str):
            row = self._connection(connection)
            if row is None:
                raise ConnectionUnavailable(
                    f"Nothing is connected for '{connection}' yet.", "CONNECTION_NOT_FOUND"
                )
        elif isinstance(connection, ConnectionView):
            row = Connection.objects.filter(pk=connection.id).first()
            if row is None:
                raise ConnectionUnavailable(f"Connection '{connection.name}' was deleted.", "CONNECTION_NOT_FOUND")
        else:
            row = connection
        if script.workspace_id != row.workspace_id:
            raise ValueError("A connection can only be granted to a script of its own workspace.")
        grant, created = ConnectionGrant.objects.get_or_create(
            script=script, connection=row, defaults={"active": active}
        )
        if not created and grant.active != active:
            grant.active = active
            grant.save(update_fields=["active"])
        return grant

    def credentials(self, key):
        """Fresh auth material for plugin code outside a run (a page, a "Send now" button).

        The dict a script's ``pyrunner_connections.credentials()`` gets:
        ``headers``, ``params``, ``token``, ``base_url`` (``basic`` / ``smtp``
        per auth kind) and, for OAuth, ``expires_in``. An OAuth access token is
        refreshed first when it is about to expire. Never a refresh token or a
        client secret. Raises ConnectionUnavailable.
        """
        from core.services.connections import ConnectionLookupError, credentials_for

        connection = self._connection(key)
        if connection is None:
            raise ConnectionUnavailable(f"Nothing is connected for '{key}' yet.", "CONNECTION_NOT_FOUND")
        try:
            return credentials_for(connection)
        except ConnectionLookupError as e:
            raise ConnectionUnavailable(e.message, e.code) from None


# --------------------------------------------------------------------------- #
# Libraries — shared multi-module code, attached to scripts
# --------------------------------------------------------------------------- #

class LibraryAPI:
    """Owner-scoped shared Python code that scripts attach and import.

    The seam for any plugin bigger than one file: bundle the pipeline once as a
    Library and ``attach()`` it to each worker script, instead of duplicating it
    into every script's code. The canonical provisioning shape is

        libs = LibraryAPI(OWNER)
        lib = libs.upsert("myplugin_lib", modules=read_lib_folder())
        for script in workers:
            libs.attach(script, lib)

    ``upsert`` is idempotent BY CONTENT: re-provisioning on every settings save
    writes a new revision only when the code actually changed, so history stays
    signal rather than one entry per save.

    Note ``key`` is a Python import name (``from myplugin_lib.pipeline import
    run``), so unlike DataStoreAPI it is NOT auto-prefixed with the owner slug —
    ``"myplugin:lib"`` is not importable. Keys are unique per workspace; prefix
    yours with the plugin slug to stay collision-free.
    """

    def __init__(self, owner=None, workspace=None):
        self.owner = owner
        self._workspace = workspace

    def _ws(self):
        return _resolve_workspace(self._workspace)

    def _qs(self):
        from core.models import Library

        qs = Library.objects.filter(workspace=self._ws())
        if self.owner:
            return qs.filter(owner_plugin=self.owner)
        return qs.filter(owner_plugin__isnull=True)

    def upsert(self, key, *, modules, name=None, description=None, owner_key=None,
               created_by=None):
        """Create or update an owned library and save ``modules`` as a revision.

        Idempotent on ``(owner_plugin, owner_key)`` — ``owner_key`` defaults to
        ``key``, so a plugin may rename the import key without orphaning the row.
        A new revision is written ONLY if the module content changed.

        Raises ``ValueError`` if the key is not a legal import name, is reserved,
        or is already taken in this workspace by a different owner — never
        silently hijacks another owner's library.
        """
        from django.core.exceptions import ValidationError

        from core.models import Library
        from core.models.library import validate_library_key, validate_modules

        try:
            validate_library_key(key)
            validate_modules(modules)
        except ValidationError as e:
            raise ValueError("; ".join(e.messages)) from e

        handle = owner_key if owner_key is not None else key
        library = self._qs().filter(owner_key=handle).first() if self.owner else None
        if library is None:
            library = self._qs().filter(key=key).first()

        # The key is unique per workspace across ALL owners, so a row here that
        # isn't the one we're upserting belongs to someone else. Checked for both
        # create AND rename (a plugin changing its import key), so provisioning
        # gets a named error instead of an IntegrityError from deep in save().
        clash = Library.objects.filter(workspace=self._ws(), key=key)
        if library is not None:
            clash = clash.exclude(pk=library.pk)
        clash = clash.first()
        if clash is not None:
            owner_desc = (
                f"plugin '{clash.owner_plugin}'" if clash.owner_plugin else "a user"
            )
            raise ValueError(
                f"Library key '{key}' is already used in this workspace by {owner_desc}."
            )

        if library is None:
            library = Library(
                key=key,
                workspace=self._ws(),
                owner_plugin=self.owner,
                created_by=created_by,
            )

        library.key = key
        library.owner_key = handle if self.owner else None
        if name is not None:
            library.name = name
        elif not library.name:
            library.name = key
        if description is not None:
            library.description = description
        library.save()

        library.save_revision(modules, created_by=created_by)
        return library

    def upsert_from_folder(self, key, folder, **kwargs):
        """``upsert`` with ``modules`` read from ``folder``'s top-level ``*.py`` files.

        The canonical provisioning call: point it at the plugin's bundled ``lib/``
        folder and the folder becomes the library. Uses the same reader the dev-mode
        auto-resync uses, so what you get on activation and what you get from a
        dev-mode edit can never diverge.

        Raises ``ValueError`` if the folder has no modules — a silent no-op here
        would leave the plugin's scripts importing nothing.
        """
        from core.services.library_service import read_library_folder

        modules = read_library_folder(folder)
        if not modules:
            raise ValueError(
                f"upsert_from_folder: no .py modules found in {folder!r}"
            )
        return self.upsert(key, modules=modules, **kwargs)

    def get(self, key):
        return self._qs().filter(key=key).first()

    def list(self):
        return list(self._qs().order_by("key"))

    def attach(self, script, library):
        """Attach ``library`` to ``script`` so its runs can import it (idempotent).

        Raises ``ValueError`` on a cross-workspace attach — a script must never
        import code its workspace cannot see.
        """
        from django.core.exceptions import ValidationError

        from core.models import ScriptLibrary

        existing = ScriptLibrary.objects.filter(script=script, library=library).first()
        if existing is not None:
            return existing

        attachment = ScriptLibrary(script=script, library=library)
        try:
            attachment.full_clean()
        except ValidationError as e:
            raise ValueError("; ".join(e.messages)) from e
        attachment.save()
        return attachment

    def detach(self, script, library):
        """Detach ``library`` from ``script``. Returns True if an attachment went away."""
        from core.models import ScriptLibrary

        deleted, _ = ScriptLibrary.objects.filter(
            script=script, library=library
        ).delete()
        return bool(deleted)


# --------------------------------------------------------------------------- #
# Runs — a read-model DECOUPLED from the ORM
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class RunView:
    """An immutable, ORM-free snapshot of a single Run.

    Plugins OBSERVE runs through this read-model instead of holding a live
    ``core.models.Run``: no ORM object leaks past the SDK boundary, so a plugin's
    status endpoint can't accidentally trigger a query or couple itself to the
    core schema (the very coupling the "sdk-usage" doctor warn discourages).
    ``as_dict()`` returns only JSON-serializable values (datetimes → ISO 8601),
    so a plugin can hand it straight to ``JsonResponse``.

    ``inputs``, ``result`` and ``progress`` (2.5) carry what the run started
    with and what its script reported through ``pyrunner_run``, all with secret
    values masked. ``inputs`` is None when the script declares none, and also
    when a secret can't be resolved (the values can't be checked, so they are
    withheld, as on the run page). ``result`` is the dict the script set;
    ``progress`` is ``{"pct", "message", "updated_at"}``. Both None until set.

    ``cancel_reason`` is ``"concurrency_skip"`` on a CANCELLED run the
    script's Skip concurrency policy didn't start, ``"maintenance"`` on one a
    maintenance window didn't start, "" otherwise. ``is_held``
    is True while a Queue-policy run waits for the runs ahead of it.
    ``stop_requested_at`` is when someone stopped the run: while it is set
    and the run is still RUNNING, its script is shutting down.
    """

    id: str
    status: str
    trigger_type: str
    created_at: Optional[datetime]
    started_at: Optional[datetime]
    ended_at: Optional[datetime]
    duration: Optional[float]
    exit_code: Optional[int]
    pid: Optional[int]
    task_id: str
    is_finished: bool
    is_running: bool
    inputs: Optional[dict] = None
    result: Optional[dict] = None
    progress: Optional[dict] = None
    cancel_reason: str = ""
    is_held: bool = False
    stop_requested_at: Optional[datetime] = None

    @classmethod
    def _from_run(cls, run, masks=None):
        """Build a RunView from a Run instance (no module-top core import — the
        instance carries its own ``Status`` choices, so we stay import-light).

        ``masks`` is an optional dict shared across one listing, so runs of the
        same script and workspace resolve their secret set once.
        """
        return cls(
            id=str(run.id),
            status=str(run.status),
            trigger_type=str(run.trigger_type),
            created_at=run.created_at,
            started_at=run.started_at,
            ended_at=run.ended_at,
            duration=run.duration,
            exit_code=run.exit_code,
            pid=run.pid,
            task_id=run.task_id or "",
            is_finished=run.is_finished,
            is_running=run.status == run.Status.RUNNING,
            inputs=_masked_run_inputs(run, masks if masks is not None else {}),
            result=run.result,
            progress=run.progress,
            cancel_reason=run.cancel_reason or "",
            is_held=run.is_held,
            stop_requested_at=run.stop_requested_at,
        )

    def as_dict(self) -> dict:
        """JSON-serializable dict (datetimes → ISO 8601) for a status endpoint."""

        def _iso(value):
            return value.isoformat() if value is not None else None

        return {
            "id": self.id,
            "status": self.status,
            "trigger_type": self.trigger_type,
            "created_at": _iso(self.created_at),
            "started_at": _iso(self.started_at),
            "ended_at": _iso(self.ended_at),
            "duration": self.duration,
            "exit_code": self.exit_code,
            "pid": self.pid,
            "task_id": self.task_id,
            "is_finished": self.is_finished,
            "is_running": self.is_running,
            "inputs": self.inputs,
            "result": self.result,
            "progress": self.progress,
            "cancel_reason": self.cancel_reason,
            "is_held": self.is_held,
            "stop_requested_at": _iso(self.stop_requested_at),
        }


def _masked_run_inputs(run, masks: dict):
    """A run's inputs with secret values masked, or None (none, or can't check).

    The same masking the run page's Inputs card applies; ``masks`` caches each
    (workspace, script) secret set for the duration of one listing.
    """
    from core.executor import mask_run_inputs
    from core.services.secret_backends import SecretResolutionError

    try:
        return mask_run_inputs(run, masks)
    except SecretResolutionError:
        return None


# --------------------------------------------------------------------------- #
# Scripts
# --------------------------------------------------------------------------- #

class InputsError(ValueError):
    """``ScriptAPI.queue_run`` refused the inputs; no run was created.

    ``message`` is one sentence naming every bad input and ``field_errors``
    maps each input name to its reason, so a plugin form can show them next
    to its fields. A subclass of ValueError, so an ``except ValueError`` that
    already guards ``queue_run`` keeps working.
    """

    def __init__(self, message, field_errors=None):
        super().__init__(message)
        self.message = str(message)
        self.field_errors = dict(field_errors or {})


def _active_schedule(script):
    """The script's schedule when it fires on its own (not manual, not paused)."""
    from core.models import ScriptSchedule

    schedule = ScriptSchedule.objects.filter(script_id=script.pk).first()
    return schedule if schedule is not None and schedule.is_scheduled else None


class ScriptAPI:
    """Owner-scoped scripts. ``upsert`` is idempotent on (owner_plugin, owner_key)."""

    def __init__(self, owner=None, workspace=None):
        self.owner = owner
        self._workspace = workspace

    def _ws(self):
        return _resolve_workspace(self._workspace)

    def _qs(self):
        from core.models import Script

        ws = self._ws()
        qs = Script.objects.filter(workspace=ws)
        if self.owner:
            return qs.filter(owner_plugin=self.owner)
        return qs.filter(owner_plugin__isnull=True)

    def upsert(
        self,
        *,
        key=None,
        name=None,
        code=None,
        environment=None,
        timeout_seconds=None,
        injection_mode=None,
        description=None,
        is_enabled=None,
        notify_on=None,
        notify_email=None,
        created_by=None,
        inputs_schema=None,
        concurrency_policy=None,
    ):
        """Create or update an owned script (idempotent on (owner_plugin, owner_key)).

        ``key`` is the stable per-owner handle (required for owned scripts). The
        human-facing ``name`` is auto-derived from it if omitted. Plugin-owned
        scripts default to ``injection_mode='selected'`` (so they receive only
        granted and same-owner secrets); ``isolation_mode`` is left at the model
        default ``'inherit'`` so ``resolve_isolation`` (the sandbox policy) decides.

        ``inputs_schema`` declares the inputs the script takes: an object JSON
        Schema in the subset the script editor's Inputs tab writes (see
        "Inputs and results" in docs/plugins.md). It is checked the way the editor checks it, and
        ``{}`` removes the declaration; None, like every other argument here,
        leaves it as it is. A schema whose required inputs have no default is
        refused while the script has an active schedule, because a scheduled
        run would have nothing to fill them with. Raises ValueError, before
        anything is saved.

        ``concurrency_policy`` says what a trigger does while the script
        already has a run in flight: ``"allow"`` (the default) starts another
        run, ``"skip"`` creates the run as CANCELLED with ``cancel_reason``
        ``"concurrency_skip"``, ``"queue"`` holds it and starts it when the
        runs ahead of it end. It applies to every trigger, ``queue_run`` too.
        """
        from core.models import Script
        from core.services.script_inputs import check_inputs_schema, schedule_conflict

        if self.owner and not key:
            raise ValueError("ScriptAPI.upsert requires key= for an owned script")
        if concurrency_policy is not None and concurrency_policy not in Script.ConcurrencyPolicy.values:
            raise ValueError(
                f"concurrency_policy must be one of {', '.join(Script.ConcurrencyPolicy.values)}"
            )
        set_schema = inputs_schema is not None
        if set_schema:
            # None from here on = the script declares no inputs.
            inputs_schema = check_inputs_schema(inputs_schema)

        if self.owner:
            script = self._qs().filter(owner_key=key).first()
        else:
            # Legacy lane: match by name within the workspace (owner-NULL).
            script = self._qs().filter(name=name).first() if name else None

        creating = script is None
        if set_schema and not creating and _active_schedule(script):
            conflict = schedule_conflict(inputs_schema)
            if conflict:
                raise ValueError(f"The script has an active schedule. {conflict}")
        if creating:
            script = Script(workspace=self._ws(), owner_plugin=self.owner, owner_key=key)
            # Default scoped injection for owned scripts; legacy stays 'all'.
            script.injection_mode = injection_mode or (
                Script.InjectionMode.SELECTED if self.owner else Script.InjectionMode.ALL
            )
            if created_by is not None:
                script.created_by = created_by

        script.name = name or script.name or (f"{self.owner}:{key}" if self.owner else key)
        if code is not None:
            script.code = code
        if injection_mode is not None:
            script.injection_mode = injection_mode
        if description is not None:
            script.description = description
        if is_enabled is not None:
            script.is_enabled = is_enabled
        if notify_on is not None:
            script.notify_on = notify_on
        if notify_email is not None:
            script.notify_email = notify_email
        if timeout_seconds is not None:
            script.timeout_seconds = timeout_seconds
        if set_schema:
            script.inputs_schema = inputs_schema
        if concurrency_policy is not None:
            script.concurrency_policy = concurrency_policy

        env = _resolve_environment(environment)
        if env is not None:
            script.environment = env
        elif creating:
            raise ValueError(
                "ScriptAPI.upsert requires environment= (instance or name) on create"
            )

        script.save()
        return script

    def set_environment(self, environment):
        """Bulk-set the Environment on EVERY script this plugin owns (one call).

        The "pick a venv on the plugin page, all connected scripts follow"
        behavior. No-op in the legacy (owner=None) lane. Returns the count updated.
        """
        if not self.owner:
            return 0
        env = _resolve_environment(environment)
        if env is None:
            raise ValueError("set_environment: unknown environment")
        return self._qs().update(environment=env)

    def queue_run(self, key, *, triggered_by=None, inputs=None):
        """Queue a tracked Run for the owned script ``key`` (via the RunBackend seam).

        ``inputs`` is a dict of values for the inputs the script declares
        (``upsert(inputs_schema=)``); defaults fill in whatever is left out.
        They are checked before the run exists: bad inputs raise
        :class:`InputsError` and create no run, not even a failed one.

        The script's concurrency policy applies (``upsert(concurrency_policy=)``):
        while the script has a run in flight, a Skip-policy script returns the
        run already CANCELLED (``run.cancel_reason == "concurrency_skip"``)
        and a Queue-policy one returns it held (PENDING, ``run.is_held``).
        A maintenance window that blocks manual runs returns it CANCELLED
        with ``run.cancel_reason == "maintenance"``.
        """
        from core.models import Run
        from core.services.run_events import plugin_event
        from core.services.script_inputs import ScriptInputsError, prepare_inputs
        from core.tasks import queue_script_run

        script = self.get(key)
        if script is None:
            raise ValueError(f"No owned script with key={key!r}")
        try:
            prepared = prepare_inputs(script, inputs)
            run = Run.objects.create(
                script=script,
                workspace_id=script.workspace_id,
                status=Run.Status.PENDING,
                triggered_by=triggered_by,
                trigger_type=Run.TriggerType.MANUAL,
                code_snapshot=script.code,
                inputs=prepared,
                event=plugin_event(self.owner, key, triggered_by),
            )
            # Checks the inputs again against the schema as it is now, and
            # deletes the pending row if it changed in between.
            queue_script_run(run)
        except ScriptInputsError as e:
            raise InputsError(e.message, e.field_errors) from None
        return run

    def get(self, key):
        if self.owner:
            return self._qs().filter(owner_key=key).first()
        return self._qs().filter(name=key).first()

    def list(self):
        return list(self._qs().order_by("name"))

    # -- Run lifecycle: OBSERVE + CONTROL a provisioned script's runs --------- #

    def _runs_qs(self, key):
        """Runs of the owned script ``key``, newest first (owner+workspace scoped).

        Scoping is exact and free: we resolve the single Script via ``get()``
        (already owner+workspace filtered) and return its runs, so a plugin can
        never observe another owner's or another workspace's runs. Empty when no
        such owned script exists.
        """
        from core.models import Run

        script = self.get(key)
        if script is None:
            return Run.objects.none()
        return Run.objects.filter(script=script).order_by("-created_at")

    def latest_run(self, key):
        """Return the most recent Run of the owned script ``key`` as a RunView,
        or None when the script has never run (or doesn't exist). Poll this for a
        plugin's live status badge.

        Runs the Skip concurrency policy or a maintenance window didn't
        start are passed over: a trigger skipped while a run is in flight must
        not hide that run from the badge. ``runs()`` lists them."""
        from core.models import Run

        run = (
            self._runs_qs(key)
            .exclude(
                cancel_reason__in=[
                    Run.CancelReason.CONCURRENCY_SKIP,
                    Run.CancelReason.MAINTENANCE,
                ]
            )
            .first()
        )
        return RunView._from_run(run) if run is not None else None

    def runs(self, key, *, limit=20):
        """Return recent run history (newest first) as RunViews, owner-scoped.

        ``limit`` caps the count (default 20); a non-positive limit yields []. The
        result is a list of plain RunViews — no ORM objects leak to the caller.
        """
        masks = {}
        return [RunView._from_run(r, masks) for r in self._runs_qs(key)[: max(0, int(limit))]]

    def cancel_latest_run(self, key):
        """Cancel the latest pending/running run of the owned script ``key``.

        Reuses the shared force-stop path (``TaskService.force_stop_run``) — the
        SAME code the tasks Stop button calls. A PENDING run is dequeued and
        flipped to CANCELLED at once. A RUNNING run is stopped gracefully: its
        script gets SIGTERM and its stop grace period to shut down, and the run
        turns CANCELLED once the process has ended (``RunView.stop_requested_at``
        is set meanwhile). Calling this again while it is shutting down kills
        it at once. Returns True if a run was cancelled or asked to stop, False
        when nothing was cancellable (no run in a pending/running state, or no
        such owned script). Drives a plugin's "Stop" button without importing
        core.models.
        """
        from core.models import Run
        from core.services.task_service import TaskService

        script = self.get(key)
        if script is None:
            return False
        run = (
            Run.objects.filter(
                script=script,
                status__in=[Run.Status.RUNNING, Run.Status.PENDING],
            )
            .order_by("-created_at")
            .first()
        )
        if run is None:
            return False
        changed, _msg = TaskService.force_stop_run(run)
        return changed


# --------------------------------------------------------------------------- #
# DataStores — the plugin's database (no plugin models/migrations)
# --------------------------------------------------------------------------- #

class _OwnedDataStore:
    """Thin dict-ish handle over a DataStore's entries (JSON values), via the ORM."""

    def __init__(self, store):
        self._store = store

    @property
    def name(self):
        return self._store.name

    @property
    def model(self):
        return self._store

    def set(self, key, value):
        from core.models import DataStoreEntry

        DataStoreEntry.objects.update_or_create(
            datastore=self._store, key=key, defaults={"value_json": json.dumps(value)}
        )

    def get(self, key, default=None):
        from core.models import DataStoreEntry

        entry = DataStoreEntry.objects.filter(datastore=self._store, key=key).first()
        return json.loads(entry.value_json) if entry is not None else default

    def all(self):
        from core.models import DataStoreEntry

        return {
            e.key: json.loads(e.value_json)
            for e in DataStoreEntry.objects.filter(datastore=self._store)
        }


class DataStoreAPI:
    """Owner-scoped key-value stores. The stored ``name`` is auto-derived
    ``"<owner>:<key>"`` (owner=None ⇒ the raw key), keeping ``name`` unique while
    the plugin refers to it by a short key."""

    def __init__(self, owner=None, workspace=None):
        self.owner = owner
        self._workspace = workspace

    def _ws(self):
        return _resolve_workspace(self._workspace)

    def _name_for(self, key):
        return f"{self.owner}:{key}" if self.owner else key

    def upsert(self, key, *, description=None, created_by=None):
        """Ensure the owned store exists (idempotent); return a handle for entries."""
        from core.models import DataStore

        name = self._name_for(key)
        store = DataStore.objects.filter(workspace=self._ws(), name=name).first()
        if store is None:
            store = DataStore(name=name, workspace=self._ws())
            if created_by is not None:
                store.created_by = created_by
        store.owner_plugin = self.owner
        store.owner_key = key
        if description is not None:
            store.description = description
        store.save()
        return _OwnedDataStore(store)

    def get(self, key):
        from core.models import DataStore

        store = DataStore.objects.filter(
            workspace=self._ws(), name=self._name_for(key)
        ).first()
        return _OwnedDataStore(store) if store is not None else None

    def list(self):
        from core.models import DataStore

        qs = DataStore.objects.filter(workspace=self._ws())
        qs = qs.filter(owner_plugin=self.owner) if self.owner else qs.filter(
            owner_plugin__isnull=True
        )
        return [_OwnedDataStore(s) for s in qs.order_by("name")]


# --------------------------------------------------------------------------- #
# Databases — real SQL (managed Postgres schemas on the data server)
# --------------------------------------------------------------------------- #

class DatabaseAPI:
    """Owner-scoped managed SQL databases — a real Postgres schema + role per
    database, isolated by Postgres itself. The stored ``name`` is auto-derived
    ``"<owner>:<key>"`` (owner=None ⇒ the raw key), exactly like DataStoreAPI.

    ``provision()`` creates REAL server-side objects, so the instance must have
    a data server attached (``PYRUNNER_DATA_DB_URL``); it raises
    ``DatabaseProvisionError`` otherwise — call ``is_available()`` first for a
    graceful degrade path. The plugin's WORKER script then connects with
    ``pyrunner_db.connect("<owner>:<key>")`` after a ``grant()``; the plugin's
    own VIEWS (a dashboard reading its tables) use ``dsn()`` + psycopg.
    """

    def __init__(self, owner=None, workspace=None):
        self.owner = owner
        self._workspace = workspace

    def _ws(self):
        return _resolve_workspace(self._workspace)

    def _name_for(self, key):
        return f"{self.owner}:{key}" if self.owner else key

    def is_available(self):
        """Whether this instance has a data server attached."""
        from core.services import DatabaseService

        return DatabaseService.is_configured()

    def provision(self, key, *, description=None, created_by=None):
        """Ensure the owned database exists AND is provisioned server-side.

        Idempotent on the auto-derived name: re-running updates the same row and
        re-stamps the server objects (which also heals a row stuck in
        ``status='error'`` from an earlier failure). Raises
        ``DatabaseProvisionError`` when the data server is unreachable or not
        configured — fail-closed, never a silently-unusable database.
        """
        from core.models import Database
        from core.services import DatabaseService

        name = self._name_for(key)
        database = Database.objects.filter(workspace=self._ws(), name=name).first()
        if database is None:
            return DatabaseService.create_database(
                name=name,
                workspace=self._ws(),
                description=description or "",
                created_by=created_by,
                owner_plugin=self.owner,
                owner_key=key if self.owner else None,
            )

        database.owner_plugin = self.owner
        database.owner_key = key if self.owner else None
        if description is not None:
            database.description = description
        database.save()
        DatabaseService.provision(database)
        return database

    def get(self, key):
        from core.models import Database

        return Database.objects.filter(
            workspace=self._ws(), name=self._name_for(key)
        ).first()

    def list(self):
        from core.models import Database

        qs = Database.objects.filter(workspace=self._ws())
        qs = (
            qs.filter(owner_plugin=self.owner)
            if self.owner
            else qs.filter(owner_plugin__isnull=True)
        )
        return list(qs.order_by("name"))

    def grant(self, script, database, *, active=True):
        """Attach ``database`` to ``script`` so its runs can connect (idempotent).

        Database access is EXPLICIT-ONLY — unlike secrets there is no 'all'
        injection mode, so a plugin's worker script needs this grant before
        ``pyrunner_db`` will resolve the database's credentials.
        """
        from core.models import DatabaseGrant

        grant, created = DatabaseGrant.objects.get_or_create(
            script=script, database=database, defaults={"active": active}
        )
        if not created and grant.active != active:
            grant.active = active
            grant.save(update_fields=["active"])
        return grant

    def dsn(self, key):
        """The scoped DSN of the owned database, or None when it doesn't exist
        or isn't ready. For the plugin's OWN server-side code (e.g. a dashboard
        view reading its tables with psycopg) — handle it like a password; the
        connection is confined to the database's schema either way."""
        from core.services import DatabaseService

        database = self.get(key)
        if database is None or not database.is_ready:
            return None
        return DatabaseService.scoped_dsn(database)


# --------------------------------------------------------------------------- #
# Object storage
# --------------------------------------------------------------------------- #

class StorageKeyError(ValueError):
    """Raised when a storage key would escape the owner's prefix, or when the
    owner is missing. Loud on purpose: silently sanitizing a traversal attempt
    turns it into a write to the WRONG key, which is worse than a refusal."""


class StorageError(Exception):
    """Raised when a storage operation fails or the seam is unavailable."""


class StorageAPI:
    """Owner- and workspace-scoped object storage on the assets connection.

    Every key is forced under ``apps/<owner>/<workspace-id>/``, and callers only
    ever see keys RELATIVE to that prefix — a plugin cannot name, read, or delete
    another plugin's object, nor another workspace's, because it has no way to
    express one.

    Both scoping axes match the rest of the SDK: ``owner`` stamps the resource
    like every other facade, and ``workspace`` scopes it like Secrets/DataStores/
    Databases (``workspace=None`` ⇒ the default workspace, the same rule the
    scheduler uses). The plugin slug comes FIRST in the key so uninstall is one
    prefix delete across every workspace — see ``delete_all``.

    The connection itself is instance-global (Services → Object Storage), like
    the active AI provider: buckets and credentials are operator infrastructure,
    not tenant data. Without one the seam is unavailable and every operation
    raises ``StorageError``; call ``is_available()`` first for a graceful degrade
    path, exactly like ``DatabaseAPI``.

        storage = StorageAPI(OWNER)
        if storage.is_available():
            storage.put("avatars/abc.jpg", data, content_type="image/jpeg")
            url = storage.url("avatars/abc.jpg")

    ``url()`` returns a permanent public URL when the connection has a
    ``public_base_url``, else a presigned URL that EXPIRES (default 1 hour, 7-day
    ceiling). Bake presigned URLs into a long-lived page or an email and they will
    rot; that is what the public base URL is for.
    """

    PREFIX_ROOT = "apps"

    def __init__(self, owner=None, workspace=None):
        # Unlike Secrets/DataStores there is NO unowned lane: a key with no owner
        # prefix is exactly the escape this class exists to prevent.
        if not owner:
            raise StorageKeyError("StorageAPI requires an owner (plugin slug)")
        self.owner = owner
        # Resolved lazily (see _ws_id): uninstall builds a StorageAPI purely to
        # call delete_all(), which is workspace-independent and must not require a
        # default workspace to exist.
        self._workspace = workspace

    # ------------------------------------------------------------------ #
    # Prefix enforcement
    # ------------------------------------------------------------------ #

    def _ws_id(self):
        """The id of the workspace this instance addresses.

        Accepts a Workspace instance OR a bare id, because the loopback endpoint
        derives only an id from the run's signed token. Raises rather than
        building an ``apps/<owner>/None/`` prefix — a key that silently pools
        every workspace's objects is the leak this scoping prevents.
        """
        from core.models import Workspace

        workspace = self._workspace
        if workspace is None:
            workspace = Workspace.get_default()
        if workspace is None:
            raise StorageError("No workspace is available to scope storage to.")
        return getattr(workspace, "pk", workspace)

    @property
    def owner_root(self):
        """Every workspace's space for this owner, e.g. ``apps/yt_comments/``.

        Only uninstall addresses this level; ordinary calls go through ``prefix``.
        """
        return f"{self.PREFIX_ROOT}/{self.owner}/"

    @property
    def prefix(self):
        """This owner's key prefix in THIS workspace,
        e.g. ``apps/yt_comments/3f2b…/``."""
        return f"{self.owner_root}{self._ws_id()}/"

    def _full_key(self, key):
        """Validate a caller key and return the absolute bucket key.

        Rejects (never rewrites): empty keys, absolute keys, backslashes, NUL, and
        any ``..`` segment. Backslashes are rejected rather than normalized
        because S3 treats ``\\`` as an ordinary character while a Windows-minded
        caller means a separator — the two readings disagree, so refuse.
        """
        if not isinstance(key, str) or not key.strip():
            raise StorageKeyError("Storage key must be a non-empty string")

        key = key.strip()

        if key.startswith("/"):
            raise StorageKeyError(f"Storage key must be relative, got: {key!r}")
        if "\\" in key:
            raise StorageKeyError(f"Storage key must not contain backslashes: {key!r}")
        if "\x00" in key:
            raise StorageKeyError("Storage key must not contain NUL")
        if any(segment == ".." for segment in key.split("/")):
            raise StorageKeyError(f"Storage key must not traverse upward: {key!r}")

        return f"{self.prefix}{key}"

    def _relative_key(self, full_key):
        """Strip the owner prefix off a bucket key for return to the caller."""
        if full_key.startswith(self.prefix):
            return full_key[len(self.prefix):]
        return full_key

    # ------------------------------------------------------------------ #
    # Connection
    # ------------------------------------------------------------------ #

    def _connection(self):
        from core.services.s3_service import S3Service

        connection = S3Service.for_assets()
        if connection is None or not connection.enabled or not connection.is_configured:
            return None
        return connection

    def is_available(self):
        """Whether storage can actually be used here.

        Both axes have to resolve: a usable assets connection AND a workspace to
        scope keys under. Never raises — the whole point is a cheap degrade check
        a plugin can branch on.
        """
        if self._connection() is None:
            return False
        try:
            self._ws_id()
        except StorageError:
            return False
        return True

    def _require_connection(self):
        connection = self._connection()
        if connection is None:
            raise StorageError(
                "No assets storage connection is configured. Set one in "
                "Services → Object Storage."
            )
        return connection

    # ------------------------------------------------------------------ #
    # Objects
    # ------------------------------------------------------------------ #

    def put(self, key, data, content_type="application/octet-stream"):
        """Write ``data`` (bytes or str) at ``key``, relative to the owner prefix.

        Returns the relative key on success; raises ``StorageError`` on failure —
        a silent False would let a plugin believe it archived something it didn't.
        """
        from core.services.s3_service import S3Service

        connection = self._require_connection()
        full_key = self._full_key(key)

        if isinstance(data, str):
            data = data.encode("utf-8")
        if not isinstance(data, (bytes, bytearray)):
            raise StorageError(f"Storage data must be bytes or str, got {type(data).__name__}")

        result = S3Service.upload_file(
            connection, bytes(data), full_key, content_type=content_type
        )
        if not result.get("success"):
            raise StorageError(f"Storage put failed for {key!r}: {result.get('error')}")
        return key

    def get(self, key):
        """Read the object's bytes, or None when it doesn't exist."""
        from core.services.s3_service import S3Service

        connection = self._require_connection()
        return S3Service.get_file(connection, self._full_key(key))

    def delete(self, key):
        """Delete one object. True when the delete was issued cleanly."""
        from core.services.s3_service import S3Service

        connection = self._require_connection()
        return S3Service.delete_file(connection, self._full_key(key))

    def list(self, prefix=""):
        """List this owner's objects as dicts of ``key`` (RELATIVE to the owner
        prefix), ``size``, ``last_modified``, ``etag``.

        ``prefix`` filters within the owner's own space and is validated like any
        other key, so ``list("../")`` cannot enumerate the bucket.
        """
        from core.services.s3_service import S3Service

        connection = self._require_connection()
        # An empty prefix means "everything of mine", not "everything".
        full_prefix = self._full_key(prefix) if prefix else self.prefix

        return [
            {**f, "key": self._relative_key(f["key"])}
            for f in S3Service.list_files(connection, full_prefix)
        ]

    def url(self, key, expires_in=3600):
        """A URL for the object.

        Public connection (``public_base_url`` set) → a permanent, hot-linkable
        URL, and ``expires_in`` is irrelevant. Otherwise → a presigned URL that
        expires after ``expires_in`` seconds (7-day ceiling). Returns None when a
        presigned URL could not be signed.
        """
        from core.services.s3_service import S3Service

        connection = self._require_connection()
        full_key = self._full_key(key)

        if connection.is_public:
            return f"{connection.public_base_url.rstrip('/')}/{full_key}"

        return S3Service.presigned_url(connection, full_key, expires_in=expires_in)

    def delete_all(self):
        """Delete this owner's objects in EVERY workspace. Returns the count.

        Deliberately owner-wide rather than workspace-scoped: its caller is plugin
        uninstall (`remove_data`), and a plugin is installed — and removed —
        instance-wide, exactly like the owned-row cleanup beside it, which also
        filters on ``owner_plugin`` with no workspace clause. Leaving one
        workspace's files behind after an uninstall would strand them with no UI
        that can ever reach them again.

        Uses ``owner_root``, so it needs no default workspace to exist.
        """
        from core.services.s3_service import S3Service

        connection = self._connection()
        if connection is None:
            return 0
        return S3Service.delete_prefix(connection, self.owner_root)


# --------------------------------------------------------------------------- #
# Vector collections — embeddings + similarity search
# --------------------------------------------------------------------------- #

class VectorCollectionHandle:
    """Every operation on one collection — the same contract as the worker-side
    ``pyrunner_vectors`` helper (same names, arguments and result shapes).

    Errors raise ``VectorError`` with a stable ``code``. ``model`` is the
    ``VectorCollection`` row for read-only display.
    """

    def __init__(self, collection):
        self._collection = collection

    @property
    def name(self):
        return self._collection.name

    @property
    def model(self):
        return self._collection

    @property
    def dimension(self):
        return self._collection.dimension

    @property
    def metric(self):
        return self._collection.metric

    @property
    def embedding_model(self):
        return self._collection.embedding_model

    def _handle(self):
        from core.services import VectorService

        if not self._collection.is_ready:
            raise VectorError(
                f"Collection '{self.name}' is in an error state "
                f"({self._collection.last_error or 'unknown'}).",
                "not_ready",
            )
        return VectorService.handle(self._collection)

    def add_texts(self, items):
        """Embed and upsert ``[{"id", "text", "payload"?}]`` → ``{"added", "tokens", "truncated"}``."""
        return self._handle().add_texts(items)

    def add_vectors(self, items):
        """Upsert ``[{"id", "vector", "payload"?, "text"?}]`` as given → ``{"added"}``."""
        return self._handle().add_vectors(items)

    def search(self, query, k=5, filter=None, *, timeout=None):  # noqa: A002 — the contract's name
        """The ``k`` points nearest to ``query`` (embedded with the collection's
        provider), best first: ``[{"id", "score", "payload", "text"}]``."""
        handle = self._handle()
        if timeout is None:
            return handle.search(query, k=k, filter=filter)
        import time
        from core.vectors.budget import BudgetSession
        from core.vectors.service import VectorService

        with BudgetSession(time.monotonic() + min(30, max(1, float(timeout)))) as session:
            return VectorService.handle(self._collection, session=session).search(query, k=k, filter=filter)

    def search_vector(self, vector, k=5, filter=None):  # noqa: A002
        return self._handle().search_vector(vector, k=k, filter=filter)

    def get(self, ids, include_vectors=False):
        return self._handle().get(ids, include_vectors=include_vectors)

    def delete(self, ids=None, filter=None):  # noqa: A002
        """Delete by ``ids`` or by ``filter`` (exactly one) → ``{"deleted"}``."""
        return self._handle().delete(ids=ids, filter=filter)

    def count(self, filter=None):  # noqa: A002
        return self._handle().count(filter=filter)

    def scroll(self, cursor=None, limit=100, filter=None, include_vectors=False):  # noqa: A002
        """One page: ``(items, next_cursor)``; ``next_cursor`` is None at the end."""
        return self._handle().scroll(
            cursor=cursor, limit=limit, filter=filter, include_vectors=include_vectors
        )

    def embed(self, texts, input_type="document"):
        """Embed ``texts`` with the collection's provider (counted, nothing stored)."""
        return self._handle().embed(texts, input_type=input_type)

    def stats(self):
        return self._handle().stats()


class VectorAPI:
    """Owner-scoped vector collections. The stored ``name`` is auto-derived
    ``"<owner>:<key>"`` (owner=None ⇒ the raw key), exactly like DataStoreAPI and
    DatabaseAPI; a worker script opens the same collection with
    ``pyrunner_vectors.get_collection("<owner>:<key>")``.

    ``provision()`` creates the collection on a vector store — the default one
    (the built-in Local store unless an admin changed it) or a named
    ``connection`` — with an optional ``embedding_provider`` whose model and
    dimension are **pinned** to it. Re-provisioning with the same arguments
    returns the same collection; different ones raise ``VectorError`` (a new
    model or dimension is a new collection + re-embed, never a silent change of
    space).

        vectors = VectorAPI(OWNER)
        if vectors.is_available():
            docs = vectors.provision("docs", embedding_provider="OpenAI")
            docs.add_texts([{"id": "p1", "text": "...", "payload": {"url": "..."}}])
            hits = docs.search("how do I ...", k=5)

    Deleting the plugin with "remove data" deletes the collections it owns,
    points included.
    """

    def __init__(self, owner=None, workspace=None):
        self.owner = owner
        self._workspace = workspace

    def _ws(self):
        return _resolve_workspace(self._workspace)

    def _name_for(self, key):
        return f"{self.owner}:{key}" if self.owner else key

    def _qs(self):
        from core.models import VectorCollection

        qs = VectorCollection.objects.filter(workspace=self._ws()).select_related(
            "connection", "embedding_provider"
        )
        return qs.filter(owner_plugin=self.owner) if self.owner else qs.filter(
            owner_plugin__isnull=True
        )

    def is_available(self):
        """Whether a vector store connection is enabled (the seeded Local one is,
        unless an admin turned it off)."""
        from core.services import VectorService

        return VectorService.is_available()

    @staticmethod
    def choices():
        """Enabled instance providers/stores, as names only (never credentials)."""
        from core.models import EmbeddingProvider, VectorConnection

        return {
            "providers": list(EmbeddingProvider.objects.filter(enabled=True).order_by("name").values_list("name", flat=True)),
            "connections": list(VectorConnection.objects.filter(enabled=True).order_by("name").values_list("name", flat=True)),
        }

    @staticmethod
    def _by_name(model, value, label):
        """Accept an instance or a name; None stays None."""
        if value is None or not isinstance(value, str):
            return value
        found = model.objects.filter(name=value).first()
        if found is None:
            raise VectorError(f"No {label} named '{value}'.", "invalid")
        return found

    def provision(
        self,
        key,
        *,
        embedding_provider=None,
        connection=None,
        dimension=None,
        metric="cosine",
        description=None,
        created_by=None,
    ):
        """Ensure the owned collection exists (idempotent on the auto-derived name)
        and return its handle.

        ``embedding_provider`` / ``connection`` take a row or its name. Without a
        provider the collection is vectors-only and needs ``dimension``. An
        existing collection whose pinned provider, dimension, metric or store
        differs from the arguments raises ``VectorError`` (code ``invalid``);
        ``description`` (when given) is updated, and a row stuck in
        ``status='error'`` is retried.
        """
        from core.models import EmbeddingProvider, VectorCollection, VectorConnection
        from core.services import VectorService

        provider = self._by_name(EmbeddingProvider, embedding_provider, "embedding provider")
        store = self._by_name(VectorConnection, connection, "vector store connection")
        name = self._name_for(key)
        existing = self._qs().filter(name=name).first()
        if existing is None:
            if VectorCollection.objects.filter(workspace=self._ws(), name=name).exists():
                raise VectorError(
                    f"A collection named '{name}' already exists in this workspace and "
                    "is not owned by this plugin.",
                    "invalid",
                )
            collection = VectorService.create_collection(
                workspace=self._ws(),
                name=name,
                connection=store,
                embedding_provider=provider,
                dimension=dimension,
                metric=metric,
                description=description or "",
                owner_plugin=self.owner,
                owner_key=key if self.owner else None,
                created_by=created_by,
            )
            return VectorCollectionHandle(collection)

        mismatched = []
        if (provider.pk if provider else None) != existing.embedding_provider_id:
            mismatched.append("embedding provider")
        elif provider is not None and provider.model != existing.embedding_model:
            mismatched.append("embedding model")
        elif provider is not None and provider.dimension not in (None, existing.dimension):
            mismatched.append("provider dimension")
        if dimension is not None and int(dimension) != existing.dimension:
            mismatched.append("dimension")
        if metric != existing.metric:
            mismatched.append("metric")
        if store is not None and store.pk != existing.connection_id:
            mismatched.append("vector store")
        if mismatched:
            raise VectorError(
                f"Collection '{name}' already exists with a different "
                f"{', '.join(mismatched)}; its shape is pinned. Provision a new key "
                "(and re-embed) to change it.",
                "invalid",
            )
        if description is not None and description != existing.description:
            existing.description = description
            existing.save(update_fields=["description", "updated_at"])
        if not existing.is_ready:
            VectorService.retry_backend(existing)
        return VectorCollectionHandle(existing)

    def get(self, key):
        """The owned collection's handle, or None."""
        collection = self._qs().filter(name=self._name_for(key)).first()
        return VectorCollectionHandle(collection) if collection is not None else None

    def list(self):
        return [VectorCollectionHandle(c) for c in self._qs().order_by("name")]

    def delete(self, key):
        """Delete the owned collection and its points. True when one was deleted."""
        from core.services import VectorService

        collection = self._qs().filter(name=self._name_for(key)).first()
        if collection is None:
            return False
        VectorService.delete_collection(collection)
        return True


# --------------------------------------------------------------------------- #
# Brains
# --------------------------------------------------------------------------- #

class BrainAPI:
    """Provision plugin-owned brains, grant access, and read through the registry.

    A plugin owns ``<owner>-<key>`` by default. ``get_brain(slug)`` also opens a
    user brain when its owner has granted this plugin access. No direct content
    write method exists: content changes go through the human-reviewed feed door.
    """

    def __init__(self, owner, workspace=None, *, tier=None):
        from core.plugins import is_valid_plugin_slug

        if not is_valid_plugin_slug(owner):
            raise ValueError("BrainAPI requires a plugin owner slug.")
        if tier not in {None, "public", "agents-only", "private"}:
            raise ValueError("Unknown Brain tier.")
        self.owner, self._workspace, self.tier = owner, workspace, tier

    def _ws(self):
        return _resolve_workspace(self._workspace)

    def _context(self):
        from dataclasses import replace
        from core.brain.access import principal_context

        return replace(principal_context(self._ws(), "plugin", self.owner), tier=self.tier)

    def _call(self, slug, operation, params):
        from core.api.registry import registry

        return registry.call(f"brain.{operation}", self._context(), {**params, "brain": slug})

    def get_brain(self, slug):
        from core.script_helpers.pyrunner_brain import Brain

        brain = Brain(slug, _call=self._call)
        brain.ping()
        return brain

    def list(self, *, offset=0, limit=1000):
        from core.api.registry import registry

        return registry.call("brain.list", self._context(), {"offset": offset, "limit": limit})

    def provision(self, key, *, slug=None, name=None, created_by=None):
        from django.db import transaction
        from core.api.errors import ApiError
        from core.brain.service import BrainService
        from core.models import Brain, Workspace

        if not isinstance(key, str) or not key or len(key) > 100:
            raise ValueError("Brain key must contain 1–100 characters.")
        workspace = self._ws()
        with transaction.atomic():
            # Serializes first provisioning as well as the idempotent path.
            Workspace.objects.select_for_update().get(pk=workspace.pk)
            brain = Brain.objects.for_workspace(workspace).filter(
                owner_plugin=self.owner, owner_key=key,
            ).first()
            if brain is None:
                brain = BrainService.create(workspace, slug or f"{self.owner}-{key}",
                                            name=name, author=created_by,
                                            owner_plugin=self.owner, owner_key=key)
            elif slug is not None and slug != brain.slug:
                raise ApiError("invalid_input", "This key already has a different brain slug.")
        return self.get_brain(brain.slug)

    def grant(self, key, *, subject_kind, subject_id, max_tier="public", can_propose=False):
        from core.brain.access import unknown, validate_subject
        from core.models import Brain, BrainGrant

        brain = Brain.objects.for_workspace(self._ws()).filter(
            owner_plugin=self.owner, owner_key=key,
        ).first()
        if brain is None:
            raise unknown()
        identifier = validate_subject(self._ws(), subject_kind, subject_id, max_tier, can_propose)
        grant, _ = BrainGrant.objects.update_or_create(
            brain=brain, subject_kind=subject_kind, subject_id=identifier,
            defaults={"max_tier": max_tier, "can_propose": can_propose},
        )
        return grant


# --------------------------------------------------------------------------- #
# Schedules
# --------------------------------------------------------------------------- #

_TIME_HINT = "use HH:MM, 24-hour, e.g. '09:00'"


def _schedule_time(value, label):
    """One run time, normalized the way the console's schedule form does it
    ('9:5' -> '09:05'). Anything else is a ValueError naming ``label``."""
    parts = value.strip().split(":") if isinstance(value, str) else []
    try:
        hour, minute = int(parts[0]), int(parts[1])
        valid = len(parts) == 2 and 0 <= hour <= 23 and 0 <= minute <= 59
    except (IndexError, ValueError):
        valid = False
    if not valid:
        raise ValueError(f"{label} {value!r} is not a valid time; {_TIME_HINT}")
    return f"{hour:02d}:{minute:02d}"


def _schedule_day(lo, hi, hint):
    """A parser for one weekday (0-6) or day of the month (1-31): an int, or a
    string holding one ("3"), as the single ``weekday`` kwarg always took."""

    def parse(value, label):
        number = value
        if isinstance(value, str):
            try:
                number = int(value.strip())
            except ValueError:
                pass  # still a str: refused just below, with the message
        if isinstance(number, bool) or not isinstance(number, int) or not lo <= number <= hi:
            raise ValueError(f"{label} {value!r} is not {hint}")
        return number

    return parse


_weekday = _schedule_day(0, 6, "a weekday; use 0 (Monday) to 6 (Sunday)")
_month_day = _schedule_day(1, 31, "a day of the month; use 1 to 31")


def _schedule_list(name, values, parse):
    """Check one of ``ScheduleAPI.sync``'s list kwargs: a non-empty list or
    tuple, each item normalized by ``parse``, no value given twice."""
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"{name} must be a list, got {type(values).__name__}")
    if not values:
        raise ValueError(f"{name} is empty; give at least one value")
    out = []
    for i, item in enumerate(values):
        value = parse(item, f"{name}[{i}]")
        if value in out:
            raise ValueError(f"{name} lists {value!r} twice")
        out.append(value)
    return out


class ScheduleAPI:
    """Wraps ScriptSchedule + ScheduleService so a plugin schedules a run without
    touching django-q directly (and the global-pause rules still apply)."""

    def __init__(self, owner=None, workspace=None):
        self.owner = owner
        self._workspace = workspace

    def sync(
        self,
        script,
        *,
        mode,
        time_str=None,
        weekday=None,
        month=None,
        day=None,
        interval_minutes=None,
        cron=None,
        tz="UTC",
        times=None,
        weekdays=None,
        month_days=None,
    ):
        """Create/update the script's schedule and push it to django-q2.

        ``mode`` is a ``ScriptSchedule.RunMode`` value ('manual'/'interval'/'daily'
        /'weekly'/'monthly'/'yearly'/'cron'). Every time and day is written in
        ``tz``. Per mode:

        - interval: ``interval_minutes``.
        - daily: ``times`` (a list of HH:MM, e.g. ``["09:00", "18:00"]``).
        - weekly: ``weekdays`` (0=Monday … 6=Sunday) and ``times``.
        - monthly: ``month_days`` (1-31) and ``times``.
        - yearly: ``month``, ``day`` and one time.
        - cron: a raw 5-field expression via ``cron``.

        The single-value kwargs still work: ``time_str="09:00"`` is
        ``times=["09:00"]`` and ``weekday=0`` is ``weekdays=[0]``; passing both
        forms of one is an error. The result is the row the console's schedule
        form saves for the same choices: times normalized to HH:MM and kept in
        the order given, days sorted. A list kwarg the mode doesn't use, an
        empty list, a repeated value or an unknown mode is a ValueError, and
        nothing is saved.

        Any mode but manual is refused (ValueError, nothing saved) while the
        script declares a required input with no default: a scheduled run has
        nobody to supply it. The script editor applies the same rule.
        """
        from core.models import Script, ScriptSchedule
        from core.services.schedule_service import ScheduleService
        from core.services.script_inputs import schedule_conflict

        Mode = ScriptSchedule.RunMode
        if mode not in Mode.values:
            raise ValueError(f"Unknown mode {mode!r}; use one of: {', '.join(Mode.values)}")
        mode = Mode(mode)

        list_modes = {
            "times": (Mode.DAILY, Mode.WEEKLY, Mode.MONTHLY, Mode.YEARLY),
            "weekdays": (Mode.WEEKLY,),
            "month_days": (Mode.MONTHLY,),
        }
        given = {"times": times, "weekdays": weekdays, "month_days": month_days}
        for name, value in given.items():
            if value is not None and mode not in list_modes[name]:
                raise ValueError(f"{name} doesn't apply to {mode.value} mode")
        if time_str and times is not None:
            raise ValueError("pass time_str or times, not both")
        if weekday is not None and weekdays is not None:
            raise ValueError("pass weekday or weekdays, not both")

        def run_times():
            if times is not None:
                return _schedule_list("times", times, _schedule_time)
            if time_str:
                return [_schedule_time(time_str, "time_str")]
            raise ValueError(
                f"{mode.value} mode requires times=['HH:MM', ...] (or time_str='HH:MM')"
            )

        # Only the chosen mode's fields; every other mode's is cleared below.
        fields = {}
        if mode == Mode.INTERVAL:
            if not interval_minutes:
                raise ValueError("interval mode requires interval_minutes")
            # Same bounds as the form; django-q2 stores minutes in a small int,
            # so an out-of-range value would otherwise fail later at sync time.
            try:
                minutes = int(interval_minutes)
            except (TypeError, ValueError):
                raise ValueError("interval_minutes must be a whole number of minutes")
            lo, hi = ScriptSchedule.INTERVAL_MIN_MINUTES, ScriptSchedule.INTERVAL_MAX_MINUTES
            if not lo <= minutes <= hi:
                raise ValueError(f"interval_minutes must be between {lo} and {hi}")
            fields["interval_minutes"] = minutes
        elif mode == Mode.DAILY:
            fields["daily_times"] = run_times()
        elif mode == Mode.WEEKLY:
            if weekdays is not None:
                days = _schedule_list("weekdays", weekdays, _weekday)
            elif weekday is not None:
                days = [_weekday(weekday, "weekday")]
            else:
                raise ValueError(
                    "weekly mode requires weekdays=[...] (0=Mon … 6=Sun) or weekday"
                )
            fields["weekly_days"] = sorted(days)
            fields["weekly_times"] = run_times()
        elif mode == Mode.MONTHLY:
            if month_days is None:
                raise ValueError("monthly mode requires month_days=[...] (1-31)")
            fields["monthly_days"] = sorted(_schedule_list("month_days", month_days, _month_day))
            fields["monthly_times"] = run_times()
        elif mode == Mode.CRON:
            is_valid, error = ScheduleService.validate_cron_expression(cron, tz or "UTC")
            if not is_valid:
                raise ValueError(f"Invalid cron expression: {error}")
            fields["cron_expression"] = (cron or "").strip()
        elif mode == Mode.YEARLY:
            if month is None or day is None:
                raise ValueError("yearly mode requires month and day")
            run_at = run_times()
            if len(run_at) != 1:
                raise ValueError("yearly mode runs once a year; give one time")
            from calendar import monthrange

            try:
                month, day = int(month), int(day)
                if not 1 <= month <= 12 or not 1 <= day <= monthrange(2024, month)[1]:
                    raise ValueError
            except (TypeError, ValueError):
                raise ValueError("yearly mode requires a valid month and day")
            fields.update(yearly_month=month, yearly_day=day, yearly_time=run_at[0])

        if mode != Mode.MANUAL:
            # The stored schema, not the caller's copy, which may be stale.
            schema = (
                Script.objects.filter(pk=script.pk)
                .values_list("inputs_schema", flat=True)
                .first()
            )
            conflict = schedule_conflict(schema)
            if conflict:
                raise ValueError(f"Schedule not saved. {conflict}")

        sched, _ = ScriptSchedule.objects.get_or_create(
            script=script, defaults={"workspace_id": script.workspace_id}
        )
        sched.run_mode = mode.value
        sched.timezone = tz or "UTC"
        sched.interval_minutes = None
        sched.daily_times = []
        sched.weekly_days = []
        sched.weekly_times = []
        sched.monthly_days = []
        sched.monthly_times = []
        sched.cron_expression = ""
        sched.yearly_month = None
        sched.yearly_day = None
        sched.yearly_time = ""
        for name, value in fields.items():
            setattr(sched, name, value)

        sched.is_active = mode != Mode.MANUAL
        sched.save()
        ScheduleService.sync_schedule(sched)
        return sched

    def list(self):
        from core.models import ScriptSchedule

        qs = ScriptSchedule.objects.filter(script__workspace=_resolve_workspace(self._workspace))
        if self.owner:
            qs = qs.filter(script__owner_plugin=self.owner)
        return list(qs)


# --------------------------------------------------------------------------- #
# External HTTP API (API 2.3) — handlers for /api/v1/plugins/<slug>/…
#
# A plugin declares resources in plugin.json ("provides.api_resources") and
# marks handlers in its <plugin>/api.py with @resource. Core's dispatcher
# (core.views.api.plugins) owns 100% of auth, rate limiting, and workspace
# scoping — a handler never sees an HttpRequest, a token, or a tenancy
# decision. A plugin's api.py imports ONLY this module (import-light lane).
# --------------------------------------------------------------------------- #

# Attribute the @resource marker sets; the dispatcher scans for it. An
# attribute (not a global registry) keeps re-imports idempotent and makes slug
# inference a non-problem.
API_RESOURCE_ATTR = "_pyrunner_api_resource"


def resource(name):
    """Mark a function in a plugin's ``api.py`` as the handler for ``name``.

    The handler receives an :class:`APIRequest` and returns a JSON-serializable
    dict that IS the response body (core adds no envelope). Raise
    :class:`APIError` for a clean 4xx; any other exception becomes a generic
    500 with the traceback logged server-side.

    The resource must ALSO be declared in plugin.json under
    ``provides.api_resources`` — an undeclared handler is never served.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("@resource requires a non-empty name string")

    def marker(func):
        setattr(func, API_RESOURCE_ATTR, name)
        return func

    return marker


@dataclass(frozen=True)
class APIRequest:
    """Frozen request context handed to a @resource handler.

    ``workspace`` is derived server-side from the API token — treat it as an
    opaque handle to pass into SDK constructors; the handler cannot widen it.
    ``params`` holds the first value per query key; ``params_list`` holds every
    value (``?tag=a&tag=b`` → ``{"tag": ["a", "b"]}``).
    """

    workspace: object
    resource: str
    item_id: Optional[str]
    method: str
    params: dict
    params_list: dict


class APIError(Exception):
    """The one sanctioned way for a handler to return an error response.

    ``status`` must be 400–499 (the dispatcher rejects anything else as a
    plugin bug — a plugin must not fabricate redirects or fake 5xx).
    """

    def __init__(self, message, *, code="BAD_REQUEST", status=400):
        super().__init__(message)
        self.message = str(message)
        self.code = code
        self.status = status


class ChannelAPI:
    """Read-only, workspace-scoped view of the notification Channels.

    The channel-picker seam (e.g. an alert form listing where a plugin may
    send via ``pyrunner_notify``). List-only by design: channels are
    user-configured infrastructure a plugin selects, never creates — and the
    listing is scoped to the workspace so channel names can't leak across
    tenants through a plugin page.
    """

    def __init__(self, owner=None, workspace=None):
        self.owner = owner
        self._workspace = workspace

    def list(self):
        from core.models import Channel

        ws = _resolve_workspace(self._workspace)
        return [
            {"name": c.name, "channel_type": c.provider, "is_enabled": c.enabled}
            for c in Channel.objects.for_workspace(ws).order_by("name")
        ]


# --------------------------------------------------------------------------- #
# Public pages (API 2.3, Stage 5) — shareable HTML at /p/<token>/
#
# Same registration surface as API resources: declare in plugin.json
# ("provides.public_pages"), mark the handler in <plugin>/api.py with @page.
# The handler returns an HTML STRING rendered via Django templates
# (auto-escape — never string-concatenated HTML: public pages render scraped
# content, and a poisoned title must never become stored XSS). Core serves it
# script-free (strict CSP) with noindex/no-store/no-referrer headers.
# --------------------------------------------------------------------------- #

API_PAGE_ATTR = "_pyrunner_public_page"


def page(name):
    """Mark a function in a plugin's ``api.py`` as the renderer for public
    page ``name`` (declared in plugin.json under ``provides.public_pages``).

    The handler receives a :class:`PageRequest` and returns an HTML string.
    Render through Django templates only — auto-escaping is the XSS defense
    the strict CSP backs up, not replaces.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("@page requires a non-empty name string")

    def marker(func):
        setattr(func, API_PAGE_ATTR, name)
        return func

    return marker


@dataclass(frozen=True)
class PageRequest:
    """Frozen context handed to a @page handler.

    ``workspace`` comes from the share row (set when the page was shared),
    never from the anonymous caller. ``params`` is the query string, first
    value per key — use it for display toggles only, never authorization.
    """

    workspace: object
    page: str
    params: dict


class PublicPageAPI:
    """Share/revoke a plugin's public pages (owner-stamped, workspace-scoped).

    ``share()`` is an idempotent upsert per (plugin, page, workspace) — but a
    revoked or expired row is reactivated with a FRESHLY ROTATED token: the
    old URL stays dead permanently (a dead capability URL never resurrects).
    Plain re-share of a live page returns the existing URL unchanged.
    """

    def __init__(self, owner=None, workspace=None):
        if not owner:
            raise ValueError("PublicPageAPI requires the plugin slug as owner")
        self.owner = owner
        self._workspace = workspace

    def _ws(self):
        return _resolve_workspace(self._workspace)

    def _qs(self):
        from core.models import PluginPublicPage

        return PluginPublicPage.objects.filter(
            plugin_slug=self.owner, workspace=self._ws()
        )

    def resolve_token(self, token, *, page_name):
        """Resolve this plugin's live capability to its workspace, or None.

        For plugin-owned interactive routes: the workspace comes exclusively
        from the capability. This does not change the script-free /p/ renderer.
        Call again before returning a slow result to honor revocation.
        """
        from core.models import PluginPublicPage

        row = PluginPublicPage.objects.select_related("workspace").filter(
            plugin_slug=self.owner, page=page_name, token=token, is_active=True,
        ).first()
        if row is None or row.is_expired or row.workspace is None:
            return None
        if self._workspace is not None and row.workspace_id != self._workspace.pk:
            return None
        return PageRequest(workspace=row.workspace, page=row.page, params={})

    # Sentinel: distinguishes "share() called without expires_at" (keep the
    # row's current expiry) from an explicit expires_at=None (clear it).
    _KEEP = object()

    def share(self, page_name, *, expires_at=_KEEP, created_by=None):
        """Ensure ``page_name`` is shared; return its public URL path.

        ``expires_at`` is only written when explicitly passed — a plain
        re-share never silently clears (or changes) an existing expiry. Pass
        ``expires_at=None`` to explicitly remove one. A revoked/expired row is
        always reactivated with a fresh expiry state and a ROTATED token.
        """
        from django.utils import timezone as _tz

        from core.models import PluginPublicPage

        explicit_expiry = expires_at is not self._KEEP
        row = self._qs().filter(page=page_name).first()
        if row is None:
            row = PluginPublicPage.objects.create(
                plugin_slug=self.owner,
                page=page_name,
                workspace=self._ws(),
                token=PluginPublicPage.generate_token(),
                expires_at=expires_at if explicit_expiry else None,
                created_by=created_by,
            )
            return row.url_path

        was_dead = (not row.is_active) or (
            row.expires_at and row.expires_at < _tz.now()
        )
        if was_dead:
            # Reactivate with a rotated token — the old URL is dead forever.
            # The stale expiry never carries over (it would dead-end the new
            # link immediately unless the caller re-specifies one).
            row.token = PluginPublicPage.generate_token()
            row.is_active = True
            row.expires_at = expires_at if explicit_expiry else None
        elif explicit_expiry:
            row.expires_at = expires_at
        row.save(update_fields=["token", "is_active", "expires_at"])
        return row.url_path

    def get(self, page_name):
        """The live share row for ``page_name`` (dict), or None."""
        row = self._qs().filter(page=page_name).first()
        if row is None:
            return None
        return {
            "page": row.page,
            "url_path": row.url_path,
            "is_active": row.is_active,
            "is_expired": row.is_expired,
            "expires_at": row.expires_at,
            "created_at": row.created_at,
            "last_accessed_at": row.last_accessed_at,
        }

    def list(self):
        return [self.get(row.page) for row in self._qs().order_by("page")]

    def revoke(self, page_name):
        """Deactivate the share (the URL 404s permanently). True if it existed."""
        updated = self._qs().filter(page=page_name).update(is_active=False)
        return bool(updated)


# --------------------------------------------------------------------------- #
# Watchers (PyRun 2.5 Stage 7, T33) — polling triggers owned by a plugin, and
# the adapters a plugin ships in <plugin>/triggers.py.
# --------------------------------------------------------------------------- #

class TriggerError(Exception):
    """Raise from a ``TriggerAdapter``'s ``check``/``test``: the console shows ``message`` as is.

    Keep secrets out of it (a connection's own values are masked anyway).
    """

    def __init__(self, message):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class TriggerContext:
    """What a plugin adapter's ``check``/``test`` gets besides its settings.

    ``workspace`` is the watcher's (pass it on to the other SDK classes:
    ``DataStoreAPI(OWNER, workspace=ctx.workspace)``); ``watcher_id`` is None
    for the console's Test of a form not saved yet; ``connection`` is the
    watcher's connection (a ConnectionView) when the adapter
    ``requires_connection``; ``limit`` is the most events to return (None = no
    limit); ``timeout`` the seconds network calls may take.
    """

    workspace: object
    watcher_id: Optional[str]
    connection: Optional[ConnectionView]
    limit: Optional[int]
    timeout: float
    _connection_row: object = field(default=None, repr=False, compare=False)

    def credentials(self):
        """The connection's auth material (the dict ``ConnectionAPI.credentials`` returns).

        Raises ConnectionUnavailable when the watcher has no connection or it
        can't be used (expired, revoked, unreadable).
        """
        from core.services.connections import ConnectionLookupError, credentials_for

        if self._connection_row is None:
            raise ConnectionUnavailable("This watcher has no connection.", "CONNECTION_NOT_FOUND")
        try:
            return credentials_for(self._connection_row)
        except ConnectionLookupError as e:
            raise ConnectionUnavailable(e.message, e.code) from None


class TriggerAdapter:
    """Base class for a plugin's watcher adapters: subclass it in ``<plugin>/triggers.py``.

    Each subclass defined there (with a literal ``key``) becomes the adapter
    ``"<slug>:<key>"`` in the watcher form's picker, in every workspace, once
    the plugin is loaded and the doctor's ``triggers`` rule passes. Declare:

    - ``key``: a lowercase letter, then letters, digits and underscores;
      stable (watchers store it). ``title`` and ``description``;
    - ``config_schema``: a flat object JSON Schema (string, integer, number
      and boolean settings; ``enum``, ``pattern``, ``minimum``/``maximum``,
      ``minLength``/``maxLength``, ``default``, ``title``, ``description``). It generates
      the form and validates what is saved;
    - ``requires_connection`` and ``providers`` (connection preset keys, e.g.
      ``("google",)``; empty = any) when it checks as one of the user's
      connections (``ctx.credentials()``).

    And implement:

    - ``check(ctx, config, state) -> (events, new_state)``: what is new since
      ``state`` (None on the first check: report nothing, only mark where the
      source is). Events are ``{"id": <stable id>, "item": <JSON>}``, oldest
      first, at most ``ctx.limit``; ``new_state`` is JSON and moves only past
      the events returned. Raise TriggerError on failure: the watcher backs
      off and keeps its old state;
    - ``test(ctx, config) -> (message, items)``: a read-only look for the
      console's Test button (a short line and a few sample items).

    ``config`` arrives with the schema's defaults filled in. Both run in a
    worker thread that is waited for ``ctx.timeout`` + 5 seconds: keep them
    short, and never write anything in ``test``.
    """

    key = ""
    title = ""
    description = ""
    config_schema = {"type": "object", "properties": {}}
    requires_connection = False
    providers = ()

    def check(self, ctx, config, state):
        raise NotImplementedError

    def test(self, ctx, config):
        raise NotImplementedError


class WatcherAPI:
    """Watchers a plugin creates for scripts (owner-scoped, idempotent by ``key``).

    A watcher polls a source every ``interval`` seconds and starts one run of
    its script per new item, like one made on the script's Triggers page.
    The console shows a plugin's watchers with the plugin's pill; people can
    turn them off and on there (and Check now), not edit them. They are
    deleted with the plugin.
    """

    def __init__(self, owner, workspace=None):
        if not owner:
            raise ValueError("WatcherAPI needs the plugin's slug: watchers are owned by a plugin.")
        self.owner = owner
        self._workspace = workspace

    def _ws(self):
        return _resolve_workspace(self._workspace)

    def _qs(self):
        from core.models import Watcher

        return Watcher.objects.filter(workspace=self._ws(), owner_plugin=self.owner)

    def _adapter(self, adapter):
        from core.triggers import registry

        if isinstance(adapter, TriggerAdapter) or (isinstance(adapter, type) and issubclass(adapter, TriggerAdapter)):
            adapter = f"{self.owner}:{adapter.key}"
        if not isinstance(adapter, str) or not adapter:
            raise ValueError("adapter must be an adapter key, e.g. 'rss_new_item' or one of this plugin's.")
        found = registry.get(adapter)
        if found is None and ":" not in adapter:
            found = registry.get(f"{self.owner}:{adapter}")
        if found is None:
            raise ValueError(f"No adapter {adapter!r} is available (is the plugin that ships it loaded?).")
        return found

    def _script(self, script):
        from core.models import Script

        if isinstance(script, str):
            row = Script.objects.filter(workspace=self._ws(), owner_plugin=self.owner, owner_key=script).first()
            if row is None:
                raise ValueError(f"This plugin has no script {script!r} in the workspace.")
            return row
        if not isinstance(script, Script) or script.workspace_id != self._ws().pk:
            raise ValueError("script must be a Script of the workspace (or the key of one of this plugin's).")
        return script

    def _connection(self, connection, adapter):
        from core.models import Connection

        if connection is None:
            if adapter.requires_connection:
                raise ValueError(f'The adapter "{adapter.key}" needs a connection.')
            return None
        if not adapter.requires_connection:
            raise ValueError(f'The adapter "{adapter.key}" doesn\'t use a connection.')
        if isinstance(connection, str):
            from core.services.plugin_connections import connection_for

            row = connection_for(self._ws(), self.owner, connection)
            if row is None:
                raise ConnectionUnavailable(f"Nothing is connected for '{connection}' yet.", "CONNECTION_NOT_FOUND")
        elif isinstance(connection, ConnectionView):
            row = Connection.objects.filter(pk=connection.id).first()
            if row is None:
                raise ConnectionUnavailable(f"Connection '{connection.name}' was deleted.", "CONNECTION_NOT_FOUND")
        elif isinstance(connection, Connection):
            row = connection
        else:
            raise ValueError("connection must be a requirement key, a ConnectionView or a Connection.")
        if row.workspace_id != self._ws().pk:
            raise ValueError("The connection must belong to the watcher's workspace.")
        if adapter.providers and row.provider not in adapter.providers:
            wanted = " or ".join(adapter.providers)
            raise ValueError(f'The adapter "{adapter.key}" needs a {wanted} connection, not {row.provider}.')
        return row

    @staticmethod
    def _input_map(input_map, script):
        from core.watcher_forms import INPUT_MAP_MAX

        if input_map is None:
            return {}
        if not isinstance(input_map, dict):
            raise ValueError('input_map must be a dict: {"input": "item.path"}.')
        if len(input_map) > INPUT_MAP_MAX:
            raise ValueError(f"input_map: at most {INPUT_MAP_MAX} inputs.")
        declared = set((script.inputs_schema or {}).get("properties", {}))
        for name, path in input_map.items():
            if not isinstance(path, str) or not path.strip():
                raise ValueError(f'input_map "{name}": the path must be text, like "item.title".')
            if name not in declared:
                raise ValueError(f'input_map "{name}": not an input of the script "{script.name}".')
        return {name: path.strip() for name, path in input_map.items()}

    @staticmethod
    def _bounded(label, value, lo, hi):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{label} must be a whole number.")
        if not lo <= value <= hi:
            raise ValueError(f"{label} must be between {lo} and {hi}.")
        return value

    def sync(self, script, adapter, config=None, connection=None, interval=None, *, key=None, name=None,
             input_map=None, fire_cap_per_hour=None, created_by=None):
        """Create or update this plugin's watcher ``key``; returns it (a ``Watcher`` row).

        - ``script``: a Script of the workspace, or the key of one of this
          plugin's scripts. The watcher starts one run of it per new item.
        - ``adapter``: an adapter key (``"rss_new_item"``, ``"http_poll"``,
          ``"gmail_new_message"``...), or one of this plugin's own adapters:
          its key bare (``"new_order"``) or full (``"<slug>:new_order"``), or
          its TriggerAdapter class.
        - ``config``: the adapter's settings, checked against its
          ``config_schema``. ``connection``: for an adapter that needs one (of
          its providers), a requirement key of this plugin's
          (``requires_connections``), a ConnectionView or a Connection.
        - ``interval``: seconds between checks (60 to 7 days; a new watcher
          without it checks every 300). ``fire_cap_per_hour``: the most runs
          it starts an hour (60 when left out).
        - ``key``: the watcher's owner key, 100 characters at most (default
          ``"<script key or id>:<adapter key>"``). ``name``: shown in the
          console (default ``"<adapter title>: <script name>"``).
          ``input_map``: ``{"input": "item.path"}`` fills the script's
          declared inputs from the event.
        - ``created_by``: a user, who gets the email if it turns itself off.

        Everything is checked before anything is saved (ValueError, or
        ConnectionUnavailable when nothing serves a requirement key). A new
        watcher starts on; an update leaves it on or off. Changing the
        adapter, its settings or the connection starts its state over (the
        next check only marks where the source is), as the console's form
        does.
        """
        from django.db import IntegrityError, transaction

        from core.models import Watcher
        from core.triggers.adapters import validate_config

        script = self._script(script)
        adapter = self._adapter(adapter)
        cleaned, errors = validate_config(adapter.config_schema, {} if config is None else config)
        if errors:
            raise ValueError(
                "Bad adapter settings: "
                + "; ".join(f"{setting or 'config'}: {reason}" for setting, reason in sorted(errors.items()))
            )
        connection = self._connection(connection, adapter)
        input_map = self._input_map(input_map, script)
        if key is None:
            own = script.owner_plugin == self.owner and script.owner_key
            key = f"{script.owner_key if own else script.pk}:{adapter.key}"
        if not isinstance(key, str) or not key.strip() or len(key) > 100:
            raise ValueError("key must be text of 1 to 100 characters (pass key= when the default is longer).")
        name = (f"{adapter.title}: {script.name}" if name is None else str(name)).strip()[:120]
        if not name:
            raise ValueError("name can't be empty.")
        fields = {"script": script, "adapter": adapter.key, "config": cleaned, "connection": connection,
                  "name": name, "input_map": input_map}
        if interval is not None:
            fields["interval_seconds"] = self._bounded(
                "interval", interval, Watcher.INTERVAL_MIN_SECONDS, Watcher.INTERVAL_MAX_SECONDS)
        if fire_cap_per_hour is not None:
            fields["fire_cap_per_hour"] = self._bounded("fire_cap_per_hour", fire_cap_per_hour, 1,
                                                        Watcher.FIRE_CAP_MAX)

        watcher = self._qs().filter(owner_key=key).first()
        if watcher is None:
            try:
                with transaction.atomic():
                    return Watcher.objects.create(
                        workspace=self._ws(), owner_plugin=self.owner, owner_key=key,
                        created_by=created_by, **fields,
                    )
            except IntegrityError:
                # Another process made it first: update that one instead.
                watcher = self._qs().get(owner_key=key)
        source = (watcher.adapter, watcher.config, watcher.connection_id)
        for attr, value in fields.items():
            setattr(watcher, attr, value)
        if source != (watcher.adapter, watcher.config, watcher.connection_id):
            # A different source: what was seen of the old one means nothing.
            watcher.state = None
            watcher.next_check_at = None
        watcher.save()
        return watcher

    def get(self, key):
        """This plugin's watcher ``key`` (a ``Watcher`` row), or None."""
        return self._qs().filter(owner_key=key).first()

    def list(self):
        """This plugin's watchers in the workspace, by name."""
        return list(self._qs().select_related("script").order_by("name", "created_at"))

    def _get(self, key):
        watcher = self.get(key)
        if watcher is None:
            raise ValueError(f"This plugin has no watcher {key!r} in the workspace.")
        return watcher

    def pause(self, key):
        """Turn the watcher off; its state is kept. Returns it."""
        watcher = self._get(key)
        if watcher.enabled:
            watcher.enabled = False
            watcher.save(update_fields=["enabled", "updated_at"])
        return watcher

    def resume(self, key):
        """Turn the watcher on, like the console's Turn on: failures cleared, checked within a minute. Returns it."""
        watcher = self._get(key)
        if not watcher.enabled:
            watcher.enabled = True
            watcher.auto_disabled_at = None
            watcher.error_count = 0
            watcher.next_check_at = None
            watcher.save(update_fields=["enabled", "auto_disabled_at", "error_count", "next_check_at", "updated_at"])
        return watcher

    def delete(self, key):
        """Delete the watcher ``key`` (the runs it started stay). True if it existed."""
        deleted, _ = self._qs().filter(owner_key=key).delete()
        return bool(deleted)
