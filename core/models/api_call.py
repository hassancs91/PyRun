"""
One row per audited call to the unified API and MCP (2.5 Stage 2, T13).

``core.services.api_audit.record`` writes a row for every call that isn't a
plain read (``scripts.run``, ``runs.stop``, writes) and for every failure of
an authenticated call, whatever its class. Rows older than
``api_audit.RETENTION_DAYS`` are deleted by the daily housekeeping task.
The token's Activity page in the console lists them.

Not in backups: backups don't carry API tokens, and this is their log.
"""

from django.db import models


class ApiCall(models.Model):
    # Kept (token NULL) when the token is revoked, until the purge. No console
    # page lists those rows yet; they go with the workspace (CASCADE below).
    token = models.ForeignKey(
        "core.APIToken",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="api_calls",
    )
    workspace = models.ForeignKey(
        "core.Workspace",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="api_calls",
    )
    # The endpoint slug (``scripts.run``), or what was asked for when it isn't
    # one: an unknown REST slug, an MCP method (``mcp:tools/list``).
    slug = models.CharField(max_length=64)
    ok = models.BooleanField(default=False)
    # The HTTP status the call answered with; for an MCP tool call, the status
    # the same call gets over REST (a tool error is HTTP 200 over MCP).
    status = models.PositiveSmallIntegerField(default=200)
    # The error code (``rate_limited``, ``writes_disabled``...), blank when ok.
    code = models.CharField(max_length=64, blank=True, default="")
    ms = models.PositiveIntegerField(default=0)
    ip = models.CharField(max_length=64, blank=True, default="")
    request_id = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "core_api_call"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["token", "-created_at"], name="apicall_token_recent")]

    def __str__(self):
        return f"{self.slug} {self.status} ({self.created_at:%Y-%m-%d %H:%M:%S})"
