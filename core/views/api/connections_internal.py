"""
Internal connections API (loopback-only): the door ``pyrunner_connections`` uses.

A run gets no connection credentials in its environment. The helper asks here,
with the signed per-run token the other internal APIs use; the run (and so the
script whose grants decide) comes from the token, never from the body.

POST /internal/connections/token  {"connection": "<name or provider>"}
    -> the auth material: headers and query parameters for the base URL, the
       bare token (API key and bearer kinds), the basic-auth pair or the SMTP
       login, plus what /info gives. Every secret value in it is recorded on the
       run first (core.services.connections.issue), so the run's output masks it.
POST /internal/connections/info   {"connection": "..."}  -> no secrets
POST /internal/connections/list   {}                     -> the granted connections, no secrets

Errors: 404 CONNECTION_NOT_FOUND (missing and not granted look the same),
409 AMBIGUOUS / CONNECTION_REVOKED / CONNECTION_EXPIRED / CONNECTION_UNSUPPORTED,
503 CONNECTION_UNREADABLE (the encryption key changed).
"""

import json

from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from core.views.api.decorators import internal_datastore_token_required

_MAX_BODY_BYTES = 16 * 1024


def _error(code, message, status):
    return JsonResponse({"error": {"code": code, "message": message}}, status=status)


def _payload(request):
    raw = request.read(_MAX_BODY_BYTES + 1)
    if len(raw) > _MAX_BODY_BYTES:
        return None, _error("TOO_LARGE", f"Request exceeds {_MAX_BODY_BYTES} bytes.", 413)
    try:
        data = json.loads(raw or b"{}")
    except (ValueError, TypeError):
        return None, _error("BAD_REQUEST", "Body must be valid JSON", 400)
    if not isinstance(data, dict):
        return None, _error("BAD_REQUEST", "Body must be a JSON object", 400)
    return data, None


def _run_id(request):
    return (getattr(request, "datastore_run", None) or {}).get("run_id")


def _lookup(request):
    from core.services.connections import ConnectionLookupError, resolve_for_run

    data, err = _payload(request)
    if err:
        return None, None, err
    try:
        run, connection = resolve_for_run(_run_id(request), data.get("connection"))
    except ConnectionLookupError as e:
        return None, None, _error(e.code, e.message, e.status)
    return run, connection, None


@csrf_exempt
@require_http_methods(["POST"])
@internal_datastore_token_required
def token(request: HttpRequest) -> JsonResponse:
    from core.services.connections import ConnectionLookupError, issue

    run, connection, err = _lookup(request)
    if err:
        return err
    try:
        material = issue(run, connection)
    except ConnectionLookupError as e:
        return _error(e.code, e.message, e.status)
    return JsonResponse({"ok": True, **material})


@csrf_exempt
@require_http_methods(["POST"])
@internal_datastore_token_required
def info(request: HttpRequest) -> JsonResponse:
    from core.services.connections import info as connection_info

    _run, connection, err = _lookup(request)
    if err:
        return err
    return JsonResponse({"ok": True, **connection_info(connection)})


@csrf_exempt
@require_http_methods(["POST"])
@internal_datastore_token_required
def list_connections(request: HttpRequest) -> JsonResponse:
    from core.services.connections import ConnectionLookupError, granted_connections, run_for
    from core.services.connections import info as connection_info

    _data, err = _payload(request)
    if err:
        return err
    try:
        run = run_for(_run_id(request))
    except ConnectionLookupError as e:
        return _error(e.code, e.message, e.status)
    return JsonResponse(
        {"ok": True, "connections": [connection_info(c) for c in granted_connections(run.script)]}
    )
