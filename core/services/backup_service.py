"""
Service for creating and restoring PyRun backups.
"""
import base64
import gzip
import hashlib
import io
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from core.models import (
    Brain,
    Connection,
    Database,
    DatabaseGrant,
    DataStore,
    DataStoreEntry,
    EmbeddingProvider,
    Environment,
    GlobalSettings,
    Library,
    MaintenanceWindow,
    OAuthClient,
    PackageOperation,
    Run,
    ScheduleHistory,
    Script,
    ScriptSchedule,
    Secret,
    SecretProvider,
    Table,
    TableRow,
    Tag,
    User,
    VectorCollection,
    VectorConnection,
    Watcher,
    Workspace,
    WorkspaceMembership,
)
from core.models.library import hash_modules
from core.services.encryption_service import EncryptionError, EncryptionService
from core.services.schedule_service import ScheduleService
from core.services.script_env import usable_env_vars
from pyrunner.version import __version__ as PYRUNNER_VERSION


def _restored_response_code(value):
    """A backup's run ``response_code`` when it is an HTTP status, else None (unknown)."""
    if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
        return value
    return None


class BackupService:
    """
    Service for creating and restoring PyRun backups.
    """

    # 1.2.0 — tenancy: a ``workspaces`` array + ``workspace_id`` on each scoped
    # row, so a whole-instance restore round-trips workspaces instead of
    # collapsing every tenant into the default (tenancy Stage 5). Backward
    # compatible: a pre-1.2.0 backup has neither, and restore maps its rows to the
    # default workspace.
    # 1.3.0 — Plugin Platform v2: ``owner_plugin``/``owner_key`` on
    # Script/Secret/DataStore, ``injection_mode`` + embedded ``secret_grants`` on
    # scripts. Backward compatible: a pre-1.3.0 backup has none of these, and
    # restore defaults them to NULL owner / ``injection_mode='all'`` / no grants
    # (= today's behavior), so old backups never hit a UNIQUE violation.
    # 1.4.0 — close the fields added after the format was first designed: weekly/
    # monthly schedule fields (``weekly_days``/``weekly_times``/``monthly_days``/
    # ``monthly_times`` — a weekly/monthly schedule previously restored as an empty
    # shell) and, on scripts, ``tags`` (embedded ``{name, color}``; get_or_create
    # by the globally-unique name on restore), ``archived_at``/``archived_by`` (soft
    # delete), and ``isolation_mode`` (sandbox opt-in). Backward compatible: a
    # pre-1.4.0 backup omits all of these and restore defaults them (empty lists /
    # not-archived / ``isolation_mode='inherit'``).
    #
    # 1.5.0 — Databases (managed Postgres): a ``databases`` array with row
    # metadata (schema/role names preserved verbatim — a dump only replays into
    # the same schema name), the Fernet-encrypted role password (same same-key
    # requirement as secrets), embedded per-database script ``grants``, and —
    # when the data server is reachable and pg_dump is installed — a plain-SQL
    # ``dump_sql`` of each ready schema (``dump_skipped_reason`` records why one
    # is absent). Restore re-provisions and replays; with no data server
    # attached, databases are skipped with a warning. Backward compatible: a
    # pre-1.5.0 backup has no ``databases`` key.
    #
    # 1.6.0 — External Secret Providers: a ``secret_providers`` array (one row per
    # configured external secrets backend — Vault/AWS/Infisical/Doppler/custom —
    # with ``credentials_encrypted`` carried VERBATIM, the same same-ENCRYPTION_KEY
    # stance as secrets) and three new fields on each secret: ``source``
    # (local/external), ``provider_name`` (the referenced profile BY NAME — ids
    # differ across instances, names are the stable join key), and ``external_ref``.
    # On restore, providers are upserted by name (never delete existing — they are
    # instance credential config, like the AI-provider rows below) and external
    # secrets relink to them by name. Backward compatible: a pre-1.6.0 backup has no
    # ``secret_providers`` key and no ``source`` on its secrets → every secret
    # defaults to ``source="local"`` (today's path, byte-for-byte).
    #
    # 1.7.0 — Script Libraries: a ``libraries`` array (one row per library, with
    # its HEAD revision's ``modules`` inlined and ``current_version`` carried
    # verbatim), plus ``libraries`` (attachment ids) on each script and
    # ``library_versions`` on each run. HEAD ONLY, by design: a backup restores a
    # working system, and forensic revision history is not operational state —
    # so restore recreates a single revision numbered ``current_version`` rather
    # than renumbering to 1. That keeps every restored run's stamp meaningful
    # (a run pinned to the head still resolves); a run pinned to an OLDER version
    # fails closed after restore, which is the documented trade. Backward
    # compatible: a pre-1.7.0 backup has no ``libraries`` key, no attachments and
    # no run stamps → nothing to materialize, exactly today's behavior.
    #
    # Deliberately NOT in the backup (documented so the omission reads as a choice,
    # not an oversight): S3/backup credentials, AI provider rows + settings,
    # reCAPTCHA keys, ``admin_url_slug``, worker/heartbeat settings, the Channels
    # subsystem (channels + members + messages), and public API tokens. These are
    # deployment-/secret-bound to a specific instance — restoring them onto a
    # different host would be wrong (stale endpoints) or a credential-leak vector —
    # so they are reconfigured per instance rather than carried in a portable dump.
    # 1.8.0 - Cron expression mode (``cron_expression``) and yearly mode
    # (``yearly_month``/``yearly_day``/``yearly_time``) on each schedule row.
    # Backward compatible: a pre-1.8.0 backup omits them and restore defaults
    # them ("" / None); only rows in those run modes ever use them.
    #
    # 1.9.0 — Vector seam: ``embedding_providers`` and ``vector_connections``
    # arrays (instance-global profiles, credentials VERBATIM, upserted by name on
    # restore exactly like secret providers — these ARE carried, unlike the AI
    # provider rows, because collections relink to them by name) and a
    # ``vector_collections`` array: row metadata joined to its connection/provider
    # BY NAME, plus — for Local collections with ``include_in_backup`` — the
    # SQLite image base64-inline (``file_b64``) up to PYRUNNER_VECTOR_BACKUP_MAX_MB;
    # larger ones carry ``file_skipped_reason`` and restore empty with a warning.
    # Qdrant contents are never carried (the store is the operator's). Backward
    # compatible: a pre-1.9.0 backup has none of the three keys → no-op; the
    # seeded Local connection stays.
    # 1.10.0 — Brain blobs, full revision history, derived index and grants.
    # Opted-out brains are omitted. API tokens and their grants are not restored.
    # Older backups without a brains key leave existing brains unchanged.
    # 1.11.0 — Brain feeds and unique applied-revision links. Restored queues
    # require an explicit retry; restoring a backup never starts paid AI work.
    # 1.12.0 — Optional Brain vector binding; transient reader jobs are omitted.
    # 1.13.0 — GitHub Brain sync configuration/baseline; jobs omitted, polling paused.
    # Next (1.14.0, bumped once for the whole 2.5 train): ``inputs_schema`` on
    # scripts and ``inputs`` on runs. Older backups omit both; restore leaves
    # them NULL (no declared inputs), which is how every pre-2.5 script runs.
    # ``result`` and ``progress`` on runs (what pyrunner_run reported, stored
    # masked); older backups omit them and restore leaves them NULL.
    # ``event`` on runs (what started the run, stored raw like inputs); older
    # backups omit it and restore leaves it NULL, as for a pre-2.5 run.
    # ``concurrency_policy`` on scripts (older backups: "allow", the pre-2.5
    # behaviour); ``cancel_reason`` and ``queued_behind`` on runs (older
    # backups: blank / NULL). ``queued_behind`` is restored only on finished
    # runs, and a held run's ``queued_payload`` is never exported: a restore
    # must not start a run that was waiting its turn when the backup was taken.
    # Retry settings on scripts (``retry_max_attempts``, ``retry_delay_seconds``,
    # ``retry_backoff_multiplier``, ``retry_on_timeout``; older backups: the
    # model defaults, no retries) and ``attempt``, ``retry_of`` and
    # ``next_attempt_at`` on runs (older backups: 1 / NULL / NULL).
    # ``retry_of`` is linked once every run is in, and only when that run was
    # restored too. ``next_attempt_at`` is restored only on finished runs, for
    # the same reason as ``queued_behind``: a restore never starts a retry.
    # ``maintenance_windows`` (global ones with no workspace). A backup with
    # the section replaces the instance's windows; an older one without it
    # leaves them as they are.
    # ``stop_grace_seconds`` on scripts (older backups: 10, the model default)
    # and ``stop_requested_at`` on runs (older backups: NULL). Like
    # ``queued_behind``, ``stop_requested_at`` is restored only on finished
    # runs: an unfinished one never comes back half-stopped.
    # ``env_vars`` on scripts (older backups: none, ``{}``). Restore keeps only
    # the entries the editor would accept (core.services.script_env).
    # ``expose_as_tool`` and ``tool_name`` on scripts (older backups: not a
    # tool, no name). Restore drops a malformed name, and when two restored
    # scripts in one workspace expose the same name the first keeps it and the
    # second comes back unexposed (core.api.script_tools.restored_tool); an
    # exposed script that loses its tool this way is named in the warnings.
    # ``response_code`` on runs (the status a webhook call got; older backups:
    # NULL, shown as unknown).
    # Webhook signature and rate fields on scripts (``webhook_signature``,
    # ``webhook_signature_secret_encrypted`` carried verbatim like the other
    # encrypted values, header, prefix, encoding, timestamp header, tolerance,
    # ``webhook_rate_per_minute``, ``webhook_rate_burst``; older backups: no
    # signature and the default limits). A value that isn't valid falls back
    # to its default (core.services.webhook_signatures.restored_settings); an
    # unknown preset restores as none and a custom header name the console
    # would refuse restores blank, both named in the warnings; with no
    # signature the stored secret is dropped.
    # ``webhook_mode`` on scripts (async | sync; older backups and anything
    # else: async).
    # ``connections`` and ``oauth_clients`` (credentials and client secrets
    # carried encrypted, like secrets) plus each script's ``connection_grants``;
    # a connection carries its app (``oauth_client_id``) and token expiry. A
    # backup without either key leaves this instance's rows of that kind alone.
    # ``watchers`` (2.5 T31: adapter, config, input map, interval, fire cap,
    # state, event log and health). A backup with the section replaces the
    # watchers; one without it has none (they go with their scripts). A
    # watcher whose connection didn't come back restores turned off, named in
    # the warnings; a claim in progress (``checking_since``) is never carried.
    # ``tables`` (2.5 T38: columns, description, owner fields and every row with
    # its id, data, times and creator). A backup with the section replaces the
    # tables; one without it leaves this instance's tables alone. Row ids are
    # kept (scripts address rows by id); ``row_count`` is recounted.
    BACKUP_VERSION = "1.13.0"
    MAX_BACKUP_SIZE_MB = 100
    # Default cap on a gzip backup's expanded size (F92, zip-bomb guard);
    # PYRUNNER_BACKUP_MAX_EXPANDED_MB overrides it. The default is above the
    # upload cap, so any backup accepted as plain JSON also loads gzipped.
    MAX_BACKUP_EXPANDED_MB = 256

    # Backup format constants
    FORMAT_JSON = "json"
    FORMAT_GZIP = "gzip"
    SUPPORTED_FORMATS = [FORMAT_JSON, FORMAT_GZIP]

    @classmethod
    def create_backup(
        cls,
        include_runs: bool = True,
        max_runs: int = 1000,
        include_package_operations: bool = False,
        include_datastores: bool = True,
        created_by_user=None,
    ) -> dict:
        """
        Create a complete backup of the PyRun instance.

        Args:
            include_runs: Include run history in backup
            max_runs: Maximum number of runs to include (0 = all, default 1000)
            include_package_operations: Include package operation history
            include_datastores: Include datastores and their entries
            created_by_user: User creating the backup (for metadata)

        Returns:
            dict: Backup data structure (ready to serialize to JSON)
        """
        backup_data = {
            "backup_metadata": {
                "version": cls.BACKUP_VERSION,
                "pyrunner_version": PYRUNNER_VERSION,
                "created_at": cls._serialize_datetime(timezone.now()),
                "instance_name": GlobalSettings.get_settings().instance_name,
                "encryption_key_hash": cls._calculate_encryption_key_hash(),
                "include_runs": include_runs,
                "include_run_history_count": max_runs if include_runs else 0,
                "created_by_email": created_by_user.email if created_by_user else None,
            },
            "global_settings": cls._export_global_settings(),
            "workspaces": cls._export_workspaces(),
            "environments": cls._export_environments(),
            "users": cls._export_users(),
            "libraries": cls._export_libraries(),
            "scripts": cls._export_scripts(),
            "script_schedules": cls._export_schedules(),
            "schedule_history": cls._export_schedule_history(),
            "secret_providers": cls._export_secret_providers(),
            "secrets": cls._export_secrets(),
        }

        if include_runs:
            backup_data["runs"] = cls._export_runs(max_runs)
        else:
            backup_data["runs"] = []

        if include_package_operations:
            backup_data["package_operations"] = cls._export_package_operations()
        else:
            backup_data["package_operations"] = []

        if include_datastores:
            backup_data["datastores"] = cls._export_datastores()
        else:
            backup_data["datastores"] = []

        backup_data["databases"] = cls._export_databases()
        backup_data["embedding_providers"] = cls._export_embedding_providers()
        backup_data["vector_connections"] = cls._export_vector_connections()
        backup_data["vector_collections"] = cls._export_vector_collections()
        from core.brain.backup import export_brains
        backup_data["brains"] = export_brains()
        backup_data["maintenance_windows"] = cls._export_maintenance_windows()
        backup_data["oauth_clients"] = cls._export_oauth_clients()
        backup_data["connections"] = cls._export_connections()
        backup_data["watchers"] = cls._export_watchers()
        backup_data["tables"] = cls._export_tables()

        return backup_data

    @classmethod
    def _export_tables(cls) -> List[dict]:
        """Tables (2.5 T38) with their rows, read in id order a chunk at a time."""
        out = []
        for table in Table.objects.select_related("created_by").order_by("created_at"):
            rows = [
                {
                    "id": row.id,
                    "data": row.data,
                    "created_at": cls._serialize_datetime(row.created_at),
                    "updated_at": cls._serialize_datetime(row.updated_at),
                    "created_by_email": email,
                }
                for row, email in (
                    (r, r.created_by.email if r.created_by_id else None)
                    for r in TableRow.objects.filter(table=table).select_related("created_by")
                    .order_by("id").iterator(chunk_size=2000)
                )
            ]
            out.append({
                "id": str(table.id),
                "workspace_id": str(table.workspace_id),
                "name": table.name,
                "slug": table.slug,
                "columns": table.columns,
                "description": table.description,
                "owner_plugin": table.owner_plugin,
                "owner_key": table.owner_key,
                "created_by_email": table.created_by.email if table.created_by else None,
                "created_at": cls._serialize_datetime(table.created_at),
                "updated_at": cls._serialize_datetime(table.updated_at),
                "rows": rows,
            })
        return out

    @classmethod
    def _import_tables(cls, tables: List[dict], user_map: dict, current_user, ws_map: dict, default_ws) -> Tuple[int, List[str]]:
        """Replace the tables with the backup's; ids of tables and rows are kept.

        A table whose slug or name another restored table of the same
        workspace already took (two source workspaces folded into the default)
        is skipped with a warning. The row data is written as it was saved:
        it was checked against the columns when it was written.
        """
        from django.db import connection
        from django.db.models import Max

        Table.objects.all().delete()
        warnings, restored = [], 0
        taken = set()
        for row in tables:
            workspace = cls._resolve_workspace(row.get("workspace_id"), ws_map, default_ws)
            keys = {(workspace.pk, "slug", row["slug"]), (workspace.pk, "name", row["name"].lower())}
            if keys & taken:
                warnings.append(f'Table "{row["name"]}": another restored table in its workspace has the same '
                                "name or slug, so it was left out.")
                continue
            taken |= keys
            table = Table.objects.create(
                id=row["id"], workspace=workspace, name=row["name"], slug=row["slug"],
                columns=row.get("columns") or [], description=row.get("description", ""),
                owner_plugin=row.get("owner_plugin"), owner_key=row.get("owner_key"),
                created_by=user_map.get(row.get("created_by_email"), current_user),
            )
            batch = []
            for item in row.get("rows") or []:
                batch.append(TableRow(
                    id=item["id"], table=table, data=item.get("data") or {},
                    created_at=cls._deserialize_datetime(item.get("created_at")) or timezone.now(),
                    updated_at=cls._deserialize_datetime(item.get("updated_at")) or timezone.now(),
                    created_by=user_map.get(item.get("created_by_email")),
                ))
                if len(batch) >= 1000:
                    TableRow.objects.bulk_create(batch)
                    batch = []
            if batch:
                TableRow.objects.bulk_create(batch)
            Table.objects.filter(pk=table.pk).update(row_count=TableRow.objects.filter(table=table).count())
            restored += 1
        # Rows keep their ids, so Postgres' id sequence must move past them. It
        # never moves back: like SQLite's AUTOINCREMENT, an id a deleted row had
        # isn't handed out again (a script may still hold it).
        top = TableRow.objects.aggregate(top=Max("id"))["top"]
        if connection.vendor == "postgresql" and top:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_get_serial_sequence(%s, 'id')", [TableRow._meta.db_table])
                sequence = cursor.fetchone()[0]
                if sequence:
                    cursor.execute(
                        f"SELECT setval(%s::regclass, GREATEST(%s, (SELECT last_value FROM {sequence})))",
                        [sequence, top],
                    )
        return restored, warnings

    @classmethod
    def _export_watchers(cls) -> List[dict]:
        """Watchers (2.5 T31), with their state so a restore doesn't re-fire old items."""
        return [
            {
                "id": str(w.id),
                "workspace_id": str(w.workspace_id),
                "name": w.name,
                "script_id": str(w.script_id),
                "adapter": w.adapter,
                "connection_id": str(w.connection_id) if w.connection_id else None,
                "config": w.config,
                "input_map": w.input_map,
                "interval_seconds": w.interval_seconds,
                "fire_cap_per_hour": w.fire_cap_per_hour,
                "enabled": w.enabled,
                "state": w.state,
                "recent_events": w.recent_events,
                "next_check_at": cls._serialize_datetime(w.next_check_at),
                "last_checked_at": cls._serialize_datetime(w.last_checked_at),
                "last_event_at": cls._serialize_datetime(w.last_event_at),
                "last_error": w.last_error,
                "error_count": w.error_count,
                "auto_disabled_at": cls._serialize_datetime(w.auto_disabled_at),
                "cap_window_start": cls._serialize_datetime(w.cap_window_start),
                "cap_window_count": w.cap_window_count,
                "owner_plugin": w.owner_plugin,
                "owner_key": w.owner_key,
                "created_by_email": w.created_by.email if w.created_by else None,
                "created_at": cls._serialize_datetime(w.created_at),
                "updated_at": cls._serialize_datetime(w.updated_at),
            }
            for w in Watcher.objects.select_related("created_by").order_by("created_at")
        ]

    @classmethod
    def _import_watchers(cls, rows: List[dict], script_map: dict, user_map: dict) -> Tuple[int, List[str]]:
        """Replace the watchers with the backup's (after scripts and connections).

        A watcher follows its script (skipped, with a warning, when the script
        didn't come back) into the script's workspace. Its connection is
        looked up by id in that workspace; when it's gone, the watcher comes
        back without one and turned off. A plugin's watcher whose owner key
        another one in the workspace already has (two plugin watchers can't
        share one, T33) is left out.
        """
        warnings = []
        Watcher.objects.all().delete()
        restored = 0
        owner_keys = set()
        for row in rows:
            script = script_map.get(row.get("script_id"))
            if script is None:
                warnings.append(f'Watcher "{row.get("name")}": its script wasn\'t in the backup, so it was left out.')
                continue
            if row.get("owner_plugin") and row.get("owner_key"):
                owner = (script.workspace_id, row["owner_plugin"], row["owner_key"])
                if owner in owner_keys:
                    warnings.append(
                        f'Watcher "{row.get("name")}": another watcher of the "{row["owner_plugin"]}" plugin '
                        f'has its key "{row["owner_key"]}", so it was left out.'
                    )
                    continue
                owner_keys.add(owner)
            enabled = bool(row.get("enabled", True))
            connection = None
            if row.get("connection_id"):
                connection = Connection.objects.filter(
                    pk=row["connection_id"], workspace_id=script.workspace_id,
                ).first()
                if connection is None:
                    if enabled:
                        warnings.append(
                            f'Watcher "{row["name"]}": its connection is gone, so it was turned off. '
                            "Edit it to pick a connection, then turn it on."
                        )
                    enabled = False
            Watcher.objects.create(
                id=row["id"],
                workspace_id=script.workspace_id,
                name=row["name"],
                script=script,
                adapter=row.get("adapter") or "",
                connection=connection,
                config=row.get("config") or {},
                input_map=row.get("input_map") or {},
                interval_seconds=max(int(row.get("interval_seconds") or Watcher.DEFAULT_INTERVAL_SECONDS),
                                     Watcher.INTERVAL_MIN_SECONDS),
                fire_cap_per_hour=int(row.get("fire_cap_per_hour") or Watcher.DEFAULT_FIRE_CAP),
                enabled=enabled,
                state=row.get("state"),
                recent_events=row.get("recent_events") or [],
                next_check_at=cls._deserialize_datetime(row.get("next_check_at")),
                last_checked_at=cls._deserialize_datetime(row.get("last_checked_at")),
                last_event_at=cls._deserialize_datetime(row.get("last_event_at")),
                last_error=row.get("last_error") or "",
                error_count=int(row.get("error_count") or 0),
                auto_disabled_at=cls._deserialize_datetime(row.get("auto_disabled_at")),
                cap_window_start=cls._deserialize_datetime(row.get("cap_window_start")),
                cap_window_count=int(row.get("cap_window_count") or 0),
                owner_plugin=row.get("owner_plugin"),
                owner_key=row.get("owner_key"),
                created_by=user_map.get(row.get("created_by_email")),
            )
            restored += 1
        return restored, warnings

    @classmethod
    def _export_global_settings(cls) -> dict:
        """Export GlobalSettings to dict."""
        settings_obj = GlobalSettings.get_settings()
        return {
            "instance_name": settings_obj.instance_name,
            "timezone": settings_obj.timezone,
            "email_backend": settings_obj.email_backend,
            "smtp_host": settings_obj.smtp_host,
            "smtp_port": settings_obj.smtp_port,
            "smtp_username": settings_obj.smtp_username,
            "smtp_password_encrypted": settings_obj.smtp_password_encrypted,
            "smtp_use_tls": settings_obj.smtp_use_tls,
            "smtp_from_email": settings_obj.smtp_from_email,
            "resend_api_key_encrypted": settings_obj.resend_api_key_encrypted,
            "resend_from_email": settings_obj.resend_from_email,
            "default_notification_email": settings_obj.default_notification_email,
            "retention_days": settings_obj.retention_days,
            "retention_count": settings_obj.retention_count,
            "auto_cleanup_enabled": settings_obj.auto_cleanup_enabled,
            "schedules_paused": settings_obj.schedules_paused,
            "api_writes_enabled": settings_obj.api_writes_enabled,
            "plugin_trust_policy": settings_obj.plugin_trust_policy,
            "plugin_store_url": settings_obj.plugin_store_url,
        }

    @classmethod
    def _export_workspaces(cls) -> List[dict]:
        """Export workspaces so a restore can round-trip tenancy (Stage 5)."""
        workspaces = []
        for ws in Workspace.objects.all().order_by("created_at"):
            workspaces.append({
                "id": str(ws.id),
                "name": ws.name,
                "is_default": ws.is_default,
                "created_at": cls._serialize_datetime(ws.created_at),
                "updated_at": cls._serialize_datetime(ws.updated_at),
            })
        return workspaces

    @classmethod
    def _export_maintenance_windows(cls) -> List[dict]:
        """Maintenance windows, global (no workspace) and per workspace."""
        def clock(t):
            return t.isoformat() if t is not None else None

        return [
            {
                "id": str(w.id),
                "workspace_id": str(w.workspace_id) if w.workspace_id else None,
                "name": w.name,
                "kind": w.kind,
                "starts_at": cls._serialize_datetime(w.starts_at),
                "ends_at": cls._serialize_datetime(w.ends_at),
                "start_time": clock(w.start_time),
                "end_time": clock(w.end_time),
                "weekdays": list(w.weekdays or []),
                "timezone": w.timezone,
                "enabled": w.enabled,
                "blocks": list(w.blocks or []),
                "created_at": cls._serialize_datetime(w.created_at),
                "updated_at": cls._serialize_datetime(w.updated_at),
            }
            for w in MaintenanceWindow.objects.order_by("created_at")
        ]

    @classmethod
    def _import_maintenance_windows(cls, rows: List[dict], ws_map: dict, default_ws) -> Tuple[int, List[str]]:
        """Replace the windows with the backup's. A window keeps no workspace
        (global) when it had none; otherwise its workspace is mapped like
        every other scoped row (default workspace fallback). A time zone this
        server doesn't know becomes UTC, with a warning naming the window."""
        from datetime import time
        from zoneinfo import available_timezones

        def clock(value):
            return time.fromisoformat(value) if value else None

        known = available_timezones() | {"UTC"}
        warnings = []
        MaintenanceWindow.objects.all().delete()
        for row in rows:
            ws_id = row.get("workspace_id")
            tz = row.get("timezone") or "UTC"
            if tz not in known:
                warnings.append(
                    f'Maintenance window "{row["name"]}": time zone {tz!r} is unknown '
                    "on this server, so it now uses UTC. Edit the window to set its zone."
                )
                tz = "UTC"
            MaintenanceWindow.objects.create(
                id=row["id"],
                workspace=cls._resolve_workspace(ws_id, ws_map, default_ws) if ws_id else None,
                name=row["name"],
                kind=row.get("kind") or MaintenanceWindow.Kind.WEEKLY,
                starts_at=cls._deserialize_datetime(row.get("starts_at")),
                ends_at=cls._deserialize_datetime(row.get("ends_at")),
                start_time=clock(row.get("start_time")),
                end_time=clock(row.get("end_time")),
                weekdays=[int(d) for d in row.get("weekdays") or []],
                timezone=tz,
                enabled=bool(row.get("enabled", True)),
                blocks=[b for b in row.get("blocks") or [] if b in MaintenanceWindow.Block.values],
            )
        return len(rows), warnings

    @classmethod
    def _export_environments(cls) -> List[dict]:
        """Export all environments to list of dicts."""
        environments = []
        for env in Environment.objects.all().order_by("created_at"):
            environments.append({
                "id": str(env.id),
                "name": env.name,
                "description": env.description,
                "path": env.path,
                "workspace_id": str(env.workspace_id) if env.workspace_id else None,
                "python_version": env.python_version,
                "requirements": env.requirements,
                "is_default": env.is_default,
                "is_active": env.is_active,
                "created_at": cls._serialize_datetime(env.created_at),
                "updated_at": cls._serialize_datetime(env.updated_at),
                "created_by_email": env.created_by.email if env.created_by else None,
            })
        return environments

    @classmethod
    def _export_users(cls) -> List[dict]:
        """Export users (basic info only, no passwords)."""
        users = []
        for user in User.objects.all().order_by("date_joined"):
            users.append({
                "id": user.id,
                "email": user.email,
                "is_verified": user.is_verified,
                "is_staff": user.is_staff,
                "is_superuser": user.is_superuser,
                "notify_on_login": user.notify_on_login,
                "date_joined": cls._serialize_datetime(user.date_joined),
            })
        return users

    @classmethod
    def _export_libraries(cls) -> List[dict]:
        """Export libraries with their HEAD revision inlined (never full history).

        ``current_version`` is carried verbatim so restore can recreate the head
        under its original number — a restored run's ``library_versions`` stamp
        must keep pointing at the content it actually ran.
        """
        from core.models import Library, LibraryRevision

        libraries = []
        queryset = Library.objects.select_related("created_by").order_by("created_at")
        # One query for every head revision instead of one per library.
        heads = {
            rev.library_id: rev
            for rev in LibraryRevision.objects.filter(
                library__in=queryset, version=models.F("library__current_version")
            )
        }
        for library in queryset:
            head = heads.get(library.id)
            libraries.append({
                "id": str(library.id),
                "key": library.key,
                "name": library.name,
                "description": library.description,
                "workspace_id": str(library.workspace_id) if library.workspace_id else None,
                "owner_plugin": library.owner_plugin,
                "owner_key": library.owner_key,
                "current_version": library.current_version,
                "modules": head.modules if head else {},
                "created_at": cls._serialize_datetime(library.created_at),
                "updated_at": cls._serialize_datetime(library.updated_at),
                "created_by_email": library.created_by.email if library.created_by else None,
            })
        return libraries

    @classmethod
    def _export_scripts(cls) -> List[dict]:
        """Export scripts with all fields."""
        scripts = []
        queryset = (
            Script.objects.select_related("environment", "created_by", "archived_by")
            .prefetch_related("tags", "secret_grants", "library_attachments", "connection_grants")
            .all()
            .order_by("created_at")
        )
        for script in queryset:
            scripts.append({
                "id": str(script.id),
                "name": script.name,
                "description": script.description,
                "code": script.code,
                "inputs_schema": script.inputs_schema,
                "env_vars": script.env_vars or {},
                "concurrency_policy": script.concurrency_policy,
                "environment_id": str(script.environment.id),
                "workspace_id": str(script.workspace_id) if script.workspace_id else None,
                "owner_plugin": script.owner_plugin,
                "owner_key": script.owner_key,
                "injection_mode": script.injection_mode,
                "isolation_mode": script.isolation_mode,
                "timeout_seconds": script.timeout_seconds,
                "stop_grace_seconds": script.stop_grace_seconds,
                "retry_max_attempts": script.retry_max_attempts,
                "retry_delay_seconds": script.retry_delay_seconds,
                "retry_backoff_multiplier": script.retry_backoff_multiplier,
                "retry_on_timeout": script.retry_on_timeout,
                "is_enabled": script.is_enabled,
                "expose_as_tool": script.expose_as_tool,
                "tool_name": script.tool_name,
                "webhook_token": script.webhook_token,
                "webhook_signature": script.webhook_signature,
                "webhook_signature_secret_encrypted": script.webhook_signature_secret_encrypted,
                "webhook_signature_header": script.webhook_signature_header,
                "webhook_signature_prefix": script.webhook_signature_prefix,
                "webhook_signature_encoding": script.webhook_signature_encoding,
                "webhook_signature_timestamp_header": script.webhook_signature_timestamp_header,
                "webhook_signature_tolerance": script.webhook_signature_tolerance,
                "webhook_rate_per_minute": script.webhook_rate_per_minute,
                "webhook_rate_burst": script.webhook_rate_burst,
                "webhook_mode": script.webhook_mode,
                "notify_on": script.notify_on,
                "notify_email": script.notify_email,
                "notify_webhook_url": script.notify_webhook_url,
                "notify_webhook_enabled": script.notify_webhook_enabled,
                "retention_days_override": script.retention_days_override,
                "retention_count_override": script.retention_count_override,
                # Tags are instance-global (unique name). Exported as {name, color}
                # and re-attached via get_or_create-by-name on restore, so a restore
                # onto an instance that already has the tag reuses it.
                "tags": [
                    {"name": t.name, "color": t.color}
                    for t in script.tags.all()
                ],
                # Soft-delete (archive) state. archived_by follows the same
                # simplified user mapping as created_by on restore.
                "archived_at": cls._serialize_datetime(script.archived_at),
                "archived_by_email": script.archived_by.email if script.archived_by else None,
                # Embedded per-script secret grants (selected-mode injection).
                # Imported after secrets+scripts exist; old backups have none.
                "secret_grants": [
                    {"secret_id": str(g.secret_id), "active": g.active}
                    for g in script.secret_grants.all()
                ],
                # Attached libraries (ids; UUIDs are preserved across restore).
                # Imported in their own pass once both sides exist, exactly like
                # secret_grants. Old backups carry none.
                "libraries": [
                    str(a.library_id) for a in script.library_attachments.all()
                ],
                # Explicit connection grants (2.5 T23), imported after both the
                # connections and the scripts exist. Old backups carry none.
                "connection_grants": [
                    {"connection_id": str(g.connection_id), "active": g.active}
                    for g in script.connection_grants.all()
                ],
                "created_at": cls._serialize_datetime(script.created_at),
                "updated_at": cls._serialize_datetime(script.updated_at),
                "created_by_email": script.created_by.email if script.created_by else None,
            })
        return scripts

    @classmethod
    def _export_schedules(cls) -> List[dict]:
        """Export script schedules (without q_schedule_ids - will be regenerated)."""
        schedules = []
        for schedule in ScriptSchedule.objects.select_related("script", "created_by").all():
            schedules.append({
                "id": str(schedule.id),
                "script_id": str(schedule.script.id),
                "workspace_id": str(schedule.workspace_id) if schedule.workspace_id else None,
                "run_mode": schedule.run_mode,
                "interval_minutes": schedule.interval_minutes,
                "daily_times": schedule.daily_times,
                "weekly_days": schedule.weekly_days,
                "weekly_times": schedule.weekly_times,
                "monthly_days": schedule.monthly_days,
                "monthly_times": schedule.monthly_times,
                "cron_expression": schedule.cron_expression,
                "yearly_month": schedule.yearly_month,
                "yearly_day": schedule.yearly_day,
                "yearly_time": schedule.yearly_time,
                "timezone": schedule.timezone,
                "is_active": schedule.is_active,
                "created_at": cls._serialize_datetime(schedule.created_at),
                "updated_at": cls._serialize_datetime(schedule.updated_at),
                "created_by_email": schedule.created_by.email if schedule.created_by else None,
            })
        return schedules

    @classmethod
    def _export_schedule_history(cls) -> List[dict]:
        """Export schedule history for audit trail."""
        history = []
        for item in ScheduleHistory.objects.select_related("schedule", "changed_by").all().order_by("created_at"):
            history.append({
                "id": str(item.id),
                "schedule_id": str(item.schedule.id),
                "change_type": item.change_type,
                "previous_config": item.previous_config,
                "new_config": item.new_config,
                "changed_by_email": item.changed_by.email if item.changed_by else None,
                "created_at": cls._serialize_datetime(item.created_at),
            })
        return history

    @classmethod
    def _export_embedding_providers(cls) -> List[dict]:
        """Vector seam (1.9.0): embedding-provider profiles, credential VERBATIM.

        Same stance as secret providers: instance-global config that the backup
        carries so collections relink to their provider by NAME on restore; the
        ciphertext decrypts on any target restoring with the same ENCRYPTION_KEY.
        """
        rows = []
        for p in EmbeddingProvider.objects.all().order_by("created_at"):
            rows.append({
                "id": str(p.id),
                "provider_type": p.provider_type,
                "name": p.name,
                "base_url": p.base_url,
                "credential_encrypted": p.credential_encrypted,
                "model": p.model,
                "dimension": p.dimension,
                "max_batch": p.max_batch,
                "enabled": p.enabled,
                "last_tested_at": cls._serialize_datetime(p.last_tested_at),
                "tokens_embedded": p.tokens_embedded,
                "embed_calls": p.embed_calls,
                "created_at": cls._serialize_datetime(p.created_at),
                "updated_at": cls._serialize_datetime(p.updated_at),
            })
        return rows

    @classmethod
    def _export_vector_connections(cls) -> List[dict]:
        """Vector seam (1.9.0): vector-store connections, credentials VERBATIM."""
        rows = []
        for c in VectorConnection.objects.all().order_by("created_at"):
            rows.append({
                "id": str(c.id),
                "provider_type": c.provider_type,
                "name": c.name,
                "config": c.config,
                "credentials_encrypted": c.credentials_encrypted,
                "enabled": c.enabled,
                "is_default": c.is_default,
                "last_tested_at": cls._serialize_datetime(c.last_tested_at),
                "created_at": cls._serialize_datetime(c.created_at),
                "updated_at": cls._serialize_datetime(c.updated_at),
            })
        return rows

    @classmethod
    def _export_vector_collections(cls) -> List[dict]:
        """Vector seam (1.9.0): collection rows + the Local store image, inline.

        A backup is one gzipped JSON document (no zip, no sidecar files), so a
        Local collection's SQLite image rides along base64-encoded — but only up
        to ``PYRUNNER_VECTOR_BACKUP_MAX_MB``: a bigger index is listed with
        ``file_skipped_reason`` and restored empty (re-embed), so one large
        collection can never make the archive unloadable through the web
        restore path. ``include_in_backup`` is the per-collection opt-out;
        Qdrant contents are the operator's and are never carried (row only).
        """
        from core.vectors.local_store import LocalStore

        cap_mb = int(getattr(settings, "PYRUNNER_VECTOR_BACKUP_MAX_MB", 50))
        cap = cap_mb * 1024 * 1024
        rows = []
        for col in (
            VectorCollection.objects.select_related("connection", "embedding_provider")
            .order_by("created_at")
        ):
            row = {
                "id": str(col.id),
                "workspace_id": str(col.workspace_id) if col.workspace_id else None,
                "name": col.name,
                "description": col.description,
                "owner_plugin": col.owner_plugin,
                "owner_key": col.owner_key,
                "connection_name": col.connection.name,
                "embedding_provider_name": (
                    col.embedding_provider.name if col.embedding_provider_id else None
                ),
                "embedding_model": col.embedding_model,
                "dimension": col.dimension,
                "metric": col.metric,
                "backend_ref": col.backend_ref,
                "points_count": col.points_count,
                "tokens_embedded": col.tokens_embedded,
                "embed_calls": col.embed_calls,
                "include_in_backup": col.include_in_backup,
                "created_at": cls._serialize_datetime(col.created_at),
                "updated_at": cls._serialize_datetime(col.updated_at),
                "file_b64": None,
                "file_bytes": 0,
                "file_skipped_reason": "",
            }
            path = col.file_path
            if not col.include_in_backup:
                reason = "excluded"
            elif path is None:
                reason = "backend"
            elif not path.exists():
                reason = "missing"
            else:
                try:
                    image = LocalStore(path, col.dimension, col.metric).export_bytes()
                    row["file_bytes"] = len(image)
                    if len(image) > cap:
                        reason = (
                            f"too_large ({len(image) // (1024 * 1024)} MB > "
                            f"PYRUNNER_VECTOR_BACKUP_MAX_MB={cap_mb})"
                        )
                    else:
                        row["file_b64"] = base64.b64encode(image).decode("ascii")
                        reason = ""
                except Exception as e:
                    reason = f"error: {e}"
            row["file_skipped_reason"] = reason
            rows.append(row)
        return rows

    @classmethod
    def _export_secret_providers(cls) -> List[dict]:
        """Export external secret-provider profiles (External Secret Providers).

        Instance-global config (no ``workspace_id``), like the AI-provider rows —
        but unlike those, these ARE carried, so external secrets round-trip. The
        credential blob is exported VERBATIM: it is Fernet-encrypted with the
        instance ``ENCRYPTION_KEY``, which the backup already pins via
        ``encryption_key_hash`` — so it is decryptable on any target restoring with
        the same key (and unreadable, as intended, without it).
        """
        providers = []
        for p in SecretProvider.objects.all().order_by("created_at"):
            providers.append({
                "id": str(p.id),
                "provider_type": p.provider_type,
                "name": p.name,
                "config": p.config,
                "credentials_encrypted": p.credentials_encrypted,
                "cache_ttl": p.cache_ttl,
                "on_error": p.on_error,
                "last_tested_at": cls._serialize_datetime(p.last_tested_at),
                "created_at": cls._serialize_datetime(p.created_at),
                "updated_at": cls._serialize_datetime(p.updated_at),
            })
        return providers

    @classmethod
    def _export_connections(cls) -> List[dict]:
        """Export connections (2.5 T23): credentials stay Fernet-encrypted, as secrets.

        Their grants travel with the scripts (``connection_grants``).
        """
        from core.models import Connection

        return [
            {
                "id": str(c.id),
                "workspace_id": str(c.workspace_id),
                "name": c.name,
                "provider": c.provider,
                "auth_kind": c.auth_kind,
                "config": c.config,
                "credentials_encrypted": c.credentials_encrypted,
                "scopes": c.scopes,
                "account_label": c.account_label,
                "status": c.status,
                "last_tested_at": cls._serialize_datetime(c.last_tested_at),
                "last_error": c.last_error,
                "grant_all_scripts": c.grant_all_scripts,
                "owner_plugin": c.owner_plugin,
                "owner_key": c.owner_key,
                "oauth_client_id": str(c.oauth_client_id) if c.oauth_client_id else None,
                "token_expires_at": cls._serialize_datetime(c.token_expires_at),
                "created_at": cls._serialize_datetime(c.created_at),
                "updated_at": cls._serialize_datetime(c.updated_at),
                "created_by_email": c.created_by.email if c.created_by else None,
            }
            for c in Connection.objects.select_related("created_by").order_by("created_at")
        ]

    @classmethod
    def _export_oauth_clients(cls) -> List[dict]:
        """Export OAuth apps (2.5 T24): the client secret stays Fernet-encrypted."""
        return [
            {
                "id": str(c.id),
                "workspace_id": str(c.workspace_id) if c.workspace_id else None,
                "provider": c.provider,
                "client_id": c.client_id,
                "client_secret_encrypted": c.client_secret_encrypted,
                "config": c.config,
                "created_at": cls._serialize_datetime(c.created_at),
                "updated_at": cls._serialize_datetime(c.updated_at),
                "created_by_email": c.created_by.email if c.created_by else None,
            }
            for c in OAuthClient.objects.select_related("created_by").order_by("created_at")
        ]

    @classmethod
    def _export_secrets(cls) -> List[dict]:
        """Export secrets (keep encrypted values)."""
        secrets = []
        for secret in Secret.objects.select_related("created_by", "provider").all().order_by("created_at"):
            secrets.append({
                "id": str(secret.id),
                "key": secret.key,
                "workspace_id": str(secret.workspace_id) if secret.workspace_id else None,
                "owner_plugin": secret.owner_plugin,
                "owner_key": secret.owner_key,
                # External Secret Providers: a local secret carries its Fernet
                # value; an external secret carries no value (encrypted_value is
                # blank) and instead points at a provider BY NAME + a reference.
                "source": secret.source,
                "provider_name": secret.provider.name if secret.provider_id else None,
                "external_ref": secret.external_ref,
                "encrypted_value": secret.encrypted_value,
                "description": secret.description,
                "created_at": cls._serialize_datetime(secret.created_at),
                "updated_at": cls._serialize_datetime(secret.updated_at),
                "created_by_email": secret.created_by.email if secret.created_by else None,
            })
        return secrets

    @classmethod
    def _export_runs(cls, max_count: int = 1000) -> List[dict]:
        """Export most recent runs."""
        runs = []
        queryset = Run.objects.select_related("script", "triggered_by").all().order_by("-created_at")
        if max_count > 0:
            queryset = queryset[:max_count]

        for run in queryset:
            runs.append({
                "id": str(run.id),
                "script_id": str(run.script.id),
                "workspace_id": str(run.workspace_id) if run.workspace_id else None,
                "status": run.status,
                "exit_code": run.exit_code,
                "stdout": run.stdout,
                "stderr": run.stderr,
                "started_at": cls._serialize_datetime(run.started_at),
                "ended_at": cls._serialize_datetime(run.ended_at),
                "code_snapshot": run.code_snapshot,
                # code_snapshot's counterpart for shared code — without it a
                # restored run couldn't say which library versions it ran.
                "library_versions": run.library_versions,
                # What the run started with (re-run reads it); raw, like the
                # rest of a backup's run data.
                "inputs": run.inputs,
                # What the script reported (pyrunner_run); masked when stored.
                "result": run.result,
                "progress": run.progress,
                # What started the run (webhook body, chat message...); raw.
                "event": run.event,
                # Concurrency policy outcome: skipped, or waited behind a run.
                "cancel_reason": run.cancel_reason,
                "queued_behind": str(run.queued_behind) if run.queued_behind else None,
                # Retry on failure: which attempt, of which first run.
                "attempt": run.attempt,
                "retry_of": str(run.retry_of_id) if run.retry_of_id else None,
                "next_attempt_at": cls._serialize_datetime(run.next_attempt_at),
                # Graceful stop: when someone stopped the run.
                "stop_requested_at": cls._serialize_datetime(run.stop_requested_at),
                # Webhook runs: the HTTP status the call was answered with.
                "response_code": run.response_code,
                "trigger_type": run.trigger_type,
                "triggered_by_email": run.triggered_by.email if run.triggered_by else None,
                "created_at": cls._serialize_datetime(run.created_at),
            })
        return runs

    @classmethod
    def _export_package_operations(cls) -> List[dict]:
        """Export package operations."""
        operations = []
        for op in PackageOperation.objects.select_related("environment", "created_by").all().order_by("created_at"):
            operations.append({
                "id": str(op.id),
                "environment_id": str(op.environment.id),
                "operation": op.operation,
                "package_spec": op.package_spec,
                "status": op.status,
                "output": op.output,
                "error": op.error,
                "created_at": cls._serialize_datetime(op.created_at),
                "started_at": cls._serialize_datetime(op.started_at),
                "completed_at": cls._serialize_datetime(op.completed_at),
                "created_by_email": op.created_by.email if op.created_by else None,
            })
        return operations

    @classmethod
    def _export_datastores(cls) -> List[dict]:
        """Export DataStores and their entries."""
        datastores = []
        for ds in DataStore.objects.select_related("created_by").all().order_by("created_at"):
            entries = []
            for entry in ds.entries.all().order_by("key"):
                entries.append({
                    "id": str(entry.id),
                    "key": entry.key,
                    "value_json": entry.value_json,
                    "created_at": cls._serialize_datetime(entry.created_at),
                    "updated_at": cls._serialize_datetime(entry.updated_at),
                })

            datastores.append({
                "id": str(ds.id),
                "name": ds.name,
                "workspace_id": str(ds.workspace_id) if ds.workspace_id else None,
                "owner_plugin": ds.owner_plugin,
                "owner_key": ds.owner_key,
                "description": ds.description,
                "created_at": cls._serialize_datetime(ds.created_at),
                "updated_at": cls._serialize_datetime(ds.updated_at),
                "created_by_email": ds.created_by.email if ds.created_by else None,
                "entries": entries,
            })
        return datastores

    @classmethod
    def _export_databases(cls) -> List[dict]:
        """Export managed databases: row metadata + grants + a pg_dump of each
        READY schema when the data server and client binaries allow it.

        Best-effort per database: a failed/unavailable dump records its reason
        in ``dump_skipped_reason`` instead of failing the whole backup — the
        metadata (and everything else in the backup) is still exact.
        """
        from core.services.database_service import DatabaseService

        databases = []
        for db in Database.objects.select_related("created_by").all().order_by("created_at"):
            dump_sql, skip_reason = None, ""
            if not DatabaseService.is_configured():
                skip_reason = "no data server attached (PYRUNNER_DATA_DB_URL unset)"
            elif not db.is_ready:
                skip_reason = f"database status is '{db.status}'"
            else:
                try:
                    dump_sql = DatabaseService.dump_schema(db)
                except Exception as e:
                    skip_reason = str(e)

            databases.append({
                "id": str(db.id),
                "name": db.name,
                "workspace_id": str(db.workspace_id) if db.workspace_id else None,
                "owner_plugin": db.owner_plugin,
                "owner_key": db.owner_key,
                # Preserved verbatim: the dump's statements are qualified with
                # this schema name, so restore must recreate it 1:1.
                "schema_name": db.schema_name,
                "role_name": db.role_name,
                # Fernet-encrypted — same same-ENCRYPTION_KEY requirement the
                # backup already imposes for secrets (encryption_key_hash).
                "encrypted_password": db.encrypted_password,
                "description": db.description,
                "grants": [
                    {"script_id": str(g.script_id), "active": g.active}
                    for g in db.grants.all()
                ],
                "dump_sql": dump_sql,
                "dump_skipped_reason": skip_reason,
                "created_at": cls._serialize_datetime(db.created_at),
                "updated_at": cls._serialize_datetime(db.updated_at),
                "created_by_email": db.created_by.email if db.created_by else None,
            })
        return databases

    @classmethod
    def _calculate_encryption_key_hash(cls) -> str:
        """Calculate SHA256 hash of current ENCRYPTION_KEY."""
        if not EncryptionService.is_configured():
            return ""

        encryption_key = settings.ENCRYPTION_KEY
        return hashlib.sha256(encryption_key.encode()).hexdigest()

    @classmethod
    def _serialize_datetime(cls, dt: Optional[datetime]) -> Optional[str]:
        """Serialize datetime to ISO format string."""
        if dt is None:
            return None
        return dt.isoformat()

    @classmethod
    def _deserialize_datetime(cls, dt_str: Optional[str]) -> Optional[datetime]:
        """Deserialize ISO format string to datetime."""
        if not dt_str:
            return None
        return datetime.fromisoformat(dt_str)

    # =====================================================================
    # SERIALIZATION METHODS
    # =====================================================================

    @classmethod
    def serialize_backup(cls, backup_data: dict, format: str = FORMAT_GZIP) -> Tuple[bytes, str]:
        """
        Serialize backup data to bytes.

        Args:
            backup_data: The backup dict to serialize
            format: "json" or "gzip" (default: gzip)

        Returns:
            tuple: (bytes_data, content_type)
        """
        json_str = json.dumps(backup_data, indent=2)

        if format == cls.FORMAT_GZIP:
            # Compress with gzip
            buffer = io.BytesIO()
            with gzip.GzipFile(fileobj=buffer, mode="wb") as gz:
                gz.write(json_str.encode("utf-8"))
            return buffer.getvalue(), "application/gzip"
        else:
            return json_str.encode("utf-8"), "application/json"

    @classmethod
    def max_expanded_bytes(cls) -> int:
        """Largest expanded size of a gzip backup accepted by the web restore."""
        mb = int(getattr(settings, "PYRUNNER_BACKUP_MAX_EXPANDED_MB", cls.MAX_BACKUP_EXPANDED_MB))
        return max(mb, 1) * 1024 * 1024

    @classmethod
    def deserialize_backup(cls, data: bytes, filename: str = "") -> dict:
        """
        Deserialize backup data from bytes.
        Auto-detects format based on magic bytes or filename.

        Args:
            data: Raw bytes from backup file
            filename: Original filename (helps with detection)

        Returns:
            dict: Parsed backup data

        Raises:
            ValueError: If format is unrecognized or parsing fails
        """
        # Detect gzip by magic bytes (1f 8b)
        is_gzip = data[:2] == b"\x1f\x8b"

        # Also check filename as fallback
        if not is_gzip and filename.endswith(".gz"):
            is_gzip = True

        if is_gzip:
            limit = cls.max_expanded_bytes()
            expanded = bytearray()
            try:
                # F92: read in chunks and stop at the cap — a few hundred KB of
                # gzipped zeros must not inflate to gigabytes in the web worker.
                with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
                    while len(expanded) <= limit:
                        chunk = gz.read(1024 * 1024)
                        if not chunk:
                            break
                        expanded += chunk
            except Exception as e:
                # U75: a readable reason, not zlib's "Not a gzipped file (b'\\xf7f')".
                raise ValueError("This file isn't a PyRun backup (it is neither a gzip-compressed nor a plain JSON backup).") from e
            if len(expanded) > limit:
                raise ValueError(
                    f"This backup expands to more than {limit // (1024 * 1024)} MB, so it can't be "
                    "restored from the browser. Restore it on the server with "
                    "`python manage.py restore_backup`, or raise PYRUNNER_BACKUP_MAX_EXPANDED_MB."
                )
            try:
                json_str = expanded.decode("utf-8")
            except UnicodeDecodeError as e:
                raise ValueError("This file isn't a PyRun backup (it is neither a gzip-compressed nor a plain JSON backup).") from e
            del expanded
        else:
            try:
                json_str = data.decode("utf-8")
            except UnicodeDecodeError as e:
                raise ValueError("This file isn't a PyRun backup (it is neither a gzip-compressed nor a plain JSON backup).") from e

        try:
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            raise ValueError(f"This file isn't a valid PyRun backup (invalid JSON: {e}).") from e

    @classmethod
    def get_file_extension(cls, format: str) -> str:
        """Return appropriate file extension for format."""
        if format == cls.FORMAT_GZIP:
            return ".json.gz"
        return ".json"

    # =====================================================================
    # VALIDATION METHODS
    # =====================================================================

    @classmethod
    def validate_backup(cls, backup_data: dict) -> dict:
        """
        Validate backup structure and content.

        Returns:
            dict: {
                "valid": bool,
                "errors": list,
                "warnings": list,
                "metadata": dict
            }
        """
        errors = []
        warnings = []

        # Check required top-level keys
        required_keys = [
            "backup_metadata",
            "global_settings",
            "environments",
            "users",
            "scripts",
            "script_schedules",
            "schedule_history",
            "secrets",
            "runs",
        ]

        for key in required_keys:
            if key not in backup_data:
                errors.append(f"Missing required key: {key}")

        if errors:
            return {"valid": False, "errors": errors, "warnings": warnings, "metadata": {}}

        # Validate metadata
        metadata = backup_data.get("backup_metadata", {})
        if "version" not in metadata:
            errors.append("Missing backup version in metadata")
        elif metadata["version"] != cls.BACKUP_VERSION:
            warnings.append(
                f"Backup version {metadata['version']} differs from current version {cls.BACKUP_VERSION}"
            )

        # Validate encryption key
        if not cls.validate_encryption_key(backup_data):
            errors.append(
                "ENCRYPTION_KEY mismatch: Backup was created with a different encryption key. "
                "Encrypted secrets and credentials will be unreadable. To restore it here, set this "
                "server's ENCRYPTION_KEY to the source server's key (and restart), then upload it "
                "again — see Backup & Restore in DEPLOYMENT.md."
            )

        # Validate foreign key references
        env_ids = {env["id"] for env in backup_data.get("environments", [])}
        script_ids = {script["id"] for script in backup_data.get("scripts", [])}

        # Check script references to environments
        for script in backup_data.get("scripts", []):
            if script.get("environment_id") not in env_ids:
                errors.append(
                    f"Script '{script.get('name')}' references non-existent environment ID: {script.get('environment_id')}"
                )

        # Check schedule references to scripts
        for schedule in backup_data.get("script_schedules", []):
            if schedule.get("script_id") not in script_ids:
                errors.append(
                    f"Schedule references non-existent script ID: {schedule.get('script_id')}"
                )

        # Check run references to scripts
        for run in backup_data.get("runs", []):
            if run.get("script_id") not in script_ids:
                errors.append(
                    f"Run references non-existent script ID: {run.get('script_id')}"
                )

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
            "metadata": metadata,
        }

    @classmethod
    def validate_encryption_key(cls, backup_data: dict) -> bool:
        """
        Verify ENCRYPTION_KEY matches backup.

        Returns:
            bool: True if key matches or no encryption used
        """
        metadata = backup_data.get("backup_metadata", {})
        backup_key_hash = metadata.get("encryption_key_hash", "")

        if not backup_key_hash:
            # Backup has no encryption key hash (old format or no encryption)
            return True

        current_key_hash = cls._calculate_encryption_key_hash()
        return current_key_hash == backup_key_hash

    @classmethod
    def get_backup_preview(cls, backup_data: dict) -> dict:
        """
        Generate preview summary for user confirmation.

        Returns:
            dict: {
                "instance_name": str,
                "created_at": str,
                "created_by_email": str,
                "counts": {
                    "scripts": int,
                    "environments": int,
                    "secrets": int,
                    "runs": int,
                    ...
                },
                "warnings": list
            }
        """
        metadata = backup_data.get("backup_metadata", {})
        warnings = []

        # Check encryption key
        if not cls.validate_encryption_key(backup_data):
            warnings.append("ENCRYPTION_KEY mismatch - encrypted data may be unreadable")

        # Check version compatibility
        if metadata.get("version") != cls.BACKUP_VERSION:
            warnings.append(
                f"Backup version ({metadata.get('version')}) differs from current version ({cls.BACKUP_VERSION})"
            )

        datastores = backup_data.get("datastores", [])
        counts = {
            "scripts": len(backup_data.get("scripts", [])),
            "workspaces": len(backup_data.get("workspaces", [])),
            "environments": len(backup_data.get("environments", [])),
            "secret_providers": len(backup_data.get("secret_providers", [])),
            "secrets": len(backup_data.get("secrets", [])),
            "runs": len(backup_data.get("runs", [])),
            # Manual rows exist for every script; count real schedules (U43).
            "schedules": sum(
                1
                for row in backup_data.get("script_schedules", [])
                if row.get("run_mode", "manual") != "manual" and row.get("is_active", True)
            ),
            "users": len(backup_data.get("users", [])),
            "package_operations": len(backup_data.get("package_operations", [])),
            "datastores": len(datastores),
            "datastore_entries": sum(len(ds.get("entries", [])) for ds in datastores),
            "vector_collections": len(backup_data.get("vector_collections", [])),
            "brains": len(backup_data.get("brains", [])),
            "libraries": len(backup_data.get("libraries", [])),
            "databases": len(backup_data.get("databases", [])),
            "connections": len(backup_data.get("connections", [])),
            "oauth_clients": len(backup_data.get("oauth_clients", [])),
            # A restore replaces the watchers (an older backup without the
            # section restores none), so the preview names how many come back.
            "watchers": len(backup_data.get("watchers", [])),
            # Like watchers: an older backup without the section leaves tables alone.
            "tables": len(backup_data.get("tables", [])),
            "table_rows": sum(len(t.get("rows") or []) for t in backup_data.get("tables", [])),
        }

        return {
            "instance_name": metadata.get("instance_name", "Unknown"),
            "created_at": metadata.get("created_at", ""),
            "created_by_email": metadata.get("created_by_email", "Unknown"),
            "counts": counts,
            "warnings": warnings,
            "tokens_removed": cls.tokens_removed_by_restore(with_tables="tables" in backup_data),
        }

    @staticmethod
    def tokens_removed_by_restore(with_tables=True) -> list:
        """API tokens a restore deletes on THIS instance (F61).

        Backups don't carry API tokens and a restore leaves the token table
        alone, but it deletes every Brain, DataStore and Script first (and every
        Table when the backup has tables), and a token bound to a brain, a
        script or a table, or scoped to one store, goes with it
        (FK cascade) — even when the restored row comes back with the same id.
        Unbound tokens survive. The preview names them so a same-instance rollback doesn't
        silently break the connectors using them. ``with_tables`` False (a
        backup without a tables section leaves tables alone) skips table-bound tokens.
        """
        from django.db.models import Q

        from core.models import APIToken

        rows = (
            APIToken.objects.filter(
                Q(brain__isnull=False) | Q(datastore__isnull=False) | Q(script__isnull=False)
                | (Q(table__isnull=False) if with_tables else Q(pk__in=[]))
            )
            .select_related("brain", "datastore", "script", "table")
            .order_by("name")
        )

        def bound_to(token):
            if token.brain_id:
                return f"brain {token.brain.name}"
            if token.script_id:
                return f"script {token.script.name}"
            if token.table_id:
                return f"table {token.table.name}"
            return f"data store {token.datastore.name}"

        return [{"name": token.name, "bound_to": bound_to(token)} for token in rows]

    # =====================================================================
    # RESTORE METHODS
    # =====================================================================

    @classmethod
    @transaction.atomic
    def restore_backup(
        cls,
        backup_data: dict,
        restore_runs: bool = True,
        current_user=None,
    ) -> dict:
        """
        Restore backup data to database.

        Performs full replace of existing data.
        Wrapped in transaction for safety.

        Args:
            backup_data: Validated backup data dict
            restore_runs: Whether to restore run history
            current_user: User performing the restore (for ownership mapping)

        Returns:
            dict: {
                "success": bool,
                "counts": dict,
                "errors": list
            }
        """
        try:
            warnings: List[str] = []
            from core.brain.backup import import_brains, validate_brain_backup
            if "brains" in backup_data:
                validate_brain_backup(backup_data["brains"])

            # Delete existing data (reverse dependency order)
            Run.objects.all().delete()
            PackageOperation.objects.all().delete()
            ScheduleHistory.objects.all().delete()
            ScriptSchedule.objects.all().delete()
            Script.objects.all().delete()
            # After Scripts (whose deletion cascades the attachment rows) and
            # before the import: restore is a full replace, and a library key is
            # unique per workspace, so a surviving row would collide with the
            # incoming one. Cascades to revisions.
            Library.objects.all().delete()
            Secret.objects.all().delete()
            # Connections are full-replace when the backup has them (2.5 T23);
            # an older backup without the key leaves this instance's alone.
            if "connections" in backup_data:
                Connection.objects.all().delete()
            if "oauth_clients" in backup_data:
                OAuthClient.objects.all().delete()
            Environment.objects.all().delete()
            DataStoreEntry.objects.all().delete()
            DataStore.objects.all().delete()
            # Databases are full-replace like everything else, but their data
            # lives on the data server — deprovision each (best-effort; a
            # failure becomes a warning, never an aborted restore) before the
            # rows go, or the schemas would be orphaned server-side.
            warnings.extend(cls._cleanup_existing_databases())
            Database.objects.all().delete()
            # Vector collections are full-replace too; their Local store files go
            # with the rows (connections/providers are upserted, never deleted).
            warnings.extend(cls._cleanup_existing_vector_collections())
            VectorCollection.objects.all().delete()
            if "brains" in backup_data:
                Brain.objects.all().delete()
            # Note: We don't delete User objects to preserve authentication
            # GlobalSettings is singleton, so we update rather than delete

            # Import data in dependency order
            cls._import_global_settings(backup_data.get("global_settings", {}))

            # Tenancy Stage 5: rebuild the workspace topology FIRST so every scoped
            # row can be re-associated. The source's default workspace maps to THIS
            # instance's default (a single default always exists); non-default
            # source workspaces are recreated (UUID preserved). A pre-1.2.0 backup
            # has no workspaces/workspace_id → every row falls back to the default
            # (no tenant collapse to worry about — there was only one tenant).
            default_ws = Workspace.get_default()
            if default_ws is None:
                default_ws = Workspace.objects.create(
                    name="Default Workspace", is_default=True
                )
            ws_map = cls._import_workspaces(
                backup_data.get("workspaces", []), default_ws, current_user
            )

            user_map = cls._import_users(backup_data.get("users", []), current_user)
            env_map = cls._import_environments(backup_data.get("environments", []), user_map, current_user, ws_map, default_ws)
            # Secret providers first (external secrets relink to them by name).
            # Upsert, not full-replace: an older backup omits the key → no-op, and
            # existing provider credentials are never wiped by an unrelated restore.
            providers_restored = cls._import_secret_providers(backup_data.get("secret_providers", []))
            warnings.extend(
                cls._import_secrets(backup_data.get("secrets", []), user_map, current_user, ws_map, default_ws)
            )
            oauth_clients_restored = None
            if "oauth_clients" in backup_data:
                oauth_clients_restored, oauth_warnings = cls._import_oauth_clients(
                    backup_data["oauth_clients"], user_map, current_user, ws_map, default_ws
                )
                warnings.extend(oauth_warnings)
            connections_restored = None
            if "connections" in backup_data:
                connections_restored, connection_warnings = cls._import_connections(
                    backup_data["connections"], user_map, current_user, ws_map, default_ws
                )
                warnings.extend(connection_warnings)
            libraries_restored = cls._import_libraries(
                backup_data.get("libraries", []), user_map, current_user, ws_map, default_ws
            )
            script_map = cls._import_scripts(backup_data.get("scripts", []), env_map, user_map, current_user, ws_map, default_ws, warnings)
            # Grants reference both Secrets (imported above) and Scripts (just
            # imported), so they go in their own pass. Old backups carry none.
            cls._import_secret_grants(backup_data.get("scripts", []), script_map)
            cls._import_connection_grants(backup_data.get("scripts", []), script_map)
            # Library attachments need both sides too — same pass-ordering reason.
            cls._import_library_attachments(backup_data.get("scripts", []), script_map)
            cls._import_schedules(backup_data.get("script_schedules", []), script_map, user_map, current_user, ws_map, default_ws)
            cls._import_schedule_history(backup_data.get("schedule_history", []), user_map, current_user)

            if restore_runs:
                cls._import_runs(backup_data.get("runs", []), script_map, user_map, current_user, ws_map, default_ws)

            cls._import_package_operations(backup_data.get("package_operations", []), env_map, user_map, current_user)
            ds_map = cls._import_datastores(backup_data.get("datastores", []), user_map, current_user, ws_map, default_ws)
            databases_restored, db_warnings = cls._import_databases(
                backup_data.get("databases", []), script_map, user_map,
                current_user, ws_map, default_ws,
            )
            warnings.extend(db_warnings)
            embedding_providers_restored = cls._import_embedding_providers(
                backup_data.get("embedding_providers", [])
            )
            vector_connections_restored = cls._import_vector_connections(
                backup_data.get("vector_connections", [])
            )
            vector_collections_restored, vector_warnings = cls._import_vector_collections(
                backup_data.get("vector_collections", []), current_user, ws_map, default_ws
            )
            warnings.extend(vector_warnings)
            brains_restored, brain_warnings = import_brains(
                backup_data.get("brains", []),
                workspace_for=lambda ws_id: cls._resolve_workspace(ws_id, ws_map, default_ws),
                user_map=user_map, script_map=script_map, current_user=current_user,
            )
            warnings.extend(brain_warnings)
            maintenance_windows_restored = None
            if "maintenance_windows" in backup_data:
                maintenance_windows_restored, window_warnings = cls._import_maintenance_windows(
                    backup_data["maintenance_windows"], ws_map, default_ws
                )
                warnings.extend(window_warnings)
            watchers_restored = None
            if "watchers" in backup_data:
                watchers_restored, watcher_warnings = cls._import_watchers(
                    backup_data["watchers"], script_map, user_map
                )
                warnings.extend(watcher_warnings)
            tables_restored = None
            if "tables" in backup_data:
                tables_restored, table_warnings = cls._import_tables(
                    backup_data["tables"], user_map, current_user, ws_map, default_ws
                )
                warnings.extend(table_warnings)

            # create() can't set auto_now/auto_now_add fields: Django overwrites
            # them, so every row above was stamped with the restore time. Put
            # the backup's times back (run history, schedule history, …).
            cls._restore_timestamps(backup_data, restore_runs)

            # Regenerate django-q2 schedules
            schedules_created = cls._regenerate_all_schedules()

            counts = {
                "environments": len(env_map),
                "scripts": len(script_map),
                "secret_providers": providers_restored,
                "secrets": Secret.objects.count(),
                "runs": Run.objects.count() if restore_runs else 0,
                "schedules": ScriptSchedule.objects.count(),
                "schedules_regenerated": schedules_created,
                "datastores": len(ds_map),
                "datastore_entries": DataStoreEntry.objects.count(),
                "databases": databases_restored,
                "libraries": libraries_restored,
                "embedding_providers": embedding_providers_restored,
                "vector_connections": vector_connections_restored,
                "vector_collections": vector_collections_restored,
                "brains": brains_restored,
            }
            if maintenance_windows_restored is not None:
                counts["maintenance_windows"] = maintenance_windows_restored
            if watchers_restored is not None:
                counts["watchers"] = watchers_restored
            if tables_restored is not None:
                counts["tables"] = tables_restored
                counts["table_rows"] = TableRow.objects.count()
            if connections_restored is not None:
                counts["connections"] = connections_restored
            if oauth_clients_restored is not None:
                counts["oauth_clients"] = oauth_clients_restored

            return {"success": True, "counts": counts, "errors": [], "warnings": warnings}

        except Exception as e:
            transaction.set_rollback(True)
            return {"success": False, "counts": {}, "errors": [str(e)], "warnings": []}

    # Backup sections whose rows keep their id on restore and carry
    # created_at/updated_at.
    _TIMESTAMPED_SECTIONS = (
        ("environments", Environment),
        ("secrets", Secret),
        ("libraries", Library),
        ("scripts", Script),
        ("script_schedules", ScriptSchedule),
        ("schedule_history", ScheduleHistory),
        ("runs", Run),
        ("package_operations", PackageOperation),
        ("datastores", DataStore),
        ("maintenance_windows", MaintenanceWindow),
        ("connections", Connection),
        ("oauth_clients", OAuthClient),
        ("watchers", Watcher),
        ("tables", Table),
    )

    @classmethod
    def _restore_timestamps(cls, backup_data: dict, restore_runs: bool = True) -> None:
        """Write the backup's created_at/updated_at onto the restored rows.

        ``bulk_update`` skips ``pre_save``, so ``auto_now``/``auto_now_add``
        don't replace the values. Rows without a time in the backup keep the
        restore time. Only the id and the two timestamps are loaded.
        """

        def apply(model, rows):
            columns = {f.name for f in model._meta.concrete_fields} & {"created_at", "updated_at"}
            wanted = {}
            for row in rows:
                stamps = {
                    name: cls._deserialize_datetime(row.get(name))
                    for name in columns
                    if row.get(name)
                }
                if row.get("id") and stamps:
                    wanted[str(row["id"])] = stamps
            if not wanted:
                return
            ids = list(wanted)
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                objs = list(model.objects.filter(pk__in=chunk).only("pk", *columns))
                for obj in objs:
                    for name, value in wanted[str(obj.pk)].items():
                        setattr(obj, name, value)
                if objs:
                    model.objects.bulk_update(objs, sorted(columns))

        for key, model in cls._TIMESTAMPED_SECTIONS:
            if key == "runs" and not restore_runs:
                continue
            apply(model, backup_data.get(key) or [])
        apply(
            DataStoreEntry,
            [entry for ds in backup_data.get("datastores") or [] for entry in ds.get("entries", [])],
        )

    @classmethod
    def _import_global_settings(cls, data: dict) -> None:
        """Import GlobalSettings."""
        settings_obj = GlobalSettings.get_settings()
        settings_obj.instance_name = data.get("instance_name", "PyRun")
        settings_obj.timezone = data.get("timezone", "UTC")
        # date_format/time_format were removed (stored but never displayed);
        # old backups may still carry the keys — ignored harmlessly on restore.
        settings_obj.email_backend = data.get("email_backend", "disabled")
        settings_obj.smtp_host = data.get("smtp_host", "")
        settings_obj.smtp_port = data.get("smtp_port", 587)
        settings_obj.smtp_username = data.get("smtp_username", "")
        settings_obj.smtp_password_encrypted = data.get("smtp_password_encrypted", "")
        settings_obj.smtp_use_tls = data.get("smtp_use_tls", True)
        settings_obj.smtp_from_email = data.get("smtp_from_email", "")
        settings_obj.resend_api_key_encrypted = data.get("resend_api_key_encrypted", "")
        settings_obj.resend_from_email = data.get("resend_from_email", "")
        settings_obj.default_notification_email = data.get("default_notification_email", "")
        settings_obj.retention_days = data.get("retention_days", 0)
        settings_obj.retention_count = data.get("retention_count", 0)
        settings_obj.auto_cleanup_enabled = data.get("auto_cleanup_enabled", False)
        settings_obj.schedules_paused = data.get("schedules_paused", False)
        # Only a literal true turns writes on; an older backup leaves them off.
        settings_obj.api_writes_enabled = data.get("api_writes_enabled") is True
        # An older backup, or an unknown value, keeps the default (allow unsigned).
        policy = data.get("plugin_trust_policy")
        settings_obj.plugin_trust_policy = (
            policy if policy in GlobalSettings.PluginTrustPolicy.values
            else GlobalSettings.PluginTrustPolicy.ALLOW_UNSIGNED
        )
        # Plugin store index URL (2.5 T44): blank (PyRun's official index) when
        # absent or not an http(s) URL; every fetch is SSRF-checked anyway.
        store_url = data.get("plugin_store_url")
        settings_obj.plugin_store_url = (
            store_url.strip()
            if isinstance(store_url, str) and len(store_url.strip()) <= 500
            and store_url.strip().lower().startswith(("http://", "https://"))
            else ""
        )
        settings_obj.save()

    @classmethod
    def _import_workspaces(cls, workspaces_data: List[dict], default_ws, current_user) -> dict:
        """Rebuild the workspace topology and map backup-ws-id → local Workspace.

        The source's default workspace maps to this instance's default (keeping a
        single default); each non-default source workspace is recreated with its
        UUID preserved (reused if it already exists). The user performing the
        restore is made an Owner of each recreated workspace so the restored
        tenants are actually reachable (membership rows themselves are not part of
        the whole-instance backup — see the simplified user import).
        """
        ws_map = {}
        for w in workspaces_data:
            old_id = w["id"]
            if w.get("is_default"):
                ws_map[old_id] = default_ws  # source default → local default
                continue
            ws = Workspace.objects.filter(pk=old_id).first()
            if ws is None:
                ws = Workspace.objects.create(
                    id=old_id,
                    name=w.get("name", "Workspace"),
                    is_default=False,
                    created_at=cls._deserialize_datetime(w.get("created_at")),
                    updated_at=cls._deserialize_datetime(w.get("updated_at")),
                )
                if current_user is not None:
                    WorkspaceMembership.ensure(
                        current_user, ws, role=WorkspaceMembership.ROLE_OWNER
                    )
            ws_map[old_id] = ws
        return ws_map

    @classmethod
    def _resolve_workspace(cls, workspace_id, ws_map: dict, default_ws):
        """Map a backed-up workspace_id to a local Workspace (default fallback)."""
        if not workspace_id:
            return default_ws
        return ws_map.get(workspace_id, default_ws)

    @classmethod
    def _import_users(cls, users_data: List[dict], current_user) -> dict:
        """
        Import users and create email->user mapping.
        Maps all to current user for simplicity.

        Returns:
            dict: {"email@example.com": User instance}
        """
        user_map = {}

        # If current_user exists, map all emails to current user
        if current_user:
            for user_data in users_data:
                email = user_data.get("email")
                if email:
                    user_map[email] = current_user

        # Also handle None case
        user_map[None] = current_user if current_user else None

        # Accounts are never created or changed by a restore, except one
        # preference: the sign-in alert opt-in (2.5 T45) goes back onto the
        # existing account with the same email. Older backups lack the key.
        for user_data in users_data:
            email = user_data.get("email")
            if email and "notify_on_login" in user_data:
                User.objects.filter(email__iexact=email).update(
                    notify_on_login=bool(user_data["notify_on_login"])
                )

        return user_map

    @classmethod
    def _import_environments(cls, envs_data: List[dict], user_map: dict, current_user, ws_map: dict, default_ws) -> dict:
        """
        Import environments.

        Returns:
            dict: {"old_uuid": Environment instance}
        """
        env_map = {}

        for env_data in envs_data:
            old_id = env_data["id"]
            created_by = user_map.get(env_data.get("created_by_email"), current_user)

            env = Environment.objects.create(
                id=old_id,  # Preserve UUID
                name=env_data["name"],
                description=env_data.get("description", ""),
                path=env_data["path"],
                workspace=cls._resolve_workspace(env_data.get("workspace_id"), ws_map, default_ws),
                python_version=env_data.get("python_version", ""),
                requirements=env_data.get("requirements", ""),
                is_default=False,  # Will set default after all are created
                is_active=env_data.get("is_active", True),
                created_at=cls._deserialize_datetime(env_data.get("created_at")),
                updated_at=cls._deserialize_datetime(env_data.get("updated_at")),
                created_by=created_by,
            )
            env_map[old_id] = env

            # Set default environment
            if env_data.get("is_default"):
                env.is_default = True
                env.save()

        return env_map

    @classmethod
    def _import_secret_providers(cls, providers_data: List[dict]) -> int:
        """Import external secret-provider profiles, keyed by NAME.

        Upsert (not full-replace): the profile ``name`` is unique and stable across
        instances — the join key both for idempotent import and for relinking
        external secrets — so an existing profile of the same name is updated to
        match the backup (credentials VERBATIM; the backup's key-hash gate
        guarantees they decrypt on the target). Nothing is deleted: providers are
        instance credential config, and an older archive omits this key entirely,
        so a restore never wipes providers the backup simply predates.

        Returns the number of profiles imported.
        """
        for p in providers_data:
            SecretProvider.objects.update_or_create(
                name=p["name"],
                defaults={
                    "provider_type": p["provider_type"],
                    "config": p.get("config", {}),
                    "credentials_encrypted": p.get("credentials_encrypted", ""),
                    "cache_ttl": p.get("cache_ttl", 300),
                    "on_error": p.get("on_error", SecretProvider.OnError.FAIL),
                    "last_tested_at": cls._deserialize_datetime(p.get("last_tested_at")),
                },
            )
        return len(providers_data)

    @classmethod
    def _import_embedding_providers(cls, rows: List[dict]) -> int:
        """Upsert embedding providers by NAME (never delete — instance config)."""
        for p in rows:
            EmbeddingProvider.objects.update_or_create(
                name=p["name"],
                defaults={
                    "provider_type": p["provider_type"],
                    "base_url": p.get("base_url", ""),
                    "credential_encrypted": p.get("credential_encrypted", ""),
                    "model": p.get("model", ""),
                    "dimension": p.get("dimension"),
                    "max_batch": p.get("max_batch") or 64,
                    "enabled": bool(p.get("enabled", True)),
                    "last_tested_at": cls._deserialize_datetime(p.get("last_tested_at")),
                    "tokens_embedded": p.get("tokens_embedded") or 0,
                    "embed_calls": p.get("embed_calls") or 0,
                },
            )
        return len(rows)

    @classmethod
    def _import_vector_connections(cls, rows: List[dict]) -> int:
        """Upsert vector connections by NAME (never delete). The backup's default wins."""
        for c in rows:
            VectorConnection.objects.update_or_create(
                name=c["name"],
                defaults={
                    "provider_type": c["provider_type"],
                    "config": c.get("config") or {},
                    "credentials_encrypted": c.get("credentials_encrypted", ""),
                    "enabled": bool(c.get("enabled", True)),
                    "is_default": bool(c.get("is_default", False)),
                    "last_tested_at": cls._deserialize_datetime(c.get("last_tested_at")),
                },
            )
        return len(rows)

    @classmethod
    def _cleanup_existing_vector_collections(cls) -> List[str]:
        """Remove the Local store files of every existing collection (full replace).

        Best effort: a file that cannot be removed becomes a warning, never an
        aborted restore. Like the databases' deprovision, this runs inside the
        restore transaction but touches disk, so a later failure rolls back the
        rows and not the files — the Retry button on a collection recreates an
        empty store.
        """
        from core.vectors.local_store import LocalStore

        warnings: List[str] = []
        for col in VectorCollection.objects.all():
            path = col.file_path
            if path is None:
                continue
            try:
                LocalStore(path, col.dimension, col.metric).delete_files()
            except Exception as e:  # pragma: no cover — OS-level failure only
                warnings.append(f"Vector collection '{col.name}': could not remove its store: {e}")
        return warnings

    @classmethod
    def _import_vector_collections(
        cls, rows: List[dict], current_user, ws_map: dict, default_ws
    ) -> Tuple[int, List[str]]:
        """Recreate collections (UUID preserved) and rewrite their Local images.

        Connections and providers are joined BY NAME. A missing connection falls
        back to the default one (warning); a missing provider restores the
        collection as vectors-only (warning). A collection whose image was not
        carried (excluded / too large / backend) comes back empty with a warning
        naming the reason, so the operator knows to re-embed.
        """
        from core.vectors.local_store import LocalStore, LocalStoreError
        from core.vectors.service import VectorService

        warnings: List[str] = []
        restored = 0
        connections = {c.name: c for c in VectorConnection.objects.all()}
        providers = {p.name: p for p in EmbeddingProvider.objects.all()}
        fallback = VectorService.default_connection()

        for data in rows:
            name = data.get("name") or ""
            connection = connections.get(data.get("connection_name"))
            if connection is None:
                if fallback is None:
                    warnings.append(
                        f"Vector collection '{name}' skipped: connection "
                        f"'{data.get('connection_name')}' not found and no default is enabled."
                    )
                    continue
                warnings.append(
                    f"Vector collection '{name}': connection '{data.get('connection_name')}' "
                    f"not found, using '{fallback.name}'."
                )
                connection = fallback

            provider = None
            provider_name = data.get("embedding_provider_name")
            if provider_name:
                provider = providers.get(provider_name)
                if provider is None:
                    warnings.append(
                        f"Vector collection '{name}': embedding provider '{provider_name}' "
                        "not found; restored as vectors-only."
                    )

            workspace = cls._resolve_workspace(data.get("workspace_id"), ws_map, default_ws)
            collection = VectorCollection(
                workspace=workspace,
                name=name,
                description=data.get("description") or "",
                owner_plugin=data.get("owner_plugin"),
                owner_key=data.get("owner_key"),
                connection=connection,
                embedding_provider=provider,
                embedding_model=data.get("embedding_model") or (provider.model if provider else ""),
                dimension=int(data["dimension"]),
                metric=data.get("metric") or VectorCollection.Metric.COSINE,
                backend_ref="",
                points_count=int(data.get("points_count") or 0),
                tokens_embedded=int(data.get("tokens_embedded") or 0),
                embed_calls=int(data.get("embed_calls") or 0),
                include_in_backup=bool(data.get("include_in_backup", True)),
                created_by=current_user,
            )
            if data.get("id"):
                collection.id = data["id"]
            if connection.is_local:
                collection.backend_ref = VectorCollection.local_backend_ref(collection.id)
            else:
                collection.backend_ref = data.get("backend_ref") or ""
            collection.save()
            restored += 1

            if not connection.is_local:
                if collection.points_count:
                    warnings.append(
                        f"Vector collection '{name}': its points live on the external store "
                        "and were not carried by the backup."
                    )
                continue

            path = collection.file_path
            image_b64 = data.get("file_b64")
            try:
                if image_b64:
                    store = LocalStore.import_bytes(path, base64.b64decode(image_b64))
                    if store.dim != collection.dimension or store.metric != collection.metric:
                        raise LocalStoreError(
                            f"image is dim={store.dim}/{store.metric}, row is "
                            f"dim={collection.dimension}/{collection.metric}"
                        )
                    collection.points_count = store.count()
                else:
                    if path.exists():
                        LocalStore(path, collection.dimension, collection.metric).delete_files()
                    LocalStore.create(path, collection.dimension, collection.metric)
                    if collection.points_count:
                        warnings.append(
                            f"Vector collection '{name}': its points were not in the backup "
                            f"({data.get('file_skipped_reason') or 'no file'}); restored empty — "
                            "re-embed its content."
                        )
                    collection.points_count = 0
                collection.save(update_fields=["points_count"])
            except Exception as e:
                collection.status = VectorCollection.STATUS_ERROR
                collection.last_error = f"restore: {e}"
                collection.save(update_fields=["status", "last_error"])
                warnings.append(f"Vector collection '{name}': store could not be restored: {e}")
        return restored, warnings

    @classmethod
    def _import_secrets(cls, secrets_data: List[dict], user_map: dict, current_user, ws_map: dict, default_ws) -> List[str]:
        """Import secrets (encrypted values unchanged).

        External Secret Providers: an external secret relinks to its provider BY
        NAME (providers are imported first, so a live lookup resolves them — same
        by-key pattern as ``_import_secret_grants``). A pre-1.6.0 backup has no
        ``source`` → defaults to ``local``, today's path byte-for-byte. An external
        secret whose provider name isn't present is imported UNLINKED with a warning
        (fail-closed: it surfaces for the user to re-point, rather than silently
        becoming an empty local secret). Returns any such warnings.
        """
        warnings: List[str] = []
        for secret_data in secrets_data:
            created_by = user_map.get(secret_data.get("created_by_email"), current_user)

            source = secret_data.get("source", Secret.Source.LOCAL)
            provider = None
            if source == Secret.Source.EXTERNAL:
                provider_name = secret_data.get("provider_name")
                provider = SecretProvider.objects.filter(name=provider_name).first()
                if provider is None:
                    warnings.append(
                        f"Secret '{secret_data.get('key')}': external provider "
                        f"'{provider_name}' was not found — imported unlinked; "
                        "re-point it to a provider in the console."
                    )

            Secret.objects.create(
                id=secret_data["id"],  # Preserve UUID
                key=secret_data["key"],
                workspace=cls._resolve_workspace(secret_data.get("workspace_id"), ws_map, default_ws),
                owner_plugin=secret_data.get("owner_plugin"),
                owner_key=secret_data.get("owner_key"),
                source=source,
                provider=provider,
                external_ref=secret_data.get("external_ref", ""),
                encrypted_value=secret_data.get("encrypted_value", ""),
                description=secret_data.get("description", ""),
                created_at=cls._deserialize_datetime(secret_data.get("created_at")),
                updated_at=cls._deserialize_datetime(secret_data.get("updated_at")),
                created_by=created_by,
            )
        return warnings

    @classmethod
    def _import_scripts(cls, scripts_data: List[dict], env_map: dict, user_map: dict, current_user, ws_map: dict, default_ws, warnings: Optional[List[str]] = None) -> dict:
        """Import scripts with proper foreign key mapping.

        ``warnings`` collects what the restore couldn't keep (an MCP tool, a
        webhook signature).
        """
        from core.api.script_tools import restored_tool
        from core.services.webhook_signatures import restored_settings as restored_webhook_settings

        script_map = {}
        tool_names = set()  # (workspace id, tool name) exposed so far

        for script_data in scripts_data:
            old_id = script_data["id"]
            env = env_map.get(script_data["environment_id"])
            created_by = user_map.get(script_data.get("created_by_email"), current_user)

            if not env:
                continue  # Skip if environment not found

            # Archive state: only resolve archived_by when the script is actually
            # archived (pre-1.4.0 backups omit both keys → not archived).
            archived_at = cls._deserialize_datetime(script_data.get("archived_at"))
            archived_by_email = script_data.get("archived_by_email")
            archived_by = (
                user_map.get(archived_by_email, current_user)
                if archived_at and archived_by_email
                else None
            )

            workspace = cls._resolve_workspace(script_data.get("workspace_id"), ws_map, default_ws)
            expose_as_tool, tool_name = restored_tool(
                script_data, getattr(workspace, "pk", None), tool_names
            )
            if script_data.get("expose_as_tool") is True and not expose_as_tool and warnings is not None:
                warnings.append(
                    f'Script "{script_data["name"]}" was restored without its MCP tool: its '
                    "tool name is invalid or another restored script in its workspace uses it. "
                    "Set a tool name in its editor."
                )
            webhook_settings, signature_problems = restored_webhook_settings(script_data)
            if warnings is not None:
                warnings.extend(
                    f'Script "{script_data["name"]}" {problem}' for problem in signature_problems
                )
            script = Script.objects.create(
                id=old_id,  # Preserve UUID
                name=script_data["name"],
                description=script_data.get("description", ""),
                code=script_data["code"],
                inputs_schema=script_data.get("inputs_schema"),
                env_vars=usable_env_vars(script_data.get("env_vars")),
                concurrency_policy=script_data.get(
                    "concurrency_policy", Script.ConcurrencyPolicy.ALLOW
                ),
                environment=env,
                workspace=workspace,
                owner_plugin=script_data.get("owner_plugin"),
                owner_key=script_data.get("owner_key"),
                injection_mode=script_data.get("injection_mode", "all"),
                isolation_mode=script_data.get("isolation_mode", Script.IsolationMode.INHERIT),
                timeout_seconds=script_data.get("timeout_seconds", 300),
                stop_grace_seconds=script_data.get("stop_grace_seconds", 10),
                retry_max_attempts=script_data.get("retry_max_attempts", 0),
                retry_delay_seconds=script_data.get("retry_delay_seconds", 60),
                retry_backoff_multiplier=script_data.get("retry_backoff_multiplier", 1.0),
                retry_on_timeout=script_data.get("retry_on_timeout", False),
                is_enabled=script_data.get("is_enabled", False),
                expose_as_tool=expose_as_tool,
                tool_name=tool_name,
                webhook_token=script_data.get("webhook_token"),
                **webhook_settings,
                notify_on=script_data.get("notify_on", "never"),
                notify_email=script_data.get("notify_email", ""),
                notify_webhook_url=script_data.get("notify_webhook_url", ""),
                notify_webhook_enabled=script_data.get("notify_webhook_enabled", False),
                retention_days_override=script_data.get("retention_days_override"),
                retention_count_override=script_data.get("retention_count_override"),
                archived_at=archived_at,
                archived_by=archived_by,
                created_at=cls._deserialize_datetime(script_data.get("created_at")),
                updated_at=cls._deserialize_datetime(script_data.get("updated_at")),
                created_by=created_by,
            )

            # Re-attach tags by their globally-unique name (created if absent).
            # Tags aren't wiped by restore, so an existing tag is reused as-is.
            for tag_data in script_data.get("tags", []):
                tag, _ = Tag.objects.get_or_create(
                    name=tag_data["name"],
                    defaults={"color": tag_data.get("color", Tag.Color.GRAY)},
                )
                script.tags.add(tag)

            script_map[old_id] = script

        return script_map

    @classmethod
    def _import_libraries(cls, libraries_data: List[dict], user_map: dict, current_user, ws_map: dict, default_ws) -> int:
        """Recreate libraries and their head revision. Returns the count restored.

        The head is recreated under its ORIGINAL version number (not renumbered to
        1), so a restored run's ``library_versions`` stamp still resolves and
        ``current_version`` never moves backwards. History before the head is not
        in the backup by design — see the 1.7.0 note above.
        """
        from core.models import Library, LibraryRevision

        restored = 0
        for data in libraries_data:
            created_by = user_map.get(data.get("created_by_email"), current_user)
            library, _ = Library.objects.update_or_create(
                id=data["id"],  # Preserve UUID (script attachments resolve by id)
                defaults={
                    "key": data["key"],
                    "name": data.get("name", ""),
                    "description": data.get("description", ""),
                    "workspace": cls._resolve_workspace(data.get("workspace_id"), ws_map, default_ws),
                    "owner_plugin": data.get("owner_plugin"),
                    "owner_key": data.get("owner_key"),
                    "current_version": data.get("current_version", 0),
                    "created_by": created_by,
                },
            )
            modules = data.get("modules") or {}
            version = data.get("current_version", 0)
            if version and modules:
                LibraryRevision.objects.update_or_create(
                    library=library,
                    version=version,
                    defaults={
                        "modules": modules,
                        "content_hash": hash_modules(modules),
                        "created_by": created_by,
                    },
                )
            restored += 1
        return restored

    @classmethod
    def _import_library_attachments(cls, scripts_data: List[dict], script_map: dict) -> None:
        """Reattach libraries to scripts from the embedded ids in the script export.

        Runs after both libraries and scripts exist (UUIDs preserved, so ids
        resolve). An attachment pointing at a missing library is skipped
        (best-effort). Pre-1.7.0 backups carry no ``libraries`` key.
        """
        from core.models import Library, ScriptLibrary

        for script_data in scripts_data:
            script = script_map.get(script_data["id"])
            if not script:
                continue
            for library_id in script_data.get("libraries", []):
                library = Library.objects.filter(id=library_id).first()
                if library is None:
                    continue
                ScriptLibrary.objects.get_or_create(script=script, library=library)

    @classmethod
    def _import_secret_grants(cls, scripts_data: List[dict], script_map: dict) -> None:
        """Recreate per-script secret grants embedded in the script export.

        Runs after both secrets and scripts are imported (UUIDs preserved, so
        secrets resolve by id). A grant pointing at a missing secret/script is
        skipped (best-effort). Pre-1.3.0 backups carry no ``secret_grants`` key.
        """
        from core.models import SecretGrant

        for script_data in scripts_data:
            script = script_map.get(script_data["id"])
            if not script:
                continue
            for grant in script_data.get("secret_grants", []):
                secret = Secret.objects.filter(id=grant.get("secret_id")).first()
                if secret is None:
                    continue
                SecretGrant.objects.get_or_create(
                    script=script,
                    secret=secret,
                    defaults={"active": grant.get("active", True)},
                )

    @classmethod
    def _import_connections(cls, rows: List[dict], user_map: dict, current_user, ws_map: dict, default_ws):
        """Import connections (2.5 T23); returns (count, warnings).

        Credentials are restored encrypted as they were (like secrets: the
        backup's ENCRYPTION_KEY must match). A provider or auth kind this
        version doesn't know is kept, marked error, with a warning; a name
        already taken in its workspace (two source workspaces mapped onto one)
        is skipped with a warning.
        """
        from core.integrations.presets import get_preset

        warnings: List[str] = []
        count = 0
        taken = set()
        held = set()  # (workspace, plugin, key): one connection per plugin requirement (T25)
        for row in rows:
            workspace = cls._resolve_workspace(row.get("workspace_id"), ws_map, default_ws)
            name = str(row.get("name") or "")[:100]
            if not name or (workspace.pk, name) in taken:
                warnings.append(f"Connection '{name}': skipped (no name, or the name is taken in its workspace).")
                continue
            taken.add((workspace.pk, name))
            owner_plugin = str(row.get("owner_plugin") or "")[:100] or None
            owner_key = str(row.get("owner_key") or "")[:100] or None
            if owner_plugin and (workspace.pk, owner_plugin, owner_key) in held:
                warnings.append(
                    f"Connection '{name}': restored without its plugin ({owner_plugin}), "
                    "which already has a connection for that account."
                )
                owner_plugin = owner_key = None
            elif owner_plugin:
                held.add((workspace.pk, owner_plugin, owner_key))
            status = row.get("status") if row.get("status") in Connection.Status.values else Connection.Status.OK
            last_error = str(row.get("last_error") or "")
            preset = get_preset(row.get("provider"))
            if preset is None or row.get("auth_kind") not in preset.auth_kinds:
                status = Connection.Status.ERROR
                last_error = "This version of PyRun doesn't know this provider or sign-in."
                warnings.append(
                    f"Connection '{name}': provider '{row.get('provider')}' ({row.get('auth_kind')}) "
                    "is not known to this version; restored but unusable."
                )
            config = row.get("config")
            scopes = row.get("scopes")
            oauth_client_id = row.get("oauth_client_id")
            if oauth_client_id and not OAuthClient.objects.filter(pk=oauth_client_id).exists():
                oauth_client_id = None
            Connection.objects.create(
                id=row["id"],
                workspace=workspace,
                name=name,
                provider=str(row.get("provider") or "")[:50],
                auth_kind=str(row.get("auth_kind") or "")[:20],
                config=config if isinstance(config, dict) else {},
                credentials_encrypted=row.get("credentials_encrypted") or "",
                scopes=scopes if isinstance(scopes, list) else [],
                account_label=str(row.get("account_label") or "")[:255],
                status=status,
                last_tested_at=cls._deserialize_datetime(row.get("last_tested_at")),
                last_error=last_error,
                grant_all_scripts=row.get("grant_all_scripts") is True,
                owner_plugin=owner_plugin,
                owner_key=owner_key,
                oauth_client_id=oauth_client_id or None,
                token_expires_at=cls._deserialize_datetime(row.get("token_expires_at")),
                created_by=user_map.get(row.get("created_by_email"), current_user),
            )
            count += 1
        return count, warnings

    @classmethod
    def _import_oauth_clients(cls, rows: List[dict], user_map: dict, current_user, ws_map: dict, default_ws):
        """Import OAuth apps (2.5 T24); returns (count, warnings).

        The client secret is restored encrypted as it was. An instance default
        (no workspace) stays one; a second app for the same provider and
        workspace (two source workspaces mapped onto one) is skipped.
        """
        warnings: List[str] = []
        count = 0
        taken = set()
        for row in rows:
            workspace = (
                cls._resolve_workspace(row.get("workspace_id"), ws_map, default_ws)
                if row.get("workspace_id") else None
            )
            provider = str(row.get("provider") or "")[:50]
            key = (workspace.pk if workspace else None, provider)
            if not provider or not row.get("client_id") or key in taken:
                warnings.append(f"OAuth app for '{provider}': skipped (incomplete, or a duplicate).")
                continue
            taken.add(key)
            config = row.get("config")
            OAuthClient.objects.create(
                id=row["id"],
                workspace=workspace,
                provider=provider,
                client_id=str(row["client_id"])[:500],
                client_secret_encrypted=row.get("client_secret_encrypted") or "",
                config=config if isinstance(config, dict) else {},
                created_by=user_map.get(row.get("created_by_email"), current_user),
            )
            count += 1
        return count, warnings

    @classmethod
    def _import_connection_grants(cls, scripts_data: List[dict], script_map: dict) -> None:
        """Recreate the scripts' explicit connection grants (ids are preserved).

        A grant to a missing connection, or to one in another workspace than
        its script's, is skipped. Older backups carry none.
        """
        from core.models import ConnectionGrant

        for script_data in scripts_data:
            script = script_map.get(script_data["id"])
            if not script:
                continue
            for grant in script_data.get("connection_grants", []):
                connection = Connection.objects.filter(
                    id=grant.get("connection_id"), workspace_id=script.workspace_id
                ).first()
                if connection is None:
                    continue
                ConnectionGrant.objects.get_or_create(
                    script=script,
                    connection=connection,
                    defaults={"active": grant.get("active", True) is not False},
                )

    @classmethod
    def _import_schedules(cls, schedules_data: List[dict], script_map: dict, user_map: dict, current_user, ws_map: dict, default_ws) -> None:
        """Import schedules (q_schedule_ids will be regenerated later)."""
        for schedule_data in schedules_data:
            script = script_map.get(schedule_data["script_id"])
            created_by = user_map.get(schedule_data.get("created_by_email"), current_user)

            if not script:
                continue  # Skip if script not found

            ScriptSchedule.objects.create(
                id=schedule_data["id"],  # Preserve UUID
                script=script,
                workspace=cls._resolve_workspace(schedule_data.get("workspace_id"), ws_map, default_ws),
                run_mode=schedule_data.get("run_mode", "manual"),
                interval_minutes=schedule_data.get("interval_minutes"),
                daily_times=schedule_data.get("daily_times", []),
                weekly_days=schedule_data.get("weekly_days", []),
                weekly_times=schedule_data.get("weekly_times", []),
                monthly_days=schedule_data.get("monthly_days", []),
                monthly_times=schedule_data.get("monthly_times", []),
                cron_expression=schedule_data.get("cron_expression", ""),
                yearly_month=schedule_data.get("yearly_month"),
                yearly_day=schedule_data.get("yearly_day"),
                yearly_time=schedule_data.get("yearly_time", ""),
                timezone=schedule_data.get("timezone", "UTC"),
                is_active=schedule_data.get("is_active", True),
                q_schedule_ids=[],  # Will be regenerated
                next_run=None,  # Will be calculated
                created_at=cls._deserialize_datetime(schedule_data.get("created_at")),
                updated_at=cls._deserialize_datetime(schedule_data.get("updated_at")),
                created_by=created_by,
            )

    @classmethod
    def _import_schedule_history(cls, history_data: List[dict], user_map: dict, current_user) -> None:
        """Import schedule history."""
        for item_data in history_data:
            # Get schedule by ID
            try:
                schedule = ScriptSchedule.objects.get(id=item_data["schedule_id"])
            except ScriptSchedule.DoesNotExist:
                continue  # Skip if schedule not found

            changed_by = user_map.get(item_data.get("changed_by_email"), current_user)

            ScheduleHistory.objects.create(
                id=item_data["id"],  # Preserve UUID
                schedule=schedule,
                change_type=item_data["change_type"],
                previous_config=item_data.get("previous_config"),
                new_config=item_data.get("new_config"),
                changed_by=changed_by,
                created_at=cls._deserialize_datetime(item_data.get("created_at")),
            )

    @classmethod
    def _import_runs(cls, runs_data: List[dict], script_map: dict, user_map: dict, current_user, ws_map: dict, default_ws) -> None:
        """Import runs."""
        retry_links = []
        for run_data in runs_data:
            script = script_map.get(run_data["script_id"])
            triggered_by = user_map.get(run_data.get("triggered_by_email"), current_user)

            if not script:
                continue  # Skip if script not found

            status = run_data["status"]
            finished = status not in (Run.Status.PENDING, Run.Status.RUNNING)
            Run.objects.create(
                id=run_data["id"],  # Preserve UUID
                script=script,
                workspace=cls._resolve_workspace(run_data.get("workspace_id"), ws_map, default_ws),
                status=status,
                exit_code=run_data.get("exit_code"),
                stdout=run_data.get("stdout", ""),
                stderr=run_data.get("stderr", ""),
                started_at=cls._deserialize_datetime(run_data.get("started_at")),
                ended_at=cls._deserialize_datetime(run_data.get("ended_at")),
                code_snapshot=run_data.get("code_snapshot", ""),
                library_versions=run_data.get("library_versions"),
                inputs=run_data.get("inputs"),
                result=run_data.get("result"),
                progress=run_data.get("progress"),
                event=run_data.get("event"),
                cancel_reason=run_data.get("cancel_reason") or "",
                # Unfinished runs come back without it, so they never count as
                # held (see the 1.14.0 note on BACKUP_VERSION).
                queued_behind=run_data.get("queued_behind") if finished else None,
                attempt=run_data.get("attempt") or 1,
                # Same rule as queued_behind: an unfinished retry comes back
                # without its due time, so the heartbeat never starts it.
                next_attempt_at=(
                    cls._deserialize_datetime(run_data.get("next_attempt_at"))
                    if finished
                    else None
                ),
                stop_requested_at=(
                    cls._deserialize_datetime(run_data.get("stop_requested_at"))
                    if finished
                    else None
                ),
                response_code=_restored_response_code(run_data.get("response_code")),
                trigger_type=run_data.get("trigger_type", "manual"),
                triggered_by=triggered_by,
                created_at=cls._deserialize_datetime(run_data.get("created_at")),
            )
            if run_data.get("retry_of"):
                retry_links.append((run_data["id"], run_data["retry_of"]))

        # A retry's first attempt may come later in the file, or not at all
        # (only the newest runs are exported): link once all are in.
        if retry_links:
            restored = {
                str(pk)
                for pk in Run.objects.filter(
                    pk__in={first for _, first in retry_links}
                ).values_list("pk", flat=True)
            }
            for run_id, first in retry_links:
                if first in restored:
                    Run.objects.filter(pk=run_id).update(retry_of_id=first)

    @classmethod
    def _import_package_operations(cls, ops_data: List[dict], env_map: dict, user_map: dict, current_user) -> None:
        """Import package operations."""
        for op_data in ops_data:
            env = env_map.get(op_data["environment_id"])
            created_by = user_map.get(op_data.get("created_by_email"), current_user)

            if not env:
                continue  # Skip if environment not found

            PackageOperation.objects.create(
                id=op_data["id"],  # Preserve UUID
                environment=env,
                operation=op_data["operation"],
                package_spec=op_data.get("package_spec", ""),
                status=op_data["status"],
                output=op_data.get("output", ""),
                error=op_data.get("error", ""),
                created_at=cls._deserialize_datetime(op_data.get("created_at")),
                started_at=cls._deserialize_datetime(op_data.get("started_at")),
                completed_at=cls._deserialize_datetime(op_data.get("completed_at")),
                created_by=created_by,
            )

    @classmethod
    def _import_datastores(cls, datastores_data: List[dict], user_map: dict, current_user, ws_map: dict, default_ws) -> dict:
        """Import DataStores and their entries."""
        ds_map = {}

        for ds_data in datastores_data:
            old_id = ds_data["id"]
            created_by = user_map.get(ds_data.get("created_by_email"), current_user)

            datastore = DataStore.objects.create(
                id=old_id,  # Preserve UUID
                name=ds_data["name"],
                workspace=cls._resolve_workspace(ds_data.get("workspace_id"), ws_map, default_ws),
                owner_plugin=ds_data.get("owner_plugin"),
                owner_key=ds_data.get("owner_key"),
                description=ds_data.get("description", ""),
                created_at=cls._deserialize_datetime(ds_data.get("created_at")),
                updated_at=cls._deserialize_datetime(ds_data.get("updated_at")),
                created_by=created_by,
            )
            ds_map[old_id] = datastore

            # Import entries for this datastore
            for entry_data in ds_data.get("entries", []):
                DataStoreEntry.objects.create(
                    id=entry_data["id"],  # Preserve UUID
                    datastore=datastore,
                    key=entry_data["key"],
                    value_json=entry_data["value_json"],
                    created_at=cls._deserialize_datetime(entry_data.get("created_at")),
                    updated_at=cls._deserialize_datetime(entry_data.get("updated_at")),
                )

        return ds_map

    @classmethod
    def _cleanup_existing_databases(cls) -> List[str]:
        """Deprovision the current instance's databases before full-replace.

        Best-effort: an unreachable data server (or none attached) yields a
        warning about orphaned schemas instead of aborting the restore — core
        data must remain restorable even when the data server is down.
        """
        from core.services.database_service import DatabaseService

        warnings = []
        existing = list(Database.objects.all())
        if not existing:
            return warnings
        if not DatabaseService.is_configured():
            warnings.append(
                f"{len(existing)} existing database(s) removed from PyRun "
                "without server cleanup (no data server attached) — their "
                "schemas/roles may remain on the old server."
            )
            return warnings
        for db in existing:
            try:
                DatabaseService.deprovision(db)
            except Exception as e:
                warnings.append(
                    f"Database '{db.name}': server cleanup failed ({e}) — its "
                    f"schema '{db.schema_name}' may remain on the data server."
                )
        return warnings

    @classmethod
    def _import_databases(
        cls, databases_data: List[dict], script_map: dict, user_map: dict,
        current_user, ws_map: dict, default_ws,
    ) -> Tuple[int, List[str]]:
        """Recreate managed databases: row → provision → replay dump → grants.

        Schema/role names are restored VERBATIM (the dump's statements are
        qualified with the schema name), so this expects a fresh target — a
        clashing schema is reconciled by the idempotent provision, and a dump
        replay onto non-empty state fails loudly into a warning. With no data
        server attached, all databases are skipped with one warning.
        """
        from core.services.database_service import (
            DatabaseProvisionError,
            DatabaseService,
        )

        warnings: List[str] = []
        if not databases_data:
            return 0, warnings
        if not DatabaseService.is_configured():
            warnings.append(
                f"Skipped {len(databases_data)} database(s): no data server "
                "attached (PYRUNNER_DATA_DB_URL). Attach one and restore the "
                "backup again to recover them."
            )
            return 0, warnings

        restored = 0
        for data in databases_data:
            name = data.get("name", "?")
            try:
                db = Database.objects.create(
                    id=data["id"],  # Preserve UUID
                    name=name,
                    workspace=cls._resolve_workspace(data.get("workspace_id"), ws_map, default_ws),
                    owner_plugin=data.get("owner_plugin"),
                    owner_key=data.get("owner_key"),
                    schema_name=data["schema_name"],
                    role_name=data["role_name"],
                    encrypted_password=data.get("encrypted_password", ""),
                    description=data.get("description", ""),
                    status=Database.STATUS_PROVISIONING,
                    created_by=user_map.get(data.get("created_by_email"), current_user),
                )
            except Exception as e:
                warnings.append(f"Database '{name}': row import failed: {e}")
                continue

            restored += 1
            # Grants first — they are pure rows, restorable even if the server
            # half fails below (the script then just waits on a Retry).
            for grant in data.get("grants", []):
                script = script_map.get(grant.get("script_id"))
                if script is not None:
                    DatabaseGrant.objects.get_or_create(
                        script=script, database=db,
                        defaults={"active": grant.get("active", True)},
                    )

            try:
                DatabaseService.provision(db)
            except DatabaseProvisionError as e:
                warnings.append(
                    f"Database '{name}': provisioning failed ({e}) — left in "
                    "error status; fix the data server and use Retry on its page."
                )
                continue
            except EncryptionError:
                # A row the source never provisioned (no schema, role or
                # password) or whose password doesn't decrypt: keep the row and
                # restore everything else instead of failing the whole restore.
                db.status = Database.STATUS_ERROR
                db.last_error = "The backup holds no usable credentials for this database."
                db.save(update_fields=["status", "last_error"])
                warnings.append(
                    f"Database '{name}': the backup holds no usable credentials (it was "
                    "never provisioned on the source) — restored in error status with no "
                    "data; delete it and create it again."
                )
                continue

            dump_sql = data.get("dump_sql")
            if dump_sql:
                try:
                    DatabaseService.restore_schema_dump(db, dump_sql)
                except DatabaseProvisionError as e:
                    warnings.append(
                        f"Database '{name}': data replay failed ({e}) — the "
                        "database is provisioned but its tables may be missing "
                        "or partial."
                    )
            elif data.get("dump_skipped_reason"):
                warnings.append(
                    f"Database '{name}': the backup carried no data "
                    f"({data['dump_skipped_reason']}) — restored empty."
                )
        return restored, warnings

    @classmethod
    def _regenerate_all_schedules(cls) -> int:
        """
        Regenerate all django-q2 schedules after restore.

        Returns:
            int: Number of schedules created
        """
        count = 0
        for script_schedule in ScriptSchedule.objects.filter(is_active=True):
            if script_schedule.is_scheduled:
                ScheduleService.sync_schedule(script_schedule)
                count += 1
        return count
