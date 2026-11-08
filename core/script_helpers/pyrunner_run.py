"""
PyRun run context for scripts: the inputs this run started with, what started
it, its id, and a way to report back a structured result and live progress.

Stdlib only. ``inputs()``, ``event()`` and ``run_id()`` read what PyRun hands
every run (the ``PYRUNNER_RUN_FILE`` JSON file and ``PYRUNNER_RUN_ID``). ``set_result()`` and
``progress()`` POST to PyRun's internal loopback API with this run's signed
token, so a script can only ever report on its own run. PyRun masks secret
values in both before it stores them.

Usage:
    import pyrunner_run

    params = pyrunner_run.inputs()             # every declared input, JSON ones too
    event = pyrunner_run.event()               # {"kind": "webhook", "received_at": ..., ...}
    pyrunner_run.progress(10, "Fetching pages")
    ...
    pyrunner_run.set_result({"pages": 12, "ok": True})

    @pyrunner_run.on_stop                      # Stop button, SDK cancel or timeout
    def save_work():
        flush_buffers()
"""

import atexit
import http.client
import json
import math
import os
import signal
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request

__all__ = ["RunError", "event", "inputs", "on_stop", "progress", "run_id", "set_result"]

# Same limits PyRun enforces (core/services/run_reports.py); checked here too so
# a mistake fails in the script with a clear message instead of a refused request.
RESULT_MAX_BYTES = 256 * 1024
PROGRESS_MESSAGE_MAX_CHARS = 500
# progress() sends at most one update per this many seconds. Calls in between
# are merged, and the latest is sent when the interval is up (or at exit).
PROGRESS_MIN_INTERVAL = 1.0

_RESULT_TIMEOUT = 30
_PROGRESS_TIMEOUT = 5


class RunError(Exception):
    """PyRun couldn't provide or store something for this run.

    ``code`` is PyRun's error code when it refused a request (for example
    ``RUN_NOT_RUNNING`` or ``RESULT_TOO_LARGE``), else None.
    """

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def _run_file() -> dict:
    path = os.environ.get("PYRUNNER_RUN_FILE")
    if not path:
        raise RunError("Not inside a PyRun run: PYRUNNER_RUN_FILE is not set.")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        raise RunError(f"Could not read the run file {path}: {e}") from e
    if not isinstance(data, dict):
        raise RunError(f"The run file {path} does not hold a JSON object.")
    return data


def inputs() -> dict:
    """The inputs this run started with, defaults filled in.

    Every declared input is here, JSON objects included (only text, number and
    yes/no inputs are also set as ``INPUT_<NAME>`` environment variables).
    Returns ``{}`` when the script declares no inputs. Each call returns a new
    dict, so changing it changes nothing else.
    """
    value = _run_file().get("inputs")
    return value if isinstance(value, dict) else {}


def event() -> dict:
    """What started this run: one envelope for every trigger.

    Always has ``kind`` (``manual``, ``schedule``, ``webhook``, ``channel``,
    ``plugin`` or ``api``) and ``received_at`` (ISO 8601). The other keys
    depend on the kind: a webhook's ``method``, ``query``, ``content_type``,
    ``body`` and ``body_json``; a chat message's ``text``, ``sender`` and
    ``reply_ref``; a schedule's ``schedule_id`` and ``fired_at``. An event
    over 1 MB carries ``truncated: true`` (and ``body_truncated: true`` when
    the webhook body was cut). Each call returns a new dict.
    """
    value = _run_file().get("event")
    return value if isinstance(value, dict) else {}


def run_id() -> str:
    """This run's id: 32 hex characters, the same value as ``PYRUNNER_RUN_ID``."""
    value = os.environ.get("PYRUNNER_RUN_ID")
    if not value:
        raise RunError("Not inside a PyRun run: PYRUNNER_RUN_ID is not set.")
    return value


def _post(path: str, body: bytes, timeout: float) -> dict:
    base = os.environ.get("PYRUNNER_INTERNAL_URL")
    token = os.environ.get("PYRUNNER_INTERNAL_TOKEN")
    if not base or not token:
        raise RunError("Not inside a PyRun run: PyRun's internal API is not available.")
    req = urllib.request.Request(
        f"{base.rstrip('/')}/internal/runs/{path}",
        data=body,
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
        code = None
        try:
            error = (json.loads(raw) if raw else {}).get("error") or {}
            code = error.get("code")
            message = error.get("message") or raw.decode("utf-8", "ignore")
        except (ValueError, AttributeError):
            message = raw.decode("utf-8", "ignore")
        raise RunError(f"PyRun refused it (HTTP {e.code}): {message}", code=code) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        raise RunError(f"Could not reach PyRun: {e}") from None
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        raise RunError(
            "PyRun's answer wasn't JSON: check that PYRUNNER_INTERNAL_URL points at PyRun."
        ) from None


def set_result(result: dict) -> None:
    """Store ``result`` as this run's structured result.

    ``result`` must be a dict that ``json.dumps`` can encode (no NaN or
    Infinity), at most 256 KB as JSON. It shows on the run page, in the
    plugin SDK's ``RunView`` and in notifications. Calling it again replaces
    the earlier result. Raises ``RunError`` when PyRun refuses or can't be
    reached, so a lost result never goes unnoticed.
    """
    if not isinstance(result, dict):
        raise TypeError(f"set_result() takes a dict (a JSON object), not {type(result).__name__}")
    try:
        text = json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as e:
        raise ValueError(f"The result can't be stored as JSON: {e}") from None
    size = len(text.encode("utf-8"))
    if size > RESULT_MAX_BYTES:
        raise ValueError(
            f"The result is {size:,} bytes as JSON; the limit is {RESULT_MAX_BYTES:,}. "
            "Store large data in a DataStore or Storage and return a reference to it."
        )
    _post("result", ('{"result":' + text + "}").encode("utf-8"), _RESULT_TIMEOUT)


# --------------------------------------------------------------------------- #
# Progress: throttled, best effort
# --------------------------------------------------------------------------- #

# Re-entrant: an on_stop() callback runs in the main thread wherever the
# stop signal caught it, possibly inside progress() itself.
_progress_lock = threading.RLock()
_progress_sent_at = None  # time.monotonic() of the last send; None = nothing sent yet
_progress_pending = None  # fields merged from calls not sent yet
_progress_timer = None
_progress_atexit = False


def _check_progress(pct, message) -> dict:
    if pct is None and message is None:
        raise ValueError("progress() needs pct, message or both.")
    fields = {}
    if pct is not None:
        if isinstance(pct, bool) or not isinstance(pct, (int, float)):
            raise TypeError(f"pct must be a number from 0 to 100, not {type(pct).__name__}")
        if not math.isfinite(pct) or not 0 <= pct <= 100:
            raise ValueError(f"pct must be a number from 0 to 100, not {pct!r}")
        fields["pct"] = pct
    if message is not None:
        if not isinstance(message, str):
            raise TypeError(f"message must be a string, not {type(message).__name__}")
        if len(message) > PROGRESS_MESSAGE_MAX_CHARS:
            raise ValueError(
                f"message is {len(message)} characters; the limit is {PROGRESS_MESSAGE_MAX_CHARS}."
            )
        fields["message"] = message
    return fields


def progress(pct=None, message=None) -> None:
    """Show how far this run is, live on its run page.

    ``pct`` is a number from 0 to 100, ``message`` a short status line (at
    most 500 characters). Give either or both; what you leave out keeps its
    last value, and ``message=""`` clears the message.

    Best effort: at most one update per second is sent, calls in between are
    merged and the latest is sent when the second is up. If PyRun can't take
    an update, a warning goes to stderr and the script carries on. Wrong
    arguments raise ``TypeError`` / ``ValueError`` straight away.
    """
    global _progress_pending, _progress_timer, _progress_atexit
    fields = _check_progress(pct, message)
    with _progress_lock:
        _progress_pending = {**(_progress_pending or {}), **fields}
        if not _progress_atexit:
            atexit.register(_flush_progress_at_exit)
            _progress_atexit = True
        if _progress_sent_at is not None:
            wait = PROGRESS_MIN_INTERVAL - (time.monotonic() - _progress_sent_at)
            if wait > 0:
                if _progress_timer is None:
                    _progress_timer = threading.Timer(wait, _flush_progress)
                    _progress_timer.daemon = True
                    _progress_timer.start()
                return
        _send_pending_progress()


def _send_pending_progress() -> None:
    """Send the merged pending update. Call with ``_progress_lock`` held."""
    global _progress_pending, _progress_sent_at
    fields, _progress_pending = _progress_pending, None
    if not fields:
        return
    _progress_sent_at = time.monotonic()
    try:
        _post("progress", json.dumps(fields).encode("utf-8"), _PROGRESS_TIMEOUT)
    except RunError as e:
        sys.stderr.write(f"pyrunner_run: progress update not saved: {e}\n")


def _flush_progress() -> None:
    global _progress_timer
    with _progress_lock:
        _progress_timer = None
        _send_pending_progress()


def _flush_progress_at_exit() -> None:
    """Send an update still waiting for its interval, so the last one isn't lost."""
    global _progress_timer
    with _progress_lock:
        if _progress_timer is not None:
            _progress_timer.cancel()
            _progress_timer = None
        _send_pending_progress()


# --------------------------------------------------------------------------- #
# Graceful stop
# --------------------------------------------------------------------------- #

_stop_callbacks = []
_stop_handler_installed = False
_stopping = False


def _stop_signals() -> list:
    """SIGTERM, plus SIGBREAK on Windows (where PyRun sends CTRL_BREAK_EVENT)."""
    signals = [signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):
        signals.append(signal.SIGBREAK)
    return signals


def on_stop(callback):
    """Run ``callback()`` when PyRun stops this run, before it is killed.

    PyRun stops a run when someone presses Stop, when a plugin cancels it and
    when it times out. It sends SIGTERM (CTRL_BREAK on Windows), then gives the
    script its stop grace period (a script setting, 10 seconds by default) to
    exit before it kills it. Use the callback to save work, close
    connections or report a partial result with ``set_result()``.

    The callbacks run once, in the order they were added, in the main thread.
    An exception in one is printed to stderr and the next still runs. When they
    are done the script exits (``SystemExit`` with code 128 + the signal
    number), so ``finally`` blocks and atexit handlers still run; a callback
    can call ``sys.exit()`` itself to choose the code. Returns ``callback``, so
    it also works as a decorator. Call it from the main thread. It replaces a
    SIGTERM (and SIGBREAK) handler the script installed itself.
    """
    global _stop_handler_installed
    if not callable(callback):
        raise TypeError(f"on_stop() needs a callable, not {type(callback).__name__}")
    if not _stop_handler_installed:
        try:
            for sig in _stop_signals():
                signal.signal(sig, _handle_stop)
        except ValueError as e:
            raise RunError("on_stop() must be called from the main thread.") from e
        _stop_handler_installed = True
    _stop_callbacks.append(callback)
    return callback


def _handle_stop(signum, frame) -> None:
    global _stopping
    if _stopping:  # already shutting down: a second signal changes nothing
        return
    _stopping = True
    for callback in list(_stop_callbacks):
        try:
            callback()
        except Exception:
            sys.stderr.write(f"pyrunner_run: on_stop callback {callback!r} failed:\n")
            traceback.print_exc()
    sys.exit(128 + signum)
