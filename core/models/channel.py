"""
Channel model — multi-instance chat integrations (Channels subsystem).

A Channel is one configured connection to an external chat service (Telegram,
Slack, Discord, …). Unlike the singleton Services (S3/Claude on GlobalSettings),
you can have N channels of one provider — e.g. several Telegram bots, each bound
to a different handler.

Credentials live encrypted ON this row (a Fernet JSON blob), NOT in the Secret
namespace: scripts send *through* a channel server-side and never see raw tokens.

Both directions are wired: OUTBOUND sends, and the inbound_* columns drive the
INBOUND pipeline (webhook + handlers + approval inbox).
"""

import hashlib
import json
import secrets
import uuid

from django.conf import settings
from django.db import models

from .workspace import WorkspaceScopedManager


class Channel(models.Model):
    """A configured connection to an external chat service."""

    class Provider(models.TextChoices):
        TELEGRAM = "telegram", "Telegram"
        SLACK = "slack", "Slack"
        DISCORD = "discord", "Discord"
        # whatsapp is designed-for but not built (Meta Cloud API approval).

    class InboundHandler(models.TextChoices):
        # Empty = inbound is notify-only / disabled. 'agent' is intentionally
        # RESERVED for the future Agents plan (channels carry no agent code).
        NONE = "", "None"
        SCRIPT = "script", "Run a script"
        PYAI = "pyai", "Py AI"

    class InboundAccess(models.TextChoices):
        APPROVAL = "approval", "Approval inbox (recommended)"
        OPEN = "open", "Open"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Tenancy (matches Secret/DataStore): nullable + backfilled to the default
    # workspace; queries scope through WorkspaceScopedManager (.for_workspace).
    workspace = models.ForeignKey(
        "core.Workspace",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_index=True,
        related_name="channels",
        help_text="Workspace this channel belongs to (tenancy seam; nullable).",
    )

    objects = WorkspaceScopedManager()

    provider = models.CharField(max_length=20, choices=Provider.choices, db_index=True)
    name = models.CharField(
        max_length=120,
        help_text="A label for this connection, e.g. 'Ops Alerts' or 'Support Bot'.",
    )

    # Non-secret config (default send target, provider options).
    config = models.JSONField(default=dict, blank=True)

    # Fernet-encrypted JSON blob of provider credentials (bot token, etc.).
    creds_encrypted = models.TextField(blank=True)
    # SHA-256 of the provider's credential identity (e.g. the bot token), so we
    # can enforce "one bot = one Channel" without unique-constraining ciphertext.
    creds_fingerprint = models.CharField(max_length=64, blank=True, db_index=True)

    enabled = models.BooleanField(default=True)

    # ---- inbound (Phase 2 wiring; columns present now to avoid migration churn) ----
    inbound_enabled = models.BooleanField(default=False)
    inbound_token = models.CharField(
        max_length=64, null=True, blank=True, unique=True, db_index=True,
        help_text="Routes the public inbound webhook URL (auto-generated).",
    )
    inbound_secret_encrypted = models.TextField(blank=True)
    inbound_handler = models.CharField(
        max_length=20, choices=InboundHandler.choices, blank=True, default="",
    )
    inbound_target_id = models.UUIDField(
        null=True, blank=True,
        help_text="Handler target, e.g. the Script id when handler='script'.",
    )
    inbound_access = models.CharField(
        max_length=20, choices=InboundAccess.choices, default=InboundAccess.APPROVAL,
    )
    daily_reply_cap = models.PositiveIntegerField(
        default=0,
        help_text="Max replies to inbound messages per day: handler replies and "
        "pyrunner_notify.reply() (0 = unlimited). Cost fuse.",
    )
    daily_send_cap = models.PositiveIntegerField(
        default=0,
        help_text="Max messages sent per day: notifications, replies and script "
        "sends together (0 = unlimited). Stops a runaway schedule.",
    )

    # ---- status ----
    last_tested_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    last_inbound_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_channels",
    )

    class Meta:
        db_table = "channels"
        verbose_name = "channel"
        verbose_name_plural = "channels"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "name"], name="uniq_channel_ws_name"
            ),
            # One bot = one Channel: a provider credential identity is globally
            # unique (a Telegram bot allows a single webhook). Empty fingerprints
            # (not-yet-configured) are exempt.
            models.UniqueConstraint(
                fields=["creds_fingerprint"],
                condition=~models.Q(creds_fingerprint=""),
                name="uniq_channel_creds_fingerprint",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_provider_display()})"

    # --- credentials ---------------------------------------------------------

    @staticmethod
    def fingerprint_for(provider: str, identity: str) -> str:
        """The one-bot-one-channel fingerprint for a provider + credential identity."""
        if not identity:
            return ""
        return hashlib.sha256(f"{provider}:{identity}".encode("utf-8")).hexdigest()

    def set_credentials(self, creds: dict, identity: str = "") -> None:
        """Encrypt + store the credential blob and recompute the fingerprint.

        ``identity`` is the provider's stable credential identity (e.g. the bot
        token) used for the one-bot-one-channel fingerprint; pass "" to clear.
        """
        from core.services import EncryptionService

        self.creds_encrypted = EncryptionService.encrypt(json.dumps(creds))
        self.creds_fingerprint = self.fingerprint_for(self.provider, identity)

    def get_credentials(self) -> dict:
        """Decrypt + return the credential blob ({} if none)."""
        if not self.creds_encrypted:
            return {}
        from core.services import EncryptionService

        return json.loads(EncryptionService.decrypt(self.creds_encrypted))

    # --- inbound secret (Phase 2) -------------------------------------------

    def set_inbound_secret(self, value: str) -> None:
        from core.services import EncryptionService

        self.inbound_secret_encrypted = (
            EncryptionService.encrypt(value) if value else ""
        )

    def get_inbound_secret(self) -> str:
        if not self.inbound_secret_encrypted:
            return ""
        from core.services import EncryptionService

        return EncryptionService.decrypt(self.inbound_secret_encrypted)

    @staticmethod
    def generate_inbound_token() -> str:
        """Generate a URL-safe inbound webhook token (mirrors Script tokens)."""
        return secrets.token_urlsafe(48)

    def ensure_inbound_token(self) -> str:
        """Return the inbound token, generating one if absent."""
        if not self.inbound_token:
            self.inbound_token = self.generate_inbound_token()
        return self.inbound_token

    def ensure_inbound_secret(self) -> str:
        """Return the inbound signing secret, generating one if absent."""
        current = self.get_inbound_secret()
        if not current:
            current = secrets.token_urlsafe(32)
            self.set_inbound_secret(current)
        return current

    @staticmethod
    def day_start():
        """The start of the caps' day: midnight UTC (both daily caps reset then)."""
        from django.utils import timezone

        return timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)

    def replies_today(self) -> int:
        """Replies to inbound messages sent today (for the per-channel daily reply cap).

        Only sent ``kind="reply"`` rows count: notifications, a script's plain
        ``pyrunner_notify.send()``, PyRun's own "limit reached" notice and a
        failed send do not (F50), so a channel used for scheduled notifications
        can't hit its reply cap.
        """
        return self.messages.filter(
            direction=ChannelMessage.Direction.OUT,
            kind=ChannelMessage.Kind.REPLY,
            status=ChannelMessage.Status.SENT,
            created_at__gte=self.day_start(),
        ).count()

    def daily_cap_reached(self) -> bool:
        return bool(self.daily_reply_cap) and self.replies_today() >= self.daily_reply_cap

    def sends_today(self) -> int:
        """Messages this channel sent today, for the daily send cap.

        Notifications, replies and script sends count; PyRun's own notices
        (kind ``system``) don't, so strangers can't use the cap up.
        """
        return (
            self.messages.filter(
                direction=ChannelMessage.Direction.OUT,
                status=ChannelMessage.Status.SENT,
                created_at__gte=self.day_start(),
            )
            .exclude(kind=ChannelMessage.Kind.SYSTEM)
            .count()
        )

    def send_cap_reached(self) -> bool:
        return bool(self.daily_send_cap) and self.sends_today() >= self.daily_send_cap

    # --- helpers -------------------------------------------------------------

    @property
    def is_configured(self) -> bool:
        """Credentials are stored AND readable: a blob that can't be decrypted
        (ENCRYPTION_KEY changed) is as good as none until they are entered again."""
        if not self.creds_encrypted:
            return False
        from core.services.encryption_service import EncryptionError

        try:
            self.get_credentials()
        except (EncryptionError, ValueError):
            return False
        return True

    @property
    def default_target(self) -> str:
        """Where outbound notifications go when no explicit reply target."""
        return (self.config or {}).get("default_target", "") or ""


class ChannelMember(models.Model):
    """A sender's access to a channel's inbound handler — the approval inbox.

    Deny-by-default: an unknown sender lands as ``pending`` and the owner approves
    them from the authenticated Channels UI (the dashboard login is the trust
    anchor, never chat alone). Approving is a data-read grant for handlers like
    Py AI that can read the workspace's data.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        BLOCKED = "blocked", "Blocked"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    channel = models.ForeignKey(
        "core.Channel", on_delete=models.CASCADE, related_name="members"
    )
    sender_id = models.CharField(max_length=128, help_text="Provider user/chat id.")
    display_name = models.CharField(max_length=200, blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_channel_members",
    )
    last_seen_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "channel_members"
        verbose_name = "channel member"
        verbose_name_plural = "channel members"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["channel", "sender_id"], name="uniq_channel_member"
            ),
        ]

    def __str__(self):
        return f"{self.sender_id} [{self.status}]"


class ChannelMessage(models.Model):
    """One inbound message, or one outbound delivery (the delivery ledger).

    Every outbound message goes through ``core.services.channels.ledger.deliver``,
    which records it here and retries a temporary failure from the worker
    heartbeat. Doubles as conversation history (keyed by reply_ref) for Py AI.
    Rows older than 30 days are deleted by the daily housekeeping task.
    """

    class Direction(models.TextChoices):
        IN = "in", "Inbound"
        OUT = "out", "Outbound"

    class Kind(models.TextChoices):
        # Outbound only; inbound rows keep "".
        NOTIFICATION = "notification", "Notification"
        REPLY = "reply", "Reply"
        SCRIPT = "script", "Script send"
        SYSTEM = "system", "PyRun notice"

    class Status(models.TextChoices):
        RECEIVED = "received", "Received"  # inbound rows
        PENDING = "pending", "Sending"
        SENT = "sent", "Sent"
        RETRYING = "retrying", "Retrying"
        FAILED = "failed", "Failed"

    class DeliveryState(models.TextChoices):
        # What the provider reports after the send (sent until a callback says
        # more; Telegram has no delivery callbacks).
        NONE = "", "—"
        SENT = "sent", "Sent"
        DELIVERED = "delivered", "Delivered"
        READ = "read", "Read"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    channel = models.ForeignKey(
        "core.Channel", on_delete=models.CASCADE, related_name="messages"
    )
    direction = models.CharField(max_length=4, choices=Direction.choices, db_index=True)
    kind = models.CharField(max_length=20, choices=Kind.choices, blank=True, default="")
    text = models.TextField(blank=True)
    sender_json = models.JSONField(default=dict, blank=True)
    reply_ref_json = models.JSONField(default=dict, blank=True)
    handler = models.CharField(max_length=20, blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.RECEIVED, db_index=True
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(
        null=True, blank=True, db_index=True,
        help_text="When the heartbeat tries a pending or retrying send next.",
    )
    run = models.ForeignKey(
        "core.Run",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="channel_messages",
        help_text="The run that sent it (notifications and script sends).",
    )
    provider_message_id = models.CharField(max_length=128, blank=True)
    delivery_state = models.CharField(
        max_length=20, choices=DeliveryState.choices, blank=True, default=""
    )
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "channel_messages"
        verbose_name = "channel message"
        verbose_name_plural = "channel messages"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.direction} {self.channel_id} ({self.status})"
