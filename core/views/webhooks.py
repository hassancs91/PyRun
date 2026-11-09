"""
Webhook views for triggering scripts via HTTP.
"""

import base64
import json
import logging

from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.http import JsonResponse, HttpRequest, UnreadablePostError
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from core.models import Script, Run
from core.ratelimit import client_ip, rate_limit_exceeded, rate_limit_over, seconds_to_window_reset
from core.services import webhook_signatures
from core.services.run_events import webhook_event
from core.services.script_inputs import ScriptInputsError, prepare_inputs
from core.services.webhook_request import BODY_BASE64, event_headers
from core.tasks import queue_script_run

logger = logging.getLogger(__name__)

# Per-IP scanning floor: every webhook call from one address, whatever the
# script. Each script also has its own limit (Script.webhook_rate_per_minute
# and webhook_rate_burst); one address never gets past this one.
WEBHOOK_RATE_LIMIT = 300
WEBHOOK_RATE_WINDOW = 60  # seconds
# Per-IP auth failures (a wrong token, a bad signature). Once over it, the
# address gets 429 for the rest of the window before any lookup.
WEBHOOK_AUTHFAIL_LIMIT = 30


@csrf_exempt
@require_http_methods(["GET", "POST"])
def webhook_trigger_view(request: HttpRequest, token: str) -> JsonResponse:
    """
    Public endpoint to trigger script execution via webhook.

    Accepts GET and POST requests. The run's event carries the request
    (method, path, query, headers, body...); the WEBHOOK_* environment
    variables carry what they did before 2.5, and a POST's body is also in
    the run's body file (PYRUNNER_EVENT_BODY_FILE).

    Args:
        request: The HTTP request
        token: The webhook token (64-char URL-safe string)

    Returns:
        JsonResponse with:
        - 200: Script queued successfully (``"status": "queued"``, also when
          the script's Queue concurrency policy holds the run until the one
          ahead of it ends), or skipped (``"status": "skipped"``: the script's
          concurrency policy is Skip and it is already running)
        - 503: A maintenance window blocks webhook calls right now
          (``"status": "maintenance"``, ``until``); the ``Retry-After``
          header says how many seconds until it ends. The refused call is
          listed as a skipped run.
        - 400: The script has a required input with no default; ``inputs``
          maps each such input to the reason (no run is created). Also when
          the request body couldn't be read (the sender hung up mid-body).
        - 401: The script requires a signature and the call's is missing or
          wrong (``reason``); no run. Counts as an auth failure.
        - 403: Script is disabled
        - 404: Invalid token (counts as an auth failure)
        - 413: The body is over DATA_UPLOAD_MAX_MEMORY_SIZE (no run)
        - 429: Rate limit exceeded (``Retry-After``): the caller's address
          sent over WEBHOOK_RATE_LIMIT calls or WEBHOOK_AUTHFAIL_LIMIT auth
          failures this minute, or the script's own limit is reached
        - 500: Failed to queue

    When a run was created (200, 503, 500), the status sent is stored on it
    (``response_code``) for the script page's Recent deliveries.
    """
    # Rate limiting by IP (fixed window, shared helper). client_ip honors
    # RATELIMIT_TRUSTED_PROXY_DEPTH so a reverse-proxy deploy doesn't collapse every
    # caller onto the proxy's IP.
    ip = client_ip(request)
    if rate_limit_exceeded(
        f"webhook_rate_{ip}", WEBHOOK_RATE_LIMIT, WEBHOOK_RATE_WINDOW
    ):
        logger.warning(f"Webhook rate limit exceeded for IP: {ip}")
        return _rate_limited(seconds_to_window_reset(WEBHOOK_RATE_WINDOW))

    # An address that keeps guessing tokens or signatures is cut off before
    # it costs a lookup.
    authfail_key = f"webhook_authfail_{ip}"
    if rate_limit_over(authfail_key, WEBHOOK_AUTHFAIL_LIMIT, WEBHOOK_RATE_WINDOW):
        return _rate_limited(seconds_to_window_reset(WEBHOOK_RATE_WINDOW))

    # Find script by token
    try:
        script = Script.objects.select_related("environment").get(webhook_token=token)
    except Script.DoesNotExist:
        logger.warning(f"Webhook trigger with invalid token: {token[:8]}...")
        if rate_limit_exceeded(authfail_key, WEBHOOK_AUTHFAIL_LIMIT, WEBHOOK_RATE_WINDOW):
            return _rate_limited(seconds_to_window_reset(WEBHOOK_RATE_WINDOW))
        return JsonResponse(
            {"error": "Invalid webhook token"},
            status=404,
        )

    # A script that requires a signature checks it first, over the body
    # exactly as received: a call without a valid one learns nothing more
    # about the script and never gets a run.
    if webhook_signatures.required(script):
        try:
            body = request.body
        except (RequestDataTooBig, UnreadablePostError) as e:
            return _body_refused(script, e)
        failure = webhook_signatures.verify(script, request.headers, body)
        if failure:
            logger.warning(
                f"Webhook signature refused ({failure}) for script {script.name} from {ip}"
            )
            if rate_limit_exceeded(authfail_key, WEBHOOK_AUTHFAIL_LIMIT, WEBHOOK_RATE_WINDOW):
                return _rate_limited(seconds_to_window_reset(WEBHOOK_RATE_WINDOW))
            return JsonResponse(
                {"error": "Invalid webhook signature", "reason": failure},
                status=401,
            )

    # Check if script can run (enabled and not archived)
    if not script.can_run:
        reason = "archived" if script.is_archived else "disabled"
        logger.info(f"Webhook trigger rejected - script {reason}: {script.name}")
        return JsonResponse(
            {"error": f"Script is {reason}"},
            status=403,
        )

    # A webhook call supplies no inputs: the run gets each input's default, and
    # a script with a required input that has none is refused before any run
    # row exists.
    try:
        inputs = prepare_inputs(script, None)
    except ScriptInputsError as e:
        return _inputs_rejected(e)

    # The script's own limit: a call over it gets 429 and no run.
    retry_after = _script_rate_limited(script)
    if retry_after:
        logger.info(f"Webhook rate limit of script {script.name} reached")
        return _rate_limited(retry_after, "This script's webhook rate limit is reached. Try again later.")

    # Extract webhook data. It still reaches the task as before (the WEBHOOK_*
    # variables and the body file are built from it); the run's event carries
    # the same values plus the request's path, headers and address.
    try:
        webhook_data = _extract_webhook_data(request)
    except (RequestDataTooBig, UnreadablePostError) as e:
        return _body_refused(script, e)

    # Create a new Run record. No user/session here — the run inherits its
    # workspace from the script (tenancy Stage 1). ``response_code`` is the
    # answer every outcome below gets but a maintenance block or a failure to
    # queue, which set their own before answering. It is set here, before a
    # task exists, so no worker's save of the run can race it.
    run = Run.objects.create(
        script=script,
        workspace_id=script.workspace_id,
        status=Run.Status.PENDING,
        triggered_by=None,  # Webhook-triggered, no user
        trigger_type=Run.TriggerType.API,
        code_snapshot=script.code,
        inputs=inputs,
        event=webhook_event(
            webhook_data,
            path=_event_path(request, token),
            headers=event_headers(request.headers),
            remote_ip=ip,
        ),
        response_code=200,
    )

    # Queue for async execution
    try:
        queue_script_run(run, webhook_data=webhook_data)
        logger.info(f"Webhook triggered run {run.id} for script {script.name}")

        if run.is_maintenance_skipped:
            # A 503 with Retry-After: senders that retry deliver it again
            # once the window is over. The run is cancelled, no task.
            _record_response_code(run, 503)
            block = run.maintenance_block
            response = JsonResponse({
                "status": "maintenance",
                "error": "Paused for maintenance. Try again later.",
                "until": block.until.isoformat(),
                "run_id": str(run.id),
                "script": script.name,
            }, status=503)
            response["Retry-After"] = str(block.retry_after())
            return response
        if run.is_skipped:
            # A 200: the call did what the script asked for. A non-2xx would
            # make most senders retry into the same running script.
            return JsonResponse({
                "status": "skipped",
                "reason": "already_running",
                "run_id": str(run.id),
                "script": script.name,
            })
        return JsonResponse({
            "status": "queued",
            "run_id": str(run.id),
            "script": script.name,
        })

    except ScriptInputsError as e:
        # The schema changed since the check above; queue_script_run already
        # deleted the row, so don't save it back as a failed run.
        return _inputs_rejected(e)

    except Exception as e:
        run.status = Run.Status.FAILED
        run.stderr = f"Failed to queue task: {str(e)}"
        run.response_code = 500
        run.save()
        logger.error(f"Webhook failed to queue run {run.id}: {e}")

        return JsonResponse(
            {"error": "Failed to queue script execution"},
            status=500,
        )


def _rate_limited(retry_after: int, message: str = "Rate limit exceeded. Try again later.") -> JsonResponse:
    """429 with the seconds until the window that refused the call ends."""
    response = JsonResponse({"error": message}, status=429)
    response["Retry-After"] = str(retry_after)
    return response


def _script_rate_limited(script: Script) -> int | None:
    """Count one call against the script's limits; the seconds to wait when
    it is over one of them (None when the call may go on).

    Two fixed windows: at most ``webhook_rate_burst`` calls in one second and
    at most ``webhook_rate_per_minute`` in one minute. A call refused by the
    burst doesn't use up the minute.
    """
    burst = min(script.webhook_rate_burst, script.webhook_rate_per_minute)
    if rate_limit_exceeded(f"webhook_script_burst_{script.pk}", burst, 1):
        return 1
    if rate_limit_exceeded(
        f"webhook_script_rate_{script.pk}", script.webhook_rate_per_minute, WEBHOOK_RATE_WINDOW
    ):
        return seconds_to_window_reset(WEBHOOK_RATE_WINDOW)
    return None


def _body_refused(script: Script, error: Exception) -> JsonResponse:
    """413 for a body over DATA_UPLOAD_MAX_MEMORY_SIZE, 400 for one that
    couldn't be read (the sender hung up mid-body)."""
    if isinstance(error, RequestDataTooBig):
        limit = settings.DATA_UPLOAD_MAX_MEMORY_SIZE
        logger.info(f"Webhook body over {limit} bytes refused for script {script.name}")
        return JsonResponse(
            {"error": f"Request body too large: the limit is {limit} bytes."},
            status=413,
        )
    logger.warning(f"Webhook body for script {script.name} could not be read: {error}")
    return JsonResponse({"error": "Could not read the request body."}, status=400)


def _inputs_rejected(error: ScriptInputsError) -> JsonResponse:
    """400 for a script whose declared inputs a webhook call can't satisfy."""
    return JsonResponse(
        {"error": error.message, "inputs": error.field_errors},
        status=400,
    )


def _record_response_code(run: Run, code: int) -> None:
    """Store the status a call is about to get, on a run with no task."""
    run.response_code = code
    Run.objects.filter(pk=run.pk).update(response_code=code)


def _event_path(request: HttpRequest, token: str) -> str:
    """The request path for the event, the webhook token cut to the 8
    characters the logs show: the event is shown on the run page and to API
    callers, and the full token is what lets anyone trigger the script."""
    return request.path.replace(token, f"{token[:8]}...")


def _extract_webhook_data(request: HttpRequest) -> dict:
    """
    Extract webhook data from the request.

    Returns a dict with:
    - method: GET or POST
    - body: Request body as string (for POST); "" when it isn't UTF-8 text
    - body_json: the body parsed, for an application/json body that parses
    - body_base64: (POST) the body base64-encoded, only when it isn't UTF-8
    - query: Query parameters as dict
    - content_type: Request content type

    Raises RequestDataTooBig for a body over DATA_UPLOAD_MAX_MEMORY_SIZE and
    UnreadablePostError when the body can't be read.
    """
    data = {
        "method": request.method,
        "query": dict(request.GET),
        "content_type": request.content_type or "",
    }

    # Extract body for POST requests
    if request.method == "POST":
        raw = request.body
        try:
            body = raw.decode("utf-8")
        except UnicodeDecodeError:
            # Not text: WEBHOOK_BODY stays "" as it always was, and the bytes
            # reach the run's body file.
            data["body"] = ""
            data[BODY_BASE64] = base64.b64encode(raw).decode("ascii")
            return data
        data["body"] = body

        # Try to parse as JSON for convenience. A body nested too deep or
        # holding a number too long for Python stays text only, like any
        # body that isn't valid JSON.
        if request.content_type == "application/json":
            try:
                data["body_json"] = json.loads(body)
            except (ValueError, RecursionError):
                pass

    return data
