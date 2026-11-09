"""
Webhook signatures: a script's webhook can require every call to be signed.

The check runs in the webhook view before any run exists, over the request
body exactly as received. A call that fails it gets 401 and no run, and counts
against the caller's per-IP auth-failure budget (core.views.webhooks).

Presets (``Script.webhook_signature``):

- ``stripe``: header ``Stripe-Signature: t=<unix time>,v1=<hex>[,v1=<hex>...]``.
  The signed text is ``<t>.<body>``, the key is the endpoint's signing secret
  as Stripe shows it (``whsec_...``, used whole), and ``t`` must be within the
  script's tolerance of the server's clock. Any ``v1`` may match (Stripe sends
  two while a secret is being rolled).
- ``github``: header ``X-Hub-Signature-256: sha256=<hex>`` over the body.
- ``shopify``: header ``X-Shopify-Hmac-Sha256: <base64>`` over the body.
- ``hmac_sha256``: the script's own header, optional prefix (``sha256=``) and
  encoding (hex or base64). With a timestamp header, the signed text is
  ``<timestamp>.<body>`` and the timestamp (Unix seconds) must be within the
  tolerance.

Every preset is HMAC-SHA256 with the secret's UTF-8 bytes as the key, and
signatures are compared in constant time.
"""

import base64
import binascii
import hashlib
import hmac
import logging
import re
import time

from core.models import Script
from core.services.encryption_service import EncryptionError

logger = logging.getLogger(__name__)

Preset = Script.WebhookSignature
Encoding = Script.WebhookSignatureEncoding

STRIPE_HEADER = "Stripe-Signature"
GITHUB_HEADER = "X-Hub-Signature-256"
GITHUB_PREFIX = "sha256="
SHOPIFY_HEADER = "X-Shopify-Hmac-Sha256"

# The header each fixed preset reads (the custom preset names its own).
PRESET_HEADERS = {
    Preset.STRIPE: STRIPE_HEADER,
    Preset.GITHUB: GITHUB_HEADER,
    Preset.SHOPIFY: SHOPIFY_HEADER,
}

# A header name a custom preset may use: an HTTP token of letters, digits and
# hyphens (servers drop or mangle names with underscores).
HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*\Z")

# Why a call was refused (the ``reason`` in the 401 body).
MISSING = "missing"  # the signature header, or the timestamp header, isn't there
MALFORMED = "malformed"  # the header can't be read (prefix, encoding, fields)
EXPIRED = "expired"  # signed, but the timestamp is outside the tolerance
MISMATCH = "mismatch"  # no signature in the header matches the body
NOT_CONFIGURED = "not_configured"  # no secret stored, or it can't be decrypted


def required(script: Script) -> bool:
    """True when the script's webhook calls must be signed."""
    return script.webhook_signature != Preset.NONE


def header_for(script: Script) -> str:
    """The header the script's signature is read from ("" for none)."""
    if script.webhook_signature == Preset.HMAC_SHA256:
        return script.webhook_signature_header
    return PRESET_HEADERS.get(script.webhook_signature, "")


def preset_headers() -> dict[str, str]:
    """{preset: header} for the fixed presets, for the console's help text."""
    return {str(preset): header for preset, header in PRESET_HEADERS.items()}


def digest(secret: str, message: bytes) -> bytes:
    """HMAC-SHA256 of ``message`` keyed with the secret's UTF-8 bytes."""
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()


def verify(script: Script, headers, body: bytes, now: float | None = None) -> str | None:
    """Check one call's signature. None when it is valid, else the reason.

    ``headers`` is ``request.headers`` (case-insensitive lookups); ``body`` is
    ``request.body``, the bytes exactly as received.
    """
    preset = script.webhook_signature
    if preset == Preset.NONE:
        return None
    try:
        secret = script.get_webhook_signature_secret()
    except EncryptionError:
        logger.error(
            "Webhook signing secret of script %s can't be decrypted (was ENCRYPTION_KEY "
            "changed?); refusing its calls until the secret is saved again",
            script.pk,
        )
        return NOT_CONFIGURED
    if not secret:
        return NOT_CONFIGURED
    now = time.time() if now is None else now

    if preset == Preset.STRIPE:
        return _verify_stripe(secret, headers.get(STRIPE_HEADER), body, script, now)
    if preset == Preset.GITHUB:
        return _verify_one(secret, headers.get(GITHUB_HEADER), body, GITHUB_PREFIX, Encoding.HEX)
    if preset == Preset.SHOPIFY:
        return _verify_one(secret, headers.get(SHOPIFY_HEADER), body, "", Encoding.BASE64)
    if preset == Preset.HMAC_SHA256:
        return _verify_custom(secret, headers, body, script, now)
    logger.error("Script %s has an unknown webhook signature preset %r", script.pk, preset)
    return NOT_CONFIGURED


def _decode(value: str, encoding: str) -> bytes | None:
    """A written signature as bytes, or None when it isn't valid ``encoding``."""
    value = value.strip()
    if not value:
        return None
    try:
        if encoding == Encoding.BASE64:
            return base64.b64decode(value, validate=True)
        return bytes.fromhex(value)
    except (binascii.Error, ValueError):
        return None


def _timestamp_error(raw: str, tolerance: int, now: float) -> str | None:
    try:
        timestamp = int(raw.strip())
    except ValueError:
        return MALFORMED
    if abs(now - timestamp) > tolerance:
        return EXPIRED
    return None


def _verify_one(secret, header, body, prefix, encoding, signed=None) -> str | None:
    """One signature in ``header`` (after ``prefix``) over ``signed`` (the body)."""
    if not header:
        return MISSING
    value = header.strip()
    if prefix:
        if value[: len(prefix)].lower() != prefix.lower():
            return MALFORMED
        value = value[len(prefix):]
    received = _decode(value, encoding)
    if received is None:
        return MALFORMED
    expected = digest(secret, body if signed is None else signed)
    return None if hmac.compare_digest(expected, received) else MISMATCH


def _verify_stripe(secret, header, body, script, now) -> str | None:
    if not header:
        return MISSING
    timestamp, candidates = None, []
    for item in header.split(","):
        key, sep, value = item.strip().partition("=")
        if not sep:
            continue
        if key == "t":
            timestamp = value.strip()
        elif key == "v1":
            candidates.append(value)
    if not timestamp or not candidates:
        return MALFORMED
    expected = digest(secret, timestamp.encode("ascii", "replace") + b"." + body)
    received = [_decode(value, Encoding.HEX) for value in candidates]
    if not any(r is not None and hmac.compare_digest(expected, r) for r in received):
        return MALFORMED if all(r is None for r in received) else MISMATCH
    # Checked after the signature, as Stripe's libraries do: a valid but old
    # call is a replay.
    return _timestamp_error(timestamp, script.webhook_signature_tolerance, now)


def _verify_custom(secret, headers, body, script, now) -> str | None:
    name = script.webhook_signature_header
    if not name:
        return NOT_CONFIGURED
    timestamp_name = script.webhook_signature_timestamp_header
    timestamp = None
    signed = body
    if timestamp_name:
        timestamp = headers.get(timestamp_name)
        if not timestamp:
            return MISSING
        signed = timestamp.strip().encode("ascii", "replace") + b"." + body
    error = _verify_one(
        secret, headers.get(name), body, script.webhook_signature_prefix,
        script.webhook_signature_encoding, signed=signed,
    )
    if error or timestamp is None:
        return error
    return _timestamp_error(timestamp, script.webhook_signature_tolerance, now)


def restored_settings(data: dict) -> tuple[dict, bool]:
    """A backup's webhook signature and rate fields, made safe to store.

    Returns ``(fields, dropped)``: an older backup has none of them (no
    signature, the default limits); a value that isn't valid falls back to
    its default, and ``dropped`` is True when that cost the script its
    signature (an unknown preset), so the restore can warn about it.
    """
    signature = data.get("webhook_signature", Preset.NONE)
    dropped = signature not in Preset.values
    if dropped:
        signature = Preset.NONE
    encoding = data.get("webhook_signature_encoding")
    secret = data.get("webhook_signature_secret_encrypted")

    def text(key, max_length):
        value = data.get(key)
        return value[:max_length] if isinstance(value, str) else ""

    def number(key, default, high):
        value = data.get(key)
        ok = isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= high
        return value if ok else default

    fields = {
        "webhook_signature": signature,
        "webhook_signature_secret_encrypted": secret if isinstance(secret, str) else "",
        "webhook_signature_header": text("webhook_signature_header", 100),
        "webhook_signature_prefix": text("webhook_signature_prefix", 50),
        "webhook_signature_encoding": encoding if encoding in Encoding.values else Encoding.HEX,
        "webhook_signature_timestamp_header": text("webhook_signature_timestamp_header", 100),
        "webhook_signature_tolerance": number(
            "webhook_signature_tolerance", 300, Script.MAX_WEBHOOK_SIGNATURE_TOLERANCE
        ),
        "webhook_rate_per_minute": number(
            "webhook_rate_per_minute", 60, Script.MAX_WEBHOOK_RATE_PER_MINUTE
        ),
        "webhook_rate_burst": number("webhook_rate_burst", 30, Script.MAX_WEBHOOK_RATE_PER_MINUTE),
    }
    return fields, dropped
