"""Shared external API auth, errors and fixed-window limits.

The plugin API retains its existing envelope and auth messages. Unified API
tokens use constant-shape auth failures and the same pre-auth throttle machinery.
"""

from .exceptions import ApiError  # noqa: F401 - compatibility export

from django.conf import settings
from django.http import JsonResponse
from django.db.models import Q
from django.utils import timezone

from core.models import APIToken
from core.ratelimit import client_ip, rate_limit_exceeded, rate_limit_over, seconds_to_window_reset
from core.views.api.decorators import _extract_token, add_cors_headers

RATE_WINDOW = 60


def error_response(status, code, message, *, retry_after=None, unified=False):
    body = {"error": {"code": code, "message": message}}
    if unified:
        body["ok"] = False
        if retry_after is not None:
            body["error"]["retry_after"] = retry_after
    response = JsonResponse(body, status=status)
    if retry_after is not None:
        response["Retry-After"] = str(retry_after)
    return add_cors_headers(response)


def rate_limited(message, *, unified=False):
    return error_response(
        429,
        "rate_limited" if unified else "RATE_LIMITED",
        message,
        retry_after=seconds_to_window_reset(RATE_WINDOW),
        unified=unified,
    )


def authenticate(request, *, scope="plugin", raw=None, kind="bearer"):
    unified = scope == "api"
    key = f"{'api' if unified else 'plugin_api'}_authfail_{client_ip(request)}"
    limit = getattr(
        settings, "API_AUTHFAIL_RATE_LIMIT" if unified else "PLUGIN_API_AUTHFAIL_RATE_LIMIT", 20
    )
    if rate_limit_over(key, limit, RATE_WINDOW):
        return None, rate_limited("Too many failed authentication attempts.", unified=unified)

    def fail(message):
        if rate_limit_exceeded(key, limit, RATE_WINDOW):
            return None, rate_limited("Too many failed authentication attempts.", unified=unified)
        return None, error_response(
            401,
            "unauthorized" if unified else "UNAUTHORIZED",
            "Invalid API token." if unified else message,
            unified=unified,
        )

    raw = _extract_token(request) if raw is None else raw
    if not raw:
        return fail("API token required")
    # Every scope stores the SHA-256 digest (F53 for datastore/plugin tokens).
    digest = APIToken.digest(raw)
    lookup = ~Q(scope="api") & Q(token=digest)
    if raw.startswith(("pyrun_", "mcpurl_")):
        lookup |= Q(scope="api", token=digest)
    token = (
        APIToken.objects.select_related("workspace", "brain").filter(lookup, is_active=True).first()
    )
    if token is None:
        return fail("Invalid API token")
    if token.expires_at and token.expires_at <= timezone.now():
        return fail("API token has expired")
    # A stored digest is not itself a usable bearer credential, on ANY lane.
    if token.scope == "api" and (not raw.startswith(("pyrun_", "mcpurl_")) or token.kind != kind):
        return fail("Invalid API token")
    if token.scope != scope:
        return None, error_response(
            403,
            "scope_mismatch" if unified else "SCOPE_MISMATCH",
            "This token is not scoped to this API"
            if unified
            else "This token is not scoped to a plugin API",
            unified=unified,
        )
    if token.workspace_id is None:
        return None, error_response(
            403,
            "scope_mismatch" if unified else "SCOPE_MISMATCH",
            "This token's workspace no longer exists",
            unified=unified,
        )
    if unified and token.brain_id and token.brain.workspace_id != token.workspace_id:
        return fail("Invalid API token")
    if unified and kind == "url" and (
        not token.expires_at or not token.permissions
        or not APIToken.url_permissions_ok(token.permissions)
    ):
        return fail("Invalid API token")
    return token, None


def touch_last_used(token):
    APIToken.objects.filter(id=token.id).update(last_used_at=timezone.now())
