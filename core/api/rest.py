"""Tool-call shaped REST over the endpoint registry."""

import logging
import time

from django.core.exceptions import RequestDataTooBig
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from core.ratelimit import rate_limit_exceeded
from core.views.api.decorators import add_cors_headers, cors_preflight_response
from .context import token_context
from .errors import ApiError, authenticate, error_response, rate_limited, touch_last_used
from .registry import registry
from .openapi import document
from .schema import loads, query_params

logger = logging.getLogger(__name__)


def cors(response):
    add_cors_headers(response)
    response["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response["Cache-Control"] = "no-store"
    return response


@csrf_exempt
def dispatch(request, slug):
    started = time.monotonic()
    if request.method == "OPTIONS":
        return cors(cors_preflight_response())
    token, failure = authenticate(request, scope="api")
    if failure is not None:
        return cors(failure)
    ctx = token_context(token)
    spec = None
    try:
        if request.method not in {"GET", "POST"}:
            raise ApiError("method_not_allowed", "Use GET or POST.", status=405)
        touch_last_used(token)
        if request.method == "GET" and request.body:
            raise ApiError("invalid_input", "GET requests cannot carry a body.")
        if slug in {"_catalog", "_health", "_openapi.json"}:
            if request.GET or (request.body and loads(request.body) != {}):
                raise ApiError("invalid_input", "This endpoint takes no parameters.")
            if rate_limit_exceeded(f"api_meta_{token.pk}", 60, 60):
                return cors(rate_limited("Rate limit exceeded.", unified=True))
            data = (
                document(ctx) if slug == "_openapi.json" else
                {"endpoints": [e.catalog_entry() for e in registry.visible(ctx)]}
                if slug == "_catalog"
                else {"status": "ok"}
            )
        else:
            spec = registry.get(slug)
            if spec is None:
                raise ApiError("not_found", "Unknown endpoint.", status=404)
            if request.method == "GET":
                if spec.access != "read":
                    raise ApiError("method_not_allowed", "This endpoint requires POST.", status=405)
                params = query_params(request.GET, spec.input)
            else:
                if request.content_type != "application/json":
                    raise ApiError("invalid_input", "Use application/json.", status=415)
                if request.GET:
                    raise ApiError("invalid_input", "POST parameters belong in the JSON body.")
                params = loads(request.body)
            data = registry.call(slug, ctx, params)
        if spec is not None and spec.text:
            # A text endpoint (runs.output) answers with that one field, as text.
            response = HttpResponse(data[spec.text], content_type="text/plain; charset=utf-8")
        else:
            response = JsonResponse(data if slug == "_openapi.json" else {"ok": True, "data": data})
    except RequestDataTooBig:
        response = error_response(413, "too_large", "Request body is too large.", unified=True)
    except ApiError as exc:
        response = error_response(
            exc.status, exc.code, exc.message, retry_after=exc.retry_after, unified=True
        )
    response["X-Request-ID"] = ctx.request_id
    if slug in {"_catalog", "_health", "_openapi.json"}:
        logger.info(
            "api.call slug=%s principal=token:%s ms=%d ok=%s request_id=%s",
            slug,
            token.pk,
            int((time.monotonic() - started) * 1000),
            response.status_code < 400,
            ctx.request_id,
        )
    return cors(response)
