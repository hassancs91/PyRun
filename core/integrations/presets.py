"""
Provider presets for Connections (2.5 Stage 5, T23).

A preset says what a provider's connection looks like: the auth kinds it takes,
the form fields each kind needs (and which of them are secret), the base URL,
where an API key goes, and the "whoami" call that tests the credentials and
names the account. Each provider's preset lives with its integration, in
``core/integrations/<key>/preset.py`` as a module-level ``PRESET``; this module
holds the shared definitions and the one place credentials become what a client
sends (``auth_material``) and what output must mask (``mask_values``).
"""

import base64
import functools
import importlib
from dataclasses import dataclass, field

# Every provider with a preset. Explicit (not a directory scan) so the set is
# the same on every instance and a stray folder can't add a provider.
PRESET_MODULES = ("airtable", "http", "smtp")

API_KEY = "api_key"
BEARER = "bearer"
BASIC = "basic"
SMTP = "smtp"

SMTP_SECURITY = (("starttls", "STARTTLS"), ("ssl", "SSL/TLS"), ("none", "None (plain text)"))


@dataclass(frozen=True)
class Field:
    """One form field of a connection. Secret fields are stored encrypted."""

    name: str
    label: str
    secret: bool = False
    required: bool = True
    kind: str = "text"  # text | url | int | email | choice
    choices: tuple = ()
    default: object = ""
    help: str = ""
    placeholder: str = ""
    # False keeps leading/trailing spaces (a value prefix like "Token ").
    strip: bool = True


@dataclass(frozen=True)
class Whoami:
    """The call that tests a connection: GET ``path`` (relative to the base URL).

    The account label is the first non-empty value among ``label_keys`` in the
    JSON reply (top-level keys).
    """

    path: str
    label_keys: tuple = ()


@dataclass(frozen=True)
class Preset:
    key: str
    label: str
    description: str
    # Auth kind -> its fields, in form order. The first kind is the default.
    fields: dict = field(default_factory=dict)
    # A fixed base URL; blank = the connection's own ``base_url`` field.
    base_url: str = ""
    # Where an API key goes when the preset fixes it: (header name, value prefix).
    api_key_header: tuple = ("Authorization", "Bearer ")
    whoami: Whoami | None = None
    docs_url: str = ""

    @property
    def auth_kinds(self) -> tuple:
        return tuple(self.fields)

    def fields_for(self, auth_kind) -> tuple:
        return tuple(self.fields.get(auth_kind, ()))

    def secret_fields(self, auth_kind) -> tuple:
        return tuple(f for f in self.fields_for(auth_kind) if f.secret)

    def config_fields(self, auth_kind) -> tuple:
        return tuple(f for f in self.fields_for(auth_kind) if not f.secret)

    @property
    def is_http(self) -> bool:
        return SMTP not in self.fields


@functools.lru_cache(maxsize=1)
def presets() -> dict:
    """Every preset by key, in ``PRESET_MODULES`` order."""
    found = {}
    for key in PRESET_MODULES:
        preset = importlib.import_module(f"core.integrations.{key}.preset").PRESET
        assert preset.key == key, f"core.integrations.{key}.preset names {preset.key!r}"
        found[key] = preset
    return found


def get_preset(key):
    """The preset for ``key``, or None for a provider this version doesn't know."""
    return presets().get(key)


def base_url(preset, config) -> str:
    return (preset.base_url or (config or {}).get("base_url") or "").rstrip("/")


def auth_material(preset, auth_kind, config, credentials) -> dict:
    """What a client needs to call the service as this connection.

    ``headers`` and ``params`` go on every request to the base URL; ``token``
    is the bare key or token (API key and bearer kinds), ``basic`` the username
    and password, ``smtp`` the server and login. Keys that don't apply are None.
    """
    config = config or {}
    credentials = credentials or {}
    out = {"headers": {}, "params": {}, "token": None, "basic": None, "smtp": None}
    if auth_kind == API_KEY:
        key = credentials.get("api_key", "")
        name = config.get("param_name") or preset.api_key_header[0]
        prefix = config["prefix"] if "prefix" in config else preset.api_key_header[1]
        if config.get("location") == "query":
            out["params"][name] = key
        else:
            out["headers"][name] = f"{prefix or ''}{key}"
        out["token"] = key
    elif auth_kind == BEARER:
        token = credentials.get("token", "")
        out["headers"]["Authorization"] = f"Bearer {token}"
        out["token"] = token
    elif auth_kind == BASIC:
        username = config.get("username", "")
        password = credentials.get("password", "")
        pair = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        out["headers"]["Authorization"] = f"Basic {pair}"
        out["basic"] = {"username": username, "password": password}
    elif auth_kind == SMTP:
        out["smtp"] = {
            "host": config.get("host", ""),
            "port": int(config.get("port") or 587),
            "security": config.get("security") or "starttls",
            "username": config.get("username", ""),
            "password": credentials.get("password", ""),
            "from_address": config.get("from_address", ""),
        }
    return out


def mask_values(name, preset, auth_kind, config, credentials) -> dict:
    """``{label: value}`` of everything secret this connection can put in output.

    Its secret fields, plus the header and query values made from them (a basic
    auth header carries the password base64-encoded). Labels read
    ``connection:<name>.<field>``, so output shows ``[connection:mail.password:MASKED]``.
    """
    masks = {}
    for key, value in sorted((credentials or {}).items()):
        if isinstance(value, str) and value:
            masks[f"connection:{name}.{key}"] = value
    material = auth_material(preset, auth_kind, config, credentials)
    for where in ("headers", "params"):
        for key, value in material[where].items():
            if value and value not in masks.values():
                masks[f"connection:{name}.{key.lower()}"] = value
    if auth_kind == BASIC:
        pair = material["headers"]["Authorization"].split(" ", 1)[1]
        masks[f"connection:{name}.basic"] = pair
    return masks
