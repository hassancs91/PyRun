"""
WhatsApp provider — the WhatsApp Business Cloud API (Meta), called directly.

Outbound: free-form text inside WhatsApp's 24-hour customer-service window,
and an approved template outside it (the channel's default template, with the
message text as its ``{{1}}``) or whenever a script asks for one; inside the
window also a file by URL (image, document, audio, video) and interactive
reply buttons or a list. Inbound: the webhook's GET verification challenge
(``hub.verify_token``), the POST signature (``X-Hub-Signature-256``,
HMAC-SHA256 of the body with the app secret), text, button and list replies,
locations, contacts, reactions, and media as metadata (``download_media``
fetches an attachment, used only when the channel's "Download media" setting
is on; see ``core.services.channels.media``). Delivery callbacks (sent,
delivered, read, failed)
update the delivery ledger by the message id (``wamid``). DM-only: group
messages are ignored.

The webhook URL is pasted into the Meta app's WhatsApp configuration by the
user (PyRun can't register it through the API), and one Meta app's webhook
carries every number subscribed to it: a message or status for another phone
number id is ignored here.
"""

import hashlib
import hmac
import logging
import re
from datetime import timedelta

import requests

from core.net_errors import describe_request_error
from core.netguard import is_safe_endpoint_url

from .base import (
    ChannelError,
    ChannelProvider,
    CredentialField,
    InboundMessage,
    OutboundMessage,
    register,
)

logger = logging.getLogger(__name__)

GRAPH_VERSION = "v23.0"
GRAPH_URL = f"https://graph.facebook.com/{GRAPH_VERSION}"
_TIMEOUT = 15  # seconds
_DOWNLOAD_TIMEOUT = (10, 30)  # connect, read (seconds between bytes)

# The customer-service window: free-form messages only within 24 hours of the
# person's last message. PyRun keeps a margin so a send right at the edge (or
# a retry) goes out as a template instead of being refused.
WINDOW = timedelta(hours=24)
WINDOW_MARGIN = timedelta(minutes=5)

TEXT_LIMIT = 4096           # characters in a text message body
TEMPLATE_PARAM_LIMIT = 1024  # characters in one template text parameter
DEFAULT_LANGUAGE = "en_US"

BAD_TOKEN_MESSAGE = (
    "WhatsApp rejected the access token (expired or revoked) — create a "
    "permanent System User token in Meta Business settings and paste it again."
)
OUT_OF_WINDOW_MESSAGE = (
    "Not sent: this number hasn't messaged the channel in the last 24 hours, so "
    "WhatsApp only accepts an approved template. Set a default template on the "
    "channel, or send a template."
)
OUT_OF_WINDOW_RICH_MESSAGE = (
    "Not sent: files and buttons can only go to a number that messaged the channel "
    "in the last 24 hours. Outside that window, send an approved template."
)

# Outbound files by URL: WhatsApp fetches the link itself.
MEDIA_KINDS = ("image", "document", "audio", "video")
URL_LIMIT = 2048
CAPTION_LIMIT = 1024
FILENAME_LIMIT = 240
# Interactive messages (developers.facebook.com/docs/whatsapp/cloud-api/messages/interactive-messages).
BODY_LIMIT = 1024
HEADER_LIMIT = 60
FOOTER_LIMIT = 60
MAX_BUTTONS = 3
BUTTON_TITLE_LIMIT = 20
BUTTON_ID_LIMIT = 256
MAX_LIST_ROWS = 10
MAX_SECTIONS = 10
LIST_BUTTON_LIMIT = 20
SECTION_TITLE_LIMIT = 24
ROW_ID_LIMIT = 200
ROW_TITLE_LIMIT = 24
ROW_DESCRIPTION_LIMIT = 72

# Graph API error codes (developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes).
# 0 and 190 (and HTTP 401) are a bad token; 10 and 200-299 a missing permission.
_TOKEN_CODES = {0, 190}
_PERMISSION_CODES = {10}
_RATE_CODES = {4, 80007, 130429, 131056}
_TEMPORARY_CODES = {1, 2, 131000, 131016}

_NUMBER_RE = re.compile(r"^[0-9]{5,20}$")
_ID_RE = re.compile(r"^[0-9]{1,32}$")
_TEMPLATE_NAME_RE = re.compile(r"^[a-z0-9_]{1,512}$")
_LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(_[A-Za-z0-9]{2,4})?$")

# Inbound message types parsed into an InboundMessage; any other type
# (system, order, unsupported, ...) is ignored.
MEDIA_TYPES = ("image", "document", "audio", "video", "sticker")
MESSAGE_TYPES = ("text", "interactive", "button", "location", "contacts", "reaction") + MEDIA_TYPES


def _str(value) -> str:
    return value if isinstance(value, str) else ""


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value) -> list:
    return value if isinstance(value, list) else []


def normalize_number(value) -> str:
    """A WhatsApp number as the API wants it: digits with the country code."""
    if isinstance(value, bool) or value is None:
        return ""
    text = str(value).strip()
    for ch in " +-().":
        text = text.replace(ch, "")
    return text


def clean_template(value) -> dict:
    """A template to send, checked: ``{"name", "language", "components"}``.

    ``language`` defaults to en_US and ``components`` to none. Raises
    ValueError with a sentence for the caller.
    """
    if not isinstance(value, dict):
        raise ValueError("'template' must be an object with name, language and components.")
    unknown = set(value) - {"name", "language", "components"}
    if unknown:
        raise ValueError(f"Unknown template keys: {', '.join(sorted(unknown))}.")
    name = value.get("name")
    if not isinstance(name, str) or not _TEMPLATE_NAME_RE.match(name):
        raise ValueError("The template name must be the approved template's name (lowercase letters, digits, _).")
    language = value.get("language") or DEFAULT_LANGUAGE
    if not isinstance(language, str) or not _LANGUAGE_RE.match(language):
        raise ValueError("The template language must be a language code such as en_US or es.")
    components = value.get("components") or []
    if not isinstance(components, list) or not all(isinstance(c, dict) for c in components):
        raise ValueError("The template components must be a list of objects.")
    return {"name": name, "language": language, "components": components}


def template_param(text: str) -> str:
    """Message text as one template text parameter.

    WhatsApp refuses a parameter with a newline, a tab or more than four
    spaces in a row, or an empty one: lines are joined with " | ", spaces
    collapsed, and the result cut to the parameter limit.
    """
    lines = [" ".join(line.split()) for line in (text or "").splitlines()]
    flat = " | ".join(line for line in lines if line) or "-"
    if len(flat) > TEMPLATE_PARAM_LIMIT:
        flat = flat[: TEMPLATE_PARAM_LIMIT - 1] + "…"
    return flat


def _text(value, what: str, limit: int, *, required: bool = False) -> str:
    """A string field of a media or interactive message, checked."""
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"The {what} must be a string.")
    value = value.strip()
    if required and not value:
        raise ValueError(f"The {what} is required.")
    if len(value) > limit:
        raise ValueError(f"The {what} is longer than {limit} characters.")
    return value


def _keys(value: dict, what: str, allowed: set) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"Unknown {what} keys: {', '.join(sorted(unknown))}.")


def clean_media(value) -> dict:
    """A file to send by URL, checked: ``{"kind", "url", "caption", "filename"}``.

    ``kind`` is image, document, audio or video; WhatsApp downloads ``url``
    itself (http or https). ``caption`` doesn't apply to audio, ``filename``
    only to a document. Raises ValueError with a sentence for the caller.
    """
    if not isinstance(value, dict):
        raise ValueError("'media' must be an object with kind and url.")
    _keys(value, "media", {"kind", "url", "caption", "filename"})
    kind = value.get("kind")
    if kind not in MEDIA_KINDS:
        raise ValueError(f"The media kind must be one of: {', '.join(MEDIA_KINDS)}.")
    url = _text(value.get("url"), "media url", URL_LIMIT, required=True)
    scheme, _, rest = url.partition("://")
    if scheme.lower() not in ("http", "https") or not rest.split("/", 1)[0] or any(c.isspace() for c in url):
        raise ValueError("The media url must be an http or https link WhatsApp can download.")
    cleaned = {"kind": kind, "url": url}
    caption = _text(value.get("caption"), "caption", CAPTION_LIMIT)
    if caption:
        if kind == "audio":
            raise ValueError("Audio can't have a caption.")
        cleaned["caption"] = caption
    filename = _text(value.get("filename"), "filename", FILENAME_LIMIT)
    if filename:
        if kind != "document":
            raise ValueError("Only a document has a filename.")
        cleaned["filename"] = filename
    return cleaned


def _choices(items, what: str, *, id_limit: int, title_limit: int, description_limit: int = 0) -> list:
    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
        raise ValueError(f"The {what}s must be a list of objects with id and title.")
    cleaned = []
    for item in items:
        _keys(item, what, {"id", "title", "description"} if description_limit else {"id", "title"})
        choice = {
            "id": _text(item.get("id"), f"{what} id", id_limit, required=True),
            "title": _text(item.get("title"), f"{what} title", title_limit, required=True),
        }
        if description_limit:
            description = _text(item.get("description"), f"{what} description", description_limit)
            if description:
                choice["description"] = description
        cleaned.append(choice)
    return cleaned


def clean_interactive(value) -> dict:
    """Reply buttons or a list, checked.

    ``{"type": "buttons", "body", "buttons": [{"id", "title"}], "header", "footer"}``
    (1-3 buttons) or ``{"type": "list", "body", "button", "sections": [{"title",
    "rows": [{"id", "title", "description"}]}], "header", "footer"}`` (up to 10
    rows in all). The person's tap comes back as an inbound ``interactive``
    message whose ``details.reply.id`` is the chosen id. Raises ValueError with
    a sentence for the caller.
    """
    if not isinstance(value, dict):
        raise ValueError("'interactive' must be an object with type, body and buttons or sections.")
    kind = value.get("type")
    if kind not in ("buttons", "list"):
        raise ValueError("The interactive type must be 'buttons' or 'list'.")
    common = {"type", "body", "header", "footer"}
    _keys(value, "interactive", common | ({"buttons"} if kind == "buttons" else {"button", "sections"}))
    cleaned = {"type": kind, "body": _text(value.get("body"), "body", BODY_LIMIT, required=True)}
    for name, limit in (("header", HEADER_LIMIT), ("footer", FOOTER_LIMIT)):
        text = _text(value.get(name), name, limit)
        if text:
            cleaned[name] = text
    if kind == "buttons":
        buttons = _choices(value.get("buttons"), "button", id_limit=BUTTON_ID_LIMIT, title_limit=BUTTON_TITLE_LIMIT)
        if not 1 <= len(buttons) <= MAX_BUTTONS:
            raise ValueError(f"Send 1 to {MAX_BUTTONS} buttons.")
        for field_name in ("id", "title"):
            if len({b[field_name] for b in buttons}) != len(buttons):
                raise ValueError(f"Every button needs its own {field_name}.")
        cleaned["buttons"] = buttons
        return cleaned
    cleaned["button"] = _text(value.get("button"), "list button", LIST_BUTTON_LIMIT, required=True)
    sections = value.get("sections")
    if not isinstance(sections, list) or not all(isinstance(sec, dict) for sec in sections):
        raise ValueError("The list sections must be a list of objects with title and rows.")
    if not 1 <= len(sections) <= MAX_SECTIONS:
        raise ValueError(f"Send 1 to {MAX_SECTIONS} sections.")
    cleaned["sections"] = []
    for section in sections:
        _keys(section, "section", {"title", "rows"})
        title = _text(section.get("title"), "section title", SECTION_TITLE_LIMIT, required=len(sections) > 1)
        rows = _choices(section.get("rows"), "row", id_limit=ROW_ID_LIMIT, title_limit=ROW_TITLE_LIMIT,
                        description_limit=ROW_DESCRIPTION_LIMIT)
        if not rows:
            raise ValueError("Every section needs at least one row.")
        cleaned["sections"].append({**({"title": title} if title else {}), "rows": rows})
    ids = [row["id"] for sec in cleaned["sections"] for row in sec["rows"]]
    if len(ids) > MAX_LIST_ROWS:
        raise ValueError(f"A list has at most {MAX_LIST_ROWS} rows in all.")
    if len(set(ids)) != len(ids):
        raise ValueError("Every row needs its own id.")
    return cleaned


def rich_text(content: dict) -> str:
    """The readable version of a cleaned media or interactive message (for the log)."""
    media = _dict(content.get("media"))
    if media:
        return media.get("caption") or f"{media['kind'].capitalize()}: {media.get('filename') or media['url']}"
    return _dict(content.get("interactive")).get("body", "")


def _media_body(media: dict) -> dict:
    part = {"link": media["url"]}
    for key in ("caption", "filename"):
        if media.get(key):
            part[key] = media[key]
    return {"type": media["kind"], media["kind"]: part}


def _interactive_body(spec: dict) -> dict:
    if spec["type"] == "buttons":
        interactive = {"type": "button", "body": {"text": spec["body"]}, "action": {"buttons": [
            {"type": "reply", "reply": {"id": b["id"], "title": b["title"]}} for b in spec["buttons"]
        ]}}
    else:
        interactive = {"type": "list", "body": {"text": spec["body"]},
                       "action": {"button": spec["button"], "sections": spec["sections"]}}
    if spec.get("header"):
        interactive["header"] = {"type": "text", "text": spec["header"]}
    if spec.get("footer"):
        interactive["footer"] = {"text": spec["footer"]}
    return {"type": "interactive", "interactive": interactive}


def _cut(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _template_body(template: dict) -> dict:
    body = {"name": template["name"], "language": {"code": template.get("language") or DEFAULT_LANGUAGE}}
    if template.get("components"):
        body["components"] = template["components"]
    return {"type": "template", "template": body}


def _changes(payload: dict):
    """Every ``value`` of a ``messages`` change in a webhook delivery."""
    for entry in _list(_dict(payload).get("entry")):
        for change in _list(_dict(entry).get("changes")):
            change = _dict(change)
            if change.get("field", "messages") != "messages":
                continue
            value = _dict(change.get("value"))
            if value:
                yield _dict(entry).get("id"), value


def _int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class WhatsAppProvider(ChannelProvider):
    provider_key = "whatsapp"
    supports_inbound = True
    supports_templates = True
    supports_rich_content = True
    supports_media_download = True
    registers_webhook = False

    credential_fields = (
        CredentialField(
            name="phone_number_id",
            label="Phone number ID",
            secret=False,
            help="WhatsApp Manager → API Setup: the Phone number ID (an id, not the phone number).",
            placeholder="e.g. 106540352242922",
            required_error="The phone number ID is required.",
        ),
        CredentialField(
            name="waba_id",
            label="WhatsApp Business Account ID",
            secret=False,
            help="The WhatsApp Business Account (WABA) the number belongs to, on the same API Setup page.",
            placeholder="e.g. 102290129340398",
            required_error="The WhatsApp Business Account ID is required.",
        ),
        CredentialField(
            name="access_token",
            label="Access token",
            secret=True,
            help="A permanent System User token with the whatsapp_business_messaging permission "
            "(Meta Business settings → System users). The API Setup page's temporary token expires in 24 hours.",
            placeholder="Paste the System User token",
            required_error="An access token is required.",
        ),
        CredentialField(
            name="app_secret",
            label="App secret",
            secret=True,
            required=False,
            help="Meta app → App settings → Basic. Needed for inbound: it checks every webhook call's signature.",
            placeholder="Needed for inbound messages",
        ),
        CredentialField(
            name="verify_token",
            label="Verify token",
            secret=True,
            required=False,
            help="Any string you choose; enter the same one as the Verify token in the Meta app's webhook settings.",
            placeholder="Needed for inbound messages",
        ),
        CredentialField(
            name="default_template",
            label="Default template",
            secret=False,
            required=False,
            help="An approved template sent instead of text to a number that hasn't messaged in 24 hours. "
            "It needs one body variable, {{1}}, which gets the message text. Empty: such sends fail.",
            placeholder="e.g. pyrun_update",
        ),
        CredentialField(
            name="default_template_language",
            label="Default template language",
            secret=False,
            required=False,
            help="The default template's language code (en_US when empty).",
            placeholder="en_US",
        ),
    )
    identity_field = "phone_number_id"
    duplicate_identity_error = (
        "This WhatsApp number is already connected as another channel (one number = one channel)."
    )
    default_target_label = "Default phone number"
    default_target_help = (
        "The WhatsApp number notifications go to: country code and number, digits only. "
        "Outside its 24-hour window the default template is sent."
    )
    default_target_placeholder = "e.g. 15551234567"
    setup_template = "cpanel/channels/setup/whatsapp.html"

    def identity_for_fingerprint(self, creds: dict) -> str:
        return (creds.get("phone_number_id") or "").strip()

    # --- internals -----------------------------------------------------------

    def _creds(self, channel) -> dict:
        creds = channel.get_credentials()
        if not (creds.get("access_token") or "").strip():
            raise ChannelError("This WhatsApp channel has no access token configured.")
        if not _ID_RE.match((creds.get("phone_number_id") or "").strip()):
            raise ChannelError("The phone number ID must be the numeric id from WhatsApp Manager → API Setup.")
        return creds

    def _request(self, creds: dict, method: str, path: str, payload: dict | None = None, params=None):
        """Call the Graph API; return the decoded JSON or raise a classified ChannelError."""
        token = creds["access_token"].strip()
        url = f"{GRAPH_URL}/{path}"
        headers = {"Authorization": f"Bearer {token}"}
        try:
            if method == "GET":
                resp = requests.get(url, params=params, headers=headers, timeout=_TIMEOUT)
            else:
                resp = requests.post(url, json=payload or {}, headers=headers, timeout=_TIMEOUT)
        except requests.RequestException as e:
            text = describe_request_error(e, "WhatsApp", _TIMEOUT).replace(token, "<access token>")
            raise ChannelError(text, retryable=True)
        try:
            data = resp.json()
        except ValueError:
            data = None
        if 200 <= resp.status_code < 300 and isinstance(data, dict) and "error" not in data:
            return data
        raise self._error(resp.status_code, data, token)

    @staticmethod
    def _error(status: int, data, token: str) -> ChannelError:
        error = _dict(_dict(data).get("error"))
        if not error:
            return ChannelError(
                f"Unexpected WhatsApp response (HTTP {status}).", retryable=status >= 500 or status == 429,
            )
        code = _int(error.get("code"))
        message = _str(error.get("message")) or f"HTTP {status}"
        details = _str(_dict(error.get("error_data")).get("details"))
        text = f"WhatsApp: {message}"
        if details and details not in message:
            text += f" {details}"
        if code is not None:
            text += f" (code {code})"
        text = text.replace(token, "<access token>")
        if status == 401 or code in _TOKEN_CODES:
            return ChannelError(BAD_TOKEN_MESSAGE, auth=True)
        if code in _PERMISSION_CODES or (code is not None and 200 <= code <= 299):
            return ChannelError(text, auth=True)
        if code in _RATE_CODES or status == 429:
            return ChannelError(text, retryable=True)
        return ChannelError(text, retryable=code in _TEMPORARY_CODES or status >= 500)

    # --- outbound ------------------------------------------------------------

    def test_connection(self, channel) -> tuple[bool, str]:
        """Read the number's display name, then check it belongs to the WABA.

        Listing the account's numbers needs ``whatsapp_business_management``;
        a token with messaging only still connects, with a note.
        """
        try:
            creds = self._creds(channel)
            number_id = creds["phone_number_id"].strip()
            info = self._request(creds, "GET", number_id, params={"fields": "display_phone_number,verified_name"})
        except ChannelError as e:
            return False, str(e)
        waba = (creds.get("waba_id") or "").strip()
        if not _ID_RE.match(waba):
            return False, "The WhatsApp Business Account ID must be the numeric id from WhatsApp Manager → API Setup."
        note = ""
        try:
            listing = self._request(creds, "GET", f"{waba}/phone_numbers", params={"fields": "id", "limit": 100})
        except ChannelError as e:
            note = f" The WhatsApp Business Account couldn't be checked: {e}"
        else:
            ids = {_str(_dict(n).get("id")) for n in _list(listing.get("data"))}
            if number_id not in ids:
                return False, (
                    f"Phone number ID {number_id} isn't in WhatsApp Business Account {waba}: "
                    "check both ids on API Setup."
                )
        number = _str(info.get("display_phone_number")) or number_id
        name = _str(info.get("verified_name"))
        return True, f"Connected as {number}" + (f" ({name})." if name else ".") + note

    def last_inbound_at(self, channel, number: str):
        """When ``number`` last messaged this channel (None if never or unknown).

        The ledger's inbound rows, and the approval inbox's ``last_seen_at``
        (a sender who isn't approved yet has no inbound row but is in the
        window all the same).
        """
        from core.models import ChannelMember, ChannelMessage

        seen = [
            channel.messages.filter(direction=ChannelMessage.Direction.IN, reply_ref_json__chat_id=number)
            .order_by("-created_at").values_list("created_at", flat=True).first(),
            ChannelMember.objects.filter(channel=channel, sender_id=number)
            .values_list("last_seen_at", flat=True).first(),
        ]
        seen = [t for t in seen if t is not None]
        return max(seen) if seen else None

    def window_open(self, channel, number: str, now=None) -> bool:
        """Whether a free-form message to ``number`` is inside the 24-hour window."""
        from django.utils import timezone

        last = self.last_inbound_at(channel, number)
        return last is not None and last > (now or timezone.now()) - WINDOW + WINDOW_MARGIN

    def send(self, channel, msg: OutboundMessage) -> dict:
        creds = self._creds(channel)
        target = (msg.reply_ref or {}).get("chat_id") or channel.default_target
        to = normalize_number(target)
        if not to:
            raise ChannelError(
                "No WhatsApp number to send to — set a default phone number for this channel "
                "or send as a reply."
            )
        if not _NUMBER_RE.match(to):
            raise ChannelError(f"{target!s} isn't a WhatsApp number (country code and number, digits only).")

        auto = None
        content = msg.content or {}
        template = content.get("template")
        if template:
            try:
                body = _template_body(clean_template(template))
            except ValueError as e:
                raise ChannelError(str(e))
        elif content.get("media") or content.get("interactive"):
            # Files and buttons are free-form messages: inside the window only.
            try:
                if content.get("media"):
                    body = _media_body(clean_media(content["media"]))
                else:
                    body = _interactive_body(clean_interactive(content["interactive"]))
            except ValueError as e:
                raise ChannelError(str(e))
            if not self.window_open(channel, to):
                raise ChannelError(OUT_OF_WINDOW_RICH_MESSAGE)
        elif self.window_open(channel, to):
            body = {"type": "text", "text": {"preview_url": False, "body": _cut(msg.text, TEXT_LIMIT)}}
        else:
            name = (creds.get("default_template") or "").strip()
            if not name:
                raise ChannelError(OUT_OF_WINDOW_MESSAGE)
            language = (creds.get("default_template_language") or "").strip() or DEFAULT_LANGUAGE
            try:
                chosen = clean_template({
                    "name": name, "language": language,
                    "components": [{"type": "body", "parameters": [{"type": "text", "text": template_param(msg.text)}]}],
                })
            except ValueError as e:
                raise ChannelError(f"The channel's default template is invalid: {e}")
            body = _template_body(chosen)
            auto = {"name": chosen["name"], "language": chosen["language"]}

        data = self._request(
            creds, "POST", f"{creds['phone_number_id'].strip()}/messages",
            {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, **body},
        )
        sent = _list(data.get("messages"))
        message_id = _str(_dict(sent[0]).get("id")) if sent else ""
        result = {"message_id": message_id or None, "chat_id": to}
        if auto:
            result["auto_template"] = auto
        return result

    def download_media(self, channel, item: dict, max_bytes: int) -> tuple[bytes, str]:
        """Fetch an inbound attachment: its link from ``GET /{media-id}``, then
        the file with the same token.

        WhatsApp's link expires about five minutes after it is issued, so this
        runs while the message is dispatched. A file over ``max_bytes`` is
        refused before (when WhatsApp gives its size) or while downloading. The
        link must pass the SSRF guard and a redirect isn't followed, so the
        token goes only where WhatsApp said.
        """
        creds = self._creds(channel)
        media_id = _str(item.get("id"))
        if not _ID_RE.match(media_id):
            raise ChannelError("The attachment has no WhatsApp media id.")
        info = self._request(creds, "GET", media_id)
        cap = f"{max_bytes / (1024 * 1024):g} MB"
        size = _int(info.get("file_size"))
        if size is not None and size > max_bytes:
            raise ChannelError(f"Not downloaded: the file is {size / (1024 * 1024):.1f} MB, over the {cap} cap.")
        url = _str(info.get("url"))
        if not url.lower().startswith("https://"):
            raise ChannelError("WhatsApp gave no download link for the attachment.")
        safe, why = is_safe_endpoint_url(url, operator_hint=False)
        if not safe:
            raise ChannelError(f"Not downloaded: the download link was refused ({why}).")
        token = creds["access_token"].strip()
        try:
            resp = requests.get(
                url, headers={"Authorization": f"Bearer {token}"}, timeout=_DOWNLOAD_TIMEOUT,
                stream=True, allow_redirects=False,
            )
        except requests.RequestException as e:
            text = describe_request_error(e, "WhatsApp", _DOWNLOAD_TIMEOUT[1]).replace(token, "<access token>")
            raise ChannelError(text, retryable=True)
        data = bytearray()
        try:
            if resp.status_code != 200:
                raise ChannelError(
                    f"Not downloaded: WhatsApp answered HTTP {resp.status_code} for the file "
                    "(its link expires about five minutes after the message arrives).",
                    retryable=resp.status_code >= 500,
                )
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise ChannelError(f"Not downloaded: the file is over the {cap} cap.")
        except requests.RequestException as e:
            text = describe_request_error(e, "WhatsApp", _DOWNLOAD_TIMEOUT[1]).replace(token, "<access token>")
            raise ChannelError(text, retryable=True)
        finally:
            resp.close()
        mime = _str(info.get("mime_type")) or _str(item.get("mime")) or "application/octet-stream"
        return bytes(data), mime

    # --- inbound -------------------------------------------------------------

    def handle_challenge(self, channel, request):
        """Meta's webhook verification: echo ``hub.challenge`` for the right verify token."""
        if request.method != "GET" or "hub.mode" not in request.GET:
            return None
        from django.http import HttpResponse, HttpResponseForbidden

        expected = (channel.get_credentials().get("verify_token") or "").strip()
        provided = request.GET.get("hub.verify_token", "")
        challenge = request.GET.get("hub.challenge", "")
        if (
            request.GET.get("hub.mode") != "subscribe"
            or not expected
            or not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))
            or not challenge
            or len(challenge) > 200
        ):
            logger.warning("WhatsApp webhook verification refused for channel %s", channel.id)
            return HttpResponseForbidden("verification failed", content_type="text/plain")
        return HttpResponse(challenge, content_type="text/plain")

    def verify_inbound(self, channel, request) -> bool:
        """``X-Hub-Signature-256: sha256=<hex>``, the HMAC-SHA256 of the raw body with the app secret."""
        secret = (channel.get_credentials().get("app_secret") or "").strip()
        if not secret:
            return False
        header = request.headers.get("X-Hub-Signature-256", "")
        if not header.startswith("sha256="):
            return False
        expected = hmac.new(secret.encode("utf-8"), request.body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(header[len("sha256="):].strip().lower(), expected)

    def _own(self, channel, value: dict) -> bool:
        """The change is about this channel's phone number."""
        number_id = _str(_dict(value.get("metadata")).get("phone_number_id"))
        own = (channel.get_credentials().get("phone_number_id") or "").strip()
        return bool(own) and number_id == own

    def split_inbound(self, payload: dict) -> list:
        """One payload per message, in Meta's own shape (statuses left out)."""
        pieces = []
        for entry_id, value in _changes(payload):
            contacts = _list(value.get("contacts"))
            for message in _list(value.get("messages")):
                if not isinstance(message, dict):
                    continue
                sender = message.get("from")
                own_contacts = [c for c in contacts if _dict(c).get("wa_id") == sender] or contacts[:1]
                piece_value = {k: v for k, v in value.items() if k not in ("messages", "statuses", "contacts")}
                piece_value["contacts"] = own_contacts
                piece_value["messages"] = [message]
                pieces.append({
                    "object": payload.get("object", "whatsapp_business_account"),
                    "entry": [{"id": entry_id, "changes": [{"field": "messages", "value": piece_value}]}],
                })
        return pieces

    def apply_status_updates(self, channel, payload: dict) -> int:
        from . import ledger

        applied = 0
        for _entry_id, value in _changes(payload):
            if not self._own(channel, value):
                continue
            for status in _list(value.get("statuses")):
                status = _dict(status)
                state = _str(status.get("status"))
                error = ""
                if state == "failed":
                    errors = [_dict(e) for e in _list(status.get("errors"))]
                    if errors:
                        first = errors[0]
                        title = _str(first.get("title")) or _str(first.get("message")) or "Not delivered"
                        details = _str(_dict(first.get("error_data")).get("details"))
                        code = _int(first.get("code"))
                        error = f"WhatsApp: {title}" + (f" {details}" if details and details != title else "")
                        error += f" (code {code})" if code is not None else ""
                    else:
                        error = "WhatsApp couldn't deliver it."
                if ledger.apply_delivery_status(channel, _str(status.get("id")), state, error=error):
                    applied += 1
        return applied

    def dedup_id(self, payload: dict):
        for _entry_id, value in _changes(payload):
            for message in _list(value.get("messages")):
                message_id = _str(_dict(message).get("id"))
                if message_id:
                    return message_id
        return None

    def parse_inbound(self, channel, payload: dict):
        for _entry_id, value in _changes(payload):
            if not self._own(channel, value):
                continue
            messages = _list(value.get("messages"))
            if messages and isinstance(messages[0], dict):
                return self._message(channel, value, messages[0], payload)
        return None

    def _message(self, channel, value: dict, message: dict, payload: dict):
        kind = _str(message.get("type"))
        sender = normalize_number(_str(message.get("from")))
        # DM-only: a group message (or one without a sender) is ignored.
        if kind not in MESSAGE_TYPES or not _NUMBER_RE.match(sender) or message.get("group_id"):
            return None
        contacts = [_dict(c) for c in _list(value.get("contacts"))]
        contact = next((c for c in contacts if c.get("wa_id") == sender), {})
        name = _str(_dict(contact.get("profile")).get("name"))
        text, details, media = self._content(kind, message)
        if text is None:
            return None
        context = _dict(message.get("context"))
        if _str(context.get("id")):
            details["context"] = {"message_id": _str(context.get("id")), "from": _str(context.get("from"))}
        return InboundMessage(
            channel_id=str(channel.id),
            provider=self.provider_key,
            text=text,
            sender={"id": sender, "username": "", "display_name": name or f"+{sender}"},
            reply_ref={"chat_id": sender},
            raw=payload,
            message_type=kind,
            details=details,
            media=media,
            message_id=_str(message.get("id")),
        )

    @staticmethod
    def _content(kind: str, message: dict):
        """(text, details, media) for one message; text None ⇒ ignore it."""
        part = _dict(message.get(kind))
        if kind == "text":
            return _str(part.get("body")), {}, []
        if kind == "interactive":
            reply_type = _str(part.get("type"))
            reply = _dict(part.get(reply_type)) if reply_type in ("button_reply", "list_reply") else {}
            if not reply:
                return None, {}, []
            details = {"reply": {
                "type": reply_type, "id": _str(reply.get("id")), "title": _str(reply.get("title")),
                "description": _str(reply.get("description")),
            }}
            return _str(reply.get("title")), details, []
        if kind == "button":
            # A quick-reply button on a template message.
            details = {"reply": {
                "type": "button", "id": _str(part.get("payload")), "title": _str(part.get("text")),
                "description": "",
            }}
            return _str(part.get("text")), details, []
        if kind == "location":
            lat, lng = part.get("latitude"), part.get("longitude")
            location = {
                "latitude": lat if isinstance(lat, (int, float)) and not isinstance(lat, bool) else None,
                "longitude": lng if isinstance(lng, (int, float)) and not isinstance(lng, bool) else None,
                "name": _str(part.get("name")), "address": _str(part.get("address")),
                "url": _str(part.get("url")),
            }
            where = ", ".join(p for p in (location["name"], location["address"]) if p)
            point = (
                f"{location['latitude']}, {location['longitude']}"
                if location["latitude"] is not None and location["longitude"] is not None else ""
            )
            text = "Location: " + (f"{where} ({point})" if where and point else where or point or "(no position)")
            return text, {"location": location}, []
        if kind == "contacts":
            contacts = []
            for c in _list(message.get("contacts")):
                c = _dict(c)
                contacts.append({
                    "name": _str(_dict(c.get("name")).get("formatted_name")),
                    "phones": [_str(_dict(p).get("phone")) for p in _list(c.get("phones")) if _str(_dict(p).get("phone"))],
                    "emails": [_str(_dict(e).get("email")) for e in _list(c.get("emails")) if _str(_dict(e).get("email"))],
                })
            summary = "; ".join(
                " ".join(p for p in (c["name"], ", ".join(c["phones"])) if p) for c in contacts
            )
            return f"Contact: {summary}" if summary else "Contact", {"contacts": contacts}, []
        if kind == "reaction":
            emoji = _str(part.get("emoji"))
            if not emoji:
                return None, {}, []  # a reaction taken back: nothing to act on
            return emoji, {"reaction": {"emoji": emoji, "message_id": _str(part.get("message_id"))}}, []
        # Media: metadata only here; ``core.services.channels.media`` downloads
        # it when the channel's "Download media" setting is on.
        item = {"kind": kind, "id": _str(part.get("id")), "mime": _str(part.get("mime_type")),
                "caption": _str(part.get("caption"))}
        if kind == "document":
            item["filename"] = _str(part.get("filename"))
        if _str(part.get("sha256")):
            item["sha256"] = _str(part.get("sha256"))
        return item["caption"], {}, [item]

    def acknowledge_inbound(self, channel, msg: InboundMessage) -> None:
        """Mark the message as read (blue ticks), as a person answering would."""
        if not msg.message_id:
            return
        creds = self._creds(channel)
        self._request(
            creds, "POST", f"{creds['phone_number_id'].strip()}/messages",
            {"messaging_product": "whatsapp", "status": "read", "message_id": msg.message_id},
        )

    def set_inbound_webhook(self, channel, public_url: str):
        # The Cloud API's webhook is set in the Meta app, not by API call.
        return (
            "Inbound enabled. In your Meta app open WhatsApp → Configuration, paste the callback URL "
            "shown below with your verify token, and subscribe to the messages field."
        )

    def clear_inbound_webhook(self, channel) -> None:
        # Nothing to undo at Meta: the webhook URL now answers 404.
        return None


register(WhatsAppProvider())
