"""
Inbound attachments, downloaded for the script that handles them (2.5 Stage 8).

With a channel's "Download media" setting on (``Channel.download_media``, off
by default) the worker fetches every attachment of an inbound message routed
to a script, while it dispatches the message (WhatsApp's download link
expires about five minutes after the message arrives), and hands it to the
run in the event's ``media`` list, each item keeping its metadata (``kind``,
``id``, ``mime``, ``caption``, ``filename``) and gaining ``size`` and one of:

- ``url``: the file was stored through the storage seam, the assets storage
  connection (Services → Object Storage), when one is set and private. The
  object sits under ``apps/pyrun-channels/<workspace>/<provider>/<channel>/<run>/``
  and the link is presigned, valid for ``URL_TTL``. A public connection
  (one with a public base URL) is skipped: customers' files never get a
  permanent public link.
- ``path``: otherwise, a run-scoped file in the script's working folder,
  ``.pyrun-media-<run id>/<name>``, readable by the run (the sandbox binds that
  folder) and removed when the run ends.

A file over the channel's cap (``media_max_mb``, 16 MB by default) or one that
can't be fetched keeps its metadata and gets an ``error`` sentence instead.
With the setting off, or a Py AI or notify-only channel, nothing is downloaded
and nothing is written: Py AI can't open files, it is told what was attached
(``describe``).

Leftovers (a run that was skipped or cancelled before it started, a crash)
go in the daily housekeeping (``purge``): run folders whose run has ended or
is gone, and stored files older than the delivery ledger's retention.
"""

import logging
import os
import re
import shutil
import uuid
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from .base import ChannelError

logger = logging.getLogger(__name__)

# The storage seam's owner for channel files. A hyphen, so no plugin slug
# (``^[a-z][a-z0-9_]*$``) can name it and a plugin uninstall never touches it.
OWNER = "pyrun-channels"
DIR_PREFIX = ".pyrun-media-"
URL_TTL = 24 * 3600  # seconds a stored file's presigned link works
MB = 1024 * 1024
DEFAULT_MAX_MB = 16
MAX_MAX_MB = 100  # WhatsApp's own limit for a document
# A run folder younger than this is never purged: the handler writes it just
# before it creates the run.
_LEFTOVER_GRACE = timedelta(hours=1)

_EXTENSIONS = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
    "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/aac": ".aac", "audio/amr": ".amr",
    "video/mp4": ".mp4", "video/3gpp": ".3gp",
    "application/pdf": ".pdf", "text/plain": ".txt", "text/csv": ".csv",
    "application/msword": ".doc", "application/vnd.ms-excel": ".xls", "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
}
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _hex(run_id) -> str:
    return run_id.hex if isinstance(run_id, uuid.UUID) else uuid.UUID(str(run_id)).hex


def run_dir(script_id, run_id) -> Path:
    """The run's own folder for downloaded files, inside the script's working folder."""
    from core.executor import script_workdir

    return script_workdir(script_id) / f"{DIR_PREFIX}{_hex(run_id)}"


def _filename(item: dict, mime: str, used: set) -> str:
    """A safe file name: the document's own name, else the media id + extension."""
    name = _UNSAFE_CHARS.sub("_", os.path.basename(item.get("filename") or "")).lstrip("._")[:100]
    if not name:
        base = _UNSAFE_CHARS.sub("_", str(item.get("id") or "")).lstrip("._")[:64] or "file"
        name = base + _EXTENSIONS.get(mime.split(";")[0].strip().lower(), ".bin")
    candidate, n = name, 1
    while candidate in used:
        n += 1
        candidate = f"{n}-{name}"
    used.add(candidate)
    return candidate


def _storage(channel):
    """The storage seam for this channel's workspace, or None (unset, or public)."""
    from core.plugins.api import StorageAPI
    from core.services.s3_service import S3Service

    connection = S3Service.for_assets()
    if connection is None or connection.is_public:
        return None
    storage = StorageAPI(OWNER, workspace=channel.workspace_id)
    return storage if storage.is_available() else None


def _write_private(folder: Path, name: str, data: bytes) -> Path:
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = folder / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return path


def fetch_for_run(channel, msg, *, script_id, run_id) -> list:
    """The message's ``media`` list, each attachment downloaded (see the module doc).

    A copy of ``msg.media`` unchanged when the setting is off, the message has
    no attachments, or the provider can't download. Never raises: a failure is
    the item's ``error``.
    """
    from core.plugins.api import StorageError
    from core.services.channels import get_provider

    media = [dict(item) for item in (msg.media or [])]
    if not (channel.download_media and media):
        return media
    provider = get_provider(channel.provider)
    if not getattr(provider, "supports_media_download", False):
        return media
    max_bytes = max(1, min(channel.media_max_mb or DEFAULT_MAX_MB, MAX_MAX_MB)) * MB
    storage = _storage(channel)
    used = set()
    for item in media:
        try:
            data, mime = provider.download_media(channel, item, max_bytes)
        except ChannelError as e:
            item["error"] = str(e)
            continue
        except Exception:  # noqa: BLE001 — one bad attachment must not stop the run
            logger.exception("Downloading an attachment on channel %s failed", channel.id)
            item["error"] = "Not downloaded: an unexpected error (see the server log)."
            continue
        item["mime"] = item.get("mime") or mime
        item["size"] = len(data)
        name = _filename(item, mime, used)
        if storage is not None:
            key = f"{channel.provider}/{channel.id.hex}/{_hex(run_id)}/{name}"
            try:
                storage.put(key, data, content_type=mime)
                url = storage.url(key, expires_in=URL_TTL)
            except StorageError as e:
                url = None
                logger.warning("Storing an attachment on channel %s failed, keeping it as a file: %s", channel.id, e)
            if url:
                item["url"] = url
                continue
        try:
            item["path"] = str(_write_private(run_dir(script_id, run_id), name, data))
        except OSError as e:
            logger.warning("Writing an attachment for run %s failed: %s", _hex(run_id), e)
            item["error"] = f"Not stored: {e}"
    return media


def discard(channel, script_id, run_id, media: list) -> None:
    """Remove a run's downloaded files (the run won't start). Never raises."""
    if any("path" in item for item in media):
        shutil.rmtree(run_dir(script_id, run_id), ignore_errors=True)
    if any("url" in item for item in media):
        try:
            storage = _storage(channel)
            if storage is not None:
                for obj in storage.list(f"{channel.provider}/{channel.id.hex}/{_hex(run_id)}/"):
                    storage.delete(obj["key"])
        except Exception as e:  # noqa: BLE001 — the daily purge removes it later
            logger.warning("Removing a skipped run's stored attachments failed: %s", e)


def remove_run_files(script_id, run_id) -> None:
    """Delete the run's folder of downloaded files, if any (the run ended). Never raises."""
    try:
        folder = run_dir(script_id, run_id)
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
            if folder.exists():
                logger.warning("Failed to delete the run's attachments folder: %s", folder)
    except (ValueError, TypeError, OSError) as e:
        logger.warning("Removing run %s's attachments failed: %s", run_id, e)


def describe(media: list) -> str:
    """The attachments as one line for Py AI, which can't open files ("" when none)."""
    parts = []
    for item in media or []:
        bits = [item.get("kind") or "file"]
        if item.get("filename"):
            bits.append(f'"{item["filename"]}"')
        if item.get("mime"):
            bits.append(f"({item['mime']})")
        parts.append(" ".join(bits))
    if not parts:
        return ""
    return "[The sender attached: " + "; ".join(parts) + ". You can't open attachments.]"


def purge(now=None) -> int:
    """Delete leftover attachments; returns how many folders and files went.

    Run folders whose run has ended or is gone (older than an hour, so one
    being written for a run about to be created is kept), and stored files
    older than the delivery ledger's retention. Each half fails on its own.
    """
    from core.models import Run, Workspace

    from . import ledger

    now = now or timezone.now()
    deleted = 0
    root = Path(settings.SCRIPTS_WORKDIR)
    try:
        folders = list(root.glob(f"*/{DIR_PREFIX}*")) if root.is_dir() else []
    except OSError as e:
        logger.warning("Listing leftover attachment folders failed: %s", e)
        folders = []
    live = {Run.Status.PENDING, Run.Status.RUNNING}
    for folder in folders:
        try:
            run_id = uuid.UUID(folder.name[len(DIR_PREFIX):])
        except ValueError:
            continue
        try:
            if now.timestamp() - folder.stat().st_mtime < _LEFTOVER_GRACE.total_seconds():
                continue
            if Run.objects.filter(id=run_id, status__in=live).exists():
                continue
            shutil.rmtree(folder)
            deleted += 1
        except OSError as e:  # one locked folder doesn't stop the others
            logger.warning("Purging the attachments folder %s failed: %s", folder, e)

    from core.plugins.api import StorageAPI
    from core.services.s3_service import S3Service

    connection = S3Service.for_assets()
    if connection is None or not connection.enabled or not connection.is_configured:
        return deleted
    cutoff = now - timedelta(days=ledger.RETENTION_DAYS)
    # Every workspace, not only those with a channel now: a deleted channel's
    # files still go after the retention.
    for workspace_id in Workspace.objects.values_list("id", flat=True):
        storage = StorageAPI(OWNER, workspace=workspace_id)
        if not storage.is_available():
            continue
        try:
            for obj in storage.list():
                modified = obj.get("last_modified")
                if modified is not None and modified < cutoff and storage.delete(obj["key"]):
                    deleted += 1
        except Exception as e:  # noqa: BLE001 — housekeeping never fails on storage
            logger.warning("Purging stored attachments of workspace %s failed: %s", workspace_id, e)
    return deleted
