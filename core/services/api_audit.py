"""
The ``ApiCall`` audit trail of the unified API and MCP (2.5 Stage 2, T13).

The transports (``core.api.rest``, ``core.api.mcp``) call ``record`` once per
authenticated call. ``should_record`` says which: every call that isn't a
plain read (``run`` and ``write`` endpoints) and every failure, whatever the
endpoint. Reads that succeed only touch ``APIToken.last_used_at``, as before.

Recording never fails the call it records: a database error is logged and the
caller gets its answer anyway.
"""

import logging
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

# Rows older than this are deleted by the daily housekeeping task.
RETENTION_DAYS = 30


def should_record(access, ok):
    """Whether a call is audited: a failure, or anything but a read."""
    return not ok or access != "read"


def record(token, *, slug, ok, status, code="", ms=0, ip="", request_id=""):
    """Write one ``ApiCall`` row for ``token``'s call (never raises)."""
    from core.models import ApiCall

    try:
        ApiCall.objects.create(
            token_id=token.pk,
            workspace_id=token.workspace_id,
            slug=str(slug)[:64],
            ok=bool(ok),
            status=int(status),
            code="" if ok else str(code)[:64],
            ms=max(0, int(ms)),
            ip=str(ip)[:64],
            request_id=str(request_id)[:64],
        )
    except Exception:
        logger.exception("api.audit_failed slug=%s request_id=%s", slug, request_id)


def purge(now=None):
    """Delete audit rows older than ``RETENTION_DAYS``; returns how many."""
    from core.models import ApiCall

    cutoff = (now or timezone.now()) - timedelta(days=RETENTION_DAYS)
    deleted, _ = ApiCall.objects.filter(created_at__lt=cutoff).delete()
    return deleted
