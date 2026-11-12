"""
Tables (2.5 Stage 9): schema-in-UI data tables with JSON rows.

A ``Table`` is a named list of typed columns in one workspace; its rows are
``TableRow`` objects whose ``data`` holds ``{column key: value}``. Rows live in
PyRun's own database (SQLite or Postgres, the same code on both), so a table
needs nothing provisioned. Larger or relational data belongs in a managed
Database; the per-table row cap (``PYRUNNER_TABLE_ROW_CAP``, 100 000 by
default) is where the console points there.

Column types, value coercion and the filter language live in
``core.tables``; every write goes through ``core.tables.service.TableService``
so ``row_count`` stays exact.
"""

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from .workspace import WorkspaceScopedManager


class Table(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "core.Workspace", on_delete=models.CASCADE, related_name="tables",
    )
    name = models.CharField(max_length=100, help_text="Display name, unique per workspace")
    slug = models.SlugField(
        max_length=100,
        help_text="Handle scripts use: pyrunner_tables.table(\"<slug>\"), unique per workspace",
    )
    columns = models.JSONField(
        default=list, blank=True,
        help_text="[{key, type, required, default}]; type is text|number|boolean|date|datetime|json|link",
    )
    description = models.TextField(blank=True)
    # Plugin ownership, as on DataStore / VectorCollection (NULL = a user table).
    owner_plugin = models.CharField(max_length=100, null=True, blank=True, db_index=True)
    owner_key = models.CharField(max_length=100, null=True, blank=True, db_index=True)
    # Kept exact by TableService (conditional UPDATEs in the same transaction
    # as the row writes); the row cap is checked against it.
    row_count = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="created_tables",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "tables"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["workspace", "slug"], name="uniq_table_workspace_slug"),
            models.UniqueConstraint(fields=["workspace", "name"], name="uniq_table_workspace_name"),
        ]

    def __str__(self):
        return self.name

    @property
    def row_cap(self) -> int:
        return settings.PYRUNNER_TABLE_ROW_CAP

    @property
    def at_cap(self) -> bool:
        return self.row_count >= self.row_cap

    @property
    def near_cap(self) -> bool:
        """90 % full or more (the console warns before inserts start failing)."""
        return self.row_count >= self.row_cap * 0.9

    @classmethod
    def resolve_for_workspace(cls, handle: str, workspace_id):
        """The table whose slug (else name) is ``handle`` in the workspace.

        Raises ``Table.DoesNotExist``. ``workspace_id`` always comes from the
        server (the run's or the console's workspace), never from the script.
        """
        if workspace_id is None:
            raise cls.DoesNotExist(handle)
        found = cls.objects.filter(workspace_id=workspace_id, slug=handle).first()
        if found is None:
            found = cls.objects.filter(workspace_id=workspace_id, name=handle).first()
        if found is None:
            raise cls.DoesNotExist(handle)
        return found


class TableRow(models.Model):
    """One row. ``data`` maps column keys to values already coerced to the column type.

    The timestamps are plain fields (not auto_now) so a restore keeps them;
    TableService stamps ``updated_at`` on every write.
    """

    id = models.BigAutoField(primary_key=True)
    table = models.ForeignKey(Table, on_delete=models.CASCADE, related_name="rows")
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )

    class Meta:
        db_table = "table_rows"
        ordering = ["id"]
        indexes = [
            models.Index(fields=["table", "updated_at"], name="table_rows_updated_idx"),
            models.Index(fields=["table", "created_at"], name="table_rows_created_idx"),
        ]

    def __str__(self):
        return f"{self.table_id}#{self.id}"
