"""
Two-factor sign-in (2.5 Stage 11, T46): one authenticator per user plus
single-use recovery codes.

The TOTP secret is stored Fernet-encrypted (EncryptionService), never in the
clear. Recovery codes are stored only as SHA-256 hashes: they are long random
strings shown once, so a fast hash is enough to keep them useless if the
database leaks. The logic lives in ``core.services.two_factor``.
"""

from django.conf import settings
from django.db import models


class UserTOTP(models.Model):
    """A user's authenticator app. ``confirmed_at`` is empty while enrolling."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="totp",
    )
    secret_encrypted = models.TextField(help_text="Base32 TOTP secret, Fernet-encrypted")
    # Set once the user typed a valid code from the app; until then the
    # enrollment is pending and doesn't guard sign-in.
    confirmed_at = models.DateTimeField(null=True, blank=True)
    # The newest 30-second step a code was accepted for; a code for this step
    # or an earlier one is refused, so an observed code can't be replayed.
    last_used_step = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "core_user_totp"
        verbose_name = "two-factor authenticator"

    def __str__(self):
        state = "confirmed" if self.confirmed_at else "pending"
        return f"Authenticator for user {self.user_id} ({state})"


class RecoveryCode(models.Model):
    """One single-use recovery code, stored as a SHA-256 hash."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="recovery_codes",
    )
    code_hash = models.CharField(max_length=64)
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "core_recovery_code"
        indexes = [models.Index(fields=["user", "code_hash"], name="recovery_code_lookup")]

    def __str__(self):
        return f"Recovery code for user {self.user_id} ({'used' if self.used_at else 'unused'})"
