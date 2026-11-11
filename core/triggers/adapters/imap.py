"""
``imap_new_message``: a run per email that arrives in an IMAP mailbox.

It logs in with an IMAP connection (Connections > IMAP mailbox: host, port,
security, username and password), opens the mailbox read-only (EXAMINE) and
asks for the messages whose UID is past the last one it reported, optionally
narrowed by an IMAP search ("FROM billing@example.com", "UNSEEN"). Headers are
read with BODY.PEEK, so nothing is marked as read; the run reads the body itself.

State: ``{"uidvalidity": <the mailbox's>, "last_uid": <UID>}``. The first check
marks the mailbox's next UID and reports nothing. When the server says the
mailbox's UIDVALIDITY changed (it was rebuilt, UIDs start over), the watcher
marks again from there: mail from the gap isn't reported. A search filters
mail as it arrives: a message that only matches later (flagged an hour after)
is not reported.
"""

import email
import email.policy
import imaplib
import re

from .base import TEST_ITEMS, Adapter, AdapterError, with_defaults

# The header fields the event gets of a message (the run reads the rest).
HEADER_FIELDS = ("SUBJECT", "FROM", "TO", "CC", "DATE", "MESSAGE-ID")
_FETCH_ITEMS = f"(UID FLAGS INTERNALDATE RFC822.SIZE BODY.PEEK[HEADER.FIELDS ({' '.join(HEADER_FIELDS)})])"
_UID_RE = re.compile(rb"\bUID (\d+)")
_FLAGS_RE = re.compile(rb"\bFLAGS \(([^)]*)\)")
_DATE_RE = re.compile(rb'\bINTERNALDATE "([^"]+)"')
_SIZE_RE = re.compile(rb"\bRFC822\.SIZE (\d+)")


def _quoted(mailbox) -> str:
    return '"' + mailbox.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _code(server, name):
    """A response code the server sent with the last SELECT/EXAMINE (``UIDVALIDITY``), as an int."""
    _typ, data = server.response(name)
    for value in data or ():
        if value is None:
            continue
        text = value.decode("ascii", "replace") if isinstance(value, bytes) else str(value)
        if text.strip().isdigit():
            return int(text.strip())
    return None


def _records(data) -> list:
    """``(meta bytes, header bytes)`` per message of a UID FETCH reply."""
    records = []
    for part in data or ():
        if isinstance(part, tuple):
            records.append([part[0], part[1] or b""])
        elif isinstance(part, bytes) and records:
            records[-1][0] += b" " + part  # items the server put after the header literal
    return records


def _item(meta, header, mailbox):
    message = email.message_from_bytes(header, policy=email.policy.default)

    def field(name):
        value = message.get(name)
        return str(value).strip() if value is not None else ""

    uid = _UID_RE.search(meta)
    flags = _FLAGS_RE.search(meta)
    date = _DATE_RE.search(meta)
    size = _SIZE_RE.search(meta)
    return {
        "uid": int(uid.group(1)) if uid else None,
        "mailbox": mailbox,
        "subject": field("subject"),
        "from": field("from"),
        "to": field("to"),
        "cc": field("cc"),
        "date": field("date"),
        "message_id": field("message-id"),
        "flags": flags.group(1).decode("utf-8", "replace").split() if flags else [],
        "size": int(size.group(1)) if size else None,
        "internal_date": date.group(1).decode("ascii", "replace") if date else "",
    }


class ImapNewMessageAdapter(Adapter):
    key = "imap_new_message"
    title = "New email in an IMAP mailbox"
    description = (
        "Logs in to a mail server over IMAP and starts a run for each email that arrives in a mailbox "
        "(read-only: nothing is marked as read)."
    )
    requires_connection = True
    providers = ("imap",)
    config_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "mailbox": {
                "type": "string", "maxLength": 200, "title": "Mailbox", "default": "INBOX",
                "pattern": r"^[\x20-\x7e]+$",
                "description": "The folder to watch, as the server names it (\"INBOX\", \"Invoices\").",
            },
            "search": {
                "type": "string", "maxLength": 500, "title": "IMAP search", "default": "",
                "pattern": r"^[^\r\n]*$",
                "description": "Only mail matching these IMAP search keys when it arrives "
                               "(FROM \"billing@example.com\", UNSEEN, SUBJECT \"invoice\"). Empty: all mail.",
            },
        },
    }

    # -- the server ---------------------------------------------------------

    def _open(self, ctx):
        from core.services.connections import ConnectionLookupError, credentials_for, open_imap

        if ctx.connection is None:
            raise AdapterError("This watcher needs an IMAP connection.")
        try:
            material = credentials_for(ctx.connection)
        except ConnectionLookupError as e:
            raise AdapterError(e.message) from None
        if not material.get("imap"):
            raise AdapterError(f"Connection '{ctx.connection.name}' isn't an IMAP mailbox.")
        return open_imap(material["imap"], timeout=ctx.timeout)

    def _session(self, ctx, config, work):
        """``work(server, config, mailbox, count, uidvalidity, uidnext)`` on the mailbox, opened read-only."""
        config = with_defaults(self.config_schema, config)
        mailbox = config.get("mailbox") or "INBOX"
        # Checked again here, not only on save: a line break would end the
        # IMAP command and start another one.
        if any(ch in str(value) for value in (mailbox, config.get("search") or "") for ch in "\r\n\0"):
            raise AdapterError("The mailbox and the search can't hold line breaks.")
        try:
            server = self._open(ctx)
            try:
                typ, data = server.select(_quoted(mailbox), readonly=True)
                if typ != "OK":
                    reason = (data[0] or b"").decode("utf-8", "replace") if data else ""
                    raise AdapterError(f'The mailbox "{mailbox}" can\'t be opened: {reason or typ}.')
                count = int(data[0]) if data and data[0] and data[0].strip().isdigit() else 0
                return work(server, config, mailbox, count, _code(server, "UIDVALIDITY"), _code(server, "UIDNEXT"))
            finally:
                try:
                    server.logout()
                except Exception:  # the outcome is known; a failed LOGOUT only drops the link
                    pass
        except AdapterError:
            raise
        except imaplib.IMAP4.error as e:
            from core.services.connections import imap_reason

            raise AdapterError(f"The mail server refused: {imap_reason(e)}") from None
        except OSError as e:
            if e.__class__.__name__ == "UnsafeAddressError":
                raise AdapterError(str(e)) from None
            raise AdapterError(f"The mail server could not be reached ({e.__class__.__name__}).") from None

    @staticmethod
    def _search(server, config, first_uid):
        criteria = (config.get("search") or "").strip()
        args = ["UID", f"{max(first_uid, 1)}:*"] + ([criteria] if criteria else [])
        typ, data = server.uid("SEARCH", *args)
        if typ != "OK":
            raise AdapterError("The mail server refused the search (check the IMAP search keys).")
        return sorted({int(uid) for uid in (data[0] or b"").split() if uid.isdigit()})

    @staticmethod
    def _fetch(server, uids, mailbox) -> list:
        if not uids:
            return []
        typ, data = server.uid("FETCH", ",".join(str(uid) for uid in uids), _FETCH_ITEMS)
        if typ != "OK":
            raise AdapterError("The mail server refused to read the new messages' headers.")
        items = {}
        for meta, header in _records(data):
            item = _item(meta, header, mailbox)
            if item["uid"] is not None:
                items[item["uid"]] = item
        # In UID order; a message deleted in between is simply not there.
        return [items[uid] for uid in uids if uid in items]

    # -- the adapter --------------------------------------------------------

    def check(self, ctx, config, state):
        state = state if isinstance(state, dict) else None

        def work(server, config, mailbox, count, uidvalidity, uidnext):
            if state is None or state.get("uidvalidity") != uidvalidity:
                if uidnext is not None:
                    mark = uidnext - 1
                else:
                    every = self._search(server, {}, 1)
                    mark = every[-1] if every else 0
                return [], {"uidvalidity": uidvalidity, "last_uid": mark}
            last = int(state.get("last_uid") or 0)
            found = [uid for uid in self._search(server, config, last + 1) if uid > last]
            capped = ctx.limit is not None and len(found) > ctx.limit
            if capped:
                found = found[:ctx.limit]
            items = self._fetch(server, found, mailbox)
            events = [{"id": f"{uidvalidity}:{item['uid']}", "item": item} for item in items]
            if capped:
                new_last = found[-1]
            else:
                # Past what this check saw: mail that didn't match the search
                # when it arrived is not looked at again.
                new_last = max([last, *found, *([uidnext - 1] if uidnext is not None else [])])
            return events, {"uidvalidity": uidvalidity, "last_uid": new_last}

        return self._session(ctx, config, work)

    def test(self, ctx, config):
        def work(server, config, mailbox, count, uidvalidity, uidnext):
            latest = self._search(server, config, 1)[-TEST_ITEMS:]
            items = list(reversed(self._fetch(server, latest, mailbox)))
            noun = "message" if count == 1 else "messages"
            search = " (the newest matching the search below)" if (config.get("search") or "").strip() else ""
            return f'Connected; "{mailbox}" has {count} {noun}{search}.', items

        return self._session(ctx, config, work)
