"""
Provisioning for the YouTube Comments AI plugin — everything goes through the
SDK (``core.plugins.api``), so the plugin owns its resources, re-saves are
idempotent, and nothing imports core models/tasks/services directly.

One config form Save provisions, in the default workspace, all owned by the
``yt_comments`` slug:
  * a managed Database ``yt_comments:data``  (REQUIRED — the plugin refuses to
    provision without a data server; the worker creates its tables in it),
  * a DataStore  ``yt_comments:state``  (entry ``config`` = non-secret config;
    ``stats``/``runs``/``progress`` = dashboard data the worker writes),
  * an owner-scoped Secret  ``YT_API_KEY``,
  * a managed Script  (key ``analyze``, selected-mode injection + grants),
  * a daily Schedule.

Re-saving updates the same rows (idempotent on ``(owner_plugin, owner_key)``).

Posting replies and moderating spam need the channel owner's Google account:
a PyRun connection the plugin asks for in ``plugin.json``
(``requires_connections``, key ``youtube``). The user connects it on the
plugin page (the card from ``ConnectionAPI``); the analyzer is granted it and
asks ``pyrunner_connections`` for an access token. PyRun keeps the refresh
token and the OAuth client secret: neither reaches the plugin or its script.
"""

from pathlib import Path
from urllib.parse import urlparse

from core.plugins.api import (
    ChannelAPI,
    ConnectionAPI,
    ConnectionUnavailable,
    DatabaseAPI,
    DataStoreAPI,
    EnvironmentAPI,
    ScheduleAPI,
    ScriptAPI,
    SecretAPI,
    StorageAPI,
    StorageError,
)

from .forms import SECRET_FIELDS

OWNER = "yt_comments"
SCRIPT_KEY = "analyze"
SCRIPT_NAME = "YouTube Comments AI"
STORE_KEY = "state"
DB_KEY = "data"
DEFAULT_TIMEOUT = 3600  # first-run backfill + AI classification can be long

YT_API = "https://www.googleapis.com/youtube/v3"

# The requirement key of the YouTube account in plugin.json (requires_connections).
CONNECTION_KEY = "youtube"

# Owner secrets of the plugin's own OAuth flow (before 0.6.0). Owned secrets
# reach every run of the plugin's script, so an upgraded install deletes them
# (provision) instead of leaving a refresh token in the analyzer's environment.
LEGACY_OAUTH_SECRETS = ("YT_OAUTH_CLIENT_ID", "YT_OAUTH_CLIENT_SECRET", "YT_OAUTH_REFRESH_TOKEN")

# Keys of the non-secret config persisted to the DataStore (also prefill the form
# and form the cross-process contract checked against worker_body.py).
CONFIG_KEYS = (
    "channel_id",
    "channel_title",
    "start_date",
    "include_replies",
    "max_pages_per_run",
    "tags",
    "ai_enabled",
    "ai_model",
    "max_ai_per_run",
    "alerts_enabled",
    "digest_enabled",
    "alert_email",
    "alert_channel",
    "channel_digest_enabled",
    "reply_policies",
    "auto_post_daily_cap",
    "moderate_spam",
    "insights_enabled",
    "insights_weekday",
    "avatar_archive",
)

# Keys of the Reply Brain entry (store entry "brain", written by the Brain page —
# deliberately NOT part of the settings save, so a re-save never clobbers it).
BRAIN_KEYS = ("voice", "knowledge", "rules", "tag_guidance")

REPLY_MODES = ("off", "draft", "auto")
NEVER_AUTO_TAGS = ("urgent", "spam")  # locked guardrail — enforced here AND in the form


def _worker_code():
    """The managed Script's code = our bundled worker_body.py (read at provision).

    Guards the cross-layer contract: ``worker_body.py`` is a standalone script
    (separate process — it can't import this module), so the secret env-var names
    and config keys are shared by convention. If a rename drifts them out of sync
    we fail loudly here at Save, not silently at run time.
    """
    code = Path(__file__).with_name("worker_body.py").read_text(encoding="utf-8")
    expected = list(SECRET_FIELDS.values()) + list(CONFIG_KEYS) + list(BRAIN_KEYS)
    missing = [token for token in expected if token not in code]
    if missing:
        raise ValueError(
            "worker_body.py is out of sync with provisioning (missing references: "
            + ", ".join(missing)
            + "). Aborting to avoid a silently misconfigured analyzer."
        )
    return code


# --------------------------------------------------------------------------- #
# Reads (used by the views to render the page)
# --------------------------------------------------------------------------- #

def get_config():
    store = DataStoreAPI(OWNER).get(STORE_KEY)
    return (store.get("config", {}) or {}) if store is not None else {}


def configured_secret_keys():
    """Set of clean env-keys that already exist as owner secrets."""
    api = SecretAPI(OWNER)
    return {k for k in SECRET_FIELDS.values() if api.get(k) is not None}


def get_script():
    return ScriptAPI(OWNER).get(SCRIPT_KEY)


def get_schedule():
    scheds = ScheduleAPI(OWNER).list()
    return scheds[0] if scheds else None


def _store_entry(key, default):
    store = DataStoreAPI(OWNER).get(STORE_KEY)
    if store is None:
        return default
    val = store.get(key, default)
    return val if val is not None else default


def get_stats():
    val = _store_entry("stats", {})
    return val if isinstance(val, dict) else {}


def get_brain():
    """The Reply Brain entry with all keys present (missing → empty)."""
    val = _store_entry("brain", {})
    val = val if isinstance(val, dict) else {}
    brain = {k: val.get(k) or ("" if k != "tag_guidance" else {}) for k in BRAIN_KEYS}
    brain["tag_guidance"] = dict(brain["tag_guidance"] or {})
    return brain


def save_brain(data):
    """Persist the Brain page's cleaned form data (store entry "brain")."""
    store = DataStoreAPI(OWNER).get(STORE_KEY)
    if store is None:
        raise ValueError("Save the plugin settings first — the Brain lives in the plugin's store.")
    store.set("brain", {
        "voice": (data.get("voice") or "").strip(),
        "knowledge": (data.get("knowledge") or "").strip(),
        "rules": (data.get("rules") or "").strip(),
        "tag_guidance": dict(data.get("tag_guidance") or {}),
    })


def connection_requirements():
    """plugin.json's requires_connections with their state (for the page's card)."""
    return ConnectionAPI(OWNER).requirements()


def youtube_state(requirements=None):
    """The YouTube requirement's state (``.status``: missing, ok, missing_scopes,
    expired, revoked or error), or None when the manifest isn't installed."""
    states = requirements if requirements is not None else connection_requirements()
    return next((state for state in states if state.key == CONNECTION_KEY), None)


def legacy_oauth_secrets():
    """The pre-0.6.0 OAuth secrets still stored (deleted by the next settings save)."""
    api = SecretAPI(OWNER)
    return [key for key in LEGACY_OAUTH_SECRETS if api.get(key) is not None]


def sync_connection_grant(script=None):
    """Grant the YouTube connection the plugin holds to the analyzer (idempotent).

    Called by every settings save and by the plugin page: the user may connect
    (or pick another connection) after the analyzer was provisioned. Stop
    using / another pick drops the old grant in PyRun itself.
    """
    script = script if script is not None else get_script()
    if script is None:
        return False
    connections = ConnectionAPI(OWNER)
    if connections.get(CONNECTION_KEY) is None:
        return False
    connections.grant(script, CONNECTION_KEY)
    return True


def get_runs():
    val = _store_entry("runs", [])
    return val if isinstance(val, list) else []


def get_progress():
    return _store_entry("progress", None)


def list_environments():
    return EnvironmentAPI().list()


def list_channels():
    """Workspace-scoped channels for the alert picker (read-only, SDK 2.3)."""
    return ChannelAPI(OWNER).list()


def database_available():
    return DatabaseAPI(OWNER).is_available()


def storage_available():
    """Whether the instance has an assets storage connection (SDK 2.5)."""
    return StorageAPI(OWNER).is_available()


def storage_urls(keys):
    """{avatar_key: url} for the given storage keys — {} when the storage seam
    is unavailable. URLs are permanent when the assets connection has a public
    base URL, presigned + EXPIRING otherwise (fine for viewing the dashboard,
    not for publishing — the page hints at the difference)."""
    keys = [k for k in set(keys or []) if k]
    if not keys:
        return {}
    api = StorageAPI(OWNER)
    if not api.is_available():
        return {}
    out = {}
    for key in keys:
        try:
            out[key] = api.url(key) or ""
        except StorageError:
            out[key] = ""
    return out


def db_rows(sql, params=()):
    """Run a read query against the owned database; returns a list of dict rows.

    Raises ``LookupError`` when the database isn't provisioned/ready yet (the
    views turn that into an empty dashboard) and lets psycopg errors propagate —
    an undefined table just means the worker hasn't run, which the views also
    treat as empty state.
    """
    dsn = DatabaseAPI(OWNER).dsn(DB_KEY)
    if dsn is None:
        raise LookupError("The plugin database is not provisioned yet.")
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return conn.execute(sql, params).fetchall()


def db_execute(sql, params=()):
    """Run a write statement against the owned database (Reply Queue decisions).

    Returns the affected row count. Same error contract as ``db_rows``; the
    connection context commits on clean exit.
    """
    dsn = DatabaseAPI(OWNER).dsn(DB_KEY)
    if dsn is None:
        raise LookupError("The plugin database is not provisioned yet.")
    import psycopg

    with psycopg.connect(dsn) as conn:
        return conn.execute(sql, params).rowcount


def initial_from_config():
    """Build the form ``initial`` dict from saved config + schedule + script."""
    from .forms import tags_to_text

    cfg = get_config()
    initial = {}
    if cfg.get("channel_id"):
        initial["channel"] = cfg.get("channel_id")
    for k in ("start_date", "ai_model", "alert_email", "alert_channel"):
        if cfg.get(k):
            initial[k] = cfg.get(k)
    for k in ("include_replies", "ai_enabled", "alerts_enabled", "digest_enabled",
              "channel_digest_enabled", "moderate_spam", "insights_enabled",
              "avatar_archive"):
        if k in cfg:
            initial[k] = bool(cfg.get(k))
    for k in ("max_pages_per_run", "max_ai_per_run", "auto_post_daily_cap"):
        if cfg.get(k) is not None:
            initial[k] = cfg.get(k)
    if cfg.get("insights_weekday") is not None:
        initial["insights_weekday"] = str(cfg.get("insights_weekday"))
    if cfg.get("tags"):
        initial["tags"] = tags_to_text(cfg["tags"])
    for tag, mode in (cfg.get("reply_policies") or {}).items():
        initial[f"policy_{tag}"] = mode

    script = get_script()
    if script is not None:
        initial["notify_on"] = script.notify_on
        initial["notify_email"] = script.notify_email
        if script.environment_id:
            initial["environment"] = script.environment.name

    sched = get_schedule()
    if sched is not None:
        if sched.run_mode == "daily" and sched.daily_times:
            initial["schedule_time"] = sched.daily_times[0]
        initial["timezone"] = sched.timezone
    return initial


# --------------------------------------------------------------------------- #
# Channel resolution (YouTube Data API, web-process network at Save)
# --------------------------------------------------------------------------- #

def _extract_channel_ref(raw):
    """Normalize user input into ("id", "UC…") or ("handle", "@name")."""
    s = (raw or "").strip()
    if s.startswith(("http://", "https://")):
        parts = [p for p in urlparse(s).path.split("/") if p]
        if parts:
            if parts[0] == "channel" and len(parts) > 1:
                return ("id", parts[1])
            if parts[0].startswith("@"):
                return ("handle", parts[0])
            if parts[0] in ("c", "user") and len(parts) > 1:
                return ("handle", "@" + parts[1])
        raise ValueError(
            "Couldn't read a channel from that URL — paste the channel ID (UC…) or @handle."
        )
    if s.startswith("@"):
        return ("handle", s)
    if s.startswith("UC") and len(s) >= 20:
        return ("id", s)
    return ("handle", "@" + s)


def resolve_channel(api_key, raw):
    """Resolve user input to ``(channel_id, channel_title)`` via channels.list.

    Raises ``ValueError`` with a user-facing message on bad key/quota/unknown
    channel, and ``ConnectionError`` when YouTube is unreachable (the caller
    decides whether that's fatal — a literal UC… id can proceed unresolved).
    """
    import requests

    kind, ref = _extract_channel_ref(raw)
    params = {"part": "snippet", "key": api_key}
    params["id" if kind == "id" else "forHandle"] = ref
    try:
        resp = requests.get(f"{YT_API}/channels", params=params, timeout=10)
    except requests.exceptions.RequestException as exc:
        raise ConnectionError(
            f"Couldn't reach YouTube to resolve the channel ({exc.__class__.__name__})."
        ) from exc
    if resp.status_code in (400, 403):
        reason = ""
        try:
            reason = resp.json().get("error", {}).get("message", "")
        except ValueError:
            pass
        raise ValueError(
            f"YouTube rejected the API key (HTTP {resp.status_code}). {reason}".strip()
        )
    if resp.status_code != 200:
        raise ValueError(f"YouTube returned HTTP {resp.status_code} while resolving the channel.")
    items = resp.json().get("items") or []
    if not items:
        raise ValueError(f"No YouTube channel found for '{raw}'. Check the ID/handle.")
    snippet = items[0].get("snippet", {})
    return items[0]["id"], snippet.get("title", "")


def _secret_or_saved(values, field):
    """The submitted secret value, or the saved one when the field was left blank."""
    submitted = (values.get(field) or "").strip()
    if submitted:
        return submitted
    secret = SecretAPI(OWNER).get(SECRET_FIELDS[field])
    return secret.get_decrypted_value() if secret is not None else ""


def _clean_policies(policies, tags):
    """Sanitize the per-tag reply policy dict against the tag taxonomy.

    Only known tags survive, unknown modes coerce to "off", and the locked
    guardrail — ``urgent``/``spam`` can never be ``auto`` — is enforced here as
    a hard stop too (the form already refuses it; this covers non-form callers).
    """
    tags = dict(tags or {})
    out = {}
    for tag, mode in dict(policies or {}).items():
        if tag not in tags:
            continue
        mode = mode if mode in REPLY_MODES else "off"
        if mode == "auto" and tag in NEVER_AUTO_TAGS:
            raise ValueError(
                f"The '{tag}' tag can never be set to auto-publish — use draft."
            )
        out[tag] = mode
    for tag in tags:
        out.setdefault(tag, "off")
    return out


def test_youtube(values):
    """Probe the API key + channel from the form. Returns {ok, message}."""
    key = _secret_or_saved(values, "yt_api_key")
    if not key:
        return {"ok": False, "message": "Enter the YouTube API key (or save it first)."}
    raw = (values.get("channel") or "").strip()
    if not raw:
        return {"ok": False, "message": "Enter the channel ID, @handle, or URL to test."}
    try:
        channel_id, title = resolve_channel(key, raw)
    except (ValueError, ConnectionError) as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "message": f"Connected — found “{title}” ({channel_id})."}


# --------------------------------------------------------------------------- #
# Write (the one provisioning entry point)
# --------------------------------------------------------------------------- #

def provision(data, *, created_by=None):
    """Idempotently provision/update everything from cleaned form ``data``.

    Returns ``(script, warnings)``. ``warnings`` is a list of advisory strings.
    Raises ``ValueError`` with a user-facing message when the instance has no
    data server (managed Databases are REQUIRED for this plugin) or the channel
    can't be resolved.
    """
    warnings = []

    # 0) Hard requirement: a data server. Comments live in real SQL.
    if not DatabaseAPI(OWNER).is_available():
        raise ValueError(
            "This plugin stores comments in a managed Database, but no data server "
            "is attached to this instance. Set PYRUNNER_DATA_DB_URL (see the "
            "Databases documentation), then save again."
        )

    # 1) Resolve the channel — the worker needs a concrete channel id.
    api_key = _secret_or_saved(data, "yt_api_key")
    raw_channel = (data.get("channel") or "").strip()
    try:
        channel_id, channel_title = resolve_channel(api_key, raw_channel)
    except ConnectionError as exc:
        kind, ref = _extract_channel_ref(raw_channel)
        if kind != "id":
            raise ValueError(f"{exc} Handles need YouTube to be reachable — try again.")
        channel_id, channel_title = ref, ""
        warnings.append(f"{exc} Saved the channel id unverified.")

    # 2) Owned DataStore + non-secret config entry.
    store = DataStoreAPI(OWNER).upsert(
        STORE_KEY, description="YouTube Comments AI — config + dashboard state",
        created_by=created_by,
    )
    start_date = data.get("start_date")
    store.set("config", {
        "channel_id": channel_id,
        "channel_title": channel_title,
        "start_date": start_date.isoformat() if hasattr(start_date, "isoformat") else str(start_date or ""),
        "include_replies": bool(data.get("include_replies", True)),
        "max_pages_per_run": int(data.get("max_pages_per_run") or 30),
        "tags": dict(data.get("tags") or {}),
        "ai_enabled": bool(data.get("ai_enabled", True)),
        "ai_model": (data.get("ai_model") or "").strip(),
        "max_ai_per_run": int(data.get("max_ai_per_run") or 200),
        "alerts_enabled": bool(data.get("alerts_enabled", True)),
        "digest_enabled": bool(data.get("digest_enabled", True)),
        "alert_email": (data.get("alert_email") or "").strip(),
        "alert_channel": (data.get("alert_channel") or "").strip(),
        "channel_digest_enabled": bool(data.get("channel_digest_enabled")),
        "reply_policies": _clean_policies(data.get("reply_policies"), data.get("tags")),
        "auto_post_daily_cap": int(data.get("auto_post_daily_cap") or 10),
        "moderate_spam": bool(data.get("moderate_spam")),
        "insights_enabled": bool(data.get("insights_enabled", True)),
        "insights_weekday": min(6, max(0, int(data.get("insights_weekday") or 0))),
        "avatar_archive": bool(data.get("avatar_archive", True)),
    })

    # Channel alerts are advisory-validated: the picker only offers existing
    # channels, but the channel can be renamed/disabled later — the worker
    # degrades (logs + keeps running), so a stale pick is a warning, not a stop.
    alert_channel = (data.get("alert_channel") or "").strip()
    if alert_channel:
        match = next((c for c in list_channels() if c["name"] == alert_channel), None)
        if match is None:
            warnings.append(
                f"Channel '{alert_channel}' doesn't exist (anymore) — channel alerts "
                "won't send until you pick an existing channel."
            )
        elif not match["is_enabled"]:
            warnings.append(
                f"Channel '{alert_channel}' is disabled — channel alerts won't send "
                "until you enable it under Channels."
            )

    # 3) Owner-scoped secrets — only (re)write the ones actually supplied;
    #    a blank field keeps the existing value.
    secrets_api = SecretAPI(OWNER)
    for field_name, env_key in SECRET_FIELDS.items():
        value = (data.get(field_name) or "").strip()
        if value:
            secrets_api.upsert(env_key, value, description=f"YouTube Comments AI — {env_key}")

    # 4) Environment (must exist; should carry requests + psycopg).
    env = EnvironmentAPI().get(data["environment"])
    if env is None:
        raise ValueError("Select an environment (one that has 'requests' and 'psycopg' installed).")
    reqs = (env.requirements or "").lower()
    for pkg in ("requests", "psycopg"):
        if pkg not in reqs:
            warnings.append(
                f"Environment '{env.name}' may be missing '{pkg}'. "
                "Add it under Environments or runs will fail."
            )
    if bool(data.get("ai_enabled", True)) and "claude-agent-sdk" not in reqs:
        warnings.append(
            f"AI classification needs 'claude-agent-sdk' in environment '{env.name}' "
            "(and an active provider under Services → AI Provider) — comments will "
            "stay pending analysis until it's available."
        )

    # 5) Managed Database (real Postgres schema) — the comment archive.
    database = DatabaseAPI(OWNER).provision(
        DB_KEY, description="YouTube Comments AI — comment archive", created_by=created_by
    )

    # 6) Managed Script (selected-mode injection; only granted secrets reach it).
    script = ScriptAPI(OWNER).upsert(
        key=SCRIPT_KEY,
        name=SCRIPT_NAME,
        code=_worker_code(),
        environment=env,
        timeout_seconds=DEFAULT_TIMEOUT,
        injection_mode="selected",
        description="Managed by the YouTube Comments AI plugin — edit settings on its page.",
        is_enabled=True,
        notify_on=data.get("notify_on") or "failure",
        notify_email=data.get("notify_email") or "",
        created_by=created_by,
        # Single-run by design: a Run now or the daily tick that lands while
        # a run is in flight is skipped (the Skip concurrency policy).
        concurrency_policy="skip",
    )

    # 7) Grants: the owner secrets that exist, the database, and the YouTube
    #    connection once one is connected. The pre-0.6.0 OAuth secrets go: an
    #    owned secret reaches the script whether granted or not.
    for env_key in SECRET_FIELDS.values():
        secret = secrets_api.get(env_key)
        if secret is not None:
            secrets_api.grant(script, secret)
    for env_key in LEGACY_OAUTH_SECRETS:
        secret = secrets_api.get(env_key)
        if secret is not None:
            secret.delete()
    DatabaseAPI(OWNER).grant(script, database)
    sync_connection_grant(script)

    # 8) Daily schedule.
    ScheduleAPI(OWNER).sync(
        script,
        mode="daily",
        time_str=data.get("schedule_time") or "08:00",
        tz=data.get("timezone") or "UTC",
    )

    return script, warnings


def queue_run(triggered_by=None):
    """Queue a tracked Run of the managed analyzer script (via the RunBackend seam).

    Returns ``(run, error_message)``; ``run`` is None when not runnable, or when
    the script's Skip concurrency policy turned it away because a run is
    already in flight (the analyzer is single-run by design).
    """
    script = get_script()
    if script is None:
        return None, "Not configured yet — save the settings below first."
    if not script.can_run:
        return None, "The analyzer script is disabled or archived."
    if script.concurrency_policy != "skip":
        # Provisioned before the policy existed: set it now, on first use.
        ScriptAPI(OWNER).upsert(key=SCRIPT_KEY, concurrency_policy="skip")
    run = ScriptAPI(OWNER).queue_run(SCRIPT_KEY, triggered_by=triggered_by)
    if run.cancel_reason == "maintenance":
        return None, "Not started: a maintenance window blocks manual runs right now."
    if run.cancel_reason:
        return None, "A run is already in progress."
    return run, None


# --------------------------------------------------------------------------- #
# Live status + control
# --------------------------------------------------------------------------- #

def live_status():
    """A JSON-serializable snapshot for the page's status poller.

    Run state is AUTHORITATIVE via the SDK (``latest_run``); the phase progress
    comes from the worker's heartbeat, shown only when it belongs to the current
    run (so a previous run's bar never lingers).
    """
    run = ScriptAPI(OWNER).latest_run(SCRIPT_KEY)
    active = bool(run and run.status in ("pending", "running"))
    progress = None
    if active:
        p = get_progress()
        if p and p.get("state") != "done" and (p.get("run_id") or "") == run.id.replace("-", ""):
            progress = p
    return {
        "active": active,
        "run": run.as_dict() if run else None,
        "progress": progress,
    }


def cancel_running():
    """Stop the latest pending/running run (SDK → shared force-stop). Returns bool."""
    return ScriptAPI(OWNER).cancel_latest_run(SCRIPT_KEY)


def schedule_summary():
    """A small {mode, label, next_run} for the header status chip (or None)."""
    sched = get_schedule()
    if sched is None:
        return None
    if sched.run_mode == "daily":
        label = f"Daily {(sched.daily_times or ['?'])[0]}"
    else:
        label = sched.run_mode
    return {"mode": sched.run_mode, "label": label, "next_run": sched.next_run}


# --------------------------------------------------------------------------- #
# Immediate posting from the Reply Queue (web-process side)
# --------------------------------------------------------------------------- #

class PostError(Exception):
    """YouTube refused or couldn't take a reply. ``reason`` is YouTube's own code when it gave one."""

    def __init__(self, message, *, reason=""):
        super().__init__(message)
        self.reason = reason


def post_reply(access_token, parent_id, text):
    """``comments.insert``: post ``text`` as a reply to the top-level comment
    ``parent_id``. Returns the new reply's YouTube id. 50 quota units."""
    import requests

    try:
        resp = requests.post(
            f"{YT_API}/comments",
            params={"part": "snippet"},
            json={"snippet": {"parentId": parent_id, "textOriginal": text}},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
    except requests.exceptions.RequestException as exc:
        raise PostError(f"Couldn't reach the YouTube API ({exc.__class__.__name__}).") from exc
    if resp.status_code >= 400:
        reason, message = "", f"HTTP {resp.status_code}"
        try:
            err = resp.json().get("error", {})
            errors = err.get("errors", [])
            reason = errors[0].get("reason", "") if errors else ""
            message = err.get("message") or message
        except (ValueError, IndexError):
            pass
        raise PostError(f"YouTube refused the reply: {message}", reason=reason)
    return str(resp.json().get("id", ""))


def post_reply_now(parent_id, text):
    """Post an approved reply immediately from the dashboard.

    Returns ``(yt_reply_id, error_message)``: exactly one is truthy. Any
    failure leaves the row ``approved`` for the worker to retry. The access
    token comes from the plugin's YouTube connection; PyRun refreshes it, and
    marks the connection expired when Google refuses the refresh (the card
    then offers Reconnect).
    """
    try:
        material = ConnectionAPI(OWNER).credentials(CONNECTION_KEY)
    except ConnectionUnavailable as exc:
        if exc.code == "CONNECTION_NOT_FOUND":
            return "", "YouTube isn't connected: connect it under Settings first."
        return "", exc.message
    try:
        return post_reply(material["token"], parent_id, text), ""
    except PostError as exc:
        return "", str(exc)
