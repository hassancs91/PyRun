"""
OAuth 2.0 connections: connect, paste a token, refresh (2.5 Stage 5, T24).

An OAuth connection (``Connection.auth_kind`` oauth2) holds an access token and,
usually, a refresh token, both encrypted with the other credentials. Scripts
only ever get the access token (``core.integrations.presets.auth_material``);
the refresh token and the app's client secret never leave the server.

- **Apps.** ``OAuthClient`` rows hold a provider app's client id and secret
  (bring your own): a workspace's app, else the instance default a superuser set.
  A connection remembers the app that issued its tokens and refreshes with it.
- **Connect.** ``start()`` signs a state (workspace, user, provider, connection
  id, nonce; 10 minutes) and keeps the flow's private half (the PKCE verifier,
  the app, the name and scopes) in the browser's session under the nonce. The
  provider sends the browser back to the one ``/oauth/callback/``, where
  ``finish()`` checks the state and the session, exchanges the code, calls the
  provider's whoami and saves the connection as ok.
- **Paste a token.** ``validate_pasted()`` takes a refresh token (refreshed now
  with the app) or a long-lived access token, and checks it with whoami.
- **Refresh.** ``ensure_fresh()`` refreshes lazily, 60 seconds before the access
  token expires, single-flight: Postgres locks the row (``select_for_update``);
  SQLite, which has no row locks, claims the refresh with a conditional UPDATE
  and the others wait for its result. A provider that refuses the refresh
  marks the connection expired and emails its creator; a provider that can't
  be reached leaves it as it is (the caller gets a 503).
"""

import base64
import hashlib
import http.client
import json
import logging
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import timedelta

from django.core import signing
from django.db import connection as db_connection
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from core.integrations.presets import OAUTH2, auth_material, get_preset
from core.models import Connection, OAuthClient

logger = logging.getLogger(__name__)

STATE_SALT = "pyrunner.oauth.state"
STATE_MAX_AGE = 600  # seconds: a connect flow has 10 minutes to come back
SESSION_KEY = "pyrunner_oauth_flows"
MAX_PENDING_FLOWS = 5  # per browser session; the oldest is dropped
REFRESH_EARLY = timedelta(seconds=60)
TOKEN_TIMEOUT = 15  # seconds, for a token request
# How long a SQLite refresh claim holds: past it, another process may take over.
REFRESH_LEASE = timedelta(seconds=TOKEN_TIMEOUT * 2)
_WAIT_STEP = 0.2  # seconds between looks while another process refreshes
_TOKEN_MAX_BYTES = 64 * 1024


class OAuthError(Exception):
    """A connect flow or token request that failed.

    ``refused``: the provider answered and said no (a bad code, a revoked or
    expired refresh token, a wrong client secret). Otherwise it couldn't be
    reached or failed on its side, and trying again later may work.
    """

    def __init__(self, message, refused=False):
        super().__init__(message)
        self.message = message
        self.refused = refused


def _now():
    return timezone.now()


def _sleep(seconds):
    time.sleep(seconds)


# --------------------------------------------------------------------------- #
# Apps
# --------------------------------------------------------------------------- #


def client_for(workspace, provider):
    """The app new ``provider`` connections in ``workspace`` use, or None.

    The workspace's own app wins over the instance default.
    """
    return (
        OAuthClient.objects.filter(workspace=workspace, provider=provider).first()
        or OAuthClient.objects.filter(workspace__isnull=True, provider=provider).first()
    )


def callback_url(request) -> str:
    """The one redirect URI every provider app registers."""
    return request.build_absolute_uri(reverse("oauth_callback"))


# --------------------------------------------------------------------------- #
# Token requests
# --------------------------------------------------------------------------- #


def _hide(text, secret, sent) -> str:
    """``text`` (a provider's error) with the app secret and what was sent masked."""
    from core.executor import mask_secrets_in_value

    masks = {"oauth_app.client_secret": secret}
    for key in ("code", "code_verifier", "refresh_token"):
        masks[f"oauth.{key}"] = sent.get(key, "")
    return mask_secrets_in_value(text, {k: v for k, v in masks.items() if v})


def _parse_reply(raw) -> dict:
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        # A form-encoded reply (GitHub's, when it ignores Accept).
        data = dict(urllib.parse.parse_qsl(raw.decode("utf-8", "replace")))
    return data if isinstance(data, dict) else {}


def _token_request(preset, client, data) -> dict:
    """POST to the provider's token endpoint as ``client``.

    Returns ``{access_token, refresh_token, expires_at, scopes}`` (refresh token
    "" when none came, expires_at None for a token that doesn't expire, scopes
    None when the reply didn't list them). Raises OAuthError.
    """
    from core.netguard import guarded_opener, is_safe_endpoint_url

    oauth = preset.oauth
    _authorize, token_url = oauth.urls(client.config)
    try:
        secret = client.get_secret()
    except Exception:
        logger.error("OAuth app %s: client secret could not be decrypted", client.pk)
        raise OAuthError(
            f"The {preset.label} OAuth app's client secret couldn't be decrypted "
            "(was ENCRYPTION_KEY changed?): enter it again."
        ) from None
    body = dict(data)
    headers = {"Accept": "application/json"}
    if oauth.client_auth == "basic":
        pair = f"{client.client_id}:{secret}".encode("utf-8")
        headers["Authorization"] = "Basic " + base64.b64encode(pair).decode("ascii")
    else:
        body["client_id"] = client.client_id
        if secret:
            body["client_secret"] = secret
    if oauth.token_format == "json":
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    else:
        payload = urllib.parse.urlencode(body).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    host = urllib.parse.urlsplit(token_url).netloc
    ok, why = is_safe_endpoint_url(token_url)
    if not ok:
        raise OAuthError(why)
    request = urllib.request.Request(token_url, data=payload, headers=headers, method="POST")
    try:
        with guarded_opener().open(request, timeout=TOKEN_TIMEOUT) as response:
            status, raw = response.status, response.read(_TOKEN_MAX_BYTES)
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read(_TOKEN_MAX_BYTES)
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        raise OAuthError(f"Could not reach {host}: {getattr(e, 'reason', e)}") from None
    reply = _parse_reply(raw)
    if status >= 500 or status == 429:
        raise OAuthError(f"{host} answered HTTP {status}; try again later.")
    access = reply.get("access_token")
    if status >= 400 or not isinstance(access, str) or not access:
        error = str(reply.get("error") or f"HTTP {status}")
        description = str(reply.get("error_description") or "")
        message = f"{preset.label} refused: {error}{' (' + description + ')' if description else ''}"
        raise OAuthError(_hide(message, secret, data), refused=True)
    expires_at = None
    try:
        expires_in = int(reply.get("expires_in") or 0)
    except (TypeError, ValueError):
        expires_in = 0
    if expires_in > 0:
        expires_at = _now() + timedelta(seconds=expires_in)
    scope = reply.get("scope")
    scopes = None
    if isinstance(scope, str):
        scopes = [s for s in scope.replace(",", " ").split() if s]
    refresh = reply.get("refresh_token")
    return {
        "access_token": access,
        "refresh_token": refresh if isinstance(refresh, str) else "",
        "expires_at": expires_at,
        "scopes": scopes,
    }


def exchange(preset, client, *, code, redirect_uri, verifier=None) -> dict:
    """Trade an authorization code for tokens."""
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}
    if verifier:
        data["code_verifier"] = verifier
    return _token_request(preset, client, data)


def refresh(preset, client, refresh_token) -> dict:
    """A new access token for ``refresh_token`` (and a new refresh token, if the provider rotates them)."""
    return _token_request(preset, client, {"grant_type": "refresh_token", "refresh_token": refresh_token})


def whoami(preset, config, access_token) -> str:
    """Call the provider's whoami with ``access_token``; returns the account label. Raises OAuthError."""
    from core.services.connections import http_check

    material = auth_material(preset, OAUTH2, config, {"access_token": access_token})
    try:
        return http_check(preset, config, material)
    except ValueError as e:
        raise OAuthError(str(e), refused=True) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        host = urllib.parse.urlsplit(preset.base_url).netloc
        raise OAuthError(f"Could not reach {host}: {getattr(e, 'reason', e)}") from None


def apply_tokens(connection, tokens, *, keep_refresh_token=True) -> None:
    """Put ``tokens`` on ``connection`` (not saved).

    A provider that doesn't send a new refresh token on refresh means "keep the
    one you have"; ``keep_refresh_token`` False drops it (another app's).
    """
    saved = {}
    if keep_refresh_token:
        try:
            saved = connection.get_credentials()
        except Exception:
            # Unreadable (ENCRYPTION_KEY changed): only the new tokens are kept.
            logger.warning("Connection %s: saved credentials unreadable; replaced by new tokens", connection.pk)
            saved = {}
    credentials = {"access_token": tokens["access_token"]}
    refresh_token = tokens.get("refresh_token") or saved.get("refresh_token", "")
    if refresh_token:
        credentials["refresh_token"] = refresh_token
    connection.set_credentials(credentials)
    connection.token_expires_at = tokens.get("expires_at")
    if tokens.get("scopes") is not None:
        connection.scopes = tokens["scopes"]


def validate_pasted(preset, client, *, refresh_token="", access_token="", config=None):
    """(tokens, account label) for a pasted token, checked with whoami. Raises OAuthError.

    A refresh token is refreshed right away with ``client`` (so it is known to
    work); an access token alone is kept as it is, without an expiry.
    """
    if refresh_token:
        if client is None:
            raise OAuthError(
                f"Add a {preset.label} OAuth app first (Connections → OAuth apps): a refresh token "
                "is refreshed with its client id and secret."
            )
        tokens = refresh(preset, client, refresh_token)
        tokens["refresh_token"] = tokens["refresh_token"] or refresh_token
    elif access_token:
        tokens = {"access_token": access_token, "refresh_token": "", "expires_at": None, "scopes": None}
    else:
        raise OAuthError("Paste a refresh token or an access token.")
    return tokens, whoami(preset, config or {}, tokens["access_token"])


# --------------------------------------------------------------------------- #
# Connect flow
# --------------------------------------------------------------------------- #


def _pkce_pair():
    verifier = secrets.token_urlsafe(64)  # 86 characters (RFC 7636: 43 to 128)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def sign_state(payload: dict) -> str:
    return signing.dumps(payload, salt=STATE_SALT, compress=True)


def read_state(value) -> dict:
    """The state a provider sent back; raises OAuthError when it is expired or not ours."""
    try:
        state = signing.loads(value or "", salt=STATE_SALT, max_age=STATE_MAX_AGE)
    except signing.SignatureExpired:
        raise OAuthError("This sign-in took longer than 10 minutes: start it again.") from None
    except signing.BadSignature:
        raise OAuthError("This sign-in reply wasn't started here or was changed: start it again.") from None
    if not isinstance(state, dict):
        raise OAuthError("This sign-in reply wasn't started here or was changed: start it again.")
    return state


def start(request, *, preset, client, name, config, scopes, connection=None, grant_all_scripts=False,
          owner=None, next_url="") -> str:
    """Begin connecting (or reconnecting ``connection``); returns the provider's authorize URL.

    ``owner`` = (plugin slug, requirement key) makes a new connection the one
    that plugin uses for that key (T25); ``next_url`` (a local path) is where
    the callback sends the browser instead of the connection's page.
    """
    nonce = secrets.token_urlsafe(16)
    connection_id = str(connection.pk if connection is not None else uuid.uuid4())
    redirect = callback_url(request)
    state = sign_state({
        "w": str(request.workspace.pk),
        "u": str(request.user.pk),
        "p": preset.key,
        "c": connection_id,
        "n": nonce,
    })
    authorize_url, _token_url = preset.oauth.urls(client.config)
    params = {
        "response_type": "code",
        "client_id": client.client_id,
        "redirect_uri": redirect,
        "state": state,
    }
    if scopes:
        params["scope"] = preset.oauth.scope_separator.join(scopes)
    params.update(dict(preset.oauth.authorize_params))
    flow = {
        "client": str(client.pk),
        "name": name,
        "config": config,
        "scopes": list(scopes),
        "grant_all_scripts": bool(grant_all_scripts),
        "new": connection is None,
        "owner": list(owner) if owner else None,
        "next": next_url or "",
        "redirect_uri": redirect,
        "at": time.time(),
    }
    if preset.oauth.pkce:
        flow["verifier"], params["code_challenge"] = _pkce_pair()
        params["code_challenge_method"] = "S256"
    now = time.time()
    flows = {
        key: value
        for key, value in (request.session.get(SESSION_KEY) or {}).items()
        if isinstance(value, dict) and now - value.get("at", 0) < STATE_MAX_AGE
    }
    flows[nonce] = flow
    request.session[SESSION_KEY] = dict(
        sorted(flows.items(), key=lambda item: item[1]["at"])[-MAX_PENDING_FLOWS:]
    )
    joiner = "&" if urllib.parse.urlsplit(authorize_url).query else "?"
    return authorize_url + joiner + urllib.parse.urlencode(params)


def finish(request, *, state_value, code):
    """Complete a connect flow from the callback. Raises OAuthError.

    Returns (the saved connection, its workspace, the flow's ``next_url`` or "").
    """
    from core.models import Workspace

    state = read_state(state_value)
    flows = request.session.get(SESSION_KEY) or {}
    flow = flows.pop(str(state.get("n")), None)
    request.session[SESSION_KEY] = flows
    if not isinstance(flow, dict):
        raise OAuthError(
            "This sign-in wasn't started in this browser session, or was already used: start it again."
        )
    if str(state.get("u")) != str(request.user.pk):
        raise OAuthError("This sign-in was started by another user: start it again.")
    workspace, ok = Workspace.resolve_for(request.user, requested_id=state.get("w"))
    if not ok or workspace is None:
        raise OAuthError("You no longer have access to the workspace this connection is for.")
    preset = get_preset(state.get("p"))
    if preset is None or preset.oauth is None:
        raise OAuthError("This version of PyRun doesn't know that provider.")
    client = OAuthClient.objects.filter(pk=flow.get("client"), provider=preset.key).filter(
        Q(workspace__isnull=True) | Q(workspace=workspace)
    ).first()
    if client is None:
        raise OAuthError(f"The {preset.label} OAuth app was deleted while you were signing in.")
    if not code:
        raise OAuthError(f"{preset.label} sent no authorization code back.")
    connection = Connection.objects.filter(pk=state.get("c"), workspace=workspace).first()
    if connection is None and not flow.get("new"):
        raise OAuthError("This connection was deleted while you were signing in.")
    if connection is not None and (connection.provider != preset.key or connection.auth_kind != OAUTH2):
        raise OAuthError("This sign-in doesn't match the connection it was started for.")
    if connection is None and Connection.objects.filter(workspace=workspace, name=flow.get("name")).exists():
        raise OAuthError(f"A connection named \"{flow.get('name')}\" was added meanwhile: pick another name.")
    owner = flow.get("owner") if connection is None else None
    if owner and Connection.objects.filter(
        workspace=workspace, owner_plugin=owner[0], owner_key=owner[1]
    ).exists():
        raise OAuthError("This plugin got a connection for that account meanwhile: nothing was changed.")
    tokens = exchange(
        preset, client, code=code, redirect_uri=flow["redirect_uri"], verifier=flow.get("verifier")
    )
    config = flow.get("config") if isinstance(flow.get("config"), dict) else {}
    label = whoami(preset, config if connection is None else connection.config, tokens["access_token"])
    if connection is None:
        connection = Connection(
            id=state["c"],
            workspace=workspace,
            name=flow["name"],
            provider=preset.key,
            auth_kind=OAUTH2,
            config=config,
            grant_all_scripts=flow.get("grant_all_scripts") is True,
            created_by=request.user,
        )
        if owner:
            connection.owner_plugin, connection.owner_key = str(owner[0]), str(owner[1])
    # A refresh token belongs to the app that issued it: keep the saved one only
    # when the provider sent none and the app is the same.
    apply_tokens(connection, tokens, keep_refresh_token=connection.oauth_client_id == client.pk)
    if tokens.get("scopes") is None:
        connection.scopes = [s for s in flow.get("scopes") or [] if isinstance(s, str)]
    connection.oauth_client = client
    connection.account_label = (label or "")[:255]
    connection.status = Connection.Status.OK
    connection.last_error = ""
    connection.last_tested_at = _now()
    connection.refreshing_until = None
    connection.save()
    return connection, workspace, str(flow.get("next") or "")


# --------------------------------------------------------------------------- #
# Lazy, single-flight refresh
# --------------------------------------------------------------------------- #


def needs_refresh(connection, now=None) -> bool:
    """True when ``connection``'s access token expires within the next minute."""
    if connection.auth_kind != OAUTH2 or connection.token_expires_at is None:
        return False
    return connection.token_expires_at - REFRESH_EARLY <= (now or _now())


def _masked(connection, message) -> str:
    from core.executor import mask_secrets_in_value
    from core.services.connections import LAST_ERROR_MAX_CHARS, masks_for

    masks = masks_for(connection)
    client = connection.oauth_client if connection.oauth_client_id else None
    if client is not None:
        try:
            secret = client.get_secret()
        except Exception:
            secret = ""  # nothing to mask; the token request logged the failure
        if secret:
            masks["oauth_app.client_secret"] = secret
    return mask_secrets_in_value(message, masks)[:LAST_ERROR_MAX_CHARS]


def _mark_expired(connection, message) -> bool:
    """Set status expired (once) with ``message``; True when this call changed it."""
    changed = (
        Connection.objects.filter(pk=connection.pk)
        .exclude(status__in=(Connection.Status.EXPIRED, Connection.Status.REVOKED))
        .update(status=Connection.Status.EXPIRED, last_error=message, updated_at=_now())
    )
    connection.status, connection.last_error = Connection.Status.EXPIRED, message
    return bool(changed)


def _refresh_row(row):
    """Refresh ``row``'s tokens and save the outcome. Returns (outcome, message).

    outcome: "ok", "expired" (newly marked: notify), "already_expired",
    "transient" (not reached; status left alone) or "unreadable".
    """
    preset = get_preset(row.provider)
    try:
        credentials = row.get_credentials()
    except Exception:
        logger.error("Connection %s: credentials could not be decrypted", row.pk)
        return "unreadable", ""
    refresh_token = credentials.get("refresh_token", "")
    client = row.oauth_client if row.oauth_client_id else None
    if preset is None or preset.oauth is None:
        return "unreadable", ""
    if not refresh_token:
        message = "The access token expired and there is no refresh token: reconnect it."
    elif client is None:
        message = f"Its {preset.label} OAuth app was deleted: add the app again, then reconnect."
    else:
        try:
            tokens = refresh(preset, client, refresh_token)
        except OAuthError as e:
            message = _masked(row, e.message)
            if not e.refused:
                logger.warning("Connection %s: token refresh failed: %s", row.pk, message)
                return "transient", message
        else:
            apply_tokens(row, tokens)
            row.status = Connection.Status.OK
            row.last_error = ""
            row.refreshing_until = None
            row.save(update_fields=[
                "credentials_encrypted", "token_expires_at", "scopes", "status",
                "last_error", "refreshing_until", "updated_at",
            ])
            return "ok", ""
    logger.warning("Connection %s: refresh refused, marked expired: %s", row.pk, message)
    return ("expired" if _mark_expired(row, message) else "already_expired"), message


def ensure_fresh(connection):
    """``connection`` with an access token good for at least a minute, refreshing it if needed.

    Returns the connection as stored now. Raises ConnectionLookupError:
    CONNECTION_EXPIRED (409) when the provider refused the refresh (the
    connection is marked expired and its creator emailed), and
    CONNECTION_REFRESH_FAILED (503) when it couldn't be reached.
    """
    from core.services.connections import ConnectionLookupError

    if not needs_refresh(connection):
        return connection
    if db_connection.vendor == "postgresql":
        # The row lock makes every other refresher wait here, then see the new token.
        with transaction.atomic():
            row = Connection.objects.select_for_update().get(pk=connection.pk)
            if row.status == Connection.Status.REVOKED:
                outcome, message = "revoked", ""
            elif row.status == Connection.Status.EXPIRED:
                outcome, message = "already_expired", row.last_error
            elif needs_refresh(row):
                outcome, message = _refresh_row(row)
            else:
                outcome, message = "ok", ""
    else:
        outcome, message, row = _refresh_claimed(connection)
    if outcome == "revoked":
        raise ConnectionLookupError(
            "CONNECTION_REVOKED",
            f"Connection '{row.name}' was revoked: enter its credentials again in Connections.",
            409,
        )
    if outcome == "expired":
        notify_expired(row, message)
    if outcome in ("expired", "already_expired"):
        raise ConnectionLookupError(
            "CONNECTION_EXPIRED",
            f"Connection '{row.name}' has expired: reconnect it in Connections. {message}".strip(),
            409,
        )
    if outcome == "transient":
        raise ConnectionLookupError(
            "CONNECTION_REFRESH_FAILED",
            f"Connection '{row.name}': its access token couldn't be refreshed just now. {message}".strip(),
            503,
        )
    if outcome == "unreadable":
        raise ConnectionLookupError(
            "CONNECTION_UNREADABLE",
            f"Connection '{row.name}' couldn't be decrypted (was ENCRYPTION_KEY changed?).",
            503,
        )
    return Connection.objects.get(pk=row.pk)


def _refresh_claimed(connection):
    """SQLite: claim the refresh with a conditional UPDATE, or wait for whoever has it."""
    deadline = time.monotonic() + REFRESH_LEASE.total_seconds()
    while True:
        now = _now()
        claimed = (
            Connection.objects.filter(pk=connection.pk, status__in=(Connection.Status.OK, Connection.Status.ERROR))
            .filter(token_expires_at__lte=now + REFRESH_EARLY)
            .filter(Q(refreshing_until__isnull=True) | Q(refreshing_until__lt=now))
            .update(refreshing_until=now + REFRESH_LEASE)
        )
        row = Connection.objects.get(pk=connection.pk)
        if claimed:
            try:
                outcome, message = _refresh_row(row)
            finally:
                Connection.objects.filter(pk=row.pk).update(refreshing_until=None)
            return outcome, message, row
        if row.status == Connection.Status.EXPIRED:
            return "already_expired", row.last_error, row
        if row.status == Connection.Status.REVOKED:
            return "revoked", "", row
        if not needs_refresh(row):
            return "ok", "", row  # someone else refreshed it
        if time.monotonic() > deadline:
            return "transient", "Another refresh of it is taking too long.", row
        _sleep(_WAIT_STEP)


def notify_expired(connection, message) -> bool:
    """Email the connection's creator that it needs reconnecting. Never raises."""
    from core.services.notification_service import NotificationService

    user = connection.created_by if connection.created_by_id else None
    if user is None or not user.email:
        return False
    preset = get_preset(connection.provider)
    label = preset.label if preset else connection.provider
    account = f" ({connection.account_label})" if connection.account_label else ""
    body = (
        f"PyRun could not refresh the access token of the {label} connection "
        f"\"{connection.name}\"{account} in the workspace \"{connection.workspace.name}\".\n\n"
        f"{message}\n\n"
        "Scripts that use it get CONNECTION_EXPIRED until it is reconnected: open Connections, "
        f"then \"{connection.name}\", and use Reconnect or paste a new token.\n"
    )
    try:
        NotificationService.send_email(
            NotificationService._subject(f'Connection "{connection.name}" needs reconnecting'),
            body,
            to=user.email,
        )
    except Exception as e:
        logger.warning("Connection %s expired; its creator could not be emailed: %s", connection.pk, e)
        return False
    return True
