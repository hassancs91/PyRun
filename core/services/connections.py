"""
Connections: grants, credentials for runs, masking and testing (2.5 Stage 5, T23).

The console, the loopback endpoints (``core/views/api/connections_internal.py``)
and the executor all go through here:

- ``granted_connections(script)`` is the one grant rule: a script gets a
  connection of its own workspace when it has an active ``ConnectionGrant`` for
  it, or when the connection grants itself to all its creator's scripts and the
  script is a user script that creator made.
- ``issue(run, connection)`` is what ``pyrunner_connections`` gets: the auth
  material (headers, query parameters, the bare token, the SMTP login). Every
  secret value in it is recorded on the run (``RunMask``) before it is handed
  out, so the run's masking passes cover it even when it wasn't there at spawn.
- ``connection_masks(run)`` is the spawn-time half: the secret values of every
  connection the run's script is granted, for the masking dict.
- ``check_connection(connection)`` calls the provider's whoami (or the test
  path, or an SMTP login) from the server, through the SSRF guard.
"""

import json
import logging
import re
import smtplib
import ssl
import urllib.error
import urllib.parse
import urllib.request

from django.db.models import Q
from django.utils import timezone

from core.integrations.presets import SMTP, auth_material, base_url, get_preset, mask_values
from core.models import Connection, ConnectionGrant, RunMask, Script

logger = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
NAME_HELP = "Letters, digits, '.', '_' and '-', starting with a letter or digit (100 at most)."
TEST_TIMEOUT = 15  # seconds, for a whoami call or an SMTP login
# A whoami reply is a small JSON object; never read more than this of it.
_WHOAMI_MAX_BYTES = 64 * 1024
LAST_ERROR_MAX_CHARS = 500


class ConnectionLookupError(Exception):
    """A connection a run asked for that it can't have. ``code``/``status`` go to the helper."""

    def __init__(self, code, message, status):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# --------------------------------------------------------------------------- #
# Grants
# --------------------------------------------------------------------------- #


def _grant_q(script):
    q = Q(grants__script_id=script.pk, grants__active=True)
    # The wildcard: "grant to all my scripts" = the user scripts its creator made.
    if script.owner_plugin is None and script.created_by_id is not None:
        q |= Q(grant_all_scripts=True, created_by_id=script.created_by_id)
    return q


def granted_connections(script):
    """The connections ``script`` may use: its workspace's, granted to it."""
    if script is None or script.workspace_id is None:
        return Connection.objects.none()
    return (
        Connection.objects.filter(_grant_q(script), workspace_id=script.workspace_id)
        .distinct()
        .order_by("name")
    )


def scripts_using(connection):
    """The live scripts ``connection`` is granted to (explicitly or by the wildcard)."""
    q = Q(connection_grants__connection=connection, connection_grants__active=True)
    if connection.grant_all_scripts and connection.created_by_id is not None:
        q |= Q(owner_plugin__isnull=True, created_by_id=connection.created_by_id)
    return (
        Script.objects.filter(q, workspace_id=connection.workspace_id, archived_at__isnull=True)
        .distinct()
        .order_by("name")
    )


def set_grants(script, connection_ids, workspace) -> None:
    """Make ``script``'s explicit grants exactly ``connection_ids`` (of ``workspace``)."""
    valid = {
        str(pk)
        for pk in Connection.objects.filter(
            workspace=workspace, pk__in=[c for c in connection_ids if c]
        ).values_list("pk", flat=True)
    }
    existing = {str(g.connection_id): g for g in ConnectionGrant.objects.filter(script=script)}
    for cid in valid - existing.keys():
        ConnectionGrant.objects.create(script=script, connection_id=cid)
    for cid in valid & existing.keys():
        if not existing[cid].active:
            ConnectionGrant.objects.filter(pk=existing[cid].pk).update(active=True)
    for cid in existing.keys() - valid:
        existing[cid].delete()


def set_scripts(connection, script_ids, scripts) -> None:
    """Make ``connection``'s explicit grants among ``scripts`` exactly ``script_ids``.

    ``scripts`` are the ones the page listed (the workspace's live scripts);
    grants to any other script (an archived one) are left alone.
    """
    listed = {str(s.pk): s for s in scripts if s.workspace_id == connection.workspace_id}
    wanted = {sid for sid in script_ids if sid in listed}
    existing = {
        str(g.script_id): g
        for g in ConnectionGrant.objects.filter(connection=connection, script_id__in=list(listed))
    }
    for sid in wanted - existing.keys():
        ConnectionGrant.objects.create(script=listed[sid], connection=connection)
    for sid in wanted & existing.keys():
        if not existing[sid].active:
            ConnectionGrant.objects.filter(pk=existing[sid].pk).update(active=True)
    for sid in existing.keys() - wanted:
        existing[sid].delete()


# --------------------------------------------------------------------------- #
# Credentials and masking
# --------------------------------------------------------------------------- #


def masks_for(connection) -> dict:
    """``{label: value}`` of this connection's secret values ({} if unreadable)."""
    preset = get_preset(connection.provider)
    if preset is None:
        return {}
    try:
        credentials = connection.get_credentials()
    except Exception:
        logger.error("Connection %s: credentials could not be decrypted", connection.pk)
        return {}
    return mask_values(connection.name, preset, connection.auth_kind, connection.config, credentials)


def connection_masks(run) -> dict:
    """The secret values of every connection the run's script is granted."""
    script = getattr(run, "script", None) if getattr(run, "script_id", None) else None
    masks = {}
    for connection in granted_connections(script):
        masks.update(masks_for(connection))
    return masks


def issued_masks(run_pk) -> dict:
    """The run's per-run mask set: values handed to it over the loopback."""
    from core.services import EncryptionService

    masks = {}
    for label, encrypted in RunMask.objects.filter(run_id=run_pk).values_list(
        "label", "value_encrypted"
    ):
        try:
            value = EncryptionService.decrypt(encrypted)
        except Exception:
            # Can't happen with the key that wrote it; logged, never shown.
            logger.error("Run %s: a recorded mask value could not be decrypted", run_pk)
            continue
        key, n = label, 2
        while key in masks and masks[key] != value:
            key, n = f"{label}#{n}", n + 1
        masks[key] = value
    return masks


def with_issued_masks(run_pk, masks: dict) -> dict:
    """Add the run's per-run mask set to ``masks``, in place; returns it."""
    for label, value in issued_masks(run_pk).items():
        if value not in masks.values():
            key = label if label not in masks else f"{label}#issued"
            masks[key] = value
    return masks


def forget_issued(run_pk) -> None:
    """Drop the run's recorded values once it has ended. Never raises."""
    try:
        RunMask.objects.filter(run_id=run_pk).delete()
    except Exception:
        logger.warning("Run %s: could not clear its recorded mask values", run_pk, exc_info=True)


def _record_masks(run_pk, masks: dict) -> None:
    from core.services import EncryptionService

    RunMask.objects.bulk_create(
        [
            RunMask(
                run_id=run_pk,
                label=label[:200],
                digest=RunMask.digest_of(value),
                value_encrypted=EncryptionService.encrypt(value),
            )
            for label, value in masks.items()
            if value
        ],
        ignore_conflicts=True,
    )


def run_for(run_id):
    from core.models import Run

    run = Run.objects.select_related("script").filter(pk=run_id).first()
    if run is None or run.script_id is None:
        raise ConnectionLookupError("RUN_NOT_FOUND", "This run no longer exists.", 404)
    return run


def resolve_for_run(run_id, ref):
    """The connection named ``ref`` (a name, or a provider key) the run may use.

    A name wins; a provider key works when exactly one granted connection has
    it. Missing and not granted look the same (no disclosure).
    """
    if not isinstance(ref, str) or not ref.strip():
        raise ConnectionLookupError("BAD_REQUEST", "Name a connection.", 400)
    ref = ref.strip()
    run = run_for(run_id)
    granted = list(granted_connections(run.script))
    match = [c for c in granted if c.name == ref] or [c for c in granted if c.provider == ref]
    if not match:
        raise ConnectionLookupError(
            "CONNECTION_NOT_FOUND",
            f"Connection '{ref}' does not exist or is not granted to this script.",
            404,
        )
    if len(match) > 1:
        names = ", ".join(sorted(c.name for c in match))
        raise ConnectionLookupError(
            "AMBIGUOUS",
            f"Several connections granted to this script are '{ref}' connections ({names}): name one.",
            409,
        )
    return run, match[0]


def info(connection) -> dict:
    """What a script (or a listing) may know about a connection: never a secret."""
    preset = get_preset(connection.provider)
    out = {
        "name": connection.name,
        "provider": connection.provider,
        "provider_label": preset.label if preset else connection.provider,
        "auth_kind": connection.auth_kind,
        "status": connection.status,
        "account_label": connection.account_label,
        "base_url": base_url(preset, connection.config) if preset and preset.is_http else None,
    }
    if connection.auth_kind == SMTP:
        config = connection.config or {}
        out["smtp"] = {
            "host": config.get("host", ""),
            "port": config.get("port"),
            "security": config.get("security", ""),
            "username": config.get("username", ""),
            "from_address": config.get("from_address", ""),
        }
    return out


def issue(run, connection) -> dict:
    """The auth material for ``run``, its secret values recorded as masks first.

    Only while the run is running: a finished run's token can't fetch credentials.
    """
    from core.models import Run

    if run.status != Run.Status.RUNNING:
        raise ConnectionLookupError(
            "RUN_NOT_RUNNING", f"This run is {run.status}: it can't get credentials any more.", 409
        )
    preset = get_preset(connection.provider)
    if preset is None or connection.auth_kind not in preset.auth_kinds:
        raise ConnectionLookupError(
            "CONNECTION_UNSUPPORTED",
            f"Connection '{connection.name}' uses a provider or sign-in this version doesn't know.",
            409,
        )
    if connection.status == Connection.Status.REVOKED:
        raise ConnectionLookupError(
            "CONNECTION_REVOKED",
            f"Connection '{connection.name}' was revoked: enter its credentials again in Connections.",
            409,
        )
    if connection.status == Connection.Status.EXPIRED:
        raise ConnectionLookupError(
            "CONNECTION_EXPIRED",
            f"Connection '{connection.name}' has expired: reconnect it in Connections.",
            409,
        )
    try:
        credentials = connection.get_credentials()
    except Exception:
        logger.error("Connection %s: credentials could not be decrypted", connection.pk)
        raise ConnectionLookupError(
            "CONNECTION_UNREADABLE",
            f"Connection '{connection.name}' couldn't be decrypted (was ENCRYPTION_KEY changed?).",
            503,
        ) from None
    material = auth_material(preset, connection.auth_kind, connection.config, credentials)
    # Recorded BEFORE the values leave: a script that prints one at once is
    # still masked by the final pass.
    _record_masks(
        run.pk,
        mask_values(connection.name, preset, connection.auth_kind, connection.config, credentials),
    )
    return {**info(connection), **material}


# --------------------------------------------------------------------------- #
# Testing (server side, through the SSRF guard)
# --------------------------------------------------------------------------- #


class _GuardedSMTP(smtplib.SMTP):
    def _get_socket(self, host, port, timeout):
        from core.netguard import guarded_create_connection

        return guarded_create_connection((host, port), timeout, source_address=self.source_address)


class _GuardedSMTPSSL(smtplib.SMTP_SSL):
    def _get_socket(self, host, port, timeout):
        from core.netguard import guarded_create_connection

        sock = guarded_create_connection((host, port), timeout, source_address=self.source_address)
        return self.context.wrap_socket(sock, server_hostname=self._host)


def _test_smtp(smtp) -> str:
    """Connect and log in; returns the account label. Raises on failure."""
    context = ssl.create_default_context()
    if smtp["security"] == "ssl":
        server = _GuardedSMTPSSL(smtp["host"], smtp["port"], timeout=TEST_TIMEOUT, context=context)
    else:
        server = _GuardedSMTP(smtp["host"], smtp["port"], timeout=TEST_TIMEOUT)
    try:
        server.ehlo()
        if smtp["security"] == "starttls":
            server.starttls(context=context)
            server.ehlo()
        if smtp["username"]:
            server.login(smtp["username"], smtp["password"])
    finally:
        # QUIT can fail after a refused login or a dropped link; close the socket then.
        try:
            server.quit()
        except Exception:
            server.close()
    return smtp["username"] or smtp["from_address"] or smtp["host"]


def _test_http(preset, connection, material) -> str:
    """GET the whoami (or test) path with the credentials; returns the account label."""
    from core.netguard import guarded_opener, is_safe_endpoint_url

    root = base_url(preset, connection.config)
    path = preset.whoami.path if preset.whoami else (connection.config or {}).get("test_path", "")
    url = root + (("/" + path.lstrip("/")) if path else "")
    if material["params"]:
        joiner = "&" if urllib.parse.urlsplit(url).query else "?"
        url += joiner + urllib.parse.urlencode(material["params"])
    ok, why = is_safe_endpoint_url(url)
    if not ok:
        raise ValueError(why)
    request = urllib.request.Request(url, headers={**material["headers"], "Accept": "application/json"})
    try:
        with guarded_opener().open(request, timeout=TEST_TIMEOUT) as response:
            raw = response.read(_WHOAMI_MAX_BYTES)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise ValueError(f"The service refused these credentials (HTTP {e.code}).") from None
        raise ValueError(f"The service answered HTTP {e.code}.") from None
    label = ""
    if preset.whoami and preset.whoami.label_keys:
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if isinstance(data, dict):
            label = next(
                (str(data[k]) for k in preset.whoami.label_keys if data.get(k)), ""
            )
    return label or (connection.config or {}).get("username", "")


def check_connection(connection) -> tuple[bool, str]:
    """Test ``connection`` and store the outcome (status, label, last test, error).

    Returns (ok, message). A failure's message is masked with the connection's
    own secret values before it is stored or shown.
    """
    preset = get_preset(connection.provider)
    if preset is None:
        return False, f"Unknown provider '{connection.provider}'."
    if connection.status == Connection.Status.REVOKED:
        return False, "This connection was revoked: enter its credentials again first."
    try:
        credentials = connection.get_credentials()
        material = auth_material(preset, connection.auth_kind, connection.config, credentials)
        if connection.auth_kind == SMTP:
            label = _test_smtp(material["smtp"])
        else:
            label = _test_http(preset, connection, material)
    except Exception as e:
        from core.executor import mask_secrets_in_value

        message = mask_secrets_in_value(str(e) or e.__class__.__name__, masks_for(connection))
        message = message[:LAST_ERROR_MAX_CHARS]
        logger.warning("Connection %s test failed: %s", connection.pk, message)
        Connection.objects.filter(pk=connection.pk).update(
            status=Connection.Status.ERROR, last_error=message, updated_at=timezone.now()
        )
        connection.status, connection.last_error = Connection.Status.ERROR, message
        return False, message
    now = timezone.now()
    fields = {"status": Connection.Status.OK, "last_error": "", "last_tested_at": now}
    if label:
        fields["account_label"] = label[:255]
    Connection.objects.filter(pk=connection.pk).update(updated_at=now, **fields)
    for key, value in fields.items():
        setattr(connection, key, value)
    return True, f"Connected{' as ' + label if label else ''}."


def revoke(connection) -> None:
    """Forget the credentials; the row, its name and grants stay for a reconnect."""
    connection.credentials_encrypted = ""
    connection.status = Connection.Status.REVOKED
    connection.last_error = ""
    connection.save(update_fields=["credentials_encrypted", "status", "last_error", "updated_at"])
