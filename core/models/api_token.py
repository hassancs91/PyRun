"""
API tokens for datastore, plugin and registry endpoints.
"""

import hashlib
import re
import secrets
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class APIToken(models.Model):
    """
    API token for accessing PyRun data via the external REST API.

    Scopes (least privilege, one scope per token):
    - ``datastore``  — a single datastore (the FK).
    - ``datastores`` — all datastores in the token's workspace (legacy "global").
    - ``plugin``     — one plugin's declared API resources (``plugin_slug``).
    - ``api``        — registry permissions, with an optional brain, script or
                       datastore binding (at most one).

    Historically named ``DataStoreAPIToken`` (the alias survives in
    ``core.models``); the table name is pinned so the rename touched no SQL.
    """

    class Scope(models.TextChoices):
        DATASTORE = "datastore", "Single datastore"
        DATASTORES = "datastores", "All datastores"
        PLUGIN = "plugin", "Plugin API"
        API = "api", "Unified API / MCP"

    class Kind(models.TextChoices):
        BEARER = "bearer", "Authorization header"
        URL = "url", "MCP URL (read-only)"

    API_PERMISSIONS = (
        ("brain:read", "Read brains"),
        ("brain:write", "Propose brain updates"),
        ("scripts:read", "Read scripts (metadata and inputs, never code)"),
        ("runs:read", "Read runs (status, inputs, result, event)"),
        ("runs:output", "Read run output (stdout and stderr, secrets masked)"),
        ("scripts:run", "Run scripts (with inputs; can wait for the result)"),
        ("runs:stop", "Stop runs"),
        ("datastores:read", "Read datastores (entries and their values)"),
        ("datastores:write", "Write datastore entries (set and delete)"),
    )
    TIERS = (("public", "Public"), ("agents-only", "Agents only"), ("private", "Private"))
    # Which permissions are write permissions comes from the registry: those
    # guarding write-class endpoints (core.api.registry.write_permissions).
    WRITES_OFF_GRANT = (
        "API writes are turned off, so write permissions can't be granted. "
        "A superuser can turn them on in Settings → API & MCP."
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Every scope stores only the SHA-256 digest of its token (API scope since
    # 2.0's Unified API; datastore/plugin scopes since F53 — migration 0065
    # hashed the plaintext rows written by 1.x). The raw value is shown once.
    token = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        help_text="API token value (auto-generated)",
    )

    # Friendly name to identify this token
    name = models.CharField(
        max_length=100,
        help_text="Friendly name for this token",
    )

    # What this token can reach. Enforced by CheckConstraints below:
    # scope="datastore" ⇔ the datastore FK is set; scope="plugin" ⇔ plugin_slug
    # is non-empty. A legacy pre-scope row was backfilled from its FK.
    scope = models.CharField(
        max_length=16,
        choices=Scope.choices,
        default=Scope.DATASTORES,
        help_text="What this token grants access to",
    )

    # The one datastore this token reaches: a single-store token's target, or
    # a Unified API token's optional binding (2.5; CASCADE like the other
    # bindings). NULL = every datastore in the token's workspace.
    datastore = models.ForeignKey(
        "DataStore",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="api_tokens",
        help_text="If set, the token reaches only this datastore. Leave empty for every datastore in its workspace.",
    )

    # Plugin scope target. Deliberately NOT an FK: plugins aren't rows (dev-mode
    # plugins have no Plugin row at all); validated against loaded plugins at
    # creation time only. A token for a later-removed plugin 404s at dispatch.
    plugin_slug = models.CharField(
        max_length=100,
        blank=True,
        default="",
        db_index=True,
        help_text="Plugin this token is scoped to (scope=plugin only)",
    )

    permissions = models.JSONField(default=list, blank=True)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.BEARER)
    token_hint = models.CharField(max_length=16, blank=True, default="")
    brain = models.ForeignKey("Brain", on_delete=models.CASCADE, null=True, blank=True,
                              related_name="api_tokens")
    brain_tier = models.CharField(max_length=16, choices=TIERS, default="public")
    # Optional binding to one script (2.5): the token then sees that script and
    # its runs only. CASCADE, like the brain binding: a token whose script is
    # deleted must never fall back to seeing every script.
    script = models.ForeignKey("Script", on_delete=models.CASCADE, null=True, blank=True,
                               related_name="api_tokens")

    # Tenancy Stage 3: the workspace this token acts in. A global (no-datastore)
    # token lists/resolves only this workspace's datastores (a single-store token
    # acts in its store's). Every scope fails closed: no workspace → 403, never
    # the default workspace (datastore tokens since 2.5).
    workspace = models.ForeignKey(
        "core.Workspace",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_index=True,
        related_name="api_tokens",
        help_text="Workspace this token is scoped to (tenancy; nullable).",
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Last time this token was used",
    )
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Optional expiration date. Leave empty for no expiration.",
    )

    # Who created this token
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_api_tokens",
    )

    # Active flag for soft-disable
    is_active = models.BooleanField(
        default=True,
        help_text="Inactive tokens cannot be used for API access",
    )

    class Meta:
        db_table = "datastore_api_tokens"
        verbose_name = "API token"
        verbose_name_plural = "API tokens"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(scope="api") & ~models.Q(permissions=[]))
                | (~models.Q(scope="api") & models.Q(permissions=[])),
                name="apitoken_api_permissions",
            ),
            models.CheckConstraint(
                condition=~models.Q(kind="url") | models.Q(scope="api", expires_at__isnull=False),
                name="apitoken_url_expiry",
            ),
            models.CheckConstraint(
                condition=models.Q(scope="api") | models.Q(brain__isnull=True, kind="bearer"),
                name="apitoken_api_binding",
            ),
            # At most one binding: a script-bound token is a Unified API token
            # without a brain binding.
            models.CheckConstraint(
                condition=models.Q(script__isnull=True)
                | models.Q(scope="api", brain__isnull=True),
                name="apitoken_one_binding",
            ),
            # scope="datastore" ⇒ datastore FK set; any other scope carries one
            # only as a Unified API token's sole binding (integrity, not just form
            # validation — a plugin/global token must never carry a store FK).
            models.CheckConstraint(
                condition=(
                    models.Q(scope="datastore", datastore__isnull=False)
                    | (~models.Q(scope="datastore") & models.Q(datastore__isnull=True))
                    | models.Q(scope="api", brain__isnull=True, script__isnull=True)
                ),
                name="apitoken_scope_datastore_fk",
            ),
            # scope="plugin" ⇔ plugin_slug non-empty.
            models.CheckConstraint(
                condition=(
                    (models.Q(scope="plugin") & ~models.Q(plugin_slug=""))
                    | (~models.Q(scope="plugin") & models.Q(plugin_slug=""))
                ),
                name="apitoken_scope_plugin_slug",
            ),
        ]

    def __str__(self):
        if self.scope == self.Scope.API:
            return f"{self.name} ({self.scope_display})"
        if self.scope == self.Scope.PLUGIN:
            return f"{self.name} (plugin: {self.plugin_slug})"
        if self.datastore:
            return f"{self.name} ({self.datastore.name})"
        return f"{self.name} (global)"

    def save(self, *args, **kwargs):
        # Legacy creation sites set only the datastore FK (pre-scope shape);
        # derive the single-store scope so those callers keep working unchanged.
        # An explicit plugin scope with an FK still hits the CheckConstraint.
        if self.datastore_id and self.scope == self.Scope.DATASTORES:
            self.scope = self.Scope.DATASTORE
        if self.scope == self.Scope.API:
            self.clean()
            if self.token.startswith(("pyrun_", "mcpurl_")):
                self.token_hint = self.token[:14]
                self.token = self.digest(self.token)
                if kwargs.get("update_fields") is not None:
                    kwargs["update_fields"] = set(kwargs["update_fields"]) | {"token", "token_hint"}
            elif not re.fullmatch(r"[a-f0-9]{64}", self.token):
                raise ValidationError("Generate API tokens with scope='api'.")
        elif self.token and not self.token_hint:
            # Datastore/plugin tokens (F53): a row without a hint still holds
            # its raw value — keep a display hint and store only the digest.
            # ``raw_token`` stays on this instance only (never stored), so the
            # creator can hand the value out once.
            self.raw_token = self.token
            self.token_hint = self.legacy_hint(self.token)
            self.token = self.digest(self.token)
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = set(kwargs["update_fields"]) | {"token", "token_hint"}
        super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        if self.scope != self.Scope.API:
            return
        allowed = {p[0] for p in self.API_PERMISSIONS}
        if self.kind not in self.Kind.values or self.brain_tier not in dict(self.TIERS):
            raise ValidationError("Unknown token kind or brain tier.")
        if (not isinstance(self.permissions, list) or not self.permissions
                or any(not isinstance(p, str) or p not in allowed for p in self.permissions)):
            raise ValidationError({"permissions": "Select at least one supported permission."})
        if self.kind == self.Kind.URL:
            if not self.expires_at:
                raise ValidationError({"expires_at": "URL tokens require an expiry."})
            if not self.url_permissions_ok(self.permissions):
                raise ValidationError({"permissions": "URL tokens allow read permissions only."})
        if self.brain_id and self.workspace_id != self.brain.workspace_id:
            raise ValidationError({"brain": "Unknown brain."})
        if self.script_id:
            if self.brain_id:
                raise ValidationError({"script": "Bind a token to one brain or one script, not both."})
            if self.workspace_id != self.script.workspace_id:
                raise ValidationError({"script": "Unknown script."})
        if self.datastore_id:
            if self.brain_id or self.script_id:
                raise ValidationError(
                    {"datastore": "Bind a token to one brain, one script or one datastore, not more."}
                )
            if self.workspace_id != self.datastore.workspace_id:
                raise ValidationError({"datastore": "Unknown datastore."})
        self._refuse_write_grants()

    @staticmethod
    def url_permissions_ok(permissions) -> bool:
        """URL tokens (the credential sits in the request path) hold only
        permissions that guard read endpoints alone: never run or write ones."""
        from core.api.registry import registry

        allowed = registry.read_permissions()
        return all(isinstance(p, str) and p in allowed for p in permissions)

    def _refuse_write_grants(self):
        """Plan D8: no write permission is granted while API writes are off.

        Only a NEW grant is refused: a token that already holds one (granted
        while writes were on) keeps it, and saves such as deactivating it still
        work. Its write calls are refused at call time instead.
        """
        from core.api.registry import registry, writes_enabled

        wanted = set(self.permissions) & registry.write_permissions()
        if not wanted or writes_enabled():
            return
        if not self._state.adding:
            stored = (
                APIToken.objects.filter(pk=self.pk).values_list("permissions", flat=True).first()
            )
            wanted -= set(stored or [])
        if wanted:
            raise ValidationError({"permissions": self.WRITES_OFF_GRANT})

    @staticmethod
    def digest(raw):
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def legacy_hint(raw) -> str:
        """Display hint for a datastore/plugin token: first 8 + last 4 characters."""
        if len(raw) <= 12:
            return "*" * len(raw)
        return f"{raw[:8]}...{raw[-4:]}"

    @staticmethod
    def generate_token(*, scope="datastores", kind="bearer") -> str:
        """Generate a URL-safe credential; unified API tokens include a kind prefix."""
        prefix = ("mcpurl_" if kind == "url" else "pyrun_") if scope == "api" else ""
        return prefix + secrets.token_urlsafe(48)

    def get_masked_token(self) -> str:
        """Return a masked version of the token for display."""
        if self.scope == self.Scope.API:
            return f"{self.token_hint or 'pyrun'}..."
        return self.token_hint or "********"

    @property
    def is_global(self) -> bool:
        """Return True if this is a global token (not scoped to a datastore)."""
        return self.scope != self.Scope.API and self.datastore is None

    @property
    def scope_display(self) -> str:
        """Return a human-readable scope description."""
        if self.scope == self.Scope.API:
            return "API: " + ", ".join(self.permissions)
        if self.scope == self.Scope.PLUGIN:
            return f"Plugin: {self.plugin_slug}"
        if self.datastore:
            return f"Datastore: {self.datastore.name}"
        return "All datastores"


# Historical name — kept importable so existing code and migrations referencing
# the old class keep working (the rename was state-only; table name unchanged).
DataStoreAPIToken = APIToken
