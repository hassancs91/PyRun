"""
What a running script reports back through ``pyrunner_run``: its result and its
progress (2.5 Stage 0, ticket T02).

The script's helper POSTs to the internal loopback endpoints
(``core/views/api/runs_internal.py``), which hand the run id from the signed
per-run token (never from the body) to ``store_result`` / ``store_progress``.
Both:

- mask secret values with the same set execution masks output with
  (``core.executor.mask_set_for_display``) BEFORE anything is stored, and refuse
  to store when a secret can't be resolved, so every reader (run page, RunView,
  notifications, backups) only ever sees the masked copy;
- write only while the run is RUNNING, in one conditional UPDATE, so a write
  racing the run's finish can never change a finished run.

The readers' helpers (the capped copies notifications carry, the run page's
display forms) live here too, next to the caps they depend on.
"""

import json
import math

from django.core.exceptions import ValidationError
from django.utils import timezone

# A result is meant for a caller (webhook reply, API, MCP tool), not for bulk
# data: big outputs belong in a DataStore, Table or Storage object.
RESULT_MAX_BYTES = 256 * 1024
# A progress message is one status line for the run page.
PROGRESS_MESSAGE_MAX_CHARS = 500
# Notifications carry the result only while it is this small (webhook payload),
# or an excerpt of it (email, chat channels).
NOTIFY_RESULT_MAX_BYTES = 16 * 1024
EMAIL_RESULT_MAX_CHARS = 2000
CHANNEL_RESULT_MAX_CHARS = 300


class RunReportError(Exception):
    """A report that can't be stored. ``code`` and ``status`` go to the helper."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def json_size(value) -> int:
    """Bytes ``value`` takes as compact UTF-8 JSON (what the caps measure)."""
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    )


def _running_run(run_id):
    from core.models import Run

    try:
        run = Run.objects.select_related("script").filter(pk=run_id).first()
    except (ValueError, ValidationError):
        run = None
    if run is None:
        raise RunReportError("RUN_NOT_FOUND", "This run no longer exists.", 404)
    if run.status != Run.Status.RUNNING:
        raise RunReportError(
            "RUN_NOT_RUNNING",
            f"This run is {run.status}, so what it reports can no longer change.",
            409,
        )
    return run


def _mask(run, value):
    """``value`` with the run's secret values masked, or refuse to store it."""
    from core.executor import mask_secrets_in_value, mask_set_for_display
    from core.services.secret_backends import SecretResolutionError

    try:
        masks = mask_set_for_display(run)
    except SecretResolutionError:
        raise RunReportError(
            "SECRETS_UNAVAILABLE",
            "A secret of this run couldn't be resolved, so this couldn't be checked "
            "for secret values. It was not saved.",
            503,
        ) from None
    try:
        return mask_secrets_in_value(value, masks)
    except RecursionError:
        raise RunReportError("BAD_REQUEST", "The value is nested too deeply.") from None


def _write(run, **fields) -> None:
    """Store ``fields`` on ``run`` only if it is still RUNNING."""
    from core.models import Run

    if not Run.objects.filter(pk=run.pk, status=Run.Status.RUNNING).update(**fields):
        raise RunReportError(
            "RUN_NOT_RUNNING", "This run has finished, so what it reports can no longer change.", 409
        )


def store_result(run_id, value) -> int:
    """Mask and store ``value`` as the run's result. Returns its size in bytes.

    Replaces any earlier result: the last ``set_result()`` call wins.
    """
    if not isinstance(value, dict):
        raise RunReportError("BAD_REQUEST", "'result' must be a JSON object.")
    run = _running_run(run_id)
    masked = _mask(run, value)
    try:
        size = json_size(masked)
    except ValueError:
        raise RunReportError("BAD_REQUEST", "NaN and Infinity aren't valid JSON.") from None
    except RecursionError:
        raise RunReportError("BAD_REQUEST", "The value is nested too deeply.") from None
    if size > RESULT_MAX_BYTES:
        raise RunReportError(
            "RESULT_TOO_LARGE",
            f"The result is {size:,} bytes; the limit is {RESULT_MAX_BYTES:,}. "
            "Store large data in a DataStore or Storage and return a reference to it.",
            413,
        )
    _write(run, result=masked)
    return size


def check_progress(update: dict) -> dict:
    """Validate a progress update; return the fields it sets.

    ``update`` holds ``pct`` and/or ``message``. A key that is present sets
    that field (``None`` clears it); a missing key leaves it unchanged.
    """
    fields = {k: update[k] for k in ("pct", "message") if k in update}
    if not fields:
        raise RunReportError("BAD_REQUEST", "Give 'pct', 'message' or both.")
    pct = fields.get("pct")
    if pct is not None:
        if isinstance(pct, bool) or not isinstance(pct, (int, float)) or not math.isfinite(pct):
            raise RunReportError("BAD_REQUEST", "'pct' must be a number from 0 to 100.")
        if not 0 <= pct <= 100:
            raise RunReportError("BAD_REQUEST", "'pct' must be a number from 0 to 100.")
    message = fields.get("message")
    if message is not None:
        if not isinstance(message, str):
            raise RunReportError("BAD_REQUEST", "'message' must be a string.")
        if len(message) > PROGRESS_MESSAGE_MAX_CHARS:
            raise RunReportError(
                "BAD_REQUEST",
                f"'message' is {len(message)} characters; the limit is {PROGRESS_MESSAGE_MAX_CHARS}.",
            )
    return fields


def store_progress(run_id, update: dict) -> dict:
    """Merge ``update`` into the run's progress, mask it and store it.

    Returns the stored progress, ``{"pct", "message", "updated_at"}``.
    """
    fields = check_progress(update)
    run = _running_run(run_id)
    current = run.progress if isinstance(run.progress, dict) else {}
    progress = {"pct": current.get("pct"), "message": current.get("message")}
    progress.update(fields)
    if progress["message"] is not None:
        progress["message"] = _mask(run, progress["message"])
    progress["updated_at"] = timezone.now().isoformat()
    _write(run, progress=progress)
    return progress


# --------------------------------------------------------------------------- #
# Readers
# --------------------------------------------------------------------------- #


def progress_for_display(progress) -> dict | None:
    """``Run.progress`` as the run page shows it: whole-number pct, text message."""
    if not isinstance(progress, dict):
        return None
    pct = progress.get("pct")
    if isinstance(pct, bool) or not isinstance(pct, (int, float)):
        pct = None
    message = progress.get("message")
    if not isinstance(message, str):
        message = ""
    if pct is None and not message:
        return None
    return {
        "pct": None if pct is None else max(0, min(100, int(round(pct)))),
        "message": message,
        "updated_at": progress.get("updated_at"),
    }


def result_text(result) -> str:
    """The result as indented JSON, keys sorted (PostgreSQL's jsonb reorders them)."""
    return json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True)


def _excerpt(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


def notification_result(run) -> tuple[dict | None, bool]:
    """``(result, too_large)`` for a webhook notification payload.

    The result itself when it fits ``NOTIFY_RESULT_MAX_BYTES``; otherwise
    ``(None, True)`` so a receiver knows to fetch it from the run.
    """
    if run.result is None:
        return None, False
    if json_size(run.result) > NOTIFY_RESULT_MAX_BYTES:
        return None, True
    return run.result, False


def result_excerpt(run, max_chars: int, *, indent: int | None = None) -> str:
    """The start of the result as JSON text, for email and chat notifications."""
    if run.result is None:
        return ""
    if indent is None:
        text = json.dumps(run.result, ensure_ascii=False, sort_keys=True)
    else:
        text = json.dumps(run.result, ensure_ascii=False, sort_keys=True, indent=indent)
    return _excerpt(text, max_chars)
