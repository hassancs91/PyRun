"""
Slack for scripts: post and update messages, upload files, list channels, read a channel's messages and list people.

The connection is a Slack app installed in your Slack workspace: its bot token
(``xoxb-...``), pasted or through Connect. Each function needs the app to have
the matching scope (the boxes on the connection form): ``chat:write`` to post
and update, ``files:write`` to upload, ``channels:read`` to list channels,
``channels:history`` to read messages, ``users:read`` to list people. The bot
posts only in channels it is a member of: invite it first (``/invite @your-app``),
or Slack answers ``not_in_channel``.

A channel is its id (``C0123ABCD``; in Slack: the channel's name, then "Copy
link" or the About tab) or, where a function says so, its name (``#general``).
Messages are Slack's own dicts (``ts``, ``text``, ``user``...): a message's
``ts`` is its id, which ``update_message`` and ``thread_ts`` take.
Lists come page by page as you read them; ``limit`` stops early.
"""

import json as _json
import os

from . import _client

PROVIDER = "slack"
PAGE_SIZE = 200  # items per page (Slack's advice for its cursor lists)

ERROR_CODES = {
    **_client.ERROR_CODES,
    "SLACK_ERROR": "Slack refused the call with an error none of the codes above covers; `slack_error` "
                   "holds Slack's own name for it (`cant_update_message`, `is_archived`...).",
}
# The paragraph after the shared one on the generated page.
ERRORS_NOTE = (
    "Slack answers most failures with HTTP 200 and `\"ok\": false`: those raise too, with the code below "
    "and Slack's error name in the message and in the exception's `slack_error` (`not_in_channel` is "
    "FORBIDDEN, `channel_not_found` NOT_FOUND, `missing_scope` FORBIDDEN naming the scope it needs). "
    "After `invalid_auth` or `token_expired` PyRun is asked once for new credentials, as after a 401."
)

# Slack's error names (api.slack.com/methods: each method's "Errors" table) -> IntegrationError codes.
_UNAUTHORIZED = {"not_authed", "invalid_auth", "token_expired", "token_revoked", "account_inactive"}
_FORBIDDEN = {"missing_scope", "not_allowed_token_type", "no_permission", "not_in_channel", "access_denied",
              "restricted_action", "restricted_action_read_only_channel", "restricted_action_thread_only_channel",
              "restricted_action_non_threadable_channel", "ekm_access_denied", "team_access_not_granted",
              "cant_update_message", "edit_window_closed"}
_NOT_FOUND = {"channel_not_found", "user_not_found", "users_not_found", "message_not_found", "thread_not_found",
              "file_not_found"}
_CONFLICT = {"is_archived", "already_in_channel"}
_RATE_LIMITED = {"ratelimited", "rate_limited"}
_SERVER = {"internal_error", "fatal_error", "service_unavailable", "request_timeout"}
_INVALID = {"no_text", "msg_too_long", "too_many_attachments", "too_many_blocks"}


def _code(error) -> str:
    for code, names in (("UNAUTHORIZED", _UNAUTHORIZED), ("FORBIDDEN", _FORBIDDEN), ("NOT_FOUND", _NOT_FOUND),
                        ("CONFLICT", _CONFLICT), ("RATE_LIMITED", _RATE_LIMITED), ("SERVER_ERROR", _SERVER),
                        ("INVALID_REQUEST", _INVALID)):
        if error in names:
            return code
    if error.startswith(("invalid_", "missing_")):
        return "INVALID_REQUEST"
    return "SLACK_ERROR"


def _check(response, body):
    """Raise the IntegrationError for a Slack reply with ``"ok": false`` (HTTP 200)."""
    if not isinstance(body, dict) or body.get("ok") is not False:
        return
    error = str(body.get("error") or "unknown_error")
    message = f"{_client.safe_url(response.url)}: Slack answered {error!r}"
    if error == "missing_scope" and body.get("needed"):
        message += f" (the app needs the {body['needed']} scope; it has {body.get('provided') or 'none'})"
    exc = _client.IntegrationError(message, _code(error), response.status_code, response=response)
    exc.slack_error = error
    raise exc


def _api(connection):
    return _client.shared(connection, PROVIDER, check=_check)


def _result(body) -> dict:
    """A reply without Slack's envelope keys."""
    return {k: v for k, v in body.items() if k not in ("ok", "warning", "response_metadata")}


def _page_size(limit) -> int:
    return min(PAGE_SIZE, limit) if limit and limit > 0 else PAGE_SIZE


def _list(connection, method, items, params, limit):
    return _api(connection).paginate(
        f"/{method}", params={**params, "limit": _page_size(limit)}, items=items,
        cursor="response_metadata.next_cursor", cursor_param="cursor", limit=limit)


def _message_body(channel, text, blocks) -> dict:
    if not text and not blocks:
        raise ValueError("A message needs text= or blocks= (with blocks, text is the notification's fallback).")
    body = {"channel": channel}
    if text:
        body["text"] = text
    if blocks:
        body["blocks"] = list(blocks)
    return body


def client(connection=None, *, timeout=30):
    """The shared client of ``connection``, for Slack methods this module has no function for.

    Paths are method names under ``https://slack.com/api``; a reply with
    ``"ok": false`` raises, as in the functions here. For example:

        slack.client().post("/reactions.add", json={"channel": channel, "timestamp": ts, "name": "eyes"})
    """
    return _client.Client(connection, provider=PROVIDER, timeout=timeout, check=_check)


def post_message(channel, text=None, *, blocks=None, thread_ts=None, reply_broadcast=False, unfurl_links=None,
                 connection=None):
    """Post a message and return ``{"channel", "ts", "message"}`` (``ts`` is the new message's id).

    ``channel`` is a channel's id or name (``#general``), or a person's user id
    (``U0123ABCD``) for a direct message from the app. ``text`` takes Slack's
    markup (``*bold*``, ``<https://example.com|a link>``, ``<@U0123ABCD>``);
    ``blocks`` is a list of Block Kit blocks (then ``text`` is the notification's
    fallback). ``thread_ts`` replies in that message's thread
    (``reply_broadcast=True`` also shows it in the channel). ``unfurl_links=False``
    turns off link previews. Needs ``chat:write``. For example:

        sent = slack.post_message("#alerts", "*Disk almost full* on web-1")
        slack.post_message("#alerts", "Cleaned up: 40 GB free.", thread_ts=sent["ts"])
    """
    body = _message_body(channel, text, blocks)
    if thread_ts:
        body["thread_ts"] = str(thread_ts)
        if reply_broadcast:
            body["reply_broadcast"] = True
    if unfurl_links is not None:
        body["unfurl_links"] = body["unfurl_media"] = bool(unfurl_links)
    return _result(_api(connection).post("/chat.postMessage", json=body))


def update_message(channel, ts, text=None, *, blocks=None, connection=None):
    """Change a message the app posted (its ``ts``) and return ``{"channel", "ts", "text", "message"}``.

    ``channel`` is the channel's id (as ``post_message`` returned it). Needs
    ``chat:write``. For example:

        sent = slack.post_message("#deploys", "Deploying v2.5...")
        slack.update_message(sent["channel"], sent["ts"], "Deployed v2.5 :white_check_mark:")
    """
    body = _message_body(channel, text, blocks)
    body["ts"] = str(ts)
    return _result(_api(connection).post("/chat.update", json=body))


def _channel_id(channel, connection) -> str:
    if not str(channel).startswith("#"):
        return str(channel)
    found = find_channel(channel, connection=connection)
    if found is None:
        raise _client.IntegrationError(
            f"No public channel named {channel} (pass a private channel's id).", "NOT_FOUND")
    return found["id"]


def upload_file(file, channel=None, *, filename=None, title=None, comment=None, thread_ts=None, alt_text=None,
                connection=None):
    """Upload a file (a path, or bytes with ``filename=``) and return Slack's file ``{"id", "title"}``.

    With ``channel`` (an id, or a public channel's name ``#reports``) it is
    shared there, with ``comment`` as its message, in ``thread_ts``'s thread when
    given; without, it stays private to the app until shared. ``alt_text``
    describes an image. Uses Slack's three-step upload (an upload URL, the
    file sent there without the token, then completing it). Needs
    ``files:write`` (and ``channels:read`` for a channel's name). The file is
    read into memory whole (Slack takes up to 1 GB). For example:

        slack.upload_file("/tmp/report.pdf", "#reports", comment="This week's numbers")
        slack.upload_file(csv_text.encode(), "C0123ABCD", filename="leads.csv")
    """
    if isinstance(file, (bytes, bytearray)):
        if not filename:
            raise ValueError("upload_file() needs filename= when the file is given as bytes.")
        content = bytes(file)
    else:
        with open(file, "rb") as handle:
            content = handle.read()
        filename = filename or os.path.basename(os.fspath(file))
    api = _api(connection)
    target = _channel_id(channel, connection) if channel else None
    form = {"filename": filename, "length": len(content)}
    if alt_text:
        form["alt_txt"] = alt_text
    ticket = api.post("/files.getUploadURLExternal", data=form)
    # The upload URL is Slack's file host: it takes the bytes without the token (a host other than the
    # connection's never gets it).
    api.request("POST", ticket["upload_url"], data=content,
                headers={"Content-Type": "application/octet-stream"})
    done = {"files": _json.dumps([{"id": ticket["file_id"], "title": title or filename}])}
    if target:
        done["channel_id"] = target
        if comment:
            done["initial_comment"] = comment
        if thread_ts:
            done["thread_ts"] = str(thread_ts)
    files = api.post("/files.completeUploadExternal", data=done).get("files") or [{"id": ticket["file_id"]}]
    return files[0]


def list_channels(*, types="public_channel", include_archived=False, limit=None, connection=None):
    """Yield the workspace's channels: Slack's channel dicts (``id``, ``name``, ``is_member``, ``topic``...).

    ``types`` is a comma-separated list of ``public_channel``,
    ``private_channel`` (needs ``groups:read``), ``mpim`` (``mpim:read``) and
    ``im`` (``im:read``). Archived
    channels are left out unless ``include_archived=True``. Needs
    ``channels:read``. For example:

        for channel in slack.list_channels():
            print(channel["id"], channel["name"], "member" if channel["is_member"] else "")
    """
    params = {"types": types, "exclude_archived": "false" if include_archived else "true"}
    return _list(connection, "conversations.list", "channels", params, limit)


def find_channel(name, *, types="public_channel", connection=None):
    """The channel named ``name`` (with or without ``#``), or None. Needs ``channels:read``.

    For example:

        alerts = slack.find_channel("#alerts")
        if alerts and not alerts["is_member"]:
            print("Invite the app to #alerts first")
    """
    wanted = str(name).lstrip("#").lower()
    for channel in list_channels(types=types, connection=connection):
        if str(channel.get("name", "")).lower() == wanted:
            return channel
    return None


def history(channel, *, oldest=None, latest=None, limit=None, connection=None):
    """Yield a channel's messages, newest first: Slack's message dicts (``ts``, ``user``, ``text``...).

    ``channel`` is the channel's id. ``oldest`` / ``latest`` are message
    ``ts`` values (or Unix times) bounding the messages returned (both
    excluded). Thread replies stay in their threads. Needs
    ``channels:history`` (``groups:history`` for a private channel). For example:

        for message in slack.history("C0123ABCD", limit=20):
            print(message["ts"], message.get("user"), message.get("text"))
    """
    params = {"channel": str(channel)}
    if oldest is not None:
        params["oldest"] = str(oldest)
    if latest is not None:
        params["latest"] = str(latest)
    return _list(connection, "conversations.history", "messages", params, limit)


def list_users(*, include_bots=False, include_deleted=False, limit=None, connection=None):
    """Yield the workspace's people: Slack's user dicts (``id``, ``name``, ``real_name``, ``profile``...).

    Bots and deactivated accounts are left out unless asked for. Needs
    ``users:read`` (``users:read.email`` for ``profile["email"]``). For example:

        names = {user["id"]: user["real_name"] for user in slack.list_users()}
    """
    users = (user for user in _list(connection, "users.list", "members", {}, None)
             if (include_bots or not (user.get("is_bot") or user.get("id") == "USLACKBOT"))
             and (include_deleted or not user.get("deleted")))
    return _client.limited(users, limit)
