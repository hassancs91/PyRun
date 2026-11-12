"""
One row per sign-in or account-security event (2.5 Stage 11, T45).

``core.services.security_events.record`` writes the rows: sign-ins that
worked or failed, lockouts, password changes and resets, admin reset links and
API token creation. Rows older than ``security_events.RETENTION_DAYS`` are
deleted by the daily housekeeping task. Superusers see them under
Users → Security.

Not in backups: it is a log, like ``ApiCall``.
"""

from django.conf import settings
from django.db import models


class SecurityEvent(models.Model):
    class Kind(models.TextChoices):
        LOGIN_OK = "login_ok", "Signed in"
        LOGIN_FAIL = "login_fail", "Sign-in failed"
        LOCKOUT = "lockout", "Locked out"
        PASSWORD_CHANGE = "password_change", "Password changed"
        RESET_REQUEST = "reset_request", "Reset requested"
        RESET = "reset", "Password reset"
        RESET_LINK = "reset_link", "Reset link generated"
        TOKEN_CREATE = "token_create", "API token created"

    # The account, when one matches. A failed sign-in for an unknown email
    # keeps only ``email``; deleting the user keeps the rows until the purge.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="security_events",
    )
    # The email the event is about, as typed (lowercased) or the account's.
    email = models.CharField(max_length=254, blank=True, default="")
    kind = models.CharField(max_length=32, choices=Kind.choices)
    ip = models.CharField(max_length=64, blank=True, default="")
    user_agent = models.CharField(max_length=255, blank=True, default="")
    # A short note: who generated a reset link, the token's name, "command line".
    detail = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "core_security_event"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "-created_at"], name="secevent_user_recent")]

    def __str__(self):
        return f"{self.kind} {self.email} ({self.created_at:%Y-%m-%d %H:%M:%S})"
