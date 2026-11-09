"""
Script model for user-created Python scripts.
"""

import secrets
import uuid

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models

from .environment import Environment
from .fields import OrderedJSONField
from .workspace import WorkspaceScopedManager


class Script(models.Model):
    """
    Represents a Python script that can be executed.
    Scripts are associated with an environment and can be run manually or on schedule.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)

    # Tenancy: nullable + backfilled to the default workspace for upgrade-safety;
    # queries scope through WorkspaceScopedManager (.for_workspace).
    workspace = models.ForeignKey(
        "core.Workspace",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_index=True,
        related_name="scripts",
        help_text="Workspace this resource belongs to (tenancy seam; nullable).",
    )

    objects = WorkspaceScopedManager()

    # Plugin ownership (Plugin Platform v2, WS3). Nullable: NULL = a normal
    # user-created script = today's semantics. ``owner_plugin`` is a slug STRING
    # (not an FK, survives plugin deletion); ``owner_key`` is the SDK's stable
    # handle for idempotent upsert on (owner_plugin, owner_key).
    owner_plugin = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        db_index=True,
        help_text="Slug of the plugin that owns this script (NULL = user-created).",
    )
    owner_key = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        db_index=True,
        help_text="Stable per-owner handle for idempotent upsert (NULL = unmanaged).",
    )

    # Secret injection mode (Plugin Platform v2, WS3). 'all' is the default and
    # the literal pre-v2 behavior: inject every user (owner-NULL) secret in the
    # workspace. 'selected' is opt-in: a user script gets only its granted
    # secrets; a plugin-owned script (the SDK default) gets granted + same-owner +
    # explicitly-global secrets (core.executor.resolve_secrets_for_run). NOTE: this is
    # SECRET scoping, NOT the sandbox ``isolation_mode`` field below.
    class InjectionMode(models.TextChoices):
        ALL = "all", "All secrets (default)"
        SELECTED = "selected", "Selected secrets only"

    injection_mode = models.CharField(
        max_length=20,
        choices=InjectionMode.choices,
        default=InjectionMode.ALL,
        help_text="Which secrets to inject. 'all' = every workspace secret "
        "(today's behavior); 'selected' = only granted/same-owner/global secrets.",
    )

    # Opt-in per-script secret grants (consulted only when injection_mode='selected').
    granted_secrets = models.ManyToManyField(
        "core.Secret",
        through="core.SecretGrant",
        through_fields=("script", "secret"),
        related_name="granted_to_scripts",
        blank=True,
        help_text="Secrets explicitly attached to this script (selected mode).",
    )

    # Shared multi-module code attached to this script (Script Libraries). Each
    # attached library is materialized onto the run's PYTHONPATH as an importable
    # package, pinned to the revision stamped on the Run at queue time. Explicit-
    # only: no attachment row, no import.
    libraries = models.ManyToManyField(
        "core.Library",
        through="core.ScriptLibrary",
        through_fields=("script", "library"),
        related_name="attached_scripts",
        blank=True,
        help_text="Libraries this script can import at run time.",
    )

    # The actual Python code
    code = models.TextField(help_text="Python code to execute")

    # Declared inputs: an object JSON Schema in the core.api.schema subset,
    # checked by core.services.script_inputs.check_inputs_schema. NULL = the
    # script takes no inputs (every script before 2.5). Run now renders a form
    # from it; the values a run gets are validated and stamped on Run.inputs.
    # OrderedJSONField: the form lists inputs in the order they were declared,
    # on PostgreSQL too.
    inputs_schema = OrderedJSONField(
        null=True,
        blank=True,
        default=None,
        help_text="JSON Schema (type object) of the inputs a run of this script takes.",
    )

    # Plain, non-secret settings exported into every run (2.5, T09), as
    # {NAME: value}. Below inputs and secrets: a secret with the same name
    # always wins. Stored and shown as typed, never masked: keys and tokens
    # belong in Secrets. Rules in core.services.script_env.
    env_vars = models.JSONField(
        default=dict,
        blank=True,
        help_text="Non-secret environment variables for every run, as {NAME: value}. "
        "Inputs and secrets with the same name win.",
    )

    # Execution settings
    environment = models.ForeignKey(
        Environment,
        on_delete=models.PROTECT,
        related_name="scripts",
        help_text="Python environment to use for execution",
    )

    # Tags for categorization
    tags = models.ManyToManyField(
        "Tag",
        blank=True,
        related_name="scripts",
        help_text="Tags for organizing and filtering scripts",
    )

    timeout_seconds = models.PositiveIntegerField(
        default=3600,  # 1 hour default
        help_text="Maximum execution time in seconds (default: 1 hour, max: 24 hours)",
    )

    # Graceful stop (2.5, T08). Stopping a run (Stop button, SDK, timeout)
    # first sends SIGTERM (CTRL_BREAK on Windows) to the script's process
    # group, waits this long, then kills it. 0 = kill straight away.
    MAX_STOP_GRACE_SECONDS = 60

    stop_grace_seconds = models.PositiveSmallIntegerField(
        default=10,
        validators=[MaxValueValidator(MAX_STOP_GRACE_SECONDS)],
        help_text="Seconds a stopped or timed-out run gets to shut down before "
        "it is killed (0 = kill at once, max 60).",
    )

    # What a trigger does while the script already has a run in flight (2.5,
    # T05). Enforced in core.tasks.queue_script_run, the funnel every trigger
    # goes through (core.services.run_concurrency holds the rules).
    class ConcurrencyPolicy(models.TextChoices):
        ALLOW = "allow", "Allow: start another run alongside it"
        SKIP = "skip", "Skip: don't start the new run"
        QUEUE = "queue", "Queue: start it when the current run ends"

    concurrency_policy = models.CharField(
        max_length=10,
        choices=ConcurrencyPolicy.choices,
        default=ConcurrencyPolicy.ALLOW,
        help_text="What happens when the script is triggered while a run is "
        "still going. Allow starts another run alongside it. Skip records the "
        "new run as skipped. Queue waits and starts it when the current run ends.",
    )

    # Retry on failure (2.5, T06; core.services.run_retries holds the rules).
    # A failed run, and a timed-out one when retry_on_timeout is on, gets up
    # to retry_max_attempts more attempts. The n-th retry waits
    # retry_delay_seconds * retry_backoff_multiplier ** (n - 1), at most a day.
    MAX_RETRY_ATTEMPTS = 10
    MAX_RETRY_DELAY_SECONDS = 86400
    MAX_RETRY_BACKOFF_MULTIPLIER = 10.0

    retry_max_attempts = models.PositiveSmallIntegerField(
        default=0,
        validators=[MaxValueValidator(MAX_RETRY_ATTEMPTS)],
        help_text="How many times a failed run is retried (0 = never).",
    )
    retry_delay_seconds = models.PositiveIntegerField(
        default=60,
        validators=[MaxValueValidator(MAX_RETRY_DELAY_SECONDS)],
        help_text="How long to wait before the first retry, in seconds.",
    )
    retry_backoff_multiplier = models.FloatField(
        default=1.0,
        validators=[
            MinValueValidator(1.0),
            MaxValueValidator(MAX_RETRY_BACKOFF_MULTIPLIER),
        ],
        help_text="Each retry waits this many times longer than the one before "
        "(1 = the same delay every time, 2 = 1x, 2x, 4x...).",
    )
    retry_on_timeout = models.BooleanField(
        default=False,
        help_text="Also retry a run that timed out.",
    )

    # Status
    is_enabled = models.BooleanField(
        default=True,
        help_text="Whether this script can be executed",
    )

    # Per-script execution-isolation toggle (sandbox Stage 3). Honored only when
    # the effective workspace policy is 'optional'; a 'required' workspace locks
    # isolation on regardless, and an 'off' instance/workspace ignores it.
    class IsolationMode(models.TextChoices):
        INHERIT = "inherit", "Inherit (workspace default)"
        SANDBOXED = "sandboxed", "Sandboxed"
        PLAIN = "plain", "Plain"

    isolation_mode = models.CharField(
        max_length=20,
        choices=IsolationMode.choices,
        default=IsolationMode.INHERIT,
        help_text="Run this script sandboxed. Effective only when the workspace "
        "policy is 'optional' (a 'required' workspace always sandboxes).",
    )

    # Webhook
    webhook_token = models.CharField(
        max_length=64,
        unique=True,
        null=True,
        blank=True,
        db_index=True,
        help_text="Unique token for webhook URL (auto-generated)",
    )

    # Webhook signature (2.5, T21; core.services.webhook_signatures holds the
    # rules). A call is checked against it before any run exists; one that
    # fails gets 401. The header, prefix, encoding and timestamp header are
    # the custom HMAC-SHA256 preset's; the presets fix their own.
    class WebhookSignature(models.TextChoices):
        NONE = "none", "None"
        STRIPE = "stripe", "Stripe"
        GITHUB = "github", "GitHub"
        SHOPIFY = "shopify", "Shopify"
        HMAC_SHA256 = "hmac_sha256", "Custom HMAC-SHA256"

    class WebhookSignatureEncoding(models.TextChoices):
        HEX = "hex", "Hex"
        BASE64 = "base64", "Base64"

    MAX_WEBHOOK_SIGNATURE_TOLERANCE = 86400
    MAX_WEBHOOK_RATE_PER_MINUTE = 10000

    webhook_signature = models.CharField(
        max_length=20,
        choices=WebhookSignature.choices,
        default=WebhookSignature.NONE,
        help_text="Signature every webhook call must carry (none = the URL alone).",
    )
    webhook_signature_secret_encrypted = models.TextField(
        blank=True,
        help_text="The signing secret, encrypted with ENCRYPTION_KEY.",
    )
    webhook_signature_header = models.CharField(
        max_length=100,
        blank=True,
        help_text="Custom HMAC-SHA256: the header that carries the signature.",
    )
    webhook_signature_prefix = models.CharField(
        max_length=50,
        blank=True,
        help_text="Custom HMAC-SHA256: text before the signature in the header (e.g. sha256=).",
    )
    webhook_signature_encoding = models.CharField(
        max_length=10,
        choices=WebhookSignatureEncoding.choices,
        default=WebhookSignatureEncoding.HEX,
        help_text="Custom HMAC-SHA256: how the signature is written.",
    )
    webhook_signature_timestamp_header = models.CharField(
        max_length=100,
        blank=True,
        help_text="Custom HMAC-SHA256: a header holding the Unix time the call "
        "was signed; when set, the signed text is '<timestamp>.<body>'.",
    )
    webhook_signature_tolerance = models.PositiveIntegerField(
        default=300,
        validators=[MinValueValidator(1), MaxValueValidator(MAX_WEBHOOK_SIGNATURE_TOLERANCE)],
        help_text="Seconds a signed timestamp may differ from the server's clock "
        "(Stripe, and custom HMAC-SHA256 with a timestamp header).",
    )

    # Per-script webhook rate limit (2.5, T21). Calls over either limit get
    # 429 and no run; the per-IP limit in core.views.webhooks is only a
    # scanning floor above these.
    webhook_rate_per_minute = models.PositiveIntegerField(
        default=60,
        validators=[MinValueValidator(1), MaxValueValidator(MAX_WEBHOOK_RATE_PER_MINUTE)],
        help_text="Webhook calls accepted per minute.",
    )
    webhook_rate_burst = models.PositiveIntegerField(
        default=30,
        validators=[MinValueValidator(1), MaxValueValidator(MAX_WEBHOOK_RATE_PER_MINUTE)],
        help_text="Webhook calls accepted within any one second.",
    )

    # Script as an MCP tool (2.5, T14; core.api.script_tools holds the rules).
    # On: MCP tools/list shows the script as the tool ``tool_name`` to tokens
    # that may run it, and calling the tool is scripts.run with wait. Exposed
    # tool names are unique per workspace (constraint below). A name has no
    # hyphen, so it can't clash with a registry tool (``<namespace>-<name>``).
    # At most 40 characters: clients prefix MCP tool names (Claude Code:
    # ``mcp__<server>__``) and many models cap a tool name at 64.
    TOOL_NAME_MAX_LENGTH = 40
    TOOL_NAME_PATTERN = r"^[a-z][a-z0-9_]*\Z"
    TOOL_NAME_RULE = "Use lowercase letters, digits and underscores, starting with a letter."

    expose_as_tool = models.BooleanField(
        default=False,
        help_text="Offer this script as a tool to MCP clients whose token may run it.",
    )
    tool_name = models.CharField(
        max_length=TOOL_NAME_MAX_LENGTH,
        blank=True,
        default="",
        validators=[RegexValidator(TOOL_NAME_PATTERN, TOOL_NAME_RULE)],
        help_text="The MCP tool name: lowercase letters, digits and underscores.",
    )

    # Notification settings
    class NotifyOn(models.TextChoices):
        NEVER = "never", "Never"
        FAILURE = "failure", "On Failure"
        SUCCESS = "success", "On Success"
        BOTH = "both", "On Success and Failure"

    notify_on = models.CharField(
        max_length=20,
        choices=NotifyOn.choices,
        default=NotifyOn.NEVER,
        help_text="When to send notifications for this script",
    )
    notify_email = models.EmailField(
        blank=True,
        help_text="Override email for this script (uses global default if empty)",
    )
    notify_webhook_url = models.URLField(
        blank=True,
        max_length=500,
        help_text="URL to POST notification webhooks to",
    )
    notify_webhook_enabled = models.BooleanField(
        default=False,
        help_text="Enable webhook notifications for this script",
    )
    # Chat-channel notifications (Channels subsystem). Run summaries are delivered
    # to each enabled channel here, gated by ``notify_on`` like email/webhook.
    notify_channels = models.ManyToManyField(
        "core.Channel",
        blank=True,
        related_name="notify_scripts",
        help_text="Chat channels to notify when this script runs (respects notify_on).",
    )

    # Retention overrides (null = use global settings)
    retention_days_override = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Override global retention days for this script",
    )
    retention_count_override = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Override global retention count for this script",
    )

    # Archive fields (soft delete)
    archived_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When this script was archived (null = not archived)",
    )
    archived_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="archived_scripts",
    )

    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scripts",
    )

    class Meta:
        db_table = "scripts"
        verbose_name = "script"
        verbose_name_plural = "scripts"
        ordering = ["-updated_at"]
        constraints = [
            # An exposed script has a tool name, and no two exposed scripts in
            # a workspace share one (archived ones included: unarchiving must
            # not collide). A name kept while the switch is off holds nothing.
            models.CheckConstraint(
                condition=models.Q(expose_as_tool=False) | ~models.Q(tool_name=""),
                name="script_exposed_tool_named",
                violation_error_message="A script offered as an MCP tool needs a tool name.",
            ),
            models.UniqueConstraint(
                fields=["workspace", "tool_name"],
                condition=models.Q(expose_as_tool=True),
                name="uniq_script_ws_tool_name",
            ),
        ]

    def __str__(self):
        if self.is_archived:
            return f"{self.name} (archived)"
        status = "enabled" if self.is_enabled else "disabled"
        return f"{self.name} ({status})"

    @property
    def is_archived(self) -> bool:
        """Check if this script is archived."""
        return self.archived_at is not None

    @property
    def can_run(self) -> bool:
        """Check if this script can be executed (enabled and not archived)."""
        return self.is_enabled and not self.is_archived

    @property
    def max_run_seconds(self) -> int:
        """The longest a run's script can be alive: its timeout plus the grace
        period a timed-out run gets to shut down before the kill."""
        return self.timeout_seconds + self.stop_grace_seconds

    @property
    def env_var_names(self) -> list:
        """The env var names, sorted (PostgreSQL doesn't keep the saved order)."""
        return sorted(self.env_vars) if isinstance(self.env_vars, dict) else []

    @property
    def last_run(self):
        """Return the most recent run for this script."""
        return self.runs.order_by("-created_at").first()

    @property
    def last_successful_run(self):
        """Return the most recent successful run for this script."""
        return self.runs.filter(status="success").order_by("-created_at").first()

    @property
    def run_count(self) -> int:
        """Return the total number of runs for this script."""
        return self.runs.count()

    @property
    def success_rate(self) -> float | None:
        """Return the success rate as a percentage, or None if no runs."""
        total = self.run_count
        if total == 0:
            return None
        successful = self.runs.filter(status="success").count()
        return (successful / total) * 100

    def get_code_preview(self, max_lines: int = 5) -> str:
        """Return a preview of the script code (first N lines)."""
        lines = self.code.split("\n")[:max_lines]
        preview = "\n".join(lines)
        if len(self.code.split("\n")) > max_lines:
            preview += "\n..."
        return preview

    @staticmethod
    def generate_webhook_token() -> str:
        """Generate a secure random webhook token (64 chars, URL-safe)."""
        return secrets.token_urlsafe(48)  # 48 bytes = 64 chars in base64

    def create_webhook_token(self) -> str:
        """Create and save a new webhook token for this script."""
        self.webhook_token = self.generate_webhook_token()
        self.save(update_fields=["webhook_token", "updated_at"])
        return self.webhook_token

    def regenerate_webhook_token(self) -> str:
        """Regenerate the webhook token, invalidating the old one."""
        return self.create_webhook_token()

    def clear_webhook_token(self) -> None:
        """Remove the webhook token, disabling webhook access."""
        self.webhook_token = None
        self.save(update_fields=["webhook_token", "updated_at"])

    @property
    def has_webhook(self) -> bool:
        """Check if this script has a webhook token configured."""
        return bool(self.webhook_token)

    def set_webhook_signature_secret(self, value: str) -> None:
        """Encrypt and store the webhook signing secret ("" clears it)."""
        from core.services import EncryptionService

        self.webhook_signature_secret_encrypted = (
            EncryptionService.encrypt(value) if value else ""
        )

    def get_webhook_signature_secret(self) -> str:
        """The webhook signing secret in clear ("" when none is stored).

        Raises EncryptionError when the stored value can't be decrypted
        (ENCRYPTION_KEY changed since it was saved).
        """
        if not self.webhook_signature_secret_encrypted:
            return ""
        from core.services import EncryptionService

        return EncryptionService.decrypt(self.webhook_signature_secret_encrypted)
