"""
Webhook views for triggering scripts via HTTP.
"""

import json
import logging

from django.http import JsonResponse, HttpRequest
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from core.models import Script, Run
from core.ratelimit import client_ip, rate_limit_exceeded
from core.services.run_events import webhook_event
from core.services.script_inputs import ScriptInputsError, prepare_inputs
from core.tasks import queue_script_run

logger = logging.getLogger(__name__)

# Rate limit: 30 requests per minute per IP
WEBHOOK_RATE_LIMIT = 30
WEBHOOK_RATE_WINDOW = 60  # seconds


@csrf_exempt
@require_http_methods(["GET", "POST"])
def webhook_trigger_view(request: HttpRequest, token: str) -> JsonResponse:
    """
    Public endpoint to trigger script execution via webhook.

    Accepts GET and POST requests. For POST, the request body and query
    parameters are passed to the script as environment variables.

    Args:
        request: The HTTP request
        token: The webhook token (64-char URL-safe string)

    Returns:
        JsonResponse with:
        - 200: Script queued successfully (``"status": "queued"``, also when
          the script's Queue concurrency policy holds the run until the one
          ahead of it ends), or skipped (``"status": "skipped"``: the script's
          concurrency policy is Skip and it is already running)
        - 400: The script has a required input with no default; ``inputs``
          maps each such input to the reason (no run is created)
        - 403: Script is disabled
        - 404: Invalid token
        - 429: Rate limit exceeded
        - 500: Failed to queue
    """
    # Rate limiting by IP (fixed window, shared helper). client_ip honors
    # RATELIMIT_TRUSTED_PROXY_DEPTH so a reverse-proxy deploy doesn't collapse every
    # caller onto the proxy's IP.
    ip = client_ip(request)
    if rate_limit_exceeded(
        f"webhook_rate_{ip}", WEBHOOK_RATE_LIMIT, WEBHOOK_RATE_WINDOW
    ):
        logger.warning(f"Webhook rate limit exceeded for IP: {ip}")
        return JsonResponse(
            {"error": "Rate limit exceeded. Try again later."},
            status=429,
        )

    # Find script by token
    try:
        script = Script.objects.select_related("environment").get(webhook_token=token)
    except Script.DoesNotExist:
        logger.warning(f"Webhook trigger with invalid token: {token[:8]}...")
        return JsonResponse(
            {"error": "Invalid webhook token"},
            status=404,
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

    # Extract webhook data. It still reaches the task as before (the WEBHOOK_*
    # variables are built from it); the run's event carries the same values.
    webhook_data = _extract_webhook_data(request)

    # Create a new Run record. No user/session here — the run inherits its
    # workspace from the script (tenancy Stage 1).
    run = Run.objects.create(
        script=script,
        workspace_id=script.workspace_id,
        status=Run.Status.PENDING,
        triggered_by=None,  # Webhook-triggered, no user
        trigger_type=Run.TriggerType.API,
        code_snapshot=script.code,
        inputs=inputs,
        event=webhook_event(webhook_data),
    )

    # Queue for async execution
    try:
        queue_script_run(run, webhook_data=webhook_data)
        logger.info(f"Webhook triggered run {run.id} for script {script.name}")

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
        run.save()
        logger.error(f"Webhook failed to queue run {run.id}: {e}")

        return JsonResponse(
            {"error": "Failed to queue script execution"},
            status=500,
        )


def _inputs_rejected(error: ScriptInputsError) -> JsonResponse:
    """400 for a script whose declared inputs a webhook call can't satisfy."""
    return JsonResponse(
        {"error": error.message, "inputs": error.field_errors},
        status=400,
    )


def _extract_webhook_data(request: HttpRequest) -> dict:
    """
    Extract webhook data from the request.

    Returns a dict with:
    - method: GET or POST
    - body: Request body as string (for POST)
    - query: Query parameters as dict
    - content_type: Request content type
    """
    data = {
        "method": request.method,
        "query": dict(request.GET),
        "content_type": request.content_type or "",
    }

    # Extract body for POST requests
    if request.method == "POST":
        try:
            body = request.body.decode("utf-8")
            data["body"] = body

            # Try to parse as JSON for convenience
            if request.content_type == "application/json":
                try:
                    data["body_json"] = json.loads(body)
                except json.JSONDecodeError:
                    pass
        except Exception:
            data["body"] = ""

    return data
