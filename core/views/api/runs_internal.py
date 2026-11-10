"""
Internal run-report API (loopback-only): what a running script tells PyRun
about itself through ``pyrunner_run``.

POST /internal/runs/result    body: {"result": {...}}
POST /internal/runs/progress  body: {"pct": 40, "message": "..."}  (either or both)
POST /internal/runs/respond   body: {"status": 200, "body": ..., "headers": {...}}

Authenticated by the same signed per-run token as the other internal routes.
The run is the one named in that token, never one named in the body, so a
script can only report on itself. Writes are refused once the run has
finished. Masking, caps and the RUNNING-only write live in
``core.services.run_reports``.
"""

import json
import logging

from django.core.exceptions import RequestDataTooBig
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from core.services.run_reports import RunReportError, store_progress, store_response, store_result
from core.views.api.decorators import internal_datastore_token_required

logger = logging.getLogger(__name__)


def _reject_constant(name):
    raise ValueError(f"{name} isn't valid JSON")


def _json_object(request: HttpRequest) -> dict:
    try:
        data = json.loads(request.body or b"{}", parse_constant=_reject_constant)
    except RequestDataTooBig:
        raise RunReportError("REQUEST_TOO_LARGE", "The request body is too large.", 413) from None
    except (ValueError, TypeError, RecursionError):
        raise RunReportError("BAD_REQUEST", "Body must be valid JSON.") from None
    if not isinstance(data, dict):
        raise RunReportError("BAD_REQUEST", "Body must be a JSON object.")
    return data


def _error(run_id, e: RunReportError) -> JsonResponse:
    logger.info("Run %s report refused (%s): %s", run_id, e.code, e.message)
    return JsonResponse({"error": {"code": e.code, "message": e.message}}, status=e.status)


@csrf_exempt
@require_http_methods(["POST"])
@internal_datastore_token_required
def result(request: HttpRequest) -> JsonResponse:
    run_id = request.datastore_run.get("run_id")
    try:
        data = _json_object(request)
        if "result" not in data:
            raise RunReportError("BAD_REQUEST", "Missing 'result'.")
        size = store_result(run_id, data["result"])
    except RunReportError as e:
        return _error(run_id, e)
    return JsonResponse({"ok": True, "bytes": size})


@csrf_exempt
@require_http_methods(["POST"])
@internal_datastore_token_required
def progress(request: HttpRequest) -> JsonResponse:
    run_id = request.datastore_run.get("run_id")
    try:
        stored = store_progress(run_id, _json_object(request))
    except RunReportError as e:
        return _error(run_id, e)
    return JsonResponse({"ok": True, "progress": stored})


@csrf_exempt
@require_http_methods(["POST"])
@internal_datastore_token_required
def respond(request: HttpRequest) -> JsonResponse:
    run_id = request.datastore_run.get("run_id")
    try:
        size = store_response(run_id, _json_object(request))
    except RunReportError as e:
        return _error(run_id, e)
    return JsonResponse({"ok": True, "bytes": size})
