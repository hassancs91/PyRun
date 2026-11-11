"""
Gmail for scripts: search and read mail, send and reply, labels, attachments, and new mail since last time.

Messages come back as dicts: Gmail's own ``id``, ``threadId``,
``labelIds``, ``snippet`` and ``internalDate`` (when Gmail got it, in
milliseconds since 1970, as text), plus the message decoded: ``headers``
(every header by its lower-case name, the first one when a header repeats),
``subject``, ``from``, ``to``, ``cc``, ``date``, ``message_id`` (its
Message-ID header), ``text`` and ``html`` (the bodies; None when it has
none) and ``attachments``, each with ``attachmentId``, ``partId``,
``filename``, ``mimeType`` and ``size`` (for ``download_attachment``).

Labels are given by name (``"Invoices"``, ``"Clients/Acme"``) or by id;
Gmail's own by their ids: ``INBOX``, ``UNREAD``, ``STARRED``,
``IMPORTANT``, ``SENT``, ``DRAFT``, ``SPAM``, ``TRASH``.

The connection is a Google connection with a Gmail scope set: **Gmail: read**
(``gmail.readonly``) searches and reads, **Gmail: send** (``gmail.send``)
only sends, **Gmail: read, label and send** (``gmail.modify``) does
everything here (``reply`` reads the message it answers; labels need it).
Its token goes only to Gmail's API host, ``https://gmail.googleapis.com``.
"""

import base64
import email.header
import os
import re

from . import _client, _mail

PROVIDER = "google"
BASE_URL = "https://gmail.googleapis.com"  # Gmail's own API host (the connection's base URL is www.googleapis.com)
PAGE_SIZE = 100  # messages per page (Gmail allows up to 500)
SIMPLE_MAX = 5 * 1024 * 1024  # larger messages go as an upload (Gmail takes up to 35 MB)
# Gmail's own labels: their ids are their names.
SYSTEM_LABELS = frozenset({"INBOX", "UNREAD", "STARRED", "IMPORTANT", "SENT", "DRAFT", "SPAM", "TRASH", "CHAT"})
_FORMATS = ("full", "metadata")
_LABEL_ID = re.compile(r"^(Label_\d+|CATEGORY_[A-Z]+)$")
_CHARSET = re.compile(r"charset=[\"']?([\w.:-]+)", re.I)


def _api(connection):
    return _client.shared(connection, PROVIDER, base_url=BASE_URL)


def _path(suffix="") -> str:
    return f"/gmail/v1/users/me{suffix}"


def _b64decode(data) -> bytes:
    data = str(data or "")
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _header(value) -> str:
    """A header value with any RFC 2047 encoded words (``=?utf-8?q?...?=``) decoded."""
    try:
        return str(email.header.make_header(email.header.decode_header(value)))
    except (ValueError, LookupError, UnicodeDecodeError):
        return value


def _part_text(part) -> str:
    charset = "utf-8"
    for header in part.get("headers") or []:
        if header.get("name", "").lower() == "content-type":
            match = _CHARSET.search(header.get("value") or "")
            if match:
                charset = match.group(1)
    data = _b64decode((part.get("body") or {}).get("data"))
    try:
        return data.decode(charset, "replace")
    except LookupError:
        return data.decode("utf-8", "replace")


def _message(raw) -> dict:
    """A Gmail message resource as this module returns it (see the module docstring)."""
    payload = raw.get("payload") or {}
    headers = {}
    for header in payload.get("headers") or []:
        headers.setdefault(str(header.get("name", "")).lower(), _header(str(header.get("value", ""))))
    found = {"text": None, "html": None}
    attachments = []

    def walk(part):
        mime_type = str(part.get("mimeType") or "").lower()
        body = part.get("body") or {}
        if part.get("filename"):
            attachments.append({"attachmentId": body.get("attachmentId"), "partId": part.get("partId"),
                                "filename": part["filename"], "mimeType": part.get("mimeType"),
                                "size": body.get("size", 0)})
        elif mime_type in ("text/plain", "text/html") and "data" in body:
            key = "text" if mime_type == "text/plain" else "html"
            if found[key] is None:
                found[key] = _part_text(part)
        for child in part.get("parts") or []:
            walk(child)

    walk(payload)
    out = {key: raw[key] for key in ("id", "threadId", "snippet", "internalDate", "sizeEstimate", "historyId")
           if key in raw}
    out["labelIds"] = list(raw.get("labelIds") or [])
    out.update(headers=headers, subject=headers.get("subject", ""), date=headers.get("date", ""),
               message_id=headers.get("message-id", ""), text=found["text"], html=found["html"],
               attachments=attachments)
    for key in ("from", "to", "cc"):
        out[key] = headers.get(key, "")
    return out


def _labels(value, name) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (bytes, dict)):
        raise TypeError(f"{name} is a label (a name or an id) or a list of them")
    return [str(v) for v in value]


def _label_ids(labels, connection, name="labels") -> list:
    """Label ids for names or ids (one or a list); ValueError for a name the mailbox doesn't have."""
    out, by_name = [], None
    for label in _labels(labels, name):
        if label in SYSTEM_LABELS or _LABEL_ID.match(label):
            out.append(label)
            continue
        if by_name is None:
            by_name = {}
            for found in list_labels(connection=connection):
                by_name.setdefault(str(found.get("name", "")).lower(), found["id"])
        if label.lower() not in by_name:
            raise ValueError(f"No Gmail label {label!r}: create it with gmail.create_label({label!r})")
        out.append(by_name[label.lower()])
    return out


def client(connection=None, *, timeout=30):
    """The shared client of ``connection``, for calls this module has no function for.

    Paths are relative to ``https://gmail.googleapis.com``. For example:

        threads = gmail.client().get("/gmail/v1/users/me/threads", params={"q": "is:starred"})
    """
    return _client.Client(connection, provider=PROVIDER, timeout=timeout, base_url=BASE_URL)


def search(query=None, *, labels=None, include_spam_trash=False, limit=None, connection=None):
    """Yield the messages matching ``query``, newest first, as ``{"id", "threadId"}``, fetching pages as it goes.

    ``query`` is a Gmail search, as in Gmail's search box
    (``"from:billing@example.com has:attachment newer_than:7d"``); none lists
    every message. ``labels`` keeps those with every label given (one or a
    list). Spam and trash are left out unless ``include_spam_trash``.
    ``limit`` stops after that many. ``get_message`` reads one. For example:

        for found in gmail.search("is:unread label:Invoices", limit=20):
            msg = gmail.get_message(found["id"])
            print(msg["from"], msg["subject"])
    """
    params = {"maxResults": min(PAGE_SIZE, limit) if limit and limit > 0 else PAGE_SIZE}
    if query:
        params["q"] = str(query)
    if labels is not None:
        params["labelIds"] = _label_ids(labels, connection)
    if include_spam_trash:
        params["includeSpamTrash"] = "true"
    api = _api(connection)

    def fetch(token):
        body = api.get(_path("/messages"), params={**params, "pageToken": token} if token else params) or {}
        return body.get("messages") or [], body.get("nextPageToken")

    yield from _client.paginate(fetch, limit=limit)


def get_message(message_id, *, format="full", connection=None):  # noqa: A002 - Gmail's own name
    """One message, decoded (see the module docstring); one that doesn't exist raises NOT_FOUND.

    ``format="metadata"`` reads only its headers and labels (no ``text``,
    ``html`` or ``attachments``: quicker, for a list). For example:

        msg = gmail.get_message(message_id)
        print(msg["subject"], msg["from"])
        print(msg["text"] or msg["html"])
    """
    if format not in _FORMATS:
        raise ValueError(f"format is one of {', '.join(_FORMATS)}, not {format!r}")
    raw = _api(connection).get(_path(f"/messages/{_client.quote(message_id)}"), params={"format": format})
    return _message(raw or {})


def _send(msg, thread_id, connection):
    raw = _mail.as_bytes(msg)
    api = _api(connection)
    if len(raw) <= SIMPLE_MAX:
        body = {"raw": base64.urlsafe_b64encode(raw).decode("ascii")}
        if thread_id:
            body["threadId"] = thread_id
        return api.post(_path("/messages/send"), json=body)
    # Larger: an upload of the message itself (multipart: the thread, then the message).
    data, content_type = _client.multipart_related({"threadId": thread_id} if thread_id else {}, raw,
                                                   "message/rfc822")
    return api.call("POST", "/upload" + _path("/messages/send"), params={"uploadType": "multipart"}, data=data,
                    headers={"Content-Type": content_type})


def send(to=None, subject="", text=None, *, html=None, cc=None, bcc=None, reply_to=None, from_address=None,
         attachments=None, headers=None, connection=None):
    """Send an email from the connection's account; returns Gmail's ``{"id", "threadId", "labelIds"}``.

    ``to``, ``cc`` and ``bcc`` take one address (``"ada@example.com"`` or
    ``"Ada Lovelace <ada@example.com>"``), several separated by commas, or a
    list. ``text`` is the plain-text body, ``html`` an HTML one (give both
    and mail apps show the HTML, or the text where they can't).
    ``from_address`` is one of the account's "Send mail as" addresses (the
    account's own by default). ``attachments`` is a list (or one) of file
    paths, ``(filename, content)`` or ``(filename, content, mime type)``
    tuples (content as bytes, or text), or dicts with ``filename``,
    ``content`` and ``mime_type``; the type is guessed from the name when not given.
    ``headers`` adds headers of your own. Gmail takes messages up to 35 MB
    (attachments count a third more, encoded). Needs a send scope (**Gmail:
    send**, or **Gmail: read, label and send**). For example:

        gmail.send("ada@example.com", "Weekly report", "The numbers are attached.",
                   attachments=["/tmp/report.pdf"])
    """
    msg = _mail.build(to=to, subject=subject, text=text, html=html, cc=cc, bcc=bcc, reply_to=reply_to,
                      from_address=from_address, attachments=attachments, headers=headers)
    return _send(msg, None, connection)


def reply(message_id, text=None, *, html=None, reply_all=False, cc=None, bcc=None, attachments=None,
          headers=None, connection=None):
    """Reply to a message, in its thread; returns Gmail's ``{"id", "threadId", "labelIds"}``.

    The reply goes to the message's Reply-To, else its sender (to its
    recipients when you sent it), with "Re: " and the headers that keep it in
    the conversation. ``reply_all`` copies everyone else it went to (To and
    Cc, without you). ``cc``, ``bcc``, ``attachments`` and ``headers`` as in
    ``send``. Needs to read the message and send (**Gmail: read, label and
    send**, or both **Gmail: read** and **Gmail: send**). For example:

        for found in gmail.search("is:unread subject:quote", limit=5):
            gmail.reply(found["id"], "Thanks, we'll get back to you today.")
            gmail.modify_labels(found["id"], remove="UNREAD")
    """
    original = get_message(message_id, format="metadata", connection=connection)
    headers_in = original["headers"]
    sent_by_me = "SENT" in original["labelIds"]
    if sent_by_me:
        to = _mail.addresses(headers_in.get("to") or None, "the message's To", lenient=True)
    else:
        sender = headers_in.get("reply-to") or headers_in.get("from") or None
        to = _mail.addresses(sender, "the message's sender", lenient=True)
    copies = []
    if reply_all:
        me = str(_api(connection).get(_path("/profile")).get("emailAddress") or "").lower()
        others = _mail.addresses(headers_in.get("cc") or None, "the message's Cc", lenient=True)
        if not sent_by_me:
            others = _mail.addresses(headers_in.get("to") or None, "the message's To", lenient=True) + others
        taken = {a.addr_spec.lower() for a in to} | {me}
        for address in others:
            if address.addr_spec.lower() not in taken:
                taken.add(address.addr_spec.lower())
                copies.append(address)
    if not to:
        raise ValueError(f"Message {message_id} has no sender to reply to")
    subject = original["subject"]
    if not re.match(r"re\s*:", subject, re.I):
        subject = f"Re: {subject}"
    message_header = original["message_id"]
    references = " ".join(part for part in (headers_in.get("references", ""), message_header) if part)
    msg = _mail.build(to=to, subject=subject, text=text, html=html,
                      cc=copies + _mail.addresses(cc, "cc"), bcc=bcc, attachments=attachments, headers=headers,
                      in_reply_to=message_header or None, references=references or None)
    return _send(msg, original.get("threadId"), connection)


def list_labels(*, connection=None):
    """Every label of the mailbox (Gmail's and yours): ``[{"id", "name", "type", ...}]``.

    For example:

        names = sorted(label["name"] for label in gmail.list_labels() if label["type"] == "user")
    """
    return (_api(connection).get(_path("/labels")) or {}).get("labels") or []


def create_label(name, *, connection=None):
    """Create a label (``"Clients/Acme"`` nests it under "Clients"); returns it. One that exists raises CONFLICT.

    For example:

        label = gmail.create_label("Processed")
    """
    body = {"name": str(name), "labelListVisibility": "labelShow", "messageListVisibility": "show"}
    return _api(connection).post(_path("/labels"), json=body)


def modify_labels(message_id, *, add=None, remove=None, connection=None):
    """Add and remove labels on a message (each one, or a list, by name or id); returns ``{"id", "threadId", "labelIds"}``.

    Removing ``UNREAD`` marks it read, adding ``STARRED`` stars it,
    removing ``INBOX`` archives it. For example:

        gmail.modify_labels(message_id, add="Processed", remove=["UNREAD", "INBOX"])
    """
    body = {}
    if add is not None:
        body["addLabelIds"] = _label_ids(add, connection, "add")
    if remove is not None:
        body["removeLabelIds"] = _label_ids(remove, connection, "remove")
    if not body.get("addLabelIds") and not body.get("removeLabelIds"):
        raise ValueError("modify_labels() needs add= or remove=")
    return _api(connection).post(_path(f"/messages/{_client.quote(message_id)}/modify"), json=body)


def download_attachment(message_id, attachment, path=None, *, connection=None):
    """An attachment's content: bytes, or written to ``path`` (then the path is returned).

    ``attachment`` is one of a message's ``attachments`` (from
    ``get_message``) or its ``attachmentId``. The content is held in memory.
    For example:

        msg = gmail.get_message(message_id)
        for attachment in msg["attachments"]:
            if attachment["filename"].endswith(".pdf"):
                gmail.download_attachment(message_id, attachment, f"/tmp/{attachment['filename']}")
    """
    if isinstance(attachment, dict):
        attachment_id, part_id = attachment.get("attachmentId"), attachment.get("partId")
    elif isinstance(attachment, str) and attachment:
        attachment_id, part_id = attachment, None
    else:
        raise TypeError("attachment is one of a message's attachments (a dict) or its attachmentId")
    api = _api(connection)
    if attachment_id:
        body = api.get(_path(f"/messages/{_client.quote(message_id)}/attachments/{_client.quote(attachment_id)}"))
        data = _b64decode((body or {}).get("data"))
    else:
        # A small attachment Gmail keeps inside the message: read it from there.
        raw = api.get(_path(f"/messages/{_client.quote(message_id)}"), params={"format": "full"}) or {}
        data = _find_part_data(raw.get("payload") or {}, part_id)
        if data is None:
            raise _client.IntegrationError(f"Message {message_id} has no attachment part {part_id!r}", "NOT_FOUND")
    if path is None:
        return data
    with open(path, "wb") as f:
        f.write(data)
    return os.fspath(path)


def _find_part_data(part, part_id):
    if part.get("partId") == part_id and "data" in (part.get("body") or {}):
        return _b64decode(part["body"]["data"])
    for child in part.get("parts") or []:
        found = _find_part_data(child, part_id)
        if found is not None:
            return found
    return None


def _wanted(message, label_id) -> bool:
    """Whether new_messages reports ``message``: mail that came in (and has ``label_id``, when one is watched)."""
    labels = set(message["labelIds"])
    if labels & ({"DRAFT", "CHAT", "SPAM", "TRASH"} - {label_id}):
        return False
    if "SENT" in labels and "INBOX" not in labels and label_id != "SENT":
        return False
    return label_id is None or label_id in labels


def _matches(api, message, query) -> bool:
    """Whether ``message`` matches the Gmail search ``query`` (asked by its Message-ID header)."""
    header = message["message_id"].strip().strip("<>")
    if not header:
        return False
    body = api.get(_path("/messages"), params={"q": f"rfc822msgid:{header} ({query})", "maxResults": 10}) or {}
    return any(found.get("id") == message["id"] for found in body.get("messages") or [])


def new_messages(state=None, *, query=None, label=None, limit=None, format="metadata",  # noqa: A002
                 connection=None):
    """The messages that came in since ``state``: ``(messages, new_state)``.

    For a scheduled script that acts on new mail (Triggers' "new email"
    watcher uses it too). Messages are ``get_message`` dicts (``format`` as
    there; ``"metadata"`` by default), oldest first. ``state`` is what the
    previous call returned (a small dict; keep it, e.g. in a datastore);
    without one, nothing is new yet and the state marks where the mailbox is.
    ``label`` reports only mail with that label (also mail that gets it
    later); ``query`` only mail matching that Gmail search (each new message
    is checked by its Message-ID header: one without it doesn't match). Drafts, chats,
    spam, trash and mail you sent (unless it came to your inbox too, or you
    watch ``label="SENT"``) are left out. ``limit`` returns at most that many
    (the rest come next time). Gmail keeps about a week of changes: a state
    older than that starts again from now, reporting nothing. For example:

        store = DataStore("gmail")              # from pyrunner_datastore import DataStore
        messages, state = gmail.new_messages(store.get("inbox"), label="INBOX")
        for msg in messages:
            print("new mail from", msg["from"], msg["subject"])
        store["inbox"] = state
    """
    if format not in _FORMATS:
        raise ValueError(f"format is one of {', '.join(_FORMATS)}, not {format!r}")
    api = _api(connection)
    start = state.get("history_id") if isinstance(state, dict) else None
    if not isinstance(start, str) or not start.isdigit():
        return [], {"history_id": str(api.get(_path("/profile"))["historyId"])}
    label_id = _label_ids(label, connection, "label")[0] if label else None
    params = {"startHistoryId": start, "historyTypes": ["messageAdded"] + (["labelAdded"] if label_id else []),
              "maxResults": 500}
    if label_id:
        params["labelId"] = label_id
    latest = {"id": start}

    def fetch(token):
        body = api.get(_path("/history"), params={**params, "pageToken": token} if token else params) or {}
        latest["id"] = str(body.get("historyId") or latest["id"])
        return body.get("history") or [], body.get("nextPageToken")

    try:
        records = list(_client.paginate(fetch))
    except _client.IntegrationError as e:
        if e.code != "NOT_FOUND":
            raise
        # The mark is older than the history Gmail keeps: start again from now.
        return [], {"history_id": str(api.get(_path("/profile"))["historyId"])}
    already = set(state.get("seen") or [])  # reported after the mark by a call that stopped at its limit
    out, looked_at, mark, reported = [], set(), start, []
    for record in records:
        ids = [added["message"]["id"] for added in record.get("messagesAdded") or []]
        ids += [added["message"]["id"] for added in record.get("labelsAdded") or []
                if label_id and label_id in (added.get("labelIds") or [])]
        for message_id in ids:
            if message_id in looked_at or message_id in already:
                continue
            if limit is not None and len(out) >= limit:
                stopped = {"history_id": mark}
                if reported or (already and mark == start):
                    stopped["seen"] = sorted(set(reported) | (already if mark == start else set()))
                return out, stopped
            looked_at.add(message_id)
            try:
                message = get_message(message_id, format=format, connection=connection)
            except _client.IntegrationError as e:
                if e.code == "NOT_FOUND":
                    continue  # deleted since
                raise
            if not _wanted(message, label_id) or (query and not _matches(api, message, query)):
                continue
            out.append(message)
            reported.append(message_id)
        mark, reported = str(record.get("id") or mark), []
    return out, {"history_id": latest["id"]}
