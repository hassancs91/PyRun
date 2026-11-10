"""
Connections: a workspace's credentials for one outside account (2.5 Stage 5, T23).

A Connection holds what a script needs to call one service as one account: an
Airtable token, a bearer token or username and password for any HTTP API, or an
SMTP login. Its shape comes from a provider preset
(``core.integrations.presets``): which auth kinds the provider takes, which
fields each needs, and how the credentials become request headers.

- Identities are tenant data, so ``workspace`` is required: a connection is
  never shared across workspaces.
- Secret fields (keys, tokens, passwords) live Fernet-encrypted, as one JSON
  object, in ``credentials_encrypted``; the rest of the form (base URL, SMTP
  host and port, username) lives in plain ``config`` so the console can show it.
- A script gets a connection only through a grant: an explicit
  ``ConnectionGrant``, or the connection's "grant to all my scripts" wildcard,
  which covers every script its creator made in the workspace.
- Scripts never get credentials in their environment. ``pyrunner_connections``
  asks the loopback for them, grant-checked, and every value it is handed is
  masked out of the run's output (``RunMask`` holds the ones issued after the
  run started).
"""

import hashlib
import uuid

from django.conf import settings
from django.db import models


class Connection(models.Model):
    """One account on one outside service, usable by the scripts it is granted to."""

    class AuthKind(models.TextChoices):
        # No preset takes OAuth 2.0 yet, so such a connection can't be made or
        # used; the value is reserved so the column never changes.
        OAUTH2 = "oauth2", "OAuth 2.0"
        API_KEY = "api_key", "API key"
        BEARER = "bearer", "Bearer token"
        BASIC = "basic", "Username and password"
        SMTP = "smtp", "SMTP login"

    class Status(models.TextChoices):
        OK = "ok", "OK"
        EXPIRED = "expired", "Expired"
        REVOKED = "revoked", "Revoked"
        ERROR = "error", "Error"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "core.Workspace",
        on_delete=models.CASCADE,
        related_name="connections",
        help_text="Workspace this connection belongs to (required: identities are tenant data).",
    )
    name = models.CharField(
        max_length=100,
        help_text="How scripts name it, e.g. 'airtable' or 'work-mail'. Unique in the workspace.",
    )
    provider = models.CharField(
        max_length=50,
        help_text="Provider preset key (core.integrations.presets), e.g. airtable, http, smtp.",
    )
    auth_kind = models.CharField(max_length=20, choices=AuthKind.choices)
    config = models.JSONField(
        default=dict,
        blank=True,
        help_text="The preset's non-secret fields (base URL, host, username...).",
    )
    credentials_encrypted = models.TextField(
        blank=True,
        help_text="Fernet-encrypted JSON object of the preset's secret fields (blank once revoked).",
    )
    scopes = models.JSONField(default=list, blank=True, help_text="OAuth scopes (OAuth connections).")
    account_label = models.CharField(
        max_length=255,
        blank=True,
        help_text="Which account this is, from the provider's whoami call when it has one.",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OK)
    last_tested_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    grant_all_scripts = models.BooleanField(
        default=False,
        help_text="Grant to every user script (not plugins') its creator made in this workspace.",
    )
    # Plugin ownership, as on Secret (ConnectionAPI, T25). NULL = a user connection.
    owner_plugin = models.CharField(max_length=100, null=True, blank=True, db_index=True)
    owner_key = models.CharField(max_length=100, null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_connections",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "connections"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["workspace", "name"], name="uniq_connection_ws_name"),
        ]

    def __str__(self):
        return f"{self.name} ({self.provider})"

    def get_credentials(self) -> dict:
        """The decrypted secret fields; {} when there are none (revoked).

        Raises EncryptionError when the stored value can't be decrypted.
        """
        if not self.credentials_encrypted:
            return {}
        import json

        from core.services import EncryptionService

        data = json.loads(EncryptionService.decrypt(self.credentials_encrypted))
        return data if isinstance(data, dict) else {}

    def set_credentials(self, credentials: dict) -> None:
        """Encrypt and store the secret fields (an empty dict clears them)."""
        import json

        from core.services import EncryptionService

        clean = {k: v for k, v in (credentials or {}).items() if v not in (None, "")}
        self.credentials_encrypted = (
            EncryptionService.encrypt(json.dumps(clean, sort_keys=True)) if clean else ""
        )


class ConnectionGrant(models.Model):
    """One Connection granted to one Script (the SecretGrant shape)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    script = models.ForeignKey(
        "core.Script", on_delete=models.CASCADE, related_name="connection_grants"
    )
    connection = models.ForeignKey(
        "core.Connection", on_delete=models.CASCADE, related_name="grants"
    )
    # An inactive grant is kept (for history) but gives nothing.
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "connection_grants"
        constraints = [
            models.UniqueConstraint(fields=["script", "connection"], name="uniq_connection_grant"),
        ]

    def __str__(self):
        return f"{self.script_id} → {self.connection_id}{'' if self.active else ' (inactive)'}"


class RunMask(models.Model):
    """A credential handed to a running script that its output must not show.

    The run's per-run mask set: ``pyrunner_connections`` gets credentials over
    the loopback while the run is going, possibly ones the run didn't have when
    it started (a grant added since, a rotated key, a refreshed OAuth token).
    Each one is recorded here, encrypted, and the masking passes (the final one
    over stdout/stderr, and result, progress and reply as they are stored) add
    them to the run's mask set. The rows go when the run ends.
    """

    id = models.BigAutoField(primary_key=True)
    run = models.ForeignKey("core.Run", on_delete=models.CASCADE, related_name="masks")
    # What the value is replaced with: ``[<label>:MASKED]``.
    label = models.CharField(max_length=200)
    # sha256 of the value, so issuing the same value twice keeps one row.
    digest = models.CharField(max_length=64)
    value_encrypted = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "run_masks"
        constraints = [
            models.UniqueConstraint(fields=["run", "digest"], name="uniq_run_mask_digest"),
        ]

    @staticmethod
    def digest_of(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
