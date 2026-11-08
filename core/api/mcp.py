"""Stateless JSON-only Streamable HTTP; no SDK, sessions or SSE workers."""

import base64
import binascii
import json
import logging
import time

from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.core.serializers.json import DjangoJSONEncoder
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from core.models import GlobalSettings
from core.ratelimit import rate_limit_exceeded
from core.views.api.decorators import add_cors_headers
from pyrunner.version import __version__
from .context import token_context
from .errors import ApiError, authenticate, error_response, rate_limited, touch_last_used
from .registry import registry
from .schema import loads

logger = logging.getLogger(__name__)
MODERN = "2026-07-28"
LEGACY = ("2025-11-25", "2025-06-18", "2025-03-26")
VERSIONS = (MODERN, *LEGACY)
META = "io.modelcontextprotocol/"
SERVER_INFO = {"name": "PyRun", "version": __version__}


class ProtocolError(Exception):
    def __init__(self, code, message, *, status=400, data=None):
        self.code, self.message, self.status, self.data = code, message, status, data


def rpc_error(exc, request_id=None):
    error = {"code": exc.code, "message": exc.message}
    if exc.data is not None:
        error["data"] = exc.data
    body = {"jsonrpc": "2.0", "error": error}
    if request_id is not None:
        body["id"] = request_id
    return JsonResponse(body, status=exc.status)


def cors(response):
    add_cors_headers(response)
    response["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    response["Access-Control-Allow-Headers"] = (
        "Authorization, X-API-Key, Content-Type, MCP-Protocol-Version, Mcp-Method, Mcp-Name"
    )
    response["Access-Control-Expose-Headers"] = "Retry-After, X-Request-ID, MCP-Protocol-Version"
    response["Cache-Control"] = "no-store"
    return response


def _version(request, method, params):
    header = request.headers.get("MCP-Protocol-Version")
    meta = params.get("_meta", {})
    if not isinstance(meta, dict):
        raise ProtocolError(-32602, "Invalid metadata.")
    modern = META + "protocolVersion" in meta or header == MODERN
    if method == "initialize" and not modern:
        requested = params.get("protocolVersion")
        if not isinstance(requested, str) or not isinstance(params.get("capabilities"), dict):
            raise ProtocolError(-32602, "Invalid initialization parameters.")
        info = params.get("clientInfo")
        if not isinstance(info, dict) or not all(
            isinstance(info.get(k), str) for k in ("name", "version")
        ):
            raise ProtocolError(-32602, "Invalid client information.")
        return requested if requested in LEGACY else LEGACY[0]
    version = meta.get(META + "protocolVersion") if modern else header or LEGACY[-1]
    if not isinstance(version, str):
        raise ProtocolError(-32602, "Protocol version metadata is required.")
    if version not in VERSIONS:
        logger.warning("mcp.unsupported_protocol_version")
        raise ProtocolError(
            -32022,
            "Unsupported protocol version",
            data={
                "supported": list(VERSIONS),
                "requested": version,
            },
        )
    if modern:
        if version != MODERN:
            raise ProtocolError(-32602, "Use initialize for legacy protocol versions.")
        if not isinstance(meta.get(META + "clientCapabilities"), dict):
            raise ProtocolError(-32602, "Client capabilities metadata is required.")
        if header != version or request.headers.get("Mcp-Method") != method:
            raise ProtocolError(-32020, "Missing or mismatched request headers.")
        if method in {"tools/call", "prompts/get", "resources/read"}:
            name = request.headers.get("Mcp-Name", "")
            if name.startswith("=?base64?") and name.endswith("?="):
                try:
                    name = base64.b64decode(name[9:-2], validate=True).decode("utf-8")
                except (ValueError, UnicodeError, binascii.Error) as exc:
                    raise ProtocolError(-32020, "Invalid name header.") from exc
            if name != params.get("name", params.get("uri")):
                raise ProtocolError(-32020, "Missing or mismatched name header.")
    return version


def _result(method, params, ctx, version):
    if method == "initialize" and version in LEGACY:
        return {
            "protocolVersion": version,
            "serverInfo": SERVER_INFO,
            "capabilities": {"tools": {}},
        }
    if method == "server/discover" and version == MODERN:
        return {"supportedVersions": list(VERSIONS), "capabilities": {"tools": {}}}
    if method == "ping" and version in LEGACY:
        return {}
    if method == "tools/list":
        if params.get("cursor") is not None:
            raise ProtocolError(-32602, "Unknown cursor.")
        return {
            "tools": [
                {
                    "name": spec.mcp_name,
                    "title": spec.title,
                    "description": spec.description,
                    "inputSchema": spec.input,
                    "annotations": {"readOnlyHint": spec.access == "read"},
                }
                for spec in registry.visible(ctx)
            ]
        }
    if method == "tools/call":
        if not isinstance(params.get("name"), str) or not isinstance(
            params.get("arguments", {}), dict
        ):
            raise ProtocolError(-32602, "Expected a tool name and object arguments.")
        # Resolve against every permitted tool, so a write the token holds while
        # writes are off answers writes_disabled instead of "unknown tool".
        spec = next((e for e in registry.permitted(ctx) if e.mcp_name == params["name"]), None)
        if spec is None:
            raise ProtocolError(-32602, "Unknown tool.")
        try:
            data = registry.call(spec.slug, ctx, params.get("arguments", {}))
            is_error, status, retry_after = False, 200, None
        except ApiError as exc:
            data = {"error": exc.as_dict()}
            is_error = True
            status, retry_after = (429 if exc.status == 429 else 200), exc.retry_after
        return (
            {
                "content": [{"type": "text", "text": json.dumps(data, cls=DjangoJSONEncoder)}],
                "structuredContent": data,
                "isError": is_error,
            },
            status,
            retry_after,
        )
    raise ProtocolError(-32601, "Method not found.", status=404)


@csrf_exempt
def dispatch(request, url_token=None):
    origin = request.headers.get("Origin")
    allowed = {request.build_absolute_uri("/").rstrip("/"), *settings.MCP_ALLOWED_ORIGINS}
    if origin is not None and origin not in allowed:
        return cors(error_response(403, "forbidden", "Origin is not allowed.", unified=True))
    if request.method == "OPTIONS":
        return cors(HttpResponse(status=204))
    if request.method != "POST":
        response = HttpResponse(status=405)
        response["Allow"] = "POST, OPTIONS"
        return cors(response)
    if url_token is not None:
        if not GlobalSettings.get_settings().mcp_url_tokens_enabled:
            # Go through the same failure counter and response shape as a bad token.
            url_token = ""
        token, failure = authenticate(request, scope="api", raw=url_token, kind="url")
    else:
        token, failure = authenticate(request, scope="api")
    if failure is not None:
        return cors(failure)
    ctx = token_context(
        token, transport="mcp", deadline=time.monotonic() + settings.MCP_CALL_BUDGET_SECONDS
    )
    request_id = None
    version = None
    try:
        if request.content_type != "application/json":
            raise ProtocolError(-32600, "Use application/json.", status=415)
        accept = request.headers.get("Accept", "")
        if not all(t in accept for t in ("application/json", "text/event-stream")):
            raise ProtocolError(
                -32600, "Accept must include application/json and text/event-stream.", status=406
            )
        try:
            body = loads(request.body)
        except ApiError as exc:
            raise ProtocolError(-32700, "Parse error.") from exc
        if (
            not isinstance(body, dict)
            or body.get("jsonrpc") != "2.0"
            or not isinstance(body.get("method"), str)
            or ("id" in body and type(body["id"]) not in (int, str))
            or "result" in body
            or "error" in body
        ):
            raise ProtocolError(-32600, "Invalid request; batches are not supported.")
        request_id = body.get("id")
        method, params = body["method"], body.get("params", {})
        if not isinstance(params, dict):
            raise ProtocolError(-32602, "Parameters must be an object.")
        version = _version(request, method, params)
        touch_last_used(token)
        if method != "tools/call" and rate_limit_exceeded(f"api_meta_{token.pk}", 60, 60):
            return cors(rate_limited("Rate limit exceeded.", unified=True))
        if request_id is None:
            # A notification must never invoke a tool, particularly a write.
            if not method.startswith("notifications/"):
                raise ProtocolError(-32600, "Requests require an id.")
            response = HttpResponse(status=202)
        else:
            result = _result(method, params, ctx, version)
            data, status, retry_after = result if isinstance(result, tuple) else (result, 200, None)
            if version == MODERN:
                data.update(resultType="complete", _meta={META + "serverInfo": SERVER_INFO})
                if method in {"tools/list", "server/discover"}:
                    data.update(cacheScope="private", ttlMs=0)
            response = JsonResponse(
                {"jsonrpc": "2.0", "id": request_id, "result": data}, status=status
            )
            if retry_after is not None:
                response["Retry-After"] = str(retry_after)
        logger.info(
            "mcp.call method=%s principal=token:%s request_id=%s",
            "notification" if request_id is None else method,
            token.pk,
            ctx.request_id,
        )
    except RequestDataTooBig:
        response = rpc_error(
            ProtocolError(-32600, "Request body is too large.", status=413), request_id
        )
    except ProtocolError as exc:
        response = rpc_error(exc, request_id)
    response["X-Request-ID"] = ctx.request_id
    if version is not None:
        response["MCP-Protocol-Version"] = version
    return cors(response)
