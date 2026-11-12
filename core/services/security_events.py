"""
Security events and the account-security emails (2.5 Stage 11, T45).

``record`` writes one ``SecurityEvent`` row; the auth views, the API token
form, the ``manage.py user`` command and the django-axes lockout signal call
it. Recording never fails the request it records: a database error is logged
and the caller carries on.

The two emails are sent from django-q tasks so the web request never waits on
(or reveals, by its timing) the mail server:

- ``send_password_reset`` looks the account up, issues the token and sends the
  reset email. The forgot-password view enqueues it for every address, known
  or not, so the response is the same either way.
- ``send_login_notification`` tells a user who opted in that their account was
  just signed in to.
"""

import logging
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger(__name__)

# Rows older than this are deleted by the daily housekeeping task.
RETENTION_DAYS = 90


def request_meta(request):
    """``(ip, user_agent)`` of ``request``, the IP proxy-aware like the lockout."""
    from core.ratelimit import client_ip

    if request is None:
        return "", ""
    return client_ip(request), request.META.get("HTTP_USER_AGENT", "")


def record(kind, *, request=None, user=None, email="", detail=""):
    """Write one ``SecurityEvent`` (never raises)."""
    from core.models import SecurityEvent

    ip, user_agent = request_meta(request)
    if not email and user is not None:
        email = user.email
    try:
        SecurityEvent.objects.create(
            user=user if user is not None and user.pk else None,
            email=str(email or "").strip().lower()[:254],
            kind=kind,
            ip=str(ip)[:64],
            user_agent=str(user_agent)[:255],
            detail=str(detail)[:255],
        )
    except Exception:
        logger.exception("security_event.record_failed kind=%s", kind)


def purge(now=None):
    """Delete events older than ``RETENTION_DAYS``; returns how many."""
    from core.models import SecurityEvent

    cutoff = (now or timezone.now()) - timedelta(days=RETENTION_DAYS)
    deleted, _ = SecurityEvent.objects.filter(created_at__lt=cutoff).delete()
    return deleted


def on_axes_lockout(sender, request=None, username=None, ip_address=None, **kwargs):
    """django-axes ``user_locked_out``: the attempt that locked an account, or
    one refused while it is locked."""
    from core.models import User

    email = str(username or "").strip().lower()
    try:
        user = User.objects.filter(email=email).first() if email else None
    except Exception:
        # Runs inside the login request: never fail it over the log.
        logger.exception("security_event.lockout_lookup_failed")
        user = None
    record("lockout", request=request, user=user, email=email)


def reset_url(base_url, token):
    """The absolute reset link for ``token`` under ``base_url`` (scheme + host)."""
    path = reverse("auth:reset_password", kwargs={"token": token.token})
    return base_url.rstrip("/") + path


# ---------------------------------------------------------------------------
# django-q tasks
# ---------------------------------------------------------------------------


def send_password_reset(email, base_url):
    """Task: issue a reset token for ``email`` and email the link.

    Nothing happens for an unknown or unverified address; the person asking
    saw the same message either way. Returns whether an email was sent.
    """
    from core.email import send_password_reset_email
    from core.models import PasswordResetToken, User

    user = User.objects.filter(email=str(email).strip().lower()).first()
    if user is None or not user.is_verified:
        return False
    token = PasswordResetToken.create_for_user(user)
    return send_password_reset_email(user.email, reset_url(base_url, token))


def send_login_notification(user_id, ip, user_agent, when, base_url):
    """Task: tell an opted-in user their account was just signed in to."""
    from core.email import send_login_notification_email
    from core.models import User

    user = User.objects.filter(pk=user_id, is_active=True).first()
    if user is None or not user.notify_on_login:
        return False
    change_url = base_url.rstrip("/") + reverse("auth:change_password")
    return send_login_notification_email(user.email, when, ip, user_agent, change_url)
