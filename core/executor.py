"""
Script executor module for PyRun.

This module handles the execution of Python scripts in isolated environments.
It is designed to be called from django-q2 async tasks.
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
import traceback
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from core.executor_backends import RunSpec, get_run_backend

# Re-exported so ``from core.executor import _kill_process_tree`` keeps working
# (TaskService.force_stop_task imports it). The implementation now lives with
# the local backend; force-stop stays pid-based for the local backend.
from core.executor_backends.local import kill_process_tree as _kill_process_tree
from core.models import GlobalSettings, Run, Secret, Workspace
from core.services import ClaudeService, EncryptionService
from core.services.library_service import (
    LibraryMaterializationError,
    materialize_libraries,
)
from core.services.run_events import default_event
from core.services.script_inputs import input_env
from core.services.secret_backends import SecretResolutionError

logger = logging.getLogger(__name__)

# Every credential-bearing variable ClaudeService._build_env can inject into a
# run (ANTHROPIC_AUTH_TOKEN carries third-party provider keys). Masked in
# output alongside the run's secrets.
AI_CREDENTIAL_ENV_KEYS = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
)

# Each stream (stdout, stderr) is stored up to MAX_OUTPUT_BYTES (1 MB) to keep
# the database small. Past that the start and the END are kept — the end is where
# a result line or a traceback usually is — with a marker in between saying how
# much was cut: the first OUTPUT_HEAD_BYTES (200 KB) and the last ~800 KB.
MAX_OUTPUT_BYTES = 1_000_000
OUTPUT_HEAD_BYTES = 200_000
# Room reserved for the marker line inside the cap.
_TRUNCATION_MARKER_RESERVE = 256
# Cut at a line break when one is this close to the cut point.
_TRUNCATION_LINE_SLACK = 4096


def run_workspace_id(run):
    """The workspace a run executes in: its own, else its script's.

    ``Run.save()`` stamps the script's workspace on new runs; this also covers a
    row saved without one before that (F82), so such a run is never treated as
    un-scoped while its script belongs to a workspace. None only when neither
    the run nor its script has a workspace (pre-tenancy data).
    """
    if run is None:
        return None
    ws_id = getattr(run, "workspace_id", None)
    if ws_id is None and getattr(run, "script_id", None):
        ws_id = getattr(run.script, "workspace_id", None)
    return ws_id


def resolve_secrets_for_run(run) -> dict:
    """Resolve the secrets to inject into (and mask in) a run, scoped to its workspace.

    The SINGLE shared resolver feeding BOTH env injection and output masking, so
    the two can never drift (no leak, no over-mask). Tenancy Stage 1.

    Workspace scoping (back-compat rule, chosen to keep a single-workspace /
    pre-tenancy instance byte-for-byte):
    - ``run`` is None, or neither the run nor its script has a workspace ⇒ no
      workspace narrowing (an un-scoped run cannot narrow). A run saved without
      a workspace whose script has one is scoped to the script's (F82).
    - otherwise ⇒ the run's-workspace secrets PLUS still-unassigned
      (``workspace IS NULL``) secrets — legacy secrets created before scoping
      keep injecting on a single-workspace instance.

    Owner / injection-mode scoping (Plugin Platform v2, WS3) layered INSIDE the
    same workspace scope so the two never fork:
    - ``injection_mode='all'`` (the default, and for any run without a script)
      ⇒ every USER (``owner_plugin IS NULL``) secret in scope. This is the literal
      pre-v2 path: on an existing instance every row is owner-NULL, so the set is
      byte-identical; plugin-owned secrets never leak into a non-owned script.
    - ``injection_mode='selected'`` ⇒ the actively-granted (attached) secrets,
      each injected under its CLEAN name:
      * a USER script (``owner_plugin`` NULL) gets ONLY its granted secrets —
        what the script form promises ("Selected = only the secrets you attach").
        Before 2.0 it also got every unowned secret, i.e. the same set as 'all'.
      * a PLUGIN script (the SDK's default) also gets its own plugin's
        (same-owner) secrets. Unowned user secrets reach it only when attached:
        before 2.0 it also got every unowned secret (F51, decision D9).
        Precedence on a clean-name clash: same-owner < explicit grant (the most
        specific wins).

    Returns:
        Dict of {clean_name: decrypted_value}.
    """
    secrets_env = {}

    # Only try to get secrets if encryption is configured
    if not EncryptionService.is_configured():
        logger.debug("Encryption not configured - secrets will not be injected")
        return secrets_env

    try:
        ws_id = run_workspace_id(run)

        def _ws_scope(qs):
            if ws_id is not None:
                return qs.filter(Q(workspace_id=ws_id) | Q(workspace__isnull=True))
            # An unscoped run (no workspace on the run or its script) only sees
            # unscoped secrets — never every workspace's (fail closed).
            return qs.filter(workspace__isnull=True)

        mode = "all"
        owner = None
        if run is not None and getattr(run, "script_id", None):
            mode = getattr(run.script, "injection_mode", "all") or "all"
            owner = getattr(run.script, "owner_plugin", None)

        if mode == "selected":
            # Build by precedence so an explicitly granted secret wins a
            # clean-name clash with the plugin's own one.
            chosen = {}  # clean_name -> Secret
            if owner:
                for s in _ws_scope(Secret.objects.filter(owner_plugin=owner)):
                    chosen[s.get_clean_name()] = s
            granted = Secret.objects.filter(
                grants__script_id=run.script_id, grants__active=True
            )
            for s in _ws_scope(granted):
                chosen[s.get_clean_name()] = s
            resolved = list(chosen.values())
        else:
            resolved = _ws_scope(Secret.objects.filter(owner_plugin__isnull=True))

        for secret in resolved:
            if getattr(secret, "source", Secret.Source.LOCAL) == Secret.Source.EXTERNAL:
                # External Secret Providers, fail-closed: an external value that
                # can't be resolved raises SecretResolutionError, which propagates
                # (past both excepts below) so execute_run fails the run pre-exec
                # with a named error — NOT the log-and-skip local decrypts get.
                secrets_env[secret.get_clean_name()] = secret.get_decrypted_value()
            else:
                try:
                    secrets_env[secret.get_clean_name()] = secret.get_decrypted_value()
                except Exception as e:
                    logger.error(f"Failed to decrypt secret {secret.key}: {e}")
    except SecretResolutionError:
        # Must escape the broad except below so the run fails with the named cause.
        raise
    except Exception as e:
        logger.error(f"Failed to load secrets: {e}")

    return secrets_env


def script_workdir(script_id) -> Path:
    """A script's own working folder, ``SCRIPTS_WORKDIR/<script id>/``.

    Every run of the script starts there (its cwd), so files it writes persist
    for its next run but never mix with other scripts' files. The folder is
    removed when the script is deleted (``remove_script_workdir``); it is not
    part of backups.
    """
    return Path(settings.SCRIPTS_WORKDIR) / str(script_id)


def remove_script_workdir(script_id) -> bool:
    """Delete a deleted script's working folder. Returns True if one was removed.

    Refuses anything that isn't a real directory directly under SCRIPTS_WORKDIR
    named exactly after the id (a symlink planted in its place is left alone).
    """
    root = Path(settings.SCRIPTS_WORKDIR)
    folder = script_workdir(script_id)
    if folder.parent != root or folder.name in ("", ".", ".."):
        return False
    if folder.is_symlink() or not folder.is_dir():
        return False
    try:
        shutil.rmtree(folder)
    except OSError as e:
        logger.warning("Could not remove the working folder of deleted script %s: %s", script_id, e)
        return False
    return True


def run_file_payload(run) -> dict:
    """What ``run.json`` holds for a run: the run, its script, trigger, inputs, event.

    Ids use ``.hex``, the same form as ``PYRUNNER_RUN_ID``. ``inputs`` is every
    stamped input (JSON ones too), ``{}`` when the script declares none.
    ``event`` is what started the run (``core.services.run_events``); a run
    queued before 2.5 has none stored and gets the minimal one for its trigger.
    """
    return {
        "run": {
            "id": run.id.hex,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "attempt": run.attempt,
        },
        "script": {"id": run.script.id.hex, "name": run.script.name},
        "trigger": {"type": str(run.trigger_type)},
        "inputs": run.inputs or {},
        "event": run.event if run.event is not None else default_event(run),
    }


def _write_run_file(run, workdir: Path) -> str:
    """Write the run's ``run.json`` into its working folder; return the path.

    It sits next to the temp script file for the same reason: the sandbox
    backend binds only the run's working folder, at the same path. The name is
    unique per run because runs of one script share that folder. It holds raw
    input values (the script needs them), so it is created private (0600) and
    deleted when the run ends.
    """
    with tempfile.NamedTemporaryFile(
        mode="w",
        prefix=".pyrun-run-",
        suffix=".json",
        delete=False,
        encoding="utf-8",
        dir=str(workdir),
    ) as fh:
        json.dump(run_file_payload(run), fh, ensure_ascii=False)
        return fh.name


def _build_script_environment(
    webhook_data: dict | None = None,
    run: "Run | None" = None,
    secrets: "dict | None" = None,
    library_root: str | None = None,
    run_file: str | None = None,
) -> dict:
    """
    Build the environment dict for script execution.

    Combines system environment with the run's declared inputs, secrets, webhook
    data, and DataStore access. Scalar inputs are added as INPUT_<NAME>.
    Precedence: system environment < inputs < secrets (a secret overrides a
    same-named input or system variable). The run's own ids
    (PYRUNNER_RUN_ID, PYRUNNER_SCRIPT_ID, PYRUNNER_SCRIPT_NAME) and
    PYRUNNER_RUN_FILE are set after the secrets, so nothing can override them.
    Webhook data is added with WEBHOOK_ prefix.

    Args:
        webhook_data: Optional webhook data from HTTP request
        run: Optional Run being executed (used for Claude usage attribution)
        secrets: Optional pre-resolved secrets (from ``resolve_secrets_for_run``).
            When omitted they are resolved here; ``execute_run`` passes the SAME dict
            it uses for output masking so injection and masking can't drift — and the
            values are decrypted once, not twice.
        library_root: Optional staging dir holding this run's materialized Script
            Libraries. Added to PYTHONPATH *after* the script helpers so a library
            can never shadow ``pyrunner_db``/``pyrunner_ai`` (the reserved-key ban
            on library keys is the second fence). None => no libraries, and
            PYTHONPATH is byte-for-byte what it was before this feature.
        run_file: Optional path of the run's ``run.json`` (``_write_run_file``),
            exported as PYRUNNER_RUN_FILE for ``pyrunner_run``.

    Returns:
        Environment dict to pass to subprocess
    """
    # Start with system environment
    env = os.environ.copy()

    # Hardening: drop PyRun's own infra secrets so they never leak into a
    # user's run (a user Secret of the same name still injects below).
    for _denied in settings.PYRUNNER_RUN_ENV_DENYLIST:
        env.pop(_denied, None)

    # Declared inputs stamped on the run at queue time, as INPUT_<NAME>.
    # Precedence: system environment < inputs < secrets.
    if run is not None and run.inputs:
        env.update(input_env(run.inputs))

    # Add secrets (overriding any existing vars with same name), scoped to the
    # run's workspace via the shared resolver.
    if secrets is None:
        secrets = resolve_secrets_for_run(run)
    env.update(secrets)

    # Add webhook data if present
    if webhook_data:
        env["WEBHOOK_METHOD"] = webhook_data.get("method", "")
        env["WEBHOOK_QUERY"] = json.dumps(webhook_data.get("query", {}))
        env["WEBHOOK_CONTENT_TYPE"] = webhook_data.get("content_type", "")

        if "body" in webhook_data:
            env["WEBHOOK_BODY"] = webhook_data["body"]

        if "body_json" in webhook_data:
            env["WEBHOOK_BODY_JSON"] = json.dumps(webhook_data["body_json"])

        # Inbound chat context (Channels Phase 2): lets the run reply via
        # pyrunner_notify.reply() to the conversation that triggered it.
        inbound = webhook_data.get("inbound")
        if inbound:
            env["INBOUND_CHANNEL"] = inbound.get("channel", "") or ""
            env["INBOUND_TEXT"] = inbound.get("text", "") or ""
            env["INBOUND_REPLY_REF"] = json.dumps(inbound.get("reply_ref") or {})
            env["INBOUND_SENDER"] = json.dumps(inbound.get("sender") or {})

    # Add AI support (Services -> AI Provider). Injects the configured
    # credential + config dir so the pyrunner_ai helper works in scripts.
    # Empty dict when AI is disabled/unconfigured.
    claude_env = ClaudeService.get_script_env()
    if claude_env:
        # Remove any stray host credential for the *other* auth method so it
        # can't override the configured one.
        for key in ClaudeService.conflicting_env_keys():
            env.pop(key, None)
        env.update(claude_env)

    # The run's own identity, for every run (before 2.5 only when an AI
    # provider was configured). pyrunner_ai attributes usage with it, plugins
    # tie their progress to it. Use .hex to match Django's UUID storage on
    # SQLite (no dashes). Set after the secrets so a secret can't spoof it.
    if run is not None:
        env["PYRUNNER_RUN_ID"] = run.id.hex
        if run.script_id:
            env["PYRUNNER_SCRIPT_ID"] = run.script.id.hex
            env["PYRUNNER_SCRIPT_NAME"] = run.script.name
    if run_file:
        env["PYRUNNER_RUN_FILE"] = str(run_file)

    # DataStore + Claude-usage access for the script helpers.
    #
    # On SQLite (the default) the helpers read the DB file directly via
    # PYRUNNER_DB_PATH — byte-for-byte with before, and with no dependency on
    # the web tier being up. On any other engine (Postgres) there is no local DB
    # file, so the helpers go through the internal loopback API instead,
    # authenticated by a signed per-run token. The token is only available with
    # a run context (it is keyed to run.id).
    from django.db import connection as _db_connection

    if _db_connection.vendor == "sqlite":
        env["PYRUNNER_DB_PATH"] = str(settings.DATABASES["default"]["NAME"])
    if run is not None:
        from core.services.datastore_token import mint_datastore_token

        env["PYRUNNER_INTERNAL_URL"] = settings.PYRUNNER_INTERNAL_BASE_URL
        env["PYRUNNER_INTERNAL_TOKEN"] = mint_datastore_token(run.id)

        # Trusted workspace id for datastore-by-name scoping (tenancy Stage 2).
        # The SQLite helper scopes lookups to this workspace; it is derived from
        # the run (never script-supplied) or else its script (F82). An un-scoped
        # run gets none, so it resolves only unassigned stores (F129).
        ws_id = run_workspace_id(run)
        if ws_id is not None:
            env["PYRUNNER_WORKSPACE_ID"] = ws_id.hex

        # Expose the owning plugin slug to its own worker script (Plugin Platform
        # v2), e.g. so it can address its auto-named DataStore by the short key.
        # Absent for user-created scripts (owner_plugin NULL).
        if run.script_id and getattr(run.script, "owner_plugin", None):
            env["PYRUNNER_OWNER_PLUGIN"] = run.script.owner_plugin

    # Add script_helpers to PYTHONPATH so scripts can import pyrunner_datastore,
    # then this run's materialized libraries. Order is the fence: helpers resolve
    # first, so an attached library always loses a name clash with them.
    helpers_path = str(Path(settings.BASE_DIR) / "core" / "script_helpers")
    # The shared stdlib URL guard uses the operator's policy, never a secret
    # or inherited shell value with the same name.
    env["PYRUNNER_ALLOW_PRIVATE_NETWORK"] = "true" if settings.PYRUNNER_ALLOW_PRIVATE_NETWORK else "false"
    path_entries = [helpers_path]
    if library_root:
        path_entries.append(str(library_root))
    existing_pythonpath = env.get("PYTHONPATH", "")
    if existing_pythonpath:
        path_entries.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(path_entries)

    return env


def _mask_secrets_in_output(output: str, secrets: dict) -> str:
    """
    Mask secret values in output to prevent accidental exposure.

    Args:
        output: The script output
        secrets: Dict of {key: value} secrets

    Returns:
        Output with secret values replaced with [KEY:MASKED]
    """
    if not output or not secrets:
        return output

    masked = output
    for key, value in secrets.items():
        if value and len(value) >= 4:  # Only mask non-trivial values
            masked = masked.replace(value, f"[{key}:MASKED]")

    return masked


def mask_secrets_in_value(value, secrets: dict):
    """``_mask_secrets_in_output`` for a JSON value, for structured run data.

    Masks every string in ``value``, object keys included. A number whose text
    contains a secret becomes the masked string. Masking the JSON text instead
    would miss a secret that JSON escaping changes (one with a quote in it).
    """
    if not secrets:
        return value
    if isinstance(value, str):
        return _mask_secrets_in_output(value, secrets)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        text = json.dumps(value)
        masked = _mask_secrets_in_output(text, secrets)
        return value if masked == text else masked
    if isinstance(value, dict):
        return {
            mask_secrets_in_value(k, secrets): mask_secrets_in_value(v, secrets)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [mask_secrets_in_value(item, secrets) for item in value]
    return value


def with_ai_credentials(secrets: dict) -> dict:
    """Add the AI credential(s) a run gets injected to a mask set, in place."""
    claude_env = ClaudeService.get_script_env()
    for cred_key in AI_CREDENTIAL_ENV_KEYS:
        if claude_env.get(cred_key):
            secrets[cred_key] = claude_env[cred_key]
    return secrets


def mask_set_for_display(run) -> dict:
    """Everything to mask out of run data shown in the console.

    The run's secrets (the same resolver execution uses) plus the injected AI
    credential. Raises SecretResolutionError when an external secret can't be
    resolved; callers then hide the data rather than show it unmasked.
    """
    return with_ai_credentials(resolve_secrets_for_run(run))


def mask_run_inputs(run, mask_sets: dict | None = None):
    """``run.inputs`` with secret values masked, for display; None when it has none.

    Inputs are stored raw because the script needs the real values, so the run
    page and the SDK's RunView mask them here. Raises SecretResolutionError when
    a secret can't be resolved; callers then withhold the values. ``mask_sets``
    caches each (workspace, script) set, or its failure, across one listing.
    """
    if not run.inputs:
        return None
    return mask_run_value(run, run.inputs, mask_sets)


def mask_run_value(run, value, mask_sets: dict | None = None):
    """``value``, raw data of ``run`` (its inputs, its event), masked for display.

    The masking behind ``mask_run_inputs``, for any stored-raw field. Raises
    SecretResolutionError when a secret can't be resolved; ``mask_sets`` is
    the same per-(workspace, script) cache.
    """
    if mask_sets is None:
        masks = mask_set_for_display(run)
    else:
        key = (run_workspace_id(run), run.script_id)
        if key not in mask_sets:
            try:
                mask_sets[key] = mask_set_for_display(run)
            except SecretResolutionError as e:
                mask_sets[key] = e
        masks = mask_sets[key]
        if isinstance(masks, SecretResolutionError):
            raise masks
    return mask_secrets_in_value(value, masks)


class ExecutorError(Exception):
    """Base exception for executor errors."""

    pass


class EnvironmentNotFoundError(ExecutorError):
    """Raised when the environment directory does not exist."""

    pass


class PythonNotFoundError(ExecutorError):
    """Raised when the Python executable is not found."""

    pass


def _truncate_output(output: str, max_bytes: int = MAX_OUTPUT_BYTES) -> str:
    """Cap ``output`` at ``max_bytes`` (UTF-8), keeping its head and its tail.

    Output within the cap is returned unchanged. Larger output keeps the first
    ``OUTPUT_HEAD_BYTES`` (scaled to ``max_bytes``) and as much of the end as
    still fits, cut at line breaks when one is near, with a marker line between
    them that says how many bytes were left out. The result stays within
    ``max_bytes`` (for any cap of a few KB or more) and never splits a character.
    """
    if not output:
        return output

    encoded = output.encode("utf-8", errors="replace")
    total = len(encoded)
    if total <= max_bytes:
        return output

    head_budget = OUTPUT_HEAD_BYTES * max_bytes // MAX_OUTPUT_BYTES
    tail_budget = max(0, max_bytes - head_budget - _TRUNCATION_MARKER_RESERVE)

    head = encoded[: _head_cut(encoded[:head_budget])]
    tail_raw = encoded[total - tail_budget :]
    tail = tail_raw[_tail_cut(tail_raw) :]

    head_text = head.decode("utf-8", errors="ignore")
    tail_text = tail.decode("utf-8", errors="ignore")
    kept_head = len(head_text.encode("utf-8"))
    kept_tail = len(tail_text.encode("utf-8"))
    omitted = total - kept_head - kept_tail
    marker = (
        f"\n[OUTPUT TRUNCATED: {omitted:,} bytes cut from the middle. Kept the "
        f"first {kept_head:,} and the last {kept_tail:,} of {total:,} bytes.]\n"
    )
    if not head_text.endswith("\n"):
        marker = "\n" + marker
    return head_text + marker + tail_text


def _head_cut(head: bytes) -> int:
    """Where to end the kept head: after its last line break if one is near."""
    nl = head.rfind(b"\n", max(0, len(head) - _TRUNCATION_LINE_SLACK))
    return nl + 1 if nl != -1 else len(head)


def _tail_cut(tail: bytes) -> int:
    """Where to start the kept tail: after its first line break if one is near."""
    nl = tail.find(b"\n", 0, _TRUNCATION_LINE_SLACK)
    return nl + 1 if nl != -1 else 0


def _validate_environment(run: Run) -> str:
    """
    Validate the environment and return the Python executable path.

    Args:
        run: The Run instance containing the script and environment

    Returns:
        The absolute path to the Python executable

    Raises:
        EnvironmentNotFoundError: If environment directory doesn't exist
        PythonNotFoundError: If Python executable doesn't exist
    """
    environment = run.script.environment

    if not environment.exists():
        raise EnvironmentNotFoundError(
            f"Environment directory not found: {environment.get_full_path()}"
        )

    python_path = environment.get_python_executable()
    if not os.path.isfile(python_path):
        raise PythonNotFoundError(f"Python executable not found: {python_path}")

    return python_path


def _run_resource_limits() -> dict | None:
    """Build the posix resource-cap dict from the legacy env settings, or None.

    These ``PYRUNNER_RUN_RLIMIT_*`` env vars predate the dashboard sandbox
    controls and are now the break-glass fallback: they apply only for caps the
    dashboard leaves unset (see ``_resolve_run_limits``). Off by default (every
    setting 0), so an unconfigured instance carries no limits — unchanged.
    """
    mem = settings.PYRUNNER_RUN_RLIMIT_MEMORY_MB
    cpu = settings.PYRUNNER_RUN_RLIMIT_CPU_SECONDS
    nproc = settings.PYRUNNER_RUN_RLIMIT_NPROC
    if not (mem or cpu or nproc):
        return None
    limits = {}
    if mem:
        limits["memory_bytes"] = mem * 1024 * 1024
    if cpu:
        limits["cpu_seconds"] = cpu
    if nproc:
        limits["nproc"] = nproc
    return limits


def limit_hint(exit_code, stderr: str, limits: dict | None) -> str | None:
    """One line naming the resource limit that most likely stopped a run (U70, F30).

    A run killed by a cap otherwise ends with CPython's fatal MemoryError dump or
    with exit -9 and empty stderr — indistinguishable from a crash.
    """
    if not limits or exit_code in (0, None):
        return None
    stderr = stderr or ""
    mem = limits.get("memory_bytes")
    if mem and "MemoryError" in stderr:
        return f"[Stopped by the {mem // (1024 * 1024)} MB memory limit (Settings → Isolation)]"
    fsize = limits.get("fsize_bytes")
    if fsize and (exit_code in (-25, 153) or "File too large" in stderr):
        return f"[Stopped by the {fsize // (1024 * 1024)} MB file-size limit (Settings → Isolation)]"
    cpu = limits.get("cpu_seconds")
    if cpu and exit_code in (-9, -24, 137, 152):
        sig = 24 if exit_code in (-24, 152) else 9
        return (
            f"[Killed by signal {sig} — most likely the {cpu} s CPU-time limit "
            "(Settings → Isolation)]"
        )
    return None


def _resolve_run_limits() -> dict | None:
    """Resolve the posix resource caps for a run, dashboard-managed (DB) first.

    Sandbox plan Stage 1 (Seam 2): isolation config lives in ``GlobalSettings``
    and is resolved per-run at execution time (no restart, no env reliance). Each
    cap is read from the DB; a DB value of 0 means "unset" and falls back to the
    legacy ``PYRUNNER_RUN_RLIMIT_*`` env var for that one cap (break-glass), so
    the two layers degrade independently. ``RLIMIT_FSIZE`` is dashboard-only.

    Returns a ``{...}`` limits dict for ``RunSpec.limits`` (consumed by the posix
    ``preexec_fn`` in ``LocalSubprocessBackend``; a no-op on Windows), or ``None``
    when nothing is configured anywhere — a default instance, byte-for-byte today.
    """
    try:
        gs = GlobalSettings.get_settings()
    except Exception as e:
        # DB not ready (e.g. mid-migration boot window) — fall back to env only
        # rather than failing the run.
        logger.warning("Could not load GlobalSettings for run limits: %s", e)
        return _run_resource_limits()

    env_limits = _run_resource_limits() or {}
    limits = {}

    mem_mb = gs.sandbox_rlimit_memory_mb
    if mem_mb:
        limits["memory_bytes"] = mem_mb * 1024 * 1024
    elif "memory_bytes" in env_limits:
        limits["memory_bytes"] = env_limits["memory_bytes"]

    cpu_s = gs.sandbox_rlimit_cpu_seconds
    if cpu_s:
        limits["cpu_seconds"] = cpu_s
    elif "cpu_seconds" in env_limits:
        limits["cpu_seconds"] = env_limits["cpu_seconds"]

    nproc = gs.sandbox_rlimit_nproc
    if nproc:
        limits["nproc"] = nproc
    elif "nproc" in env_limits:
        limits["nproc"] = env_limits["nproc"]

    fsize_mb = gs.sandbox_rlimit_fsize_mb
    if fsize_mb:
        limits["fsize_bytes"] = fsize_mb * 1024 * 1024

    return limits or None


# Strictness ordering for the isolation policy. A workspace may TIGHTEN the
# instance default toward 'required' but never weaken below it.
_SANDBOX_STRICTNESS = {"off": 0, "optional": 1, "required": 2}


@dataclass
class IsolationDecision:
    """The resolved isolation policy for one run."""

    sandbox: bool  # True => run under the sandbox backend
    mandatory: bool  # True => effective policy is 'required' (fail-closed may apply)
    reason: str = ""  # short human explanation (logs)


def resolve_isolation(run, gs=None) -> IsolationDecision:
    """Resolve whether a run is sandboxed from the DB policy hierarchy.

    instance default (``GlobalSettings.sandbox_default``) → workspace policy
    (``Workspace.sandbox_policy``, can only TIGHTEN) → per-script toggle
    (``Script.isolation_mode``, honored only under 'optional'). Resolved at run
    time, so a policy change applies on the next run with no restart.

    A default instance (sandbox_default 'off', workspace policy null, script
    'inherit') returns ``sandbox=False`` — today's behavior, byte-for-byte.
    """
    if gs is None:
        gs = GlobalSettings.get_settings()
    base = gs.sandbox_default or "off"

    # Workspace policy can only make a run stricter than the instance default —
    # an Owner/Admin can mandate isolation but can't disable an instance floor.
    workspace = None
    if run is not None:
        workspace = getattr(run, "workspace", None)
        if workspace is None and getattr(run, "script_id", None):
            workspace = run.script.workspace
    ws_policy = getattr(workspace, "sandbox_policy", None) if workspace else None

    effective = base
    if ws_policy and _SANDBOX_STRICTNESS.get(ws_policy, 0) > _SANDBOX_STRICTNESS.get(base, 0):
        effective = ws_policy

    if effective == "required":
        return IsolationDecision(True, True, "policy: required")
    if effective == "off":
        return IsolationDecision(False, False, "policy: off")

    # 'optional': honor the per-script toggle ('sandboxed' opts in; else plain).
    mode = (
        getattr(run.script, "isolation_mode", "inherit")
        if (run is not None and getattr(run, "script_id", None))
        else "inherit"
    )
    if mode == "sandboxed":
        return IsolationDecision(True, False, "optional: script sandboxed")
    return IsolationDecision(False, False, "optional: script not sandboxed")


def _select_backend_for_run(run, gs):
    """Pick the backend for a run. An explicit ``PYRUNNER_RUN_BACKEND`` env value
    is a break-glass override; otherwise the DB isolation policy decides.

    Returns ``(backend, decision)`` — ``decision.mandatory`` drives fail-closed.
    The plain/local path deliberately flows through ``get_run_backend()`` so the
    single backend seam (and the ``PYRUNNER_RUN_BACKEND=local`` break-glass value)
    keeps working unchanged; only the sandbox-policy path bypasses it.
    """
    decision = resolve_isolation(run, gs)
    override = os.environ.get("PYRUNNER_RUN_BACKEND", "").strip().lower()

    # Sandbox is selected by an explicit 'sandbox' override or by the resolved
    # policy — unless an explicit 'local' override forces the plain path.
    want_sandbox = override == "sandbox" or (decision.sandbox and override != "local")
    if want_sandbox:
        from core.executor_backends import SandboxedSubprocessBackend

        reason = "override: sandbox" if override == "sandbox" else decision.reason
        return SandboxedSubprocessBackend(), IsolationDecision(
            True, decision.mandatory, reason
        )

    # Plain path: keep using the get_run_backend() seam (byte-for-byte default;
    # honors a 'local' break-glass value), so existing callers/tests are intact.
    return get_run_backend(), IsolationDecision(False, False, decision.reason)


def _kill_abandoned_process(backend, handle, run_id) -> None:
    """Best-effort kill of a spawned process tree on an abnormal executor exit.

    The backend launches scripts in their own session, so a worker-level
    exception that interrupts ``wait()`` leaves the subprocess alive and
    detached — still writing to databases and holding resources — for a run
    PyRun is about to report dead. Never raises: every caller is already on
    an error path.
    """
    if backend is None or handle is None:
        return
    try:
        backend.kill(handle)
        logger.warning(
            "Run %s: killed abandoned script process tree (pid %s)",
            run_id,
            handle.pid,
        )
    except Exception:
        logger.exception("Run %s: failed to kill abandoned process tree", run_id)


# Seconds to wait between attempts to save a finished run when the database
# connection fails. A PostgreSQL restart while a script runs kills the worker's
# persistent connection, so the final save hit that dead connection and the run's
# status and output were lost (it stayed "running" until reconcile_stale's
# deadline). About a minute in total covers a database restart.
FINAL_SAVE_RETRY_DELAYS = (1, 2, 4, 8, 15, 30)


def _persist_final_state(run: Run) -> int:
    """Save a finished run's terminal state; return the number of rows updated.

    The conditional UPDATE never clobbers an external CANCELLED. On a
    connection-level error (OperationalError / InterfaceError) the broken
    connection is closed — outside an atomic block, as in a worker — so the next
    attempt reconnects, and the write is retried after each delay in
    ``FINAL_SAVE_RETRY_DELAYS``. If the database is still unreachable after the
    last attempt the error propagates as before and ``reconcile_stale`` settles
    the run later.
    """
    import time

    from django.db import InterfaceError, OperationalError, connection

    delays = list(FINAL_SAVE_RETRY_DELAYS)
    attempt = 0
    while True:
        attempt += 1
        try:
            return (
                Run.objects.filter(pk=run.pk)
                .exclude(status=Run.Status.CANCELLED)
                .update(
                    status=run.status,
                    stdout=run.stdout,
                    stderr=run.stderr,
                    exit_code=run.exit_code,
                    ended_at=run.ended_at,
                    pid=None,
                )
            )
        except (OperationalError, InterfaceError) as e:
            if not delays:
                logger.error(
                    f"Run {run.id}: could not save the final state after "
                    f"{attempt} attempts: {e}"
                )
                raise
            delay = delays.pop(0)
            logger.warning(
                f"Run {run.id}: saving the final state failed ({e}); "
                f"reconnecting and retrying in {delay}s (attempt {attempt})"
            )
            if not connection.in_atomic_block:
                try:
                    connection.close()
                except Exception:  # the connection is already broken
                    pass
            time.sleep(delay)


def execute_run(run: Run, webhook_data: dict | None = None) -> None:
    """
    Execute a script run and update the Run record with results.

    This function is designed to be called from a django-q2 async task.
    It handles all aspects of script execution including:
    - Writing script code to a temporary file
    - Running the script with the appropriate Python executable
    - Capturing stdout/stderr
    - Handling timeouts
    - Updating the Run record with results

    Args:
        run: The Run model instance to execute
        webhook_data: Optional webhook data to inject as environment variables

    Note:
        Execution starts by atomically claiming the PENDING run; a duplicate
        task delivery (django-q2 re-delivers past the broker `retry` window)
        finds it already claimed and returns without touching the row. Once
        claimed, the run's state is always persisted — SUCCESS, FAILED,
        TIMEOUT, or CANCELLED (set externally by force-stop, never clobbered).
    """
    # Phase 1: Claim the run — BEFORE the try/finally below. django-q2's broker
    # re-delivers any task not acknowledged within the cluster-wide `retry`
    # window (the ORM broker's lock is Conf.RETRY; a per-task timeout does NOT
    # extend it), so a run that legitimately outlives that window WILL be handed
    # to a second worker while the first is still executing. That duplicate must
    # be a TRUE no-op — entering the try/finally would stamp ended_at, clear the
    # pid and overwrite stdout/stderr on the LIVE run row, leaving it stuck at
    # 'running' with its output lost (the exact orphan entrypoint.sh's crash-
    # safety story assumes cannot happen). The claim is one conditional UPDATE
    # so two workers racing on the same PENDING run cannot both win it.
    claimed = Run.objects.filter(pk=run.pk, status=Run.Status.PENDING).update(
        status=Run.Status.RUNNING, started_at=timezone.now()
    )
    if not claimed:
        current = (
            Run.objects.filter(pk=run.pk).values_list("status", flat=True).first()
        )
        logger.warning(
            f"Run {run.id} is not in PENDING status (current: {current}). "
            "Skipping duplicate delivery."
        )
        return
    run.refresh_from_db(fields=["status", "started_at"])

    script_file_path = None
    run_file_path = None
    library_root = None
    backend = None
    handle = None
    wait_completed = False

    try:

        # Validate environment
        try:
            python_path = _validate_environment(run)
        except EnvironmentNotFoundError as e:
            run.status = Run.Status.FAILED
            run.stderr = str(e)
            run.ended_at = timezone.now()
            run.save()
            logger.error(f"Run {run.id} failed: {e}")
            return
        except PythonNotFoundError as e:
            run.status = Run.Status.FAILED
            run.stderr = str(e)
            run.ended_at = timezone.now()
            run.save()
            logger.error(f"Run {run.id} failed: {e}")
            return

        # The script's own working folder (kept between its runs, removed when
        # the script is deleted). The temp script file and the libraries staging
        # dir live inside it too: the sandbox backend binds only the run's cwd
        # writable, so everything the run needs must be under it. Dot-prefixed so
        # a script listing its folder doesn't trip over them.
        workdir = script_workdir(run.script_id)
        workdir.mkdir(parents=True, exist_ok=True)

        # Phase 2: Create temporary script file
        # Use delete=False for Windows compatibility (must close before subprocess reads)
        with tempfile.NamedTemporaryFile(
            mode="w",
            prefix=".pyrun-",
            suffix=".py",
            delete=False,
            encoding="utf-8",
            dir=str(workdir),
        ) as script_file:
            # Use code_snapshot if available (preserves code at queue time)
            code = run.code_snapshot if run.code_snapshot else run.script.code
            script_file.write(code)
            script_file_path = script_file.name

        # Phase 2a: the run's run.json (PYRUNNER_RUN_FILE) for pyrunner_run.
        try:
            run_file_path = _write_run_file(run, workdir)
        except OSError as e:
            run.status = Run.Status.FAILED
            run.exit_code = -1
            run.stderr = f"Failed to write the run file: {e}"
            logger.error("Run %s could not write its run file: %s", run.id, e)
            return  # the finally block persists this FAILED state

        # Phase 2b: Materialize the libraries pinned on this run at queue time.
        # The staging dir lives UNDER the run's workdir, which the sandbox backend
        # already binds writable and whose PYTHONPATH entries it binds into the
        # jail — so isolated runs import libraries with no extra sandbox wiring.
        if run.library_versions:
            try:
                library_root = tempfile.mkdtemp(prefix=".pyrun-libs-", dir=str(workdir))
                materialize_libraries(run, library_root)
            except LibraryMaterializationError as e:
                # Fail-closed: a pinned revision that no longer exists fails the
                # run with a named cause BEFORE spawn, never a silent ImportError
                # surfacing from inside the user's script.
                run.status = Run.Status.FAILED
                run.exit_code = -1
                run.stderr = str(e)
                logger.error("Run %s failed to materialize libraries: %s", run.id, e)
                return  # the finally block persists this FAILED state
            except OSError as e:
                run.status = Run.Status.FAILED
                run.exit_code = -1
                run.stderr = f"Failed to stage script libraries: {e}"
                logger.error("Run %s could not stage libraries: %s", run.id, e)
                return  # the finally block persists this FAILED state

        # Phase 3: Execute script
        try:
            # Build subprocess arguments
            cmd = [python_path, script_file_path]

            # Resolve secrets ONCE — the same dict feeds env injection AND output
            # masking, so masking can never drift from what was injected (and the
            # values are decrypted a single time, not twice).
            try:
                secrets = resolve_secrets_for_run(run)
            except SecretResolutionError as e:
                # An external secret could not be resolved (fail-closed). Fail the
                # run BEFORE start with the named cause rather than launching it
                # with a silently missing env var. Message carries no secret value.
                run.status = Run.Status.FAILED
                run.exit_code = -1
                run.stderr = str(e)
                logger.error("Run %s failed to resolve secrets: %s", run.id, e)
                return  # the finally block persists this FAILED state

            # Build environment with secrets and webhook data injected
            script_env = _build_script_environment(
                webhook_data,
                run=run,
                secrets=secrets,
                library_root=library_root,
                run_file=run_file_path,
            )

            # Also mask the injected AI credential in output, if any.
            with_ai_credentials(secrets)

            # Select the RunBackend from the DB isolation policy (instance →
            # workspace → script), resolved per-run; an explicit
            # PYRUNNER_RUN_BACKEND env value is a break-glass override. The chosen
            # backend launches the script in its own process group/session so the
            # web process can later kill the whole job tree (force stop / timeout)
            # without touching the django-q worker. The Run lifecycle below — pid
            # record, masking, truncation, status mapping, the cancel-safe save —
            # stays in core.
            gs = GlobalSettings.get_settings()
            backend, decision = _select_backend_for_run(run, gs)

            # Fail-closed: when isolation is REQUIRED but the host can't deliver a
            # full sandbox, an opt-in instance fails the run rather than silently
            # degrading to a weaker tier. Default (fail_closed off) degrades+warns
            # inside the backend, preserving the accident-model behavior.
            if decision.sandbox and decision.mandatory and gs.sandbox_fail_closed:
                from core.executor_backends.sandboxed import runtime_tier
                from core.services.sandbox import CAP_FULL

                if runtime_tier() != CAP_FULL:
                    run.status = Run.Status.FAILED
                    run.exit_code = -1
                    run.stderr = (
                        "Isolation is required for this run, but a full sandbox is "
                        "unavailable on this host and fail-closed is enabled "
                        f"(active tier: {runtime_tier()}). The run was not executed."
                    )
                    logger.error(
                        "Run %s blocked by fail-closed: sandbox required but unavailable",
                        run.id,
                    )
                    return  # the finally block persists this FAILED state

            spec = RunSpec(
                cmd=cmd,
                env=script_env,
                cwd=str(workdir),
                timeout=run.script.timeout_seconds,
                limits=_resolve_run_limits(),
            )
            handle = backend.start(spec)

            # Record the PID so the web process can kill this exact job tree.
            run.pid = handle.pid
            run.save(update_fields=["pid"])

            result = backend.wait(handle, spec.timeout)
            wait_completed = True

            if result.timed_out:
                run.status = Run.Status.TIMEOUT
                run.stdout = _truncate_output(
                    _mask_secrets_in_output(result.stdout or "", secrets)
                )
                run.stderr = _truncate_output(
                    _mask_secrets_in_output(result.stderr or "", secrets)
                )
                if run.stderr:
                    run.stderr += "\n\n[TIMEOUT: Script exceeded maximum execution time]"
                else:
                    run.stderr = (
                        f"[TIMEOUT: Script exceeded {run.script.timeout_seconds} seconds]"
                    )
                run.exit_code = -1
                logger.warning(
                    f"Run {run.id} timed out after {run.script.timeout_seconds}s"
                )
            else:
                # Process results - mask secrets in output
                run.stdout = _truncate_output(_mask_secrets_in_output(result.stdout, secrets))
                run.stderr = _truncate_output(_mask_secrets_in_output(result.stderr, secrets))
                run.exit_code = result.exit_code
                run.status = (
                    Run.Status.SUCCESS if result.exit_code == 0 else Run.Status.FAILED
                )
                hint = limit_hint(result.exit_code, run.stderr, spec.limits)
                if hint:
                    run.stderr = f"{run.stderr}\n\n{hint}" if run.stderr else hint

        except subprocess.SubprocessError as e:
            # Handle other subprocess errors
            run.status = Run.Status.FAILED
            run.stderr = f"Subprocess error: {str(e)}"
            run.exit_code = -1
            logger.error(f"Run {run.id} subprocess error: {e}")

    except SystemExit:
        # django-q2's per-task TimeoutException subclasses SystemExit — NOT
        # Exception — so it can tear the worker down. Left unhandled it would
        # bypass the catch-all below, abandon the script subprocess (which
        # survives detached in its own session, still writing), and let the
        # finally persist a half-stamped row. Kill the tree, stamp a terminal
        # state, and re-raise so django-q's own task accounting still records
        # the timeout.
        if not wait_completed:
            _kill_abandoned_process(backend, handle, run.id)
        run.status = Run.Status.TIMEOUT
        run.exit_code = -1
        run.stderr = (
            "[TIMEOUT: the worker's task deadline fired before the script "
            f"finished (script timeout: {run.script.timeout_seconds}s). The "
            "process tree was killed.]"
        )
        logger.error(f"Run {run.id} hit the worker task deadline; process killed")
        raise

    except Exception as e:
        # Catch-all for unexpected errors. If the failure interrupted wait(),
        # the subprocess is still alive and would survive detached — kill it so
        # a run PyRun reports failed cannot keep executing (and writing) in
        # the background.
        if not wait_completed:
            _kill_abandoned_process(backend, handle, run.id)
        run.status = Run.Status.FAILED
        run.stderr = f"Unexpected executor error: {str(e)}\n\n{traceback.format_exc()}"
        run.exit_code = -1
        logger.exception(f"Run {run.id} unexpected error")

    finally:
        # Phase 4: Cleanup and save
        # Always set end time if not already set
        if not run.ended_at:
            run.ended_at = timezone.now()

        # Persist results — but never clobber a CANCELLED status set externally
        # by the web process when it killed this job (TaskService.force_stop_task).
        # A plain refresh-then-save would leave a race window where a kill landing
        # between the two could be overwritten, so use a conditional UPDATE that
        # excludes CANCELLED. This is the documented gotcha for this feature.
        updated = _persist_final_state(run)
        if not updated:
            # Externally cancelled/killed — keep that terminal state; only make
            # sure the now-dead PID is cleared so it can't be reused for a kill.
            Run.objects.filter(pk=run.pk).update(pid=None)
            run.status = Run.Status.CANCELLED
        run.pid = None

        # Cleanup temporary files (the script and its run.json)
        for temp_path, what in ((script_file_path, "temp script file"), (run_file_path, "run file")):
            if temp_path is None:
                continue
            try:
                os.unlink(temp_path)
            except OSError as e:
                logger.warning(f"Failed to delete {what}: {e}")

        # Cleanup the materialized library staging dir (same lifetime as the temp
        # script file). ignore_errors so a Windows lock on a just-imported .pyc
        # leaves a stale dir behind rather than masking the run's real outcome.
        if library_root is not None:
            shutil.rmtree(library_root, ignore_errors=True)
            if os.path.exists(library_root):
                logger.warning(
                    "Failed to delete library staging dir: %s", library_root
                )

        logger.info(
            f"Run {run.id} completed with status {run.status} "
            f"(exit_code={run.exit_code})"
        )

        # A failed run of a script that retries schedules its next attempt
        # (the worker heartbeat starts it when due). Never raises.
        from core.services.run_retries import try_schedule_retry

        try_schedule_retry(run, webhook_data)

        # The run has ended: a Queue-policy script starts its next waiting run
        # now. try_release never raises, so this can't turn a finished run into
        # an executor error; the worker heartbeat retries anything it misses.
        from core.services.run_concurrency import try_release

        try_release(run.script_id)


def run_in_environment(
    environment,
    *,
    code: str | None = None,
    path: str | None = None,
    args: list | None = None,
    timeout: int | None = 60,
    cwd: str | None = None,
    env: dict | None = None,
) -> tuple[int, str, str]:
    """Run a helper in a PyRun environment's venv as an isolated subprocess.

    This is the safe path for plugin *compute*: third-party packages run in the
    chosen environment's venv (a subprocess), never inside the Django process —
    so a bad package fails a *call*, not the server. It reuses the executor's
    hardened pattern: the environment's Python, process-group/session isolation,
    a timeout, and captured + size-capped output.

    Provide exactly one of ``code`` (a string of Python source) or ``path`` (an
    absolute path to a ``.py`` file already on disk). ``args`` are passed as argv.

    Returns ``(exit_code, stdout, stderr)``. On timeout the whole process tree is
    killed and ``exit_code`` is ``-1``.
    """
    if (code is None) == (path is None):
        raise ValueError("Provide exactly one of `code` or `path`.")

    if not environment.exists():
        raise EnvironmentNotFoundError(
            f"Environment directory not found: {environment.get_full_path()}"
        )
    python_path = environment.get_python_executable()
    if not os.path.isfile(python_path):
        raise PythonNotFoundError(f"Python executable not found: {python_path}")

    workdir = Path(settings.SCRIPTS_WORKDIR)
    workdir.mkdir(parents=True, exist_ok=True)

    temp_file = None
    try:
        if code is not None:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".py",
                delete=False,
                encoding="utf-8",
                dir=str(workdir),
            ) as fh:
                fh.write(code)
                temp_file = fh.name
            script_path = temp_file
        else:
            script_path = path

        cmd = [python_path, script_path] + [str(a) for a in (args or [])]

        run_env = dict(os.environ)
        if env:
            run_env.update({k: str(v) for k, v in env.items()})

        # Same single execution path as execute_run: the configured backend
        # spawns in an isolated process group/session and kills the tree on
        # timeout. No Run row here — plugin compute just gets (code, out, err).
        backend = get_run_backend()
        spec = RunSpec(
            cmd=cmd,
            env=run_env,
            cwd=str(cwd) if cwd else str(workdir),
            timeout=timeout,
        )
        handle = backend.start(spec)
        result = backend.wait(handle, spec.timeout)
        if result.timed_out:
            stderr = (result.stderr or "") + f"\n\n[TIMEOUT: exceeded {timeout}s]"
            return -1, _truncate_output(result.stdout or ""), _truncate_output(stderr)
        return (
            result.exit_code,
            _truncate_output(result.stdout or ""),
            _truncate_output(result.stderr or ""),
        )
    finally:
        if temp_file:
            try:
                os.unlink(temp_file)
            except OSError:
                pass
