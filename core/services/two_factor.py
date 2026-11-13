"""
Two-factor sign-in (2.5 Stage 11, T46): TOTP authenticator apps plus
single-use recovery codes.

- TOTP is RFC 6238 over RFC 4226 HOTP (HMAC-SHA1, 6 digits, 30-second steps),
  written with ``hmac``/``hashlib``; a code from the step before or after the
  current one is accepted for clock drift. A code is accepted at most once:
  ``UserTOTP.last_used_step`` only moves forward, by a conditional UPDATE, so
  two requests racing with the same code can't both pass.
- The secret is Fernet-encrypted at rest (``EncryptionService``). Recovery
  codes are random (about 59 bits), shown once, stored as SHA-256 hashes and
  spent by a conditional UPDATE.
- Sign-in: after the password is right, the login view doesn't start the
  session. It stashes the user in the (still anonymous) session for
  ``STASH_SECONDS`` and sends the browser to the code step (or, when
  ``GlobalSettings.require_2fa`` is on and the account has no authenticator,
  to set one up). Each code attempt counts against a per-user and a per-IP
  budget (``core.ratelimit``).
- The QR code is an inline SVG made on the server (``segno``): no image URL,
  no script, nothing the Content-Security-Policy has to allow.

API tokens, MCP credentials and invite links don't go through any of this.
"""

import base64
import hashlib
import hmac
import logging
import secrets
import struct
import time
from urllib.parse import quote

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

DIGITS = 6
PERIOD = 30
# Steps either side of "now" that still count (clock drift on the phone).
DRIFT_STEPS = 1
SECRET_BYTES = 20

RECOVERY_CODE_COUNT = 10
RECOVERY_CODE_LENGTH = 12
# No 0/o, 1/l/i: codes are read off paper and typed by hand.
RECOVERY_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"

# The pre-2FA sign-in stash: who proved their password, and when.
STASH_KEY = "pyrun_2fa_pending"
STASH_SECONDS = 300
# New recovery codes, shown once on the next page.
NEW_CODES_KEY = "pyrun_2fa_new_codes"

# Code attempts per account and per client IP in a fixed window. Six digits
# with three accepted steps: five tries per five minutes keeps a guess at
# about one in 70 000 per window, and the password is needed to get here.
USER_ATTEMPTS = 5
IP_ATTEMPTS = 20
ATTEMPT_WINDOW_SECONDS = 300

MODEL_BACKEND = "django.contrib.auth.backends.ModelBackend"


class TwoFactorUnavailable(Exception):
    """The secret can't be stored or read (encryption isn't working)."""


# ---------------------------------------------------------------------------
# RFC 4226 / RFC 6238
# ---------------------------------------------------------------------------


def hotp(key: bytes, counter: int, digits: int = DIGITS, digest=hashlib.sha1) -> str:
    """The RFC 4226 one-time password for ``counter``."""
    mac = hmac.new(key, struct.pack(">Q", counter), digest).digest()
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** digits)).zfill(digits)


def totp(key: bytes, for_time: float, digits: int = DIGITS, period: int = PERIOD, digest=hashlib.sha1) -> str:
    """The RFC 6238 code for the step that contains ``for_time`` (Unix seconds)."""
    return hotp(key, int(for_time // period), digits=digits, digest=digest)


def new_secret() -> str:
    """A random base32 secret (160 bits, no padding), as authenticator apps take it."""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii").rstrip("=")


def secret_bytes(secret: str) -> bytes:
    """Decode a base32 secret (any case, padding optional)."""
    cleaned = secret.replace(" ", "").upper()
    return base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))


def matching_step(key: bytes, code: str, now: float | None = None) -> int | None:
    """The step (current one, or one either side) whose code is ``code``."""
    now = time.time() if now is None else now
    current = int(now // PERIOD)
    found = None
    for step in range(current - DRIFT_STEPS, current + DRIFT_STEPS + 1):
        # Check every candidate (no early exit) so timing doesn't say which.
        if hmac.compare_digest(hotp(key, step), code):
            found = step
    return found


def provisioning_uri(secret: str, account: str, issuer: str) -> str:
    """The ``otpauth://`` URI an authenticator app reads from the QR code."""
    label = quote(issuer, safe="") + ":" + quote(account, safe="@")
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer, safe='')}"
        f"&algorithm=SHA1&digits={DIGITS}&period={PERIOD}"
    )


def qr_svg(text: str) -> str:
    """An inline SVG QR code for ``text`` (dark on white, sized by CSS)."""
    import segno

    return segno.make(text, error="m", micro=False).svg_inline(
        scale=4, border=4, dark="#000000", light="#ffffff", omitsize=True,
        svgclass="block w-full h-auto",
    )


def group_secret(secret: str) -> str:
    """The secret in groups of four, for typing it into an app by hand."""
    return " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))


def issuer() -> str:
    """The name the authenticator app shows for this instance."""
    from core.models import GlobalSettings

    name = (GlobalSettings.get_settings().instance_name or "").strip()
    return name or "PyRun"


# ---------------------------------------------------------------------------
# Enrollment state
# ---------------------------------------------------------------------------


def authenticator(user):
    from core.models import UserTOTP

    return UserTOTP.objects.filter(user=user).first()


def is_enabled(user) -> bool:
    """Has ``user`` a confirmed authenticator (so sign-in asks for a code)?"""
    from core.models import UserTOTP

    return UserTOTP.objects.filter(user=user, confirmed_at__isnull=False).exists()


def is_required() -> bool:
    from core.models import GlobalSettings

    return bool(GlobalSettings.get_settings().require_2fa)


def sign_in_step(user) -> str | None:
    """What a correct password leads to: ``"verify"``, ``"enroll"`` or None."""
    if is_enabled(user):
        return "verify"
    if is_required():
        return "enroll"
    return None


def _decrypt(row) -> str:
    from core.services import EncryptionError, EncryptionService

    try:
        return EncryptionService.decrypt(row.secret_encrypted)
    except EncryptionError as exc:
        logger.error("two_factor.secret_unreadable user=%s", row.user_id)
        raise TwoFactorUnavailable("The authenticator secret can't be read.") from exc


def begin_enrollment(user) -> str:
    """Start (or resume) setting up an authenticator; returns the secret.

    A pending enrollment keeps its secret, so reloading the page shows the
    same QR code. Refused (ValueError) when one is already confirmed.
    """
    from core.models import UserTOTP
    from core.services import EncryptionError, EncryptionService

    row = authenticator(user)
    if row is not None and row.confirmed_at:
        raise ValueError("Two-factor sign-in is already on.")
    if row is not None:
        try:
            return _decrypt(row)
        except TwoFactorUnavailable:
            # Encrypted under another key: start over with a fresh secret.
            row.delete()
    secret = new_secret()
    try:
        encrypted = EncryptionService.encrypt(secret)
    except EncryptionError as exc:
        logger.error("two_factor.secret_not_stored user=%s", user.pk)
        raise TwoFactorUnavailable("The authenticator secret can't be stored.") from exc
    try:
        with transaction.atomic():
            UserTOTP.objects.create(user=user, secret_encrypted=encrypted)
    except IntegrityError:
        # A second tab started at the same moment: use its secret.
        return _decrypt(UserTOTP.objects.get(user=user))
    return secret


def pending_secret(user) -> str | None:
    """The secret of an enrollment that isn't confirmed yet, if any."""
    row = authenticator(user)
    if row is None or row.confirmed_at:
        return None
    return _decrypt(row)


def _clean_digits(code: str) -> str:
    return "".join(str(code or "").split())


def verify_totp(row, code: str, now: float | None = None) -> bool:
    """Is ``code`` right for ``row`` and not used before? Spends its step."""
    from core.models import UserTOTP

    code = _clean_digits(code)
    if len(code) != DIGITS or not code.isdigit():
        return False
    step = matching_step(secret_bytes(_decrypt(row)), code, now)
    if step is None:
        return False
    # Replay protection: only a step newer than the last accepted one, and
    # only one request can move it there.
    return UserTOTP.objects.filter(pk=row.pk, last_used_step__lt=step).update(last_used_step=step) == 1


def confirm_enrollment(user, code: str, now: float | None = None) -> list[str] | None:
    """Confirm a pending enrollment with a code from the app.

    Returns the new recovery codes (to show once), or None for a wrong code.
    """
    from core.models import UserTOTP

    row = authenticator(user)
    if row is None or row.confirmed_at or not verify_totp(row, code, now):
        return None
    if not UserTOTP.objects.filter(pk=row.pk, confirmed_at__isnull=True).update(confirmed_at=timezone.now()):
        return None
    return replace_recovery_codes(user)


def disable(user) -> bool:
    """Remove the authenticator and the recovery codes; True if there was one."""
    from core.models import RecoveryCode, UserTOTP

    with transaction.atomic():
        removed, _ = UserTOTP.objects.filter(user=user).delete()
        RecoveryCode.objects.filter(user=user).delete()
    return bool(removed)


# ---------------------------------------------------------------------------
# Recovery codes
# ---------------------------------------------------------------------------


def _normalize_recovery(code: str) -> str:
    return "".join(ch for ch in str(code or "").lower() if ch.isalnum())


def hash_recovery_code(code: str) -> str:
    return hashlib.sha256(_normalize_recovery(code).encode("ascii", "ignore")).hexdigest()


def _new_recovery_code() -> str:
    raw = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_CODE_LENGTH))
    return "-".join(raw[i:i + 4] for i in range(0, RECOVERY_CODE_LENGTH, 4))


def replace_recovery_codes(user) -> list[str]:
    """Issue a new set of recovery codes; every earlier one stops working."""
    from core.models import RecoveryCode

    codes = [_new_recovery_code() for _ in range(RECOVERY_CODE_COUNT)]
    with transaction.atomic():
        RecoveryCode.objects.filter(user=user).delete()
        RecoveryCode.objects.bulk_create(
            [RecoveryCode(user=user, code_hash=hash_recovery_code(code)) for code in codes]
        )
    return codes


def recovery_codes_left(user) -> int:
    from core.models import RecoveryCode

    return RecoveryCode.objects.filter(user=user, used_at__isnull=True).count()


def use_recovery_code(user, code: str) -> bool:
    """Spend one unused recovery code; False if it's wrong or already used."""
    from core.models import RecoveryCode

    normalized = _normalize_recovery(code)
    if len(normalized) != RECOVERY_CODE_LENGTH:
        return False
    match = RecoveryCode.objects.filter(
        user=user, code_hash=hash_recovery_code(normalized), used_at__isnull=True
    ).first()
    if match is None:
        return False
    return RecoveryCode.objects.filter(pk=match.pk, used_at__isnull=True).update(used_at=timezone.now()) == 1


def check_code(user, code: str, now: float | None = None) -> str | None:
    """A code typed by a user with two-factor on: ``"totp"``, ``"recovery"`` or None.

    Six digits are checked as an authenticator code, anything else as a
    recovery code. Either way the code is spent.
    """
    row = authenticator(user)
    if row is None or not row.confirmed_at:
        return None
    digits = _clean_digits(code)
    if len(digits) == DIGITS and digits.isdigit():
        return "totp" if verify_totp(row, digits, now) else None
    return "recovery" if use_recovery_code(user, code) else None


# ---------------------------------------------------------------------------
# Attempt budget and the pre-2FA stash
# ---------------------------------------------------------------------------


def attempt_allowed(request, user_id) -> bool:
    """Count one code attempt for the account and the client IP."""
    from core.ratelimit import client_ip, rate_limit_exceeded

    # Count both budgets on every attempt (no short-circuit).
    over_user = rate_limit_exceeded(f"2fa:user:{user_id}", USER_ATTEMPTS, ATTEMPT_WINDOW_SECONDS)
    over_ip = rate_limit_exceeded(f"2fa:ip:{client_ip(request)}", IP_ATTEMPTS, ATTEMPT_WINDOW_SECONDS)
    return not (over_user or over_ip)


def stash(request, user, purpose: str) -> None:
    """Remember a user whose password was right, without signing them in."""
    backend = getattr(user, "backend", None) or MODEL_BACKEND
    # A new session key, so a session id planted before the password step
    # doesn't carry over.
    request.session.cycle_key()
    request.session[STASH_KEY] = {
        "user_id": user.pk,
        "backend": backend,
        "purpose": purpose,
        "at": time.time(),
    }


def stashed(request):
    """``(user, stash)`` for a live stash, else ``(None, None)`` (expired ones are dropped)."""
    from core.models import User

    data = request.session.get(STASH_KEY)
    if not isinstance(data, dict):
        return None, None
    age = time.time() - float(data.get("at") or 0)
    user = User.objects.filter(pk=data.get("user_id"), is_active=True).first()
    if user is None or not 0 <= age <= STASH_SECONDS:
        clear_stash(request)
        return None, None
    if data.get("backend") not in settings.AUTHENTICATION_BACKENDS:
        data["backend"] = MODEL_BACKEND
    return user, data


def clear_stash(request) -> None:
    request.session.pop(STASH_KEY, None)
