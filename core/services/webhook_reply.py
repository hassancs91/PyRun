"""
The reply a webhook call that waits for its run gets (2.5, T22).

A script sets it with ``pyrunner_run.respond(status, body, headers)``. The
helper POSTs to ``/internal/runs/respond`` and ``run_reports.store_response``
stores it, masked, in the run's result under ``RESPONSE_KEY``:

    {"http_response": {"status": 200, "headers": {...}, "body": ...}}

``check_response`` is the rule that store applies (the helper checks the same
rules, so a mistake fails in the script). ``finished_reply`` builds the HTTP
answer from a finished run for the webhook view, reading the stored copy
leniently: a result written some other way (``set_result`` with that key, a
restored backup) only ever sends what the rules allow.

What a reply may carry:

- **status**: an integer, clamped to 200-599 (a 1xx would leave the caller
  waiting for the final answer; a 3xx can't redirect, as Location isn't
  allowed).
- **body**: ``None`` (empty), a string (sent as text) or any other JSON value
  (sent as JSON).
- **headers**: only ``ALLOWED_HEADERS``. The reply comes from PyRun's own
  origin, the console's: a script must not set cookies there, redirect from
  it or serve it a page, so ``Content-Type`` is limited to data types
  (``ALLOWED_CONTENT_TYPES``) and every script reply also gets a sandboxing
  Content-Security-Policy.
"""

import json
import re

from django.http import HttpResponse, JsonResponse

RESPONSE_KEY = "http_response"

# Lower-case name -> the name sent.
ALLOWED_HEADERS = {
    "content-type": "Content-Type",
    "cache-control": "Cache-Control",
    "content-language": "Content-Language",
    "retry-after": "Retry-After",
}
ALLOWED_CONTENT_TYPES = frozenset({
    "application/json",
    "application/x-www-form-urlencoded",
    "application/xml",
    "text/csv",
    "text/plain",
    "text/xml",
})
# application/problem+json, application/vnd.api+json, ...
_JSON_SUFFIX_RE = re.compile(r"^application/[a-z0-9][a-z0-9.!#$&^_-]*\+json$")
HEADER_VALUE_MAX_CHARS = 1000
STATUS_MIN, STATUS_MAX = 200, 599

TEXT_TYPE = "text/plain; charset=utf-8"
JSON_TYPE = "application/json"
# Even a text/xml reply can't run script or load anything on PyRun's origin.
SANDBOX_POLICY = "sandbox; default-src 'none'"


def content_type_allowed(value: str) -> bool:
    media = value.split(";", 1)[0].strip().lower()
    return media in ALLOWED_CONTENT_TYPES or bool(_JSON_SUFFIX_RE.match(media))


def clamp_status(status: int) -> int:
    return max(STATUS_MIN, min(STATUS_MAX, status))


def _header(name, value):
    """``(name to send, value)`` for an allowed header, or a ValueError naming the problem."""
    canonical = ALLOWED_HEADERS.get(name.lower()) if isinstance(name, str) else None
    if canonical is None:
        raise ValueError(
            f"The header {name!r} can't be set: a reply may set only "
            + ", ".join(ALLOWED_HEADERS.values())
            + "."
        )
    if not isinstance(value, str):
        raise ValueError(f"The {canonical} header's value must be a string.")
    if len(value) > HEADER_VALUE_MAX_CHARS or any(c in value for c in "\r\n\0"):
        raise ValueError(
            f"The {canonical} header's value must be one line of at most "
            f"{HEADER_VALUE_MAX_CHARS} characters."
        )
    if canonical == "Content-Type" and not content_type_allowed(value):
        raise ValueError(
            f"Content-Type {value!r} isn't allowed: use application/json (or "
            "application/...+json), text/plain, text/csv, application/xml, text/xml "
            "or application/x-www-form-urlencoded."
        )
    return canonical, value


def check_response(data: dict) -> dict:
    """The reply ``data`` (``status``, ``body``, ``headers``) asks for, made
    storable: status clamped, header names as sent. Raises ValueError."""
    status = data.get("status", 200)
    if isinstance(status, bool) or not isinstance(status, int):
        raise ValueError("'status' must be an integer.")
    headers = data.get("headers")
    if headers is None:
        headers = {}
    if not isinstance(headers, dict):
        raise ValueError("'headers' must be an object of header names and values.")
    checked = {}
    for name, value in headers.items():
        canonical, value = _header(name, value)
        if canonical in checked:
            raise ValueError(f"The {canonical} header is given twice.")
        checked[canonical] = value
    return {"status": clamp_status(status), "headers": checked, "body": data.get("body")}


def stored_response(result):
    """The reply stored in ``result``, as ``(status, headers, body)``, or None.

    Lenient: only what the rules allow is kept (status clamped, headers that
    aren't allowed or valid left out), and a value that isn't a reply at all
    gives None.
    """
    envelope = result.get(RESPONSE_KEY) if isinstance(result, dict) else None
    if not isinstance(envelope, dict):
        return None
    status = envelope.get("status")
    if isinstance(status, bool) or not isinstance(status, int):
        return None
    headers = {}
    raw_headers = envelope.get("headers")
    for name, value in (raw_headers.items() if isinstance(raw_headers, dict) else ()):
        try:
            canonical, value = _header(name, value)
        except ValueError:
            continue
        headers[canonical] = value
    return clamp_status(status), headers, envelope.get("body")


def _script_reply(status: int, headers: dict, body) -> HttpResponse:
    if body is None:
        content, content_type = b"", TEXT_TYPE
    elif isinstance(body, str):
        content, content_type = body.encode("utf-8"), TEXT_TYPE
    else:
        content = json.dumps(body, ensure_ascii=False).encode("utf-8")
        content_type = JSON_TYPE
    response = HttpResponse(
        content, status=status, content_type=headers.pop("Content-Type", content_type)
    )
    for name, value in headers.items():
        response[name] = value
    response["Content-Security-Policy"] = SANDBOX_POLICY
    return response


def finished_reply(run) -> HttpResponse:
    """The answer for a sync call whose run has ended.

    The reply the script set, however the run ended. Without one: 200 with
    the run's status and result when it succeeded, else 500 (the result too,
    if it set one).
    """
    stored = stored_response(run.result)
    if stored is not None:
        return _script_reply(*stored)
    data = {
        "status": run.status,
        "run_id": str(run.id),
        "script": run.script.name,
        "result": run.result,
    }
    if run.is_successful:
        return JsonResponse(data)
    return JsonResponse({**data, "error": "The run did not succeed."}, status=500)
