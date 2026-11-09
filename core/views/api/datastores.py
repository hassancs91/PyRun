"""
The legacy datastore REST API: ``/api/v1/datastores/…``, for datastore tokens.

Each URL is an alias of a registry endpoint (``core/api/endpoints/datastores.py``):
the view turns the token into a call context and answers with that handler's
result as it is, so the body is the one this API has always returned. Errors
keep this API's envelope, ``{"error": {"code": "NOT_FOUND", "message": ...}}``,
with upper-case codes. Unified API tokens call the same handlers as
``/api/v1/datastores.list`` (and ``.get``, ``.entries``, ``.entry``).
"""

from django.http import JsonResponse, HttpRequest
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from core.api.context import CallContext
from core.api.errors import error_response
from core.api.exceptions import ApiError
from core.api.registry import registry
from core.api.endpoints.datastores import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from core.views.api.decorators import api_token_required, add_cors_headers, token_workspace

def legacy_context(token):
    """A datastore token as a registry caller holding ``datastores:read``.

    A single-store token is bound to its store (the binding a Unified API token
    gets from the same FK) and acts in its store's workspace. ``api_token_required``
    has already refused a token without a workspace.
    """
    return CallContext(
        workspace=token_workspace(token),
        principal="token",
        principal_id=str(token.pk),
        permissions=frozenset({"datastores:read"}),
        datastore_id=token.datastore_id,
        transport="legacy",
    )


def _answer(request, slug, params):
    try:
        data = registry.call(slug, legacy_context(request.api_token), params)
    except ApiError as exc:
        return error_response(exc.status, exc.code.upper(), exc.message)
    return add_cors_headers(JsonResponse(data))


def _int_param(request, name, default, maximum=None):
    """A lenient page number or size: bad text is the default, then clamped."""
    try:
        value = max(1, int(request.GET.get(name, default)))
    except (ValueError, TypeError):
        return default
    return min(maximum, value) if maximum else value


@csrf_exempt
@require_http_methods(["GET", "OPTIONS"])
@api_token_required
def list_datastores(request: HttpRequest) -> JsonResponse:
    """GET /api/v1/datastores/ = ``datastores.list``.

    ``{"datastores": [{"name", "description", "entry_count", "created_at",
    "updated_at"}], "count": n}``
    """
    return _answer(request, "datastores.list", {})


@csrf_exempt
@require_http_methods(["GET", "OPTIONS"])
@api_token_required
def get_datastore(request: HttpRequest, name: str) -> JsonResponse:
    """GET /api/v1/datastores/<name>/ = ``datastores.get``.

    ``{"name", "description", "entry_count", "created_at", "updated_at"}``
    """
    return _answer(request, "datastores.get", {"name": name})


@csrf_exempt
@require_http_methods(["GET", "OPTIONS"])
@api_token_required
def list_entries(request: HttpRequest, name: str) -> JsonResponse:
    """GET /api/v1/datastores/<name>/entries/?page=&page_size= = ``datastores.entries``.

    ``{"entries": [{"key", "value", "created_at", "updated_at"}], "count",
    "page", "page_size", "total_pages"}``; page_size is 1-100 (default 50).
    """
    return _answer(request, "datastores.entries", {
        "name": name,
        "page": _int_param(request, "page", 1),
        "page_size": _int_param(request, "page_size", DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE),
    })


@csrf_exempt
@require_http_methods(["GET", "OPTIONS"])
@api_token_required
def get_entry(request: HttpRequest, name: str, key: str) -> JsonResponse:
    """GET /api/v1/datastores/<name>/entries/<key>/ = ``datastores.entry``.

    ``{"key", "value", "created_at", "updated_at"}``
    """
    return _answer(request, "datastores.entry", {"name": name, "key": key})
