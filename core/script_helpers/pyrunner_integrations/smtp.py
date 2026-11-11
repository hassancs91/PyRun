"""
Email through an SMTP server for scripts: send messages, with attachments.

The connection is an **SMTP** connection: the server's host and port, its
security (STARTTLS, SSL/TLS or none), the login, and the From address
messages use unless a script passes its own. Any SMTP server works: a mail
provider's (Gmail's ``smtp.gmail.com`` with an app password, Microsoft 365,
Fastmail) or a transactional service's (Postmark, SendGrid, Mailgun, Amazon
SES). The password goes only to that server, and is masked in the run's
output like every connection value.

A send is never repeated: if the server fails or the connection drops
midway, the message may or may not have gone, so ``send`` raises and leaves
the decision to the script.
"""

import email.utils
import smtplib
import socket
import ssl

from . import _client, _mail

PROVIDER = "smtp"

# The codes an IntegrationError from this module carries (besides the
# connection codes: CONNECTION_NOT_FOUND, CONNECTION_EXPIRED...). The docs
# generator prints this table instead of the HTTP one.
ERROR_CODES = {
    "UNAUTHORIZED": "The server refused the login (SMTP 535 and the like).",
    "SENDER_REFUSED": "The server refused the From address (often: the login may not send as it).",
    "RECIPIENTS_REFUSED": "The server refused every recipient. Some refused and others accepted is no error: "
                          "the result's `refused` lists them.",
    "REJECTED": "The server refused the message itself (its size or content).",
    "UNREACHABLE": "The server couldn't be reached, its TLS failed, or it closed the connection.",
    "TIMEOUT": "No reply in time.",
    "SMTP_ERROR": "Any other SMTP failure (a server without STARTTLS or login, an unexpected reply).",
    "NOT_SMTP": "The connection isn't an SMTP connection.",
}
# The paragraph above that table on the generated page.
ERRORS = (
    "Every function raises `pyrunner_integrations.IntegrationError` (a `pyrunner_connections.ConnectionsError`, "
    "so one `except` catches both) with `code`, `status` (the server's SMTP reply code, such as 535 or 550, or "
    "None) and `retry_after` (always None). Its message keeps the server's reply, never the password. Nothing "
    "is retried: a message must not go twice."
)


def _error(e, host) -> _client.IntegrationError:
    """The IntegrationError for an exception from smtplib or the socket under it."""
    status = getattr(e, "smtp_code", None)
    reply = getattr(e, "smtp_error", b"")
    reply = reply.decode("utf-8", "replace") if isinstance(reply, bytes) else str(reply or "")
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        refused = {addr: f"{code} {_text(message)}" for addr, (code, message) in e.recipients.items()}
        codes = {code for code, _message in e.recipients.values()}
        status = codes.pop() if len(codes) == 1 else None
        detail = "; ".join(f"{addr}: {why}" for addr, why in refused.items())
        return _client.IntegrationError(f"SMTP {host}: every recipient was refused ({detail})",
                                        "RECIPIENTS_REFUSED", status)
    if isinstance(e, smtplib.SMTPAuthenticationError):
        code, what = "UNAUTHORIZED", "the login was refused"
    elif isinstance(e, smtplib.SMTPSenderRefused):
        code, what = "SENDER_REFUSED", f"the sender {e.sender} was refused"
    elif isinstance(e, smtplib.SMTPDataError):
        code, what = "REJECTED", "the message was refused"
    elif isinstance(e, (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError)):
        code, what = "UNREACHABLE", "the server closed the connection"
    elif isinstance(e, smtplib.SMTPException):
        code, what = "SMTP_ERROR", str(e) if not reply else "the server refused a command"
    elif isinstance(e, (TimeoutError, socket.timeout)):
        code, what = "TIMEOUT", "no reply in time"
    elif isinstance(e, ssl.SSLError):
        code, what = "UNREACHABLE", f"TLS failed ({e.reason or type(e).__name__})"
    else:  # OSError: DNS, a refused connection, a network down
        code, what = "UNREACHABLE", f"could not connect ({type(e).__name__})"
    message = f"SMTP {host}: {what}"
    if status or reply:
        message += f": {status or ''} {reply}".rstrip()
    return _client.IntegrationError(message, code, status)


def _text(value) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def _settings(connection):
    """``(name, the connection's SMTP settings)``; IntegrationError when it can't be used."""
    name = connection or PROVIDER
    try:
        material = _client._connections.credentials(name)
    except _client._connections.ConnectionsError as e:
        raise _client.IntegrationError(str(e), e.code or "CONNECTION_ERROR", e.status) from None
    if not material.get("smtp"):
        raise _client.IntegrationError(f"Connection '{name}' is not an SMTP connection.", "NOT_SMTP")
    return name, material["smtp"]


def _open(name, settings, timeout):
    """``pyrunner_connections.smtp()``, its failures as IntegrationError (the password masked)."""
    host = f"{settings['host']}:{settings['port']}"
    try:
        return _client._connections.smtp(name, timeout=timeout)
    except _client._connections.ConnectionsError as e:
        raise _client.IntegrationError(str(e), e.code or "CONNECTION_ERROR", e.status) from None
    except (smtplib.SMTPException, OSError) as e:
        raise _scrubbed(_error(e, host), settings) from None


def _scrubbed(error, settings):
    error.args = (_client.mask_in(error.args[0], [settings.get("password")]),)
    return error


def client(connection=None, *, timeout=30):
    """An ``smtplib.SMTP`` connected and logged in as the connection, for what ``send`` doesn't do.

    Use it in a ``with`` block (it says goodbye to the server at the end).
    ``server.from_address`` is the connection's From address. Errors while
    connecting or logging in raise IntegrationError; after that, smtplib's own
    exceptions. For example:

        from email.message import EmailMessage

        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = "me@example.com", "you@example.com", "Hi"
        msg.set_content("Sent my own way.")
        with smtp.client() as server:
            server.send_message(msg)
    """
    name, settings = _settings(connection)
    return _open(name, settings, timeout)


def send(to=None, subject="", text=None, *, html=None, cc=None, bcc=None, reply_to=None, from_address=None,
         attachments=None, headers=None, timeout=30, connection=None):
    """Send one email; returns ``{"message_id", "from", "accepted", "refused"}``.

    ``to``, ``cc`` and ``bcc`` take one address (``"ada@example.com"`` or
    ``"Ada Lovelace <ada@example.com>"``), several separated by commas, or a
    list; Bcc recipients get the message without being named in it.
    ``text`` is the plain-text body, ``html`` an HTML one (give both and mail
    apps show the HTML, or the text where they can't). The From address is
    ``from_address``, else the connection's. ``attachments`` is a list (or
    one) of file paths, ``(filename, content)`` or
    ``(filename, content, mime type)`` tuples (content as bytes, or text), or
    dicts with ``filename``, ``content`` and ``mime_type``; the type is
    guessed from the name when not given.
    ``headers`` adds headers of your own (``{"X-Campaign": "may"}``).

    The result: ``message_id`` (the Message-ID header it was sent with),
    ``from``, ``accepted`` (the addresses the server took) and ``refused``
    (``{address: "550 5.1.1 ..."}`` for the ones it didn't, when it took
    some). For example:

        result = smtp.send("ada@example.com", "Your report", "It's attached.",
                           attachments=["/tmp/report.pdf"])
        print(result["message_id"], result["refused"])
    """
    name, settings = _settings(connection)
    sender = from_address or settings.get("from_address") or ""
    if not sender and "@" in (settings.get("username") or ""):
        sender = settings["username"]
    if not sender:
        raise ValueError(f"Connection '{name}' has no From address: set one on it, or pass from_address=")
    msg = _mail.build(to=to, subject=subject, text=text, html=html, cc=cc, bcc=bcc, reply_to=reply_to,
                      from_address=sender, attachments=attachments, headers=headers)
    sender_address = _mail.addresses(sender, "from_address")[0].addr_spec
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain=sender_address.rpartition("@")[2])
    recipients = _mail.bare(_mail.addresses(to, "to") + _mail.addresses(cc, "cc") + _mail.addresses(bcc, "bcc"))
    host = f"{settings['host']}:{settings['port']}"
    server = _open(name, settings, timeout)
    try:
        # send_message leaves the Bcc header out of what it sends.
        refused = server.send_message(msg, from_addr=sender_address, to_addrs=recipients)
    except (smtplib.SMTPException, OSError) as e:
        server.close()
        raise _scrubbed(_error(e, host), settings) from None
    try:
        server.quit()
    except (smtplib.SMTPException, OSError):
        # The server took the message already: a goodbye it didn't answer changes nothing,
        # and an error here would invite the script to send it again.
        server.close()
    refused = {addr: f"{code} {_text(message)}" for addr, (code, message) in (refused or {}).items()}
    return {
        "message_id": msg["Message-ID"],
        "from": sender_address,
        "accepted": [addr for addr in recipients if addr not in refused],
        "refused": refused,
    }
