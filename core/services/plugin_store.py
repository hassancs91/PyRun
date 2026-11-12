"""
Plugin store: the signed index, install and update from it, install from a URL
(2.5 T44, plan Stage 10 C).

The store is a static, signed ``index.json`` (core/plugins/trust/index.py) and
the zips it lists. ``GlobalSettings.plugin_store_url`` names the index; blank
means PyRun's own (``OFFICIAL_INDEX_URL``). A private index works the same way,
as long as it is signed by a key this PyRun trusts (core/plugins/trust/keys.py).

Like the PyRun update check, the network call runs in a background task (every
12 hours, ``core.tasks.refresh_plugin_store_task``) or when an admin presses
Refresh; pages only read the cached copy (``PluginStoreIndex``), which is
verified again on every read.

Every download goes through the SSRF guard (core/netguard.py, redirects checked
hop by hop) with a size cap: 2 MB for the index, the entry's ``size`` (at most
50 MB) for a zip, whose sha256 must then match the index. Installing hands the
zip to the normal upload path (``plugin_updates.upload``): a new slug installs,
an installed one updates in place, and the signature, publisher pin and trust
policy checks there apply unchanged.
"""

import hashlib
import io
import logging
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from core.models import GlobalSettings, Plugin, PluginStoreIndex, PluginUpdate
from core.plugins.trust.index import MAX_ZIP_BYTES, StoreIndexError, parse_index
from core.plugins.trust.keys import PYRUN_PUBLISHER
from core.services.plugin_service import PluginInstallError, PluginService

logger = logging.getLogger(__name__)

#: PyRun's own store index (the hassancs91/pyrun-plugins repo). Blank until that
#: repo is published; until then the Store tab asks for an index URL.
OFFICIAL_INDEX_URL = ""

INDEX_MAX_BYTES = 2 * 1024 * 1024
#: The background task refreshes the index this often; older is "stale".
CACHE_TTL = timedelta(hours=12)
INDEX_TIMEOUT = 20  # seconds
DOWNLOAD_TIMEOUT = 120  # seconds
USER_AGENT = "PyRun-PluginStore/1.0"

#: Per-process memo of the last verified cache row: (key, StoreIndex | None, problem).
_memo = [None]


class StoreError(PluginInstallError):
    """The store or a download failed; the message is shown to the admin."""


def index_url() -> str:
    """The index this PyRun reads: the setting, else PyRun's official one ('' when neither)."""
    configured = (GlobalSettings.get_settings().plugin_store_url or "").strip()
    return configured or OFFICIAL_INDEX_URL


def _get(url, *, max_bytes, timeout) -> bytes:
    from core.triggers.adapters import _net
    from core.triggers.adapters.base import AdapterError

    try:
        body, _ = _net.get(url, timeout=timeout, max_bytes=max_bytes, user_agent=USER_AGENT)
    except AdapterError as exc:
        raise StoreError(str(exc))
    return body


# --------------------------------------------------------------------------- #
# The index
# --------------------------------------------------------------------------- #

def refresh():
    """Fetch, verify and cache the index now; returns the StoreIndex or raises StoreError.

    A failure is recorded on the cache row (``error``) and keeps the last good
    copy of the same URL.
    """
    url = index_url()
    if not url:
        raise StoreError("No plugin store index is set. Add its URL under Store settings.")
    now = timezone.now()
    row, _ = PluginStoreIndex.objects.get_or_create(pk=1)
    if row.url != url:
        row.url, row.raw, row.fetched_at = url, "", None
    try:
        raw = _get(url, max_bytes=INDEX_MAX_BYTES, timeout=INDEX_TIMEOUT)
        index = parse_index(raw, base_url=url)
    except (StoreError, StoreIndexError) as exc:
        row.checked_at, row.error = now, f"Couldn't load the store index: {exc}"
        row.save()
        logger.warning("Plugin store refresh failed: %s", exc)
        raise StoreError(row.error)
    row.raw, row.fetched_at, row.checked_at, row.error = raw.decode("utf-8"), now, now, ""
    row.save()
    logger.info("Plugin store index refreshed: %d plugins", len(index.entries))
    return index


@dataclass
class StoreState:
    url: str
    row: object  # PluginStoreIndex | None
    index: object  # StoreIndex | None
    problem: str  # why the cached index can't be used ('' when fine)

    @property
    def stale(self) -> bool:
        checked = getattr(self.row, "checked_at", None)
        return self.index is None or checked is None or timezone.now() - checked > CACHE_TTL

    @property
    def error(self) -> str:
        return getattr(self.row, "error", "") or ""


def state() -> StoreState:
    """The cached index for the current URL, verified (no network)."""
    url = index_url()
    row = PluginStoreIndex.objects.filter(pk=1).first()
    if row is None or not url or row.url != url or not row.raw:
        return StoreState(url=url, row=row if row is not None and row.url == url else None,
                          index=None, problem="")
    key = (row.url, row.fetched_at, hashlib.sha256(row.raw.encode("utf-8")).hexdigest())
    memo = _memo[0]
    if memo is None or memo[0] != key:
        try:
            memo = (key, parse_index(row.raw.encode("utf-8"), base_url=row.url), "")
        except StoreIndexError as exc:
            memo = (key, None, f"The cached store index no longer checks out: {exc} Refresh it.")
        _memo[0] = memo
    return StoreState(url=url, row=row, index=memo[1], problem=memo[2])


# --------------------------------------------------------------------------- #
# What each entry means here
# --------------------------------------------------------------------------- #

@dataclass
class StoreItem:
    """An index entry next to what is installed; also the trust badge's ``plugin``."""

    entry: object  # IndexEntry
    installed: object  # Plugin | None
    action: str  # install | update | installed
    problem: str  # why Install/Update is not possible here ('' when it is)

    INSTALL, UPDATE, INSTALLED = "install", "update", "installed"

    # The trust badge partial reads these (cpanel/plugins/_trust_badge.html).
    @property
    def trust(self):
        return Plugin.Trust.VERIFIED if self.entry.signed else Plugin.Trust.UNSIGNED

    @property
    def signed_by(self):
        return self.entry.publisher if self.entry.signed else ""

    @property
    def verified_by_pyrun(self):
        return self.entry.signed and self.entry.publisher == PYRUN_PUBLISHER

    @property
    def provisions_summary(self):
        from core.models.plugin import format_resource_counts

        return format_resource_counts(self.entry.provisions)

    @property
    def secret_keys(self):
        keys = self.entry.provisions.get("secret_keys")
        return [str(k) for k in keys] if isinstance(keys, list) else []


def _problem(entry) -> str:
    from core.services import plugin_trust
    from core.services.plugin_updates import api_version_problem

    return (
        PluginService.min_version_problem({"min_pyrunner": entry.min_pyrunner}, entry.slug)
        or api_version_problem({"api": entry.api}, entry.slug)
        or plugin_trust.policy_problem(
            entry.slug, Plugin.Trust.VERIFIED if entry.signed else Plugin.Trust.UNSIGNED,
        )
        or ""
    )


def item_for(entry, plugin) -> StoreItem:
    from core.services.plugin_doctor import semver_key

    if plugin is None:
        action = StoreItem.INSTALL
    else:
        new, old = semver_key(entry.version), semver_key(plugin.version)
        action = StoreItem.UPDATE if new and old and new > old else StoreItem.INSTALLED
    problem = _problem(entry) if action != StoreItem.INSTALLED else ""
    return StoreItem(entry=entry, installed=plugin, action=action, problem=problem)


def items(index) -> list:
    """Every entry of ``index`` as a StoreItem (index order: by name)."""
    plugins = {p.slug: p for p in Plugin.objects.all()}
    return [item_for(e, plugins.get(e.slug)) for e in index.entries]


def updates_available() -> list:
    """StoreItems whose installed plugin has a newer version in the cached index."""
    current = state()
    if current.index is None:
        return []
    return [i for i in items(current.index) if i.action == StoreItem.UPDATE]


# --------------------------------------------------------------------------- #
# Downloads and installs
# --------------------------------------------------------------------------- #

# How ``_net.get`` starts its "over max_bytes" error.
_TOO_LARGE = "The response is larger than"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def download(entry) -> bytes:
    """The entry's zip: size-capped, sha256 checked against the index."""
    cap = entry.size or MAX_ZIP_BYTES
    try:
        data = _get(entry.download_url, max_bytes=cap, timeout=DOWNLOAD_TIMEOUT)
    except StoreError as exc:
        # The fetch stops at the index's size; say so in bytes, as for a short download
        # (not "larger than 0.0401 MB").
        if entry.size and str(exc).startswith(_TOO_LARGE):
            raise StoreError(
                f"The download of '{entry.slug}' is larger than the {entry.size} bytes the store index says."
            ) from exc
        raise
    if entry.size and len(data) != entry.size:
        raise StoreError(
            f"The download of '{entry.slug}' is {len(data)} bytes; the store index says {entry.size}."
        )
    if _sha256(data) != entry.sha256:
        raise StoreError(
            f"The download of '{entry.slug}' does not match the sha256 in the store index, "
            "so it was not installed."
        )
    return data


def _zip_slug(data: bytes) -> str:
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return PluginService._validate_zip(zf)
    except zipfile.BadZipFile:
        raise StoreError("The download is not a valid .zip archive.")


def install_from_store(slug, *, user=None, initiated_by=PluginUpdate.InitiatedBy.USER):
    """Install ``slug`` from the store, or update it when installed.

    Returns ``(plugin, update_row, warnings)`` like ``PluginService.upload``;
    ``update_row`` is None for a fresh install. Raises PluginInstallError.
    """
    current = state()
    index = current.index
    if index is None:
        if current.problem:
            raise StoreError(current.problem)
        index = refresh()
    entry = index.get(slug)
    if entry is None:
        raise StoreError(f"'{slug}' is not in the plugin store.")
    item = item_for(entry, Plugin.objects.filter(slug=slug).first())
    if item.action == StoreItem.INSTALLED:
        raise StoreError(f"'{slug}' {item.installed.version} is installed; the store has nothing newer.")
    if item.problem:
        raise StoreError(item.problem)
    data = download(entry)
    if _zip_slug(data) != slug:
        raise StoreError(f"The download for '{slug}' holds another plugin, so it was not installed.")
    from core.services import plugin_updates

    return plugin_updates.upload(
        io.BytesIO(data), user=user, source=PluginUpdate.Source.STORE, initiated_by=initiated_by,
    )


def install_from_url(url, *, sha256="", user=None):
    """Install or update a plugin from a zip at ``url`` (same guard and caps as the store).

    ``sha256``, when given, must match the download. Returns what
    ``PluginService.upload`` returns.
    """
    data = _get(url, max_bytes=MAX_ZIP_BYTES, timeout=DOWNLOAD_TIMEOUT)
    expected = (sha256 or "").strip().lower()
    if expected and _sha256(data) != expected:
        raise StoreError("The download does not match the sha256 you gave, so it was not installed.")
    from core.services import plugin_updates

    return plugin_updates.upload(io.BytesIO(data), user=user, source=PluginUpdate.Source.URL)


# --------------------------------------------------------------------------- #
# Automatic updates (plan D10: the job shape exists; off in 2.5)
# --------------------------------------------------------------------------- #

def auto_update() -> dict:
    """Update plugins whose ``update_policy`` is auto, when the hidden flag allows it.

    ``settings.PYRUNNER_PLUGIN_AUTO_UPDATE`` is off and the console offers only
    the manual policy, so in 2.5 this returns ``{"enabled": False}``. Turning
    automatic updates on later is that flag plus the policy choice.
    """
    if not getattr(settings, "PYRUNNER_PLUGIN_AUTO_UPDATE", False):
        return {"enabled": False}
    updated, failed = [], {}
    for item in updates_available():
        if item.installed.update_policy != Plugin.UpdatePolicy.AUTO or item.problem:
            continue
        try:
            install_from_store(item.entry.slug, initiated_by=PluginUpdate.InitiatedBy.AUTO)
            updated.append(item.entry.slug)
        except PluginInstallError as exc:
            failed[item.entry.slug] = str(exc)
            logger.warning("Automatic update of plugin %r failed: %s", item.entry.slug, exc)
    return {"enabled": True, "updated": updated, "failed": failed}
