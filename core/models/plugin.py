"""
Plugin registry model.

Tracks installed plugins and their lifecycle state. The DB is the source of
truth for *what should load*; the filesystem (``PLUGINS_DIR``) is the source of
truth for *what code exists*. Only ``status=ACTIVE`` plugins are ever imported
into the running process (see the guarded loader in ``pyrunner/settings.py``).
"""

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from core.plugins import is_valid_plugin_slug


def validate_plugin_slug(value: str) -> None:
    if not is_valid_plugin_slug(value):
        raise ValidationError(
            "Slug must start with a lowercase letter and contain only "
            "lowercase letters, digits, and underscores."
        )


# The resources a plugin can declare (plugin.json ``provisions``) or actually own
# (``PluginService.owned_resource_counts``). ONE table feeding both summaries, so
# a new resource type can't be added to one and forgotten in the other — which is
# exactly how the delete-confirm preview came to under-report what it deletes.
# Both plural forms are spelled out: appending "s" would render "librarys".
RESOURCE_LABELS = (
    ("scripts", "script", "scripts"),
    ("secrets", "secret", "secrets"),
    ("datastores", "data store", "data stores"),
    ("databases", "database", "databases"),
    ("libraries", "library", "libraries"),
    ("vector_collections", "vector collection", "vector collections"),
    ("tables", "table", "tables"),
    ("brains", "brain", "brains"),
    ("schedules", "schedule", "schedules"),
)


def format_resource_counts(counts) -> str:
    """Human one-liner from a ``{resource: n}`` dict, e.g. '1 script, 2 libraries'.

    Zero / absent / non-int values are skipped (``provisions`` may carry a
    ``secret_keys`` list and owned counts carry a ``total``), so the string only
    ever names what is really there. Returns '' when nothing is.
    """
    if not isinstance(counts, dict):
        return ""
    parts = []
    for key, singular, plural in RESOURCE_LABELS:
        n = counts.get(key)
        if isinstance(n, int) and not isinstance(n, bool) and n > 0:
            parts.append(f"{n} {singular if n == 1 else plural}")
    return ", ".join(parts)


class Plugin(models.Model):
    """A plugin known to PyRun (its files live on the data volume)."""

    class Status(models.TextChoices):
        INSTALLED = "installed", "Installed"  # files on disk, not loaded
        ACTIVE = "active", "Active"  # loaded into the running process
        DISABLED = "disabled", "Disabled"  # deactivated by an admin (data kept)
        ERRORED = "errored", "Errored"  # failed preflight/boot; quarantined

    class Source(models.TextChoices):
        UPLOAD = "upload", "Upload"
        GIT = "git", "Git"
        BUILTIN = "builtin", "Built-in"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    slug = models.CharField(
        max_length=100,
        unique=True,
        validators=[validate_plugin_slug],
        help_text="Matches the plugin folder name and its app_name.",
    )
    name = models.CharField(max_length=200)
    version = models.CharField(max_length=50, blank=True, default="")

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.INSTALLED,
    )
    source = models.CharField(
        max_length=20,
        choices=Source.choices,
        default=Source.UPLOAD,
    )

    error_message = models.TextField(
        blank=True,
        default="",
        help_text="Last preflight/boot failure (shown in the UI).",
    )
    manifest = models.JSONField(
        default=dict,
        blank=True,
        help_text="Parsed plugin.json contents.",
    )
    checksum = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Checksum of the unpacked folder (integrity check).",
    )

    # In-place update (2.5 T42): the version this one replaced, and its folder's
    # checksum. Rollback moves that folder back from ``plugins/.previous/`` and
    # checks it against this checksum first. Blank when there is nothing to roll
    # back to (a fresh install, or right after a rollback).
    previous_version = models.CharField(max_length=50, blank=True, default="")
    previous_checksum = models.CharField(max_length=64, blank=True, default="")

    # Signing (2.5 T43): what the installed folder's MANIFEST.sha256 + plugin.sig
    # proved (core/plugins/trust). ``pinned_publisher`` is set the first time a
    # signed version is installed and stays: from then on an update must be
    # signed by a key of that publisher (a new key id of the same publisher is a
    # rotation; unsigned or another publisher is refused).
    class Trust(models.TextChoices):
        VERIFIED = "verified", "Verified"
        UNSIGNED = "unsigned", "Unsigned"

    trust = models.CharField(max_length=20, choices=Trust.choices, default=Trust.UNSIGNED)
    signed_by = models.CharField(
        max_length=100, blank=True, default="",
        help_text="Publisher whose key signed the installed version (blank when unsigned).",
    )
    signing_key_id = models.CharField(max_length=64, blank=True, default="")
    pinned_publisher = models.CharField(
        max_length=100, blank=True, default="",
        help_text="Once installed signed, updates must be signed by this publisher.",
    )

    installed_at = models.DateTimeField(auto_now_add=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "plugins"
        ordering = ["name"]
        verbose_name = "plugin"
        verbose_name_plural = "plugins"

    def __str__(self):
        return f"{self.name} ({self.slug}) [{self.status}]"

    @property
    def is_active(self) -> bool:
        return self.status == self.Status.ACTIVE

    @property
    def verified_by_pyrun(self) -> bool:
        """Signed with one of PyRun's own keys: the "Verified · PyRun" badge."""
        from core.plugins.trust.keys import PYRUN_PUBLISHER

        return self.trust == self.Trust.VERIFIED and self.signed_by == PYRUN_PUBLISHER

    # ------------------------------------------------------------- metadata
    # Plugin Platform v2 Stage 6: the manifest (plugin.json) is the single carrier
    # for marketplace-prep metadata. It is stored verbatim in ``manifest`` on
    # install, so these read-only accessors expose new fields with NO migration.
    # Every field is optional; missing ⇒ "" / [] / {} (the console renders calmly).

    def manifest_value(self, key, default=""):
        """Read a key from the manifest JSONField (the Stage 6 metadata carrier)."""
        manifest = self.manifest if isinstance(self.manifest, dict) else {}
        value = manifest.get(key, default)
        return value if value is not None else default

    @property
    def summary(self) -> str:
        """Short tagline for cards (falls back to the long description)."""
        return self.manifest_value("summary") or self.manifest_value("description")

    @property
    def description(self) -> str:
        return self.manifest_value("description")

    @property
    def author(self) -> str:
        return self.manifest_value("author")

    @property
    def author_url(self) -> str:
        return self.manifest_value("author_url")

    @property
    def publisher(self) -> str:
        return self.manifest_value("publisher")

    @property
    def license(self) -> str:
        return self.manifest_value("license")

    @property
    def homepage(self) -> str:
        return self.manifest_value("homepage")

    @property
    def repository(self) -> str:
        return self.manifest_value("repository")

    @property
    def documentation(self) -> str:
        return self.manifest_value("documentation")

    @property
    def categories(self) -> list:
        value = self.manifest_value("categories", [])
        return [str(c) for c in value] if isinstance(value, list) else []

    @property
    def keywords(self) -> list:
        value = self.manifest_value("keywords", [])
        return [str(k) for k in value] if isinstance(value, list) else []

    @property
    def has_icon(self) -> bool:
        """True when the manifest declares a bundled icon file path."""
        return bool(self.manifest_value("icon"))

    @property
    def icon_fallback(self) -> str:
        """Emoji shown when there's no bundled icon (or it fails to load)."""
        return self.manifest_value("icon_fallback")

    @property
    def icon_url(self):
        """URL of the bundled-icon serve view, or None when no icon is declared."""
        if not self.has_icon:
            return None
        from django.urls import reverse

        return reverse("cpanel:plugin_icon", args=[self.slug])

    @property
    def provisions(self) -> dict:
        """Declared resources the plugin creates (counts + secret_keys)."""
        value = self.manifest_value("provisions", {})
        return value if isinstance(value, dict) else {}

    @property
    def provisions_summary(self) -> str:
        """Human one-liner of DECLARED resources, e.g. '1 script, 3 secrets'."""
        return format_resource_counts(self.provisions)

    def mark_errored(self, message: str) -> None:
        """Quarantine this plugin so the next boot will not load it."""
        self.status = self.Status.ERRORED
        self.error_message = message or "Plugin failed preflight."
        self.save(update_fields=["status", "error_message", "updated_at"])


class PluginUpdate(models.Model):
    """One in-place update or rollback of a plugin (2.5 T42), newest first.

    ``outcome`` follows the update through: ``refused`` (nothing changed), or the
    folder was swapped and the update steps (the plugin's ``lifecycle.py``:
    ``reprovision()`` then ``on_update(from_version)``) wait for the next restart
    while the plugin is active (``pending`` -> ``running`` -> ``ok`` /
    ``steps_failed``). A rollback before they ran ``cancelled`` them.
    """

    class Source(models.TextChoices):
        UPLOAD = "upload", "Upload"
        STORE = "store", "Store"
        URL = "url", "URL"
        ROLLBACK = "rollback", "Rollback"

    class InitiatedBy(models.TextChoices):
        USER = "user", "User"
        AUTO = "auto", "Automatic"

    class Outcome(models.TextChoices):
        PENDING = "pending", "Waiting for a restart"
        RUNNING = "running", "Running update steps"
        OK = "ok", "Done"
        STEPS_FAILED = "steps_failed", "Update steps failed"
        REFUSED = "refused", "Refused"
        CANCELLED = "cancelled", "Cancelled by a rollback"

    #: Outcomes where the plugin's folder was swapped (everything but a refusal).
    SWAPPED = (
        Outcome.PENDING, Outcome.RUNNING, Outcome.OK, Outcome.STEPS_FAILED, Outcome.CANCELLED,
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plugin = models.ForeignKey(Plugin, on_delete=models.CASCADE, related_name="updates")
    from_version = models.CharField(max_length=50, blank=True, default="")
    to_version = models.CharField(max_length=50, blank=True, default="")
    from_checksum = models.CharField(max_length=64, blank=True, default="")
    to_checksum = models.CharField(max_length=64, blank=True, default="")
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.UPLOAD)
    initiated_by = models.CharField(
        max_length=10, choices=InitiatedBy.choices, default=InitiatedBy.USER
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    outcome = models.CharField(max_length=20, choices=Outcome.choices, default=Outcome.PENDING)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "plugin_updates"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["plugin", "outcome"], name="plugin_upd_plugin_outcome")]

    def __str__(self):
        return f"{self.plugin_id}: {self.from_version} -> {self.to_version} [{self.outcome}]"
