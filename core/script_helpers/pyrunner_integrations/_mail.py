"""
Email messages for the ``gmail`` and ``smtp`` modules: one builder, so both send the same MIME.

Standard library only (``email``).
"""

import email.policy
import hashlib
import mimetypes
import os
from email.headerregistry import Address
from email.message import EmailMessage

# Bodies of non-ASCII text go quoted-printable or base64, never 8bit, so any
# server takes them.
POLICY = email.policy.default.clone(cte_type="7bit")
# Python's own extension table, not the system's (Windows' registry calls .csv application/vnd.ms-excel).
_TYPES = mimetypes.MimeTypes()
# Headers the arguments set: headers= can't replace them.
_RESERVED = frozenset({
    "from", "to", "cc", "bcc", "reply-to", "subject", "in-reply-to", "references",
    "content-type", "content-transfer-encoding", "mime-version", "content-disposition",
})


def _line(value, name) -> str:
    value = "" if value is None else str(value)
    if "\r" in value or "\n" in value:
        raise ValueError(f"{name} can't hold a line break")
    return value


def addresses(value, name, *, lenient=False) -> list:
    """``Address`` objects from one address ("Ada <ada@example.com>"), a comma-separated string or a list.

    ``lenient`` is for headers of a message received: what isn't an address
    (``undisclosed-recipients:;``, a broken entry) is left out instead of
    raising ValueError.
    """
    if value is None:
        return []
    if isinstance(value, (bytes, bytearray, dict)):
        raise TypeError(f"{name} is an email address or a list of them, not {type(value).__name__}")
    items = [value] if isinstance(value, (str, Address)) else list(value)
    out = []
    for item in items:
        if isinstance(item, Address):
            out.append(item)
            continue
        if not isinstance(item, str):
            raise TypeError(f"{name} holds email addresses as text, not {type(item).__name__}")
        if lenient:
            item = " ".join(item.splitlines())  # a folded header
        _line(item, name)
        parsed = EmailMessage(policy=POLICY)
        try:
            parsed["To"] = item
            found = list(parsed["To"].addresses)
        except (ValueError, IndexError):
            found = []
        valid = [a for a in found if "@" in a.addr_spec and a.username and a.domain]
        if lenient:
            out += valid
            continue
        if not found or len(valid) != len(found):
            raise ValueError(f"{name}: {item!r} is not an email address")
        out += found
    return out


def bare(found) -> list:
    """The plain addresses (``ada@example.com``) of ``Address`` objects, once each."""
    return list(dict.fromkeys(a.addr_spec for a in found))


def _attachment(item):
    """``(filename, bytes, maintype, subtype)`` from a path, a (filename, content[, type]) tuple or a dict."""
    mime_type = None
    if isinstance(item, (str, os.PathLike)):
        path = os.fspath(item)
        with open(path, "rb") as f:
            content = f.read()
        filename = os.path.basename(path)
    elif isinstance(item, dict):
        filename, content, mime_type = item.get("filename"), item.get("content"), item.get("mime_type")
    elif isinstance(item, (tuple, list)) and len(item) in (2, 3):
        filename, content, *rest = item
        mime_type = rest[0] if rest else None
    else:
        raise TypeError("an attachment is a file path, (filename, content[, mime type]) or "
                        "{\"filename\", \"content\", \"mime_type\"}")
    if not filename:
        raise ValueError("an attachment needs a filename")
    filename = _line(filename, "an attachment's filename")
    if isinstance(content, str):
        content = content.encode("utf-8")
    if not isinstance(content, (bytes, bytearray)):
        raise TypeError(f"attachment {filename!r}: its content is bytes or text, not {type(content).__name__}")
    mime_type = mime_type or _TYPES.guess_type(filename)[0] or "application/octet-stream"
    maintype, slash, subtype = str(mime_type).partition("/")
    if not slash or not maintype or not subtype:
        raise ValueError(f"attachment {filename!r}: {mime_type!r} is not a MIME type")
    return filename, bytes(content), maintype, subtype


def _attachments(value) -> list:
    if value is None:
        return []
    # One attachment given alone: a path, a dict, or a (filename, bytes[, type]) tuple.
    if isinstance(value, (str, os.PathLike, dict)) or (
            isinstance(value, tuple) and len(value) in (2, 3) and isinstance(value[1], (bytes, bytearray))):
        value = [value]
    return [_attachment(item) for item in value]


def build(*, to=None, subject="", text=None, html=None, cc=None, bcc=None, reply_to=None, from_address=None,
          attachments=None, headers=None, in_reply_to=None, references=None) -> EmailMessage:
    """An ``EmailMessage`` from the arguments ``send()`` takes (ValueError/TypeError for a bad one).

    Its multipart boundaries are made from the content, so the same arguments
    give the same bytes (a Gmail cassette replays them). No Date or Message-ID:
    the sender adds them (Gmail does; ``smtp.send`` sets both).
    """
    to, cc, bcc = addresses(to, "to"), addresses(cc, "cc"), addresses(bcc, "bcc")
    if not (to or cc or bcc):
        raise ValueError("a message needs a recipient: to=, cc= or bcc=")
    if text is not None and not isinstance(text, str):
        raise TypeError(f"text is a string, not {type(text).__name__}")
    if html is not None and not isinstance(html, str):
        raise TypeError(f"html is a string, not {type(html).__name__}")
    files = _attachments(attachments)
    msg = EmailMessage(policy=POLICY)
    if from_address:
        sender = addresses(from_address, "from_address")
        if len(sender) != 1:
            raise ValueError("from_address is one email address")
        msg["From"] = sender[0]
    for name, found in (("To", to), ("Cc", cc), ("Bcc", bcc)):
        if found:
            msg[name] = found
    if reply_to:
        msg["Reply-To"] = addresses(reply_to, "reply_to")
    msg["Subject"] = _line(subject, "subject")
    if in_reply_to:
        msg["In-Reply-To"] = _line(in_reply_to, "in_reply_to")
    if references:
        msg["References"] = _line(references, "references")
    for name, value in (headers or {}).items():
        name = _line(name, "a header name").strip()
        if not name or ":" in name or name.lower() in _RESERVED:
            raise ValueError(f"headers can't set {name!r}" + (": use the argument for it" if name else ""))
        msg[name] = _line(value, f"header {name}")
    if html is not None and text is not None:
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")
    elif html is not None:
        msg.set_content(html, subtype="html")
    else:
        msg.set_content(text or "")
    for filename, content, maintype, subtype in files:
        params = {}
        if maintype == "text":
            try:
                content.decode("utf-8")
                params["charset"] = "utf-8"  # else mail apps show a text file's accents as ASCII
            except UnicodeDecodeError:
                pass  # not UTF-8: no charset is claimed, the bytes go as they are
        msg.add_attachment(content, maintype=maintype, subtype=subtype, filename=filename, params=params)
    # Boundaries from a hash of the content (never random), unlike the email package's default.
    digest = hashlib.sha256(repr((text, html, [(f[0], f[2], f[3], hashlib.sha256(f[1]).hexdigest())
                                               for f in files])).encode("utf-8")).hexdigest()[:24]
    bodies = (text or "") + (html or "")
    for number, part in enumerate(p for p in msg.walk() if p.is_multipart()):
        boundary = f"pyrunner-{digest}-{number}"
        while boundary in bodies:
            boundary += "x"
        part.set_boundary(boundary)
    return msg


def as_bytes(msg) -> bytes:
    """The message as it travels (CRLF line ends)."""
    return msg.as_bytes(policy=msg.policy.clone(linesep="\r\n"))
