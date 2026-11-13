"""
In-place plugin update and rollback (2.5 T42).

Uploading a zip of an installed slug with a higher version updates the plugin
where it is, instead of the old "delete it first":

1. the zip is checked like an install (zip-slip / size safe) and unpacked into
   ``PLUGINS_DIR/.staging/`` (same filesystem, so the swap is a rename);
2. the version must be higher (semver), the doctor runs on the STAGED copy (with
   the update rules: version increase, CHANGELOG.md), then the ``min_pyrunner``
   and SDK ``api`` gates;
3. the live folder moves to ``PLUGINS_DIR/.previous/<slug>-<version>/`` and the
   staged one takes its place; an active plugin's new code must pass the
   isolated preflight, or the old folder goes straight back;
4. the row gets the new version / checksum / manifest and keeps the old ones in
   ``previous_version`` / ``previous_checksum``; a ``PluginUpdate`` row records it.

Nothing here touches what the plugin owns: its secrets, data stores, scripts and
schedules stay exactly as they are. The plugin's own update steps (an optional
``lifecycle.py`` with ``reprovision()`` and ``on_update(from_version)``) run after
the next restart, once the new code is the code that is loaded, in an isolated
process (``manage.py plugin_post_update``, called by the entrypoint after the
plugin preflight).

Rollback moves the previous folder back (after checking it against
``previous_checksum``), restores the row and asks for a restart; after that
restart ``reprovision()`` re-runs so provisioned code matches the folder again.
The previous folder is kept until the next successful update.
"""

import io
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
import zipfile
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from core.models import Plugin, PluginUpdate
from core.services import plugin_trust
from core.services.plugin_doctor import semver_key
from core.services.plugin_service import PluginInstallError, PluginService

logger = logging.getLogger(__name__)

PREVIOUS_DIRNAME = ".previous"
STAGING_DIRNAME = ".staging"
#: The plugin module whose functions are the update steps.
LIFECYCLE_MODULE = "lifecycle"
#: How long one plugin's update steps may run at boot.
STEPS_TIMEOUT = 300  # seconds
#: A lock older than this is from a request that died; it no longer blocks.
LOCK_STALE_AFTER = 600  # seconds
#: Stored error / step output is cut to this many characters (the tail is kept).
ERROR_MAX_CHARS = 4000

def version_problem(installed, new, slug):
    """A user-facing refusal when ``new`` is not a higher semver than ``installed``."""
    new_key, old_key = semver_key(new), semver_key(installed)
    if new_key is None:
        return (
            f"Plugin '{slug}': the uploaded version {new!r} is not semver "
            "(MAJOR.MINOR.PATCH), so it can't be compared with the installed one."
        )
    if old_key is None:
        return (
            f"Plugin '{slug}': the installed version {installed!r} is not semver, so "
            "an update can't tell which is newer. Delete the plugin and install the "
            "new zip instead."
        )
    if new_key <= old_key:
        which = "the same version as" if new_key == old_key else "older than"
        return (
            f"Plugin '{slug}' {installed} is installed and this zip is {new}, "
            f"{which} it. Upload a higher version to update."
        )
    return None


def api_version_problem(manifest, slug):
    """A refusal when the manifest's ``api`` needs a newer plugin SDK than this PyRun's."""
    from core.plugins.api import API_VERSION

    wanted = (manifest or {}).get("api") if isinstance(manifest, dict) else None
    if not wanted:
        return None

    def key(value):
        return tuple(int(p) for p in str(value).split("."))

    try:
        if key(wanted) > key(API_VERSION):
            return (
                f"Plugin '{slug}' needs plugin SDK {wanted}; this PyRun has {API_VERSION}. "
                "Upgrade PyRun first."
            )
    except ValueError:
        return None  # malformed: the doctor reports it
    return None


def _dev_mode_problem(slug):
    if getattr(settings, "DEV_PLUGIN", None) == f"plugins.{slug}":
        return (
            f"'{slug}' is loaded in dev mode from disk; dev-mode plugins are not "
            "updated by upload. Stop the dev server (PYRUNNER_PLUGIN_DEV) first."
        )
    return None


# --------------------------------------------------------------------------- #
# Folders
# --------------------------------------------------------------------------- #

def _plugins_dir() -> Path:
    return Path(settings.PLUGINS_DIR)


def _folder_version(version) -> str:
    return re.sub(r"[^0-9A-Za-z.+-]", "_", str(version or "")) or "unknown"


def previous_folder(slug, version) -> Path:
    """Where the folder of ``slug`` at ``version`` waits for a rollback."""
    return _plugins_dir() / PREVIOUS_DIRNAME / f"{slug}-{_folder_version(version)}"


def previous_folders(slug) -> list:
    """Every kept folder of ``slug`` (a slug has no "-", so the prefix is exact)."""
    root = _plugins_dir() / PREVIOUS_DIRNAME
    if not root.is_dir():
        return []
    return [p for p in root.iterdir() if p.is_dir() and p.name.startswith(f"{slug}-")]


def remove_previous_folders(slug) -> None:
    """Drop every kept folder of ``slug`` (the plugin is being deleted)."""
    for folder in previous_folders(slug):
        shutil.rmtree(folder, ignore_errors=True)


def _staging_root() -> Path:
    root = _plugins_dir() / STAGING_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _discard(folder: Path) -> None:
    """Move ``folder`` out of the way at once, then delete it."""
    if not folder.exists():
        return
    trash = _staging_root() / f"discard-{uuid.uuid4().hex}"
    try:
        os.rename(folder, trash)
    except OSError:
        trash = folder
    shutil.rmtree(trash, ignore_errors=True)


class _SlugLock:
    """One update or rollback of a slug at a time, across processes.

    ``mkdir`` is atomic on every filesystem PyRun runs on; a lock left by a
    request that died is ignored after ``LOCK_STALE_AFTER`` seconds.
    """

    def __init__(self, slug):
        self.path = _staging_root() / f"{slug}.lock"

    def __enter__(self):
        try:
            os.mkdir(self.path)
        except FileExistsError:
            try:
                age = time.time() - self.path.stat().st_mtime
            except OSError:
                age = 0
            if age < LOCK_STALE_AFTER:
                raise PluginInstallError(
                    "Another update or rollback of this plugin is in progress. Try again in a minute."
                )
            shutil.rmtree(self.path, ignore_errors=True)
            try:
                os.mkdir(self.path)
            except FileExistsError:
                raise PluginInstallError(
                    "Another update or rollback of this plugin is in progress. Try again in a minute."
                )
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.path, ignore_errors=True)
        return False


def _cut(text) -> str:
    text = str(text or "").strip()
    return text if len(text) <= ERROR_MAX_CHARS else "…" + text[-ERROR_MAX_CHARS:]


# --------------------------------------------------------------------------- #
# Upload entry point
# --------------------------------------------------------------------------- #

def upload(uploaded_file, *, user=None, source=PluginUpdate.Source.UPLOAD,
           initiated_by=PluginUpdate.InitiatedBy.USER):
    """Install a new plugin, or update the installed one with the same slug.

    Returns ``(plugin, update, warnings)``; ``update`` is None for a fresh
    install. ``source`` (upload, store or url) is recorded on the new Plugin row
    or on the update's history row. Raises ``PluginInstallError`` with a
    user-facing message.
    """
    data = uploaded_file.read()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            slug = PluginService._validate_zip(zf)
    except zipfile.BadZipFile:
        raise PluginInstallError("That file is not a valid .zip archive.")
    plugin = Plugin.objects.filter(slug=slug).first()
    if plugin is None:
        return PluginService.install_from_zip(io.BytesIO(data), source=source), None, []
    update_row, warnings = update(
        plugin, io.BytesIO(data), user=user, source=source, initiated_by=initiated_by,
    )
    plugin.refresh_from_db()
    return plugin, update_row, warnings


# --------------------------------------------------------------------------- #
# Update
# --------------------------------------------------------------------------- #

def update(plugin, uploaded_file, *, user=None, source=PluginUpdate.Source.UPLOAD,
           initiated_by=PluginUpdate.InitiatedBy.USER):
    """Update ``plugin`` in place from a zip of a higher version.

    Callable without a request (``user`` None, ``initiated_by`` auto) so an
    automatic update later is a caller, not a rebuild. Returns
    ``(PluginUpdate, doctor_warnings)``. A refusal raises ``PluginInstallError``
    and is recorded as a ``refused`` history row; the plugin is unchanged.
    """
    attempt = {"to_version": ""}
    try:
        return _update(plugin, uploaded_file, user=user, source=source,
                       initiated_by=initiated_by, attempt=attempt)
    except PluginInstallError as exc:
        PluginUpdate.objects.create(
            plugin=plugin,
            from_version=plugin.version,
            to_version=str(attempt["to_version"])[:50],
            from_checksum=plugin.checksum,
            source=source,
            initiated_by=initiated_by,
            user=user if getattr(user, "pk", None) else None,
            outcome=PluginUpdate.Outcome.REFUSED,
            # A preflight failure carries the new code's output: mask it like steps.
            error=_cut(mask_plugin_output(plugin.slug, str(exc))),
            finished_at=timezone.now(),
        )
        logger.warning("Update of plugin %r refused: %s", plugin.slug, exc)
        raise


def _update(plugin, uploaded_file, *, user, source, initiated_by, attempt):
    from core.services.plugin_doctor import run_doctor

    slug = plugin.slug
    checked_version = plugin.version
    problem = _dev_mode_problem(slug)
    if problem:
        raise PluginInstallError(problem)

    data = uploaded_file.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise PluginInstallError("That file is not a valid .zip archive.")

    stage = Path(tempfile.mkdtemp(prefix="update-", dir=_staging_root()))
    try:
        with zf:
            zip_slug = PluginService._validate_zip(zf)
            if zip_slug != slug:
                raise PluginInstallError(
                    f"This zip is the plugin '{zip_slug}', not '{slug}'."
                )
            PluginService._safe_extract(zf, str(stage))
        staged = stage / slug
        manifest = PluginService._read_manifest(staged, slug)
        new_version = str(manifest.get("version") or "")
        attempt["to_version"] = new_version
        # Signature, publisher pin and trust policy (2.5 T43), on the staged copy.
        trust = plugin_trust.check(staged, slug, manifest, plugin=plugin)
        problem = version_problem(plugin.version, new_version, slug)
        if problem:
            raise PluginInstallError(problem)

        report = run_doctor(staged, installed_version=plugin.version)
        if not report.ok:
            raise PluginInstallError(
                "The plugin doctor refused the new version:\n" + report.failures_text()
            )
        problem = (PluginService.min_version_problem(manifest, slug)
                   or api_version_problem(manifest, slug))
        if problem:
            raise PluginInstallError(problem)

        new_checksum = PluginService._checksum_dir(staged)
        with _SlugLock(slug):
            plugin.refresh_from_db()
            if plugin.version != checked_version:
                raise PluginInstallError("The plugin changed while this update was checked. Try again.")
            old_version, old_checksum, kept = _swap_in(plugin, staged)
            if plugin.status == Plugin.Status.ACTIVE:
                ok, output = PluginService._run_preflight(slug)
                if not ok:
                    _swap_back(slug, kept)
                    raise PluginInstallError(
                        "The new version failed the isolated preflight, so the "
                        f"installed {old_version} was put back:\n{_cut(output)}"
                    )
            for folder in previous_folders(slug):
                if folder != kept:
                    shutil.rmtree(folder, ignore_errors=True)

            plugin.name = manifest.get("name") or slug
            plugin.version = new_version
            plugin.manifest = manifest
            plugin.checksum = new_checksum
            plugin.previous_version = old_version if kept else ""
            plugin.previous_checksum = old_checksum if kept else ""
            # Where the installed code came from now (2.5 T52): the Store, a
            # URL or an upload; the Updates history keeps every earlier one.
            plugin.source = source
            plugin.save(update_fields=[
                "name", "version", "manifest", "checksum", "source",
                "previous_version", "previous_checksum", "updated_at",
                *plugin_trust.apply(plugin, trust),
            ])
            row = PluginUpdate.objects.create(
                plugin=plugin,
                from_version=old_version,
                to_version=new_version,
                from_checksum=old_checksum,
                to_checksum=new_checksum,
                source=source,
                initiated_by=initiated_by,
                user=user if getattr(user, "pk", None) else None,
                outcome=PluginUpdate.Outcome.PENDING,
            )
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    logger.info("Plugin %r updated %s -> %s", slug, row.from_version, row.to_version)
    return row, report.warnings_text()


def _swap_in(plugin, staged: Path):
    """Live folder -> ``.previous``, staged -> live. Returns (old_version, old_checksum, kept)."""
    slug = plugin.slug
    live = _plugins_dir() / slug
    old_version = plugin.version
    kept = None
    old_checksum = plugin.checksum
    if live.is_dir():
        old_checksum = PluginService._checksum_dir(live)
        kept = previous_folder(slug, old_version)
        kept.parent.mkdir(parents=True, exist_ok=True)
        _discard(kept)  # a stale copy of the same version
        os.rename(live, kept)
    try:
        os.rename(staged, live)
    except OSError as exc:
        if kept is not None:
            os.rename(kept, live)
        raise PluginInstallError(f"Could not move the new version into place ({exc}).")
    return old_version, old_checksum, kept


def _swap_back(slug, kept):
    live = _plugins_dir() / slug
    _discard(live)
    if kept is not None:
        os.rename(kept, live)


# --------------------------------------------------------------------------- #
# Rollback
# --------------------------------------------------------------------------- #

def rollback_problem(plugin):
    """Why ``plugin`` can't be rolled back now, or None when it can."""
    if not plugin.previous_version:
        return "There is no previous version to roll back to."
    problem = _dev_mode_problem(plugin.slug)
    if problem:
        return problem
    if not previous_folder(plugin.slug, plugin.previous_version).is_dir():
        return (
            f"The kept folder of {plugin.previous_version} is gone from the plugins "
            "directory, so there is nothing to roll back to."
        )
    return None


def _kept_trust(plugin, kept):
    """Trust of the kept folder (it was live here, so its __pycache__ is skipped).

    The publisher pin is not applied: the kept folder already passed it (or was
    installed before the pin). The trust policy is.
    """
    manifest = PluginService._read_manifest(kept, plugin.slug)
    trust = plugin_trust.check(kept, plugin.slug, manifest, include_generated=False, pin=False)
    return manifest, trust


def rollback(plugin, *, user=None, initiated_by=PluginUpdate.InitiatedBy.USER):
    """Put the previous version's folder and row back. Returns the PluginUpdate row."""
    problem = rollback_problem(plugin)
    if problem:
        raise PluginInstallError(problem)
    slug = plugin.slug
    with _SlugLock(slug):
        plugin.refresh_from_db()
        problem = rollback_problem(plugin)
        if problem:
            raise PluginInstallError(problem)
        kept = previous_folder(slug, plugin.previous_version)
        if PluginService._checksum_dir(kept) != plugin.previous_checksum:
            raise PluginInstallError(
                f"The kept folder of {plugin.previous_version} does not match the "
                "checksum recorded when it was replaced, so it was not restored."
            )
        manifest, trust = _kept_trust(plugin, kept)
        _swap_back(slug, kept)

        rolled_back = plugin.version
        from_checksum = plugin.checksum
        plugin.name = manifest.get("name") or slug
        plugin.version = plugin.previous_version
        plugin.checksum = plugin.previous_checksum
        plugin.manifest = manifest
        plugin.previous_version = ""
        plugin.previous_checksum = ""
        plugin.save(update_fields=[
            "name", "version", "manifest", "checksum",
            "previous_version", "previous_checksum", "updated_at",
            *plugin_trust.apply(plugin, trust),
        ])
        # Steps of the version just removed must never run.
        plugin.updates.filter(
            outcome=PluginUpdate.Outcome.PENDING, to_version=rolled_back,
        ).exclude(source=PluginUpdate.Source.ROLLBACK).update(
            outcome=PluginUpdate.Outcome.CANCELLED,
            error="Rolled back before the update steps ran.",
            finished_at=timezone.now(),
        )
        row = PluginUpdate.objects.create(
            plugin=plugin,
            from_version=rolled_back,
            to_version=plugin.version,
            from_checksum=from_checksum,
            to_checksum=plugin.checksum,
            source=PluginUpdate.Source.ROLLBACK,
            initiated_by=initiated_by,
            user=user if getattr(user, "pk", None) else None,
            outcome=PluginUpdate.Outcome.PENDING,
        )
    logger.info("Plugin %r rolled back %s -> %s", slug, row.from_version, row.to_version)
    return row


# --------------------------------------------------------------------------- #
# Restart detection
# --------------------------------------------------------------------------- #

def swapped_since_load() -> bool:
    """True when an active plugin's folder changed after this process loaded plugins."""
    loaded_at = datetime.fromtimestamp(
        float(getattr(settings, "PLUGINS_LOADED_AT", 0.0)), tz=dt_timezone.utc
    )
    return PluginUpdate.objects.filter(
        plugin__status=Plugin.Status.ACTIVE,
        outcome__in=PluginUpdate.SWAPPED,
        created_at__gt=loaded_at,
    ).exists()


# --------------------------------------------------------------------------- #
# Update steps after the restart
# --------------------------------------------------------------------------- #

def mask_plugin_output(slug, text):
    """Mask the plugin's own secret values out of output before it is stored or shown."""
    from core.executor import mask_secrets_in_value
    from core.models import Secret

    pairs = []
    for secret in Secret.objects.filter(owner_plugin=slug):
        try:
            value = secret.get_decrypted_value()
        except Exception:
            # Undecryptable: the plugin can't read it either, so it can't print it.
            continue
        if value:
            pairs.append((secret.get_clean_name(), value))
    # Longest value first, so one that contains another is masked whole. The
    # same name in two workspaces gets a suffix: both values are masked.
    mask = {}
    for name, value in sorted(pairs, key=lambda pair: -len(pair[1])):
        label, n = name, 1
        while label in mask:
            n += 1
            label = f"{name}#{n}"
        mask[label] = value
    return mask_secrets_in_value(text, mask)


def _claim(plugin):
    """Claim this plugin's pending rows for this process (conditional UPDATE each)."""
    claimed = []
    for row in plugin.updates.filter(outcome=PluginUpdate.Outcome.PENDING).order_by("created_at"):
        won = PluginUpdate.objects.filter(
            pk=row.pk, outcome=PluginUpdate.Outcome.PENDING,
        ).update(outcome=PluginUpdate.Outcome.RUNNING)
        if won:
            claimed.append(row)
    return claimed


def _fail_interrupted():
    """Rows left ``running`` by a process that died: they will never finish."""
    cutoff = timezone.now() - timezone.timedelta(seconds=STEPS_TIMEOUT * 2)
    PluginUpdate.objects.filter(
        outcome=PluginUpdate.Outcome.RUNNING, created_at__lt=cutoff,
    ).update(
        outcome=PluginUpdate.Outcome.STEPS_FAILED,
        error="Interrupted: the update steps did not finish (PyRun stopped while they ran).",
        finished_at=timezone.now(),
    )


def steps_plan(rows):
    """``(from_version, reprovision_only)`` for a set of claimed rows.

    Several updates may wait for one restart; ``on_update`` runs once, from the
    oldest version they started at. Only rollbacks waiting means only
    ``reprovision()``.
    """
    updates = [r for r in rows if r.source != PluginUpdate.Source.ROLLBACK]
    if not updates:
        return "", True
    oldest = min(updates, key=lambda r: r.created_at)
    return oldest.from_version, False


def run_pending_steps(write=print) -> dict:
    """Run the update steps every active plugin has waiting (boot, plugins off).

    Each plugin's steps run in an isolated child that loads only that plugin.
    Never raises: a failure is recorded on the rows and the boot goes on.
    Returns ``{slug: outcome}``.
    """
    results = {}
    try:
        _fail_interrupted()
        plugins = Plugin.objects.filter(
            status=Plugin.Status.ACTIVE, updates__outcome=PluginUpdate.Outcome.PENDING,
        ).distinct()
        for plugin in plugins:
            rows = _claim(plugin)
            if not rows:
                continue
            from_version, reprovision_only = steps_plan(rows)
            ok, output = _spawn_steps(plugin.slug, from_version, reprovision_only)
            outcome = PluginUpdate.Outcome.OK if ok else PluginUpdate.Outcome.STEPS_FAILED
            PluginUpdate.objects.filter(pk__in=[r.pk for r in rows]).update(
                outcome=outcome,
                error="" if ok else _cut(mask_plugin_output(plugin.slug, output)) or "Update steps failed.",
                finished_at=timezone.now(),
            )
            results[plugin.slug] = outcome
            write(f"  {'ok  ' if ok else 'FAIL'}  {plugin.slug}")
    except Exception:
        logger.exception("Running plugin update steps failed")
        write("Plugin update steps could not run: " + traceback.format_exc(limit=1).strip())
    return results


def _spawn_steps(slug, from_version, reprovision_only):
    cmd = [sys.executable, str(Path(settings.BASE_DIR) / "manage.py"), "plugin_post_update", slug]
    if reprovision_only:
        cmd.append("--reprovision-only")
    else:
        cmd += ["--from-version", from_version]
    try:
        proc = subprocess.run(
            cmd,
            env=PluginService._isolated_env(slug),
            capture_output=True,
            text=True,
            timeout=STEPS_TIMEOUT,
            cwd=str(settings.BASE_DIR),
        )
    except subprocess.TimeoutExpired:
        return False, f"The update steps timed out after {STEPS_TIMEOUT}s."
    return proc.returncode == 0, ((proc.stdout or "") + (proc.stderr or "")).strip()


def run_steps(slug, from_version="", *, reprovision_only=False, write=print):
    """Run ``plugins.<slug>.lifecycle`` steps in THIS process (the isolated child).

    ``reprovision()`` first, so provisioned scripts carry the new code, then
    ``on_update(from_version)``. A plugin without ``lifecycle.py`` has no steps.
    Exceptions propagate (the child turns them into a non-zero exit).
    """
    import importlib

    name = f"plugins.{slug}.{LIFECYCLE_MODULE}"
    try:
        module = importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name != name:
            raise
        write(f"Plugin '{slug}' has no {LIFECYCLE_MODULE}.py: no update steps.")
        return
    reprovision = getattr(module, "reprovision", None)
    if callable(reprovision):
        reprovision()
        write("reprovision(): done")
    on_update = getattr(module, "on_update", None)
    if callable(on_update) and not reprovision_only:
        on_update(from_version)
        write(f"on_update({from_version!r}): done")
