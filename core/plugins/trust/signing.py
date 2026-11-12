"""
Plugin file manifest and Ed25519 signature (2.5 T43).

A signed plugin zip carries two extra files in its top folder:

``MANIFEST.sha256``
    One line per file of the plugin, ``<sha256 hex>  <path>``, sorted by path,
    paths relative to the plugin folder with ``/``. The same format as
    ``sha256sum``, so ``sha256sum -c MANIFEST.sha256`` checks it too. The two
    trust files themselves are not listed.

``plugin.sig``
    JSON: ``format`` (1), ``slug``, ``publisher``, ``version``, ``key_id`` and
    ``signature``: base64 Ed25519 over :func:`signed_message` (a context line,
    the four fields as canonical JSON, then the manifest bytes).

:func:`verify_folder` turns a folder into a :class:`TrustResult` (verified with
a publisher and key id, or unsigned) or raises :class:`TrustError` when a trust
file is present but wrong: a file changed, missing or not listed, a bad or
unknown-key signature, a signature for another slug or version. No signature
means unsigned, never an error.
"""

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from core.plugins.trust import keys

MANIFEST_NAME = "MANIFEST.sha256"
SIGNATURE_NAME = "plugin.sig"
TRUST_FILES = (MANIFEST_NAME, SIGNATURE_NAME)
SIGNATURE_FORMAT = 1
SIGNATURE_CONTEXT = b"pyrun-plugin-signature-v1"

VERIFIED = "verified"
UNSIGNED = "unsigned"

#: Files Python writes next to the code once a plugin is loaded. Skipped when a
#: folder on this disk is re-checked (doctor, rollback); a zip being installed
#: may not ship them unless they are listed.
_GENERATED_DIRS = ("__pycache__",)
_GENERATED_SUFFIXES = (".pyc", ".pyo")

_LINE_RE = re.compile(r"^([0-9a-f]{64})  (.+)$")
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
#: plugin.json ``publisher`` and a signature's publisher (the doctor uses it too).
PUBLISHER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class TrustError(Exception):
    """A trust file is present but does not check out (the install is refused)."""


@dataclass(frozen=True)
class TrustResult:
    trust: str = UNSIGNED
    publisher: str = ""
    key_id: str = ""

    @property
    def verified(self) -> bool:
        return self.trust == VERIFIED


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #

def _is_generated(rel: str) -> bool:
    parts = rel.split("/")
    return any(p in _GENERATED_DIRS for p in parts[:-1]) or rel.endswith(_GENERATED_SUFFIXES)


def plugin_files(folder, *, include_generated=True) -> list:
    """The plugin's files as sorted relative ``/`` paths, without the trust files."""
    folder = Path(folder)
    found = []
    for path in folder.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(folder).as_posix()
        if rel in TRUST_FILES:
            continue
        if not include_generated and _is_generated(rel):
            continue
        found.append(rel)
    return sorted(found)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(folder, files=None) -> bytes:
    """``MANIFEST.sha256`` for ``files`` of a folder (default: all but generated ones)."""
    folder = Path(folder)
    lines = []
    for rel in sorted(files if files is not None else plugin_files(folder, include_generated=False)):
        if "\n" in rel or "\r" in rel:
            raise TrustError(f"File name {rel!r} contains a line break; rename it before signing.")
        lines.append(f"{_sha256_file(folder / rel)}  {rel}\n")
    return "".join(lines).encode("utf-8")


def parse_manifest(data: bytes) -> dict:
    """``{path: sha256}`` from ``MANIFEST.sha256`` bytes; TrustError when malformed."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise TrustError(f"{MANIFEST_NAME} is not UTF-8 text.")
    entries = {}
    for number, line in enumerate(text.splitlines(), start=1):
        match = _LINE_RE.match(line)
        if not match:
            raise TrustError(f"{MANIFEST_NAME} line {number} is not '<sha256>  <path>'.")
        digest, rel = match.groups()
        parts = rel.split("/")
        if rel.startswith("/") or "\\" in rel or any(p in ("", ".", "..") for p in parts):
            raise TrustError(f"{MANIFEST_NAME} line {number} has an unsafe path {rel!r}.")
        if rel in TRUST_FILES:
            raise TrustError(f"{MANIFEST_NAME} must not list {rel}.")
        if rel in entries:
            raise TrustError(f"{MANIFEST_NAME} lists {rel} twice.")
        entries[rel] = digest
    if not entries:
        raise TrustError(f"{MANIFEST_NAME} lists no files.")
    return entries


def manifest_problems(folder, entries: dict, *, include_generated=True) -> list:
    """What does not match the manifest: changed, missing and unlisted files."""
    folder = Path(folder)
    problems = []
    present = set(plugin_files(folder, include_generated=include_generated))
    for rel in sorted(entries):
        if rel not in present:
            problems.append(f"{rel} is listed but missing")
        elif _sha256_file(folder / rel) != entries[rel]:
            problems.append(f"{rel} does not match its hash")
    for rel in sorted(present - set(entries)):
        problems.append(f"{rel} is not listed")
    return problems


# --------------------------------------------------------------------------- #
# Signature
# --------------------------------------------------------------------------- #

def signed_message(manifest: bytes, *, slug, publisher, version, key_id) -> bytes:
    """The exact bytes an Ed25519 plugin signature covers."""
    header = json.dumps(
        {"key_id": key_id, "publisher": publisher, "slug": slug, "version": version},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )
    return SIGNATURE_CONTEXT + b"\n" + header.encode("ascii") + b"\n" + manifest


def public_key_b64(private_key: Ed25519PrivateKey) -> str:
    raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def load_private_key(data: bytes) -> Ed25519PrivateKey:
    """An Ed25519 private key from PEM (PKCS#8, unencrypted) bytes."""
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError) as exc:
        raise TrustError(f"Not an unencrypted PEM private key ({exc}).")
    if not isinstance(key, Ed25519PrivateKey):
        raise TrustError("The key is not an Ed25519 key.")
    return key


def private_key_pem(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _check_fields(slug, publisher, version, key_id):
    if not PUBLISHER_RE.match(publisher or ""):
        raise TrustError(f"publisher {publisher!r} must be lowercase letters/digits/_/-.")
    if not _KEY_ID_RE.match(key_id or ""):
        raise TrustError(f"key id {key_id!r} must be 1-64 letters, digits, '.', '_' or '-'.")
    if not slug or not version:
        raise TrustError("A signature needs the plugin's slug and version.")


def sign_manifest(manifest: bytes, private_key: Ed25519PrivateKey, *, slug, publisher,
                  version, key_id) -> bytes:
    """``plugin.sig`` bytes for a manifest."""
    _check_fields(slug, publisher, version, key_id)
    message = signed_message(manifest, slug=slug, publisher=publisher, version=version, key_id=key_id)
    signature = base64.b64encode(private_key.sign(message)).decode("ascii")
    body = {
        "format": SIGNATURE_FORMAT,
        "slug": slug,
        "publisher": publisher,
        "version": version,
        "key_id": key_id,
        "signature": signature,
    }
    return (json.dumps(body, indent=2, sort_keys=True) + "\n").encode("utf-8")


def verify_signature(message: bytes, signature_b64: str, key_id: str, publisher: str):
    """Check ``signature_b64`` over ``message`` with the trusted key ``key_id``.

    Also for other signed PyRun documents (the store index, T44). Raises
    TrustError; returns the TrustedKey on success.
    """
    key = keys.trusted_key(key_id)
    if key is None:
        raise TrustError(f"it is signed with key id {key_id!r}, which this PyRun does not trust.")
    if key.revoked:
        raise TrustError(f"it is signed with key id {key_id!r}, which has been revoked.")
    if key.publisher != publisher:
        raise TrustError(
            f"key id {key_id!r} belongs to the publisher {key.publisher!r}, not {publisher!r}."
        )
    try:
        signature = base64.b64decode(signature_b64 or "", validate=True)
        public = Ed25519PublicKey.from_public_bytes(base64.b64decode(key.public_key, validate=True))
    except (binascii.Error, ValueError):
        raise TrustError("the signature or the trusted key is not valid base64 Ed25519.")
    try:
        public.verify(signature, message)
    except InvalidSignature:
        raise TrustError("the signature does not match its contents.")
    return key


def _read_signature(path: Path) -> dict:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise TrustError(f"{SIGNATURE_NAME} is not valid JSON ({exc}).")
    if not isinstance(body, dict):
        raise TrustError(f"{SIGNATURE_NAME} must be a JSON object.")
    if body.get("format") != SIGNATURE_FORMAT:
        raise TrustError(f"{SIGNATURE_NAME} format {body.get('format')!r} is not supported.")
    for name in ("slug", "publisher", "version", "key_id", "signature"):
        if not isinstance(body.get(name), str) or not body[name]:
            raise TrustError(f"{SIGNATURE_NAME} is missing {name!r}.")
    return body


def verify_folder(folder, *, slug, manifest: dict, include_generated=True) -> TrustResult:
    """Check a plugin folder's trust files; see the module docstring.

    ``manifest`` is the folder's parsed plugin.json. ``include_generated`` False
    skips ``__pycache__``/``.pyc`` files (a folder that has been loaded here).
    """
    folder = Path(folder)
    manifest_path = folder / MANIFEST_NAME
    sig_path = folder / SIGNATURE_NAME
    if not manifest_path.is_file() and not sig_path.is_file():
        return TrustResult()
    if not manifest_path.is_file():
        raise TrustError(f"it has {SIGNATURE_NAME} but no {MANIFEST_NAME}.")

    manifest_bytes = manifest_path.read_bytes()
    entries = parse_manifest(manifest_bytes)

    result = TrustResult()
    if sig_path.is_file():
        body = _read_signature(sig_path)
        if body["slug"] != slug:
            raise TrustError(f"its signature is for the plugin {body['slug']!r}, not {slug!r}.")
        version = str(manifest.get("version") or "")
        if body["version"] != version:
            raise TrustError(
                f"its signature is for version {body['version']!r}, but plugin.json says {version!r}."
            )
        declared = manifest.get("publisher")
        if declared and declared != body["publisher"]:
            raise TrustError(
                f"its signature is by {body['publisher']!r}, but plugin.json names the "
                f"publisher {declared!r}."
            )
        message = signed_message(
            manifest_bytes, slug=slug, publisher=body["publisher"],
            version=body["version"], key_id=body["key_id"],
        )
        verify_signature(message, body["signature"], body["key_id"], body["publisher"])
        result = TrustResult(trust=VERIFIED, publisher=body["publisher"], key_id=body["key_id"])

    problems = manifest_problems(folder, entries, include_generated=include_generated)
    if problems:
        shown = "; ".join(problems[:5])
        more = f" (and {len(problems) - 5} more)" if len(problems) > 5 else ""
        raise TrustError(f"its files do not match {MANIFEST_NAME}: {shown}{more}.")
    return result
