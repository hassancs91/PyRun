"""
What a webhook call hands its run, besides the event (2.5, T20).

The webhook view reads the request into ``webhook_data``, the dict the run's
task gets. It has the same keys as before 2.5 (``method``, ``query``,
``content_type`` and, for a POST, ``body`` / ``body_json``), plus
``body_base64`` only when the body isn't UTF-8 text. That dict is what a held
run or a retry keeps, so everything the executor needs must be in it.

From it the executor builds:

- the body file (``raw_body``): the body exactly as received, bytes, which
  the script finds at ``PYRUNNER_EVENT_BODY_FILE``;
- ``WEBHOOK_BODY`` / ``WEBHOOK_BODY_JSON`` (``body_env``): unchanged for every
  body that can be an environment variable. One that can't (bigger than
  Linux allows for one variable, holding a NUL, or not UTF-8) gives an empty
  ``WEBHOOK_BODY`` and ``WEBHOOK_BODY_OMITTED=1`` instead of a run that can't
  start.

``event_headers`` is the header set the run's event shows.
"""

import base64
import json

# webhook_data key holding a body that isn't UTF-8 text, base64-encoded (its
# ``body`` is "", as before 2.5).
BODY_BASE64 = "body_base64"

# Linux refuses to start a process with an environment string ("NAME=value"
# plus its terminating NUL) longer than MAX_ARG_STRLEN: 32 pages of 4 KB.
ENV_ENTRY_MAX_BYTES = 32 * 4096

# Never copied into the event: login credentials and cookies (a proxy's or
# browser's, not the sender's message; the event is shown on the run page and
# to API callers) and the hop-by-hop headers that describe one connection, not
# the request (RFC 9110, section 7.6.1).
HIDDEN_HEADERS = frozenset({
    "authorization",
    "proxy-authorization",
    "cookie",
    "connection",
    "proxy-connection",
    "keep-alive",
    "proxy-authenticate",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
})


def event_headers(headers) -> dict:
    """The request's headers for the event: names in lower case, credentials
    and hop-by-hop headers left out (also any the ``Connection`` header names).
    """
    lowered = {str(name).lower(): str(value) for name, value in headers.items()}
    hidden = set(HIDDEN_HEADERS)
    hidden.update(
        token.strip().lower() for token in lowered.get("connection", "").split(",") if token.strip()
    )
    return {name: value for name, value in lowered.items() if name not in hidden}


def raw_body(webhook_data) -> bytes | None:
    """The request body exactly as received, or None for a call without one
    (a GET, a chat message)."""
    if not webhook_data or "body" not in webhook_data:
        return None
    if BODY_BASE64 in webhook_data:
        return base64.b64decode(webhook_data[BODY_BASE64])
    # ``body`` came from a strict UTF-8 decode, so encoding it gives back the
    # bytes that were sent.
    return webhook_data["body"].encode("utf-8")


def fits_env(name: str, value: str) -> bool:
    """Whether ``name=value`` can be passed to a new process on Linux."""
    if "\x00" in value:
        return False
    entry = f"{name}={value}".encode("utf-8", "surrogateescape")
    return len(entry) < ENV_ENTRY_MAX_BYTES


def body_env(webhook_data) -> dict:
    """``WEBHOOK_BODY`` (and ``WEBHOOK_BODY_JSON``) for a call with a body.

    Exactly as before 2.5 when they can be environment variables. Otherwise
    ``WEBHOOK_BODY`` is empty, there is no ``WEBHOOK_BODY_JSON``, and
    ``WEBHOOK_BODY_OMITTED`` is "1": the body is in the body file.
    """
    if not webhook_data or "body" not in webhook_data:
        return {}
    values = {"WEBHOOK_BODY": webhook_data["body"]}
    if "body_json" in webhook_data:
        values["WEBHOOK_BODY_JSON"] = json.dumps(webhook_data["body_json"])
    if BODY_BASE64 in webhook_data or not all(fits_env(k, v) for k, v in values.items()):
        return {"WEBHOOK_BODY": "", "WEBHOOK_BODY_OMITTED": "1"}
    return values
