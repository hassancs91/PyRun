"""Tool-call shaped REST over the endpoint registry."""

import logging
import time

from django.core.exceptions import RequestDataTooBig
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from core.ratelimit import client_ip, rate_limit_exceeded, seconds_to_window_reset
from core.services import api_audit
from core.views.api.decorators import add_cors_headers, cors_preflight_response
from .context import token_context
from .errors import RATE_WINDOW, ApiError, authenticate, error_response, touch_last_used
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
    code = ""
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
                raise ApiError(
                    "rate_limited",
                    "Rate limit exceeded.",
                    status=429,
                    retry_after=seconds_to_window_reset(RATE_WINDOW),
                )
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
            response = JsonResponse(
                data if slug == "_openapi.json" else {"ok": True, "data": data},
                # 202 Accepted while the work isn't done (scripts.run: a run still going).
                status=202 if spec is not None and spec.done and not data.get(spec.done) else 200,
            )
    except RequestDataTooBig:
        code = "too_large"
        response = error_response(413, code, "Request body is too large.", unified=True)
    except ApiError as exc:
        code = exc.code
        response = error_response(
            exc.status, exc.code, exc.message, retry_after=exc.retry_after, unified=True
        )
    response["X-Request-ID"] = ctx.request_id
    ms = int((time.monotonic() - started) * 1000)
    ok = response.status_code < 400
    # Every run/write call and every failure lands in ApiCall (the token's Activity).
    if api_audit.should_record(spec.access if spec is not None else "read", ok):
        api_audit.record(
            token, slug=slug, ok=ok, status=response.status_code, code=code, ms=ms,
            ip=client_ip(request), request_id=ctx.request_id,
        )
    if slug in {"_catalog", "_health", "_openapi.json"}:
        logger.info(
            "api.call slug=%s principal=token:%s ms=%d ok=%s request_id=%s",
            slug,
            token.pk,
            ms,
            ok,
            ctx.request_id,
        )
    return cors(response)
