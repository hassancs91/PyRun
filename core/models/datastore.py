"""
DataStore models for script data persistence.
"""

import json
import uuid

from django.conf import settings
from django.db import models

from .workspace import Workspace, WorkspaceScopedManager


class DataStore(models.Model):
    """
    A named data store that scripts can use for simple key-value storage.
    Data stores are backed by the instance SQLite database.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Tenancy Decision 2B: ``name`` is unique PER WORKSPACE (not globally), so two
    # workspaces can each own a store called "results". Every by-name resolver
    # (cpanel, script helper, internal API) scopes the lookup to a workspace; see
    # ``resolve_for_workspace``. The external API (core/api/endpoints/datastores.py)
    # matches its token's workspace only, without the unassigned-store fallback.
    name = models.CharField(
        max_length=100,
        help_text="Name for this data store (used in scripts), unique per workspace",
    )

    # Tenancy: nullable + backfilled to the default workspace for upgrade-safety;
    # queries scope through WorkspaceScopedManager (.for_workspace).
    workspace = models.ForeignKey(
        "core.Workspace",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_index=True,
        related_name="datastores",
        help_text="Workspace this resource belongs to (tenancy seam; nullable).",
    )

    objects = WorkspaceScopedManager()

    # Plugin ownership (Plugin Platform v2, WS3). Nullable: NULL = a normal
    # user/system store. These are GROUPING/cleanup metadata only — they are
    # NEVER part of the uniqueness key (the by-name helper + REST API depend on
    # ``name`` resolving to exactly one row per workspace). The SDK avoids
    # cross-plugin collisions by AUTO-NAMING the store ``"<owner_plugin>:<owner_key>"``,
    # keeping ``name`` unique while the plugin refers to it by its short key.
    owner_plugin = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        db_index=True,
        help_text="Slug of the plugin that owns this data store (NULL = user/system).",
    )
    owner_key = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        db_index=True,
        help_text="Stable per-owner handle for idempotent upsert (NULL = unmanaged).",
    )

    description = models.TextField(
        blank=True,
        help_text="Optional description of what this data store is used for",
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Who created this data store
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_datastores",
    )

    class Meta:
        db_table = "datastores"
        verbose_name = "data store"
        verbose_name_plural = "data stores"
        ordering = ["name"]
        constraints = [
            # Per-workspace uniqueness (NULLs are SQL-distinct, so the partial
            # constraint below reproduces "globally unique among un-scoped rows").
            models.UniqueConstraint(
                fields=["workspace", "name"],
                name="uniq_datastore_workspace_name",
            ),
            models.UniqueConstraint(
                fields=["name"],
                condition=models.Q(workspace__isnull=True),
                name="uniq_datastore_name_when_no_workspace",
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def entry_count(self) -> int:
        """Return the number of entries in this data store."""
        return self.entries.count()

    @classmethod
    def resolve_for_workspace(cls, name: str, workspace_id):
        """Resolve a datastore by ``name`` within a workspace (tenancy Decision 2B).

        Names are unique per workspace, so this returns at most one row. The
        back-compat rule keeps a single-workspace / pre-tenancy instance
        byte-for-byte:
        - an un-scoped lookup (``workspace_id`` None) resolves only unassigned
          stores, never the default workspace's (fail closed, F129; the 0035
          backfill leaves no un-scoped run or token on an upgraded instance);
        - if no store exists in the resolved workspace, fall back to a
          still-unassigned (``workspace IS NULL``) store created before scoping.

        Raises ``DataStore.DoesNotExist`` if neither exists. The workspace source
        is always trusted/server-derived (the run's workspace or the active
        workspace), never a value the script supplied.
        """
        # Fail closed (F129): no workspace resolves only still-unassigned rows,
        # never the default workspace's.

        if workspace_id is not None:
            try:
                return cls.objects.get(name=name, workspace_id=workspace_id)
            except cls.DoesNotExist:
                pass
        return cls.objects.get(name=name, workspace__isnull=True)


class DataStoreEntry(models.Model):
    """
    A key-value entry in a data store.
    Values are stored as JSON to support various data types.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    datastore = models.ForeignKey(
        DataStore,
        on_delete=models.CASCADE,
        related_name="entries",
    )

    key = models.CharField(
        max_length=255,
        help_text="Unique key within this data store",
    )

    # Value stored as JSON text (supports strings, numbers, lists, dicts, etc.)
    value_json = models.TextField(
        help_text="JSON-encoded value",
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "datastore_entries"
        verbose_name = "data store entry"
        verbose_name_plural = "data store entries"
        unique_together = [["datastore", "key"]]
        ordering = ["key"]
        indexes = [
            models.Index(fields=["datastore", "key"]),
        ]

    def __str__(self):
        return f"{self.datastore.name}:{self.key}"

    def get_value(self):
        """Deserialize and return the stored value."""
        return json.loads(self.value_json)

    def set_value(self, value) -> None:
        """Serialize and store a value."""
        self.value_json = json.dumps(value)

    def get_display_value(self) -> str:
        """Return a displayable version of the value (truncated if too long)."""
        value = self.value_json
        if len(value) > 100:
            return value[:100] + "..."
        return value
