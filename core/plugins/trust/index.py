"""
The plugin store's signed ``index.json`` (2.5 T44).

The store is a static file: ``index.json`` plus the plugin zips it points at.
The index is a JSON object::

    {
      "schema": 1,
      "generated_at": "2026-11-12T10:00:00Z",
      "plugins": [ {<entry>}, ... ],
      "signature": {"publisher": "pyrun", "key_id": "pyrun-2026", "value": "<base64>"}
    }

``value`` is an Ed25519 signature (core/plugins/trust/signing.py) over
:func:`index_message`: a context line, the publisher and key id as canonical
JSON, then everything but ``signature`` as canonical JSON (sorted keys, no
spaces, ASCII). Whitespace and key order in the file don't matter; any change
to a value does.

Each entry carries ``slug``, ``publisher``, ``name``, ``version``,
``download_url`` and ``sha256`` (required), plus ``summary``, ``description``,
``categories``, ``icon_url``, ``min_pyrunner``, ``api``, ``size``, ``signed``,
``changelog_url``, ``homepage`` and ``provisions`` (the plugin.json block), and
the reserved ``tier`` and ``requires_license`` (read, not used yet). A relative
``download_url`` / ``icon_url`` / ``changelog_url`` is resolved against the
index's own URL, so a mirror can serve the same files.

:func:`parse_index` refuses the whole index when the signature or any entry is
wrong; the store never shows a half-checked list.
"""

import base64
import json
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from core.plugins import is_valid_plugin_slug
from core.plugins.trust.signing import PUBLISHER_RE, TrustError, verify_signature

INDEX_SCHEMA = 1
INDEX_CONTEXT = b"pyrun-plugin-index-v1"
#: More entries than this is not a plugin index.
MAX_ENTRIES = 2000
#: Plugin zips are never bigger (the install's own unpacked cap is 50 MB too).
MAX_ZIP_BYTES = 50 * 1024 * 1024

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SHORT_TEXT = 200
_LONG_TEXT = 4000


class StoreIndexError(Exception):
    """The index is not a valid, correctly signed PyRun plugin index."""


@dataclass(frozen=True)
class IndexEntry:
    slug: str
    publisher: str
    name: str
    version: str
    download_url: str
    sha256: str
    summary: str = ""
    description: str = ""
    categories: tuple = ()
    icon_url: str = ""
    min_pyrunner: str = ""
    api: str = ""
    size: int = 0
    signed: bool = False
    changelog_url: str = ""
    homepage: str = ""
    provisions: dict = field(default_factory=dict, hash=False, compare=False)
    tier: str = ""
    requires_license: bool = False


@dataclass(frozen=True)
class StoreIndex:
    generated_at: str
    publisher: str
    key_id: str
    entries: tuple

    def get(self, slug):
        """The entry for ``slug``, or None."""
        return next((e for e in self.entries if e.slug == slug), None)

    @property
    def categories(self) -> list:
        return sorted({c for e in self.entries for c in e.categories}, key=str.lower)


def canonical(value) -> bytes:
    """The one byte form of a JSON value that a signature covers."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def index_message(body: dict, *, publisher, key_id) -> bytes:
    """The exact bytes an index signature covers (``body`` without ``signature``)."""
    body = {k: v for k, v in body.items() if k != "signature"}
    header = canonical({"key_id": key_id, "publisher": publisher})
    return INDEX_CONTEXT + b"\n" + header + b"\n" + canonical(body)


def sign_index(body: dict, private_key, *, publisher, key_id) -> dict:
    """``body`` plus its ``signature`` block (for ``manage.py plugin_store_index``)."""
    if not PUBLISHER_RE.match(publisher or ""):
        raise StoreIndexError(f"publisher {publisher!r} must be lowercase letters/digits/_/-.")
    signed = {k: v for k, v in body.items() if k != "signature"}
    value = private_key.sign(index_message(signed, publisher=publisher, key_id=key_id))
    signed["signature"] = {
        "publisher": publisher,
        "key_id": key_id,
        "value": base64.b64encode(value).decode("ascii"),
    }
    return signed


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #

def _text(entry, name, where, *, required=False, limit=_SHORT_TEXT) -> str:
    value = entry.get(name, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise StoreIndexError(f"{where}: {name!r} must be text.")
    value = value.strip()
    if required and not value:
        raise StoreIndexError(f"{where}: {name!r} is missing.")
    if len(value) > limit:
        raise StoreIndexError(f"{where}: {name!r} is longer than {limit} characters.")
    return value


def _url(entry, name, where, base_url, *, required=False, https_only=False) -> str:
    value = _text(entry, name, where, required=required, limit=2000)
    if not value:
        return ""
    resolved = urljoin(base_url, value) if base_url else value
    scheme = urlsplit(resolved).scheme.lower()
    allowed = ("https",) if https_only else ("http", "https")
    if scheme not in allowed or not urlsplit(resolved).netloc:
        need = "an https:// URL" if https_only else "an http(s) URL"
        raise StoreIndexError(f"{where}: {name!r} must be {need}.")
    return resolved


def entry_from_dict(raw, number, base_url="") -> IndexEntry:
    """One validated entry (``number`` is its position, for messages); StoreIndexError."""
    from core.services.plugin_doctor import semver_key

    if not isinstance(raw, dict):
        raise StoreIndexError(f"plugins[{number}] must be an object.")
    slug = raw.get("slug")
    if not is_valid_plugin_slug(slug) or len(slug) > 100:
        raise StoreIndexError(f"plugins[{number}]: slug {slug!r} is not a valid plugin slug.")
    where = f"plugin {slug!r}"
    publisher = _text(raw, "publisher", where, required=True, limit=100)
    if not PUBLISHER_RE.match(publisher):
        raise StoreIndexError(f"{where}: publisher {publisher!r} must be lowercase letters/digits/_/-.")
    version = _text(raw, "version", where, required=True, limit=50)
    if semver_key(version) is None:
        raise StoreIndexError(f"{where}: version {version!r} is not semver (MAJOR.MINOR.PATCH).")
    sha256 = _text(raw, "sha256", where, required=True, limit=64).lower()
    if not SHA256_RE.match(sha256):
        raise StoreIndexError(f"{where}: sha256 must be 64 hex characters.")
    size = raw.get("size", 0)
    if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= MAX_ZIP_BYTES:
        raise StoreIndexError(f"{where}: size must be a whole number of bytes up to {MAX_ZIP_BYTES}.")
    categories = raw.get("categories") or []
    if not isinstance(categories, list) or not all(isinstance(c, str) for c in categories):
        raise StoreIndexError(f"{where}: categories must be a list of text.")
    provisions = raw.get("provisions") or {}
    if not isinstance(provisions, dict):
        raise StoreIndexError(f"{where}: provisions must be an object.")
    for flag in ("signed", "requires_license"):
        if not isinstance(raw.get(flag, False), bool):
            raise StoreIndexError(f"{where}: {flag!r} must be true or false.")
    return IndexEntry(
        slug=slug,
        publisher=publisher,
        name=_text(raw, "name", where, required=True),
        version=version,
        download_url=_url(raw, "download_url", where, base_url, required=True),
        sha256=sha256,
        summary=_text(raw, "summary", where, limit=_LONG_TEXT),
        description=_text(raw, "description", where, limit=_LONG_TEXT),
        categories=tuple(c.strip()[:50] for c in categories if c.strip())[:10],
        icon_url=_url(raw, "icon_url", where, base_url, https_only=True),
        min_pyrunner=_text(raw, "min_pyrunner", where, limit=50),
        api=_text(raw, "api", where, limit=20),
        size=size,
        signed=raw.get("signed", False),
        changelog_url=_url(raw, "changelog_url", where, base_url),
        homepage=_url(raw, "homepage", where, base_url),
        provisions=provisions,
        tier=_text(raw, "tier", where, limit=50),
        requires_license=raw.get("requires_license", False),
    )


def parse_index(raw: bytes, *, base_url="") -> StoreIndex:
    """A verified StoreIndex from ``index.json`` bytes, or StoreIndexError."""
    try:
        doc = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise StoreIndexError(f"The index is not valid JSON ({exc}).")
    if not isinstance(doc, dict):
        raise StoreIndexError("The index must be a JSON object.")
    signature = doc.get("signature")
    if not isinstance(signature, dict) or not all(
        isinstance(signature.get(k), str) and signature.get(k) for k in ("publisher", "key_id", "value")
    ):
        raise StoreIndexError("The index is not signed (no signature with publisher, key_id and value).")
    publisher, key_id = signature["publisher"], signature["key_id"]
    try:
        verify_signature(index_message(doc, publisher=publisher, key_id=key_id),
                         signature["value"], key_id, publisher)
    except TrustError as exc:
        raise StoreIndexError(f"The index signature is not valid: {exc}")
    if doc.get("schema") != INDEX_SCHEMA:
        raise StoreIndexError(
            f"The index uses schema {doc.get('schema')!r}; this PyRun reads schema {INDEX_SCHEMA}."
        )
    plugins = doc.get("plugins")
    if not isinstance(plugins, list):
        raise StoreIndexError("The index has no 'plugins' list.")
    if len(plugins) > MAX_ENTRIES:
        raise StoreIndexError(f"The index lists more than {MAX_ENTRIES} plugins.")
    entries, seen = [], set()
    for number, item in enumerate(plugins):
        entry = entry_from_dict(item, number, base_url)
        if entry.slug in seen:
            raise StoreIndexError(f"The index lists the plugin {entry.slug!r} twice.")
        seen.add(entry.slug)
        entries.append(entry)
    generated_at = doc.get("generated_at") if isinstance(doc.get("generated_at"), str) else ""
    return StoreIndex(
        generated_at=generated_at[:40],
        publisher=publisher,
        key_id=key_id,
        entries=tuple(sorted(entries, key=lambda e: e.name.lower())),
    )
