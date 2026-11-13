"""
The watcher adapter base (2.5 Stage 7, T31).

An adapter knows how to ask one kind of source what is new. It declares:

- ``key`` (stable, stored on the watcher), ``title`` and ``description``;
- ``config_schema``: an object JSON Schema of flat settings (string, integer,
  number, boolean; ``enum``, ``pattern``, ``minimum``/``maximum``,
  ``minLength``/``maxLength``, ``default``). It generates the watcher form and validates
  what is saved (``validate_config``);
- ``requires_connection`` and ``providers``: whether a watcher needs a
  Connection, and of which providers;

and implements:

- ``check(ctx, config, state) -> (events, new_state)``: what is new since
  ``state`` (None on the first check, which reports nothing and only marks
  where the source is). Each event is ``{"id": <stable id>, "item": <JSON>}``,
  oldest first, at most ``ctx.limit`` of them; ``new_state`` is JSON and must
  only move past the events returned (anything not returned is reported
  again next time). Raise ``AdapterError`` (or any exception) on failure: the
  runner counts it, backs off and keeps the old state;
- ``test(ctx, config) -> (message, items)``: a read-only look for the
  console's Test button: a short line saying what it saw and up to
  ``TEST_ITEMS`` sample items. It never changes anything.

This is the same contract as ``core.integrations.framework.Trigger``
(``check(module, connection, config, state, limit)``): an integration's
trigger becomes an adapter by wrapping it, so there is one runner for both.
"""

import re
from dataclasses import dataclass

from core.api.errors import ApiError
from core.api.schema import validate

# Sample items the Test button shows.
TEST_ITEMS = 3
# Network time one adapter call may take (the runner also stops waiting then).
ADAPTER_TIMEOUT_SECONDS = 20
# Largest response body an adapter reads from a source.
MAX_RESPONSE_BYTES = 5 * 1024 * 1024

# Keywords validate_config understands, past core.api.schema's subset.
_EXTRA_KEYWORDS = ("pattern", "format", "minLength")


class AdapterError(Exception):
    """A check or test failed for a reason worth showing as is (no secrets in it)."""


@dataclass
class CheckContext:
    """What an adapter call gets besides its config and state."""

    # The watcher being checked; None for a Test of a form not saved yet.
    watcher: object = None
    # The Connection the watcher uses (adapters with requires_connection).
    connection: object = None
    # The workspace the watcher (or the Test) belongs to: an adapter that
    # reads a workspace resource (db_poll's database) looks it up here.
    workspace: object = None
    # At most this many events (None = no limit).
    limit: int | None = None
    # Seconds the adapter's network calls may take.
    timeout: float = ADAPTER_TIMEOUT_SECONDS


class Adapter:
    key = ""
    title = ""
    description = ""
    config_schema: dict = {"type": "object", "properties": {}}
    requires_connection = False
    # Connection providers it accepts (requires_connection); () = any.
    providers: tuple = ()
    # The plugin that registered it (None = PyRun's own).
    owner_plugin = None

    def check(self, ctx: CheckContext, config: dict, state):
        raise NotImplementedError

    def test(self, ctx: CheckContext, config: dict):
        raise NotImplementedError


def _strip(prop: dict) -> dict:
    return {k: v for k, v in prop.items() if k not in _EXTRA_KEYWORDS}


def _sentence(exc: ApiError) -> str:
    text = exc.message.split(": ", 1)[-1]
    text = text[:1].upper() + text[1:]
    return text if text.endswith(".") else text + "."


# The setting types a config_schema may use (the form has a field for each).
FLAT_TYPES = ("string", "integer", "number", "boolean")
_SETTING_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def schema_problem(schema) -> str:
    """Why ``schema`` can't be an adapter's ``config_schema``, or "" when it can.

    The flat subset ``validate_config`` and the watcher form understand: an
    object of string/integer/number/boolean settings, each with the keywords
    of ``core.api.schema`` plus ``pattern`` (a valid regular expression).
    Built-ins are written to it; a plugin's adapters are checked with it
    before they are offered (T33).
    """
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return 'config_schema must be an object schema: {"type": "object", "properties": {...}}.'
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return "config_schema properties must be an object."
    stripped = {}
    for name, prop in properties.items():
        if not isinstance(name, str) or not _SETTING_NAME_RE.match(name):
            return f"setting {name!r}: a name is letters, digits and underscores (64 at most)."
        if not isinstance(prop, dict) or prop.get("type") not in FLAT_TYPES:
            return f'setting "{name}": "type" must be one of {", ".join(FLAT_TYPES)}.'
        if "minLength" in prop and (type(prop["minLength"]) is not int or prop["minLength"] < 0):
            return f'setting "{name}": "minLength" must be a non-negative integer.'
        if "pattern" in prop:
            try:
                re.compile(prop["pattern"])
            except (re.error, TypeError):
                return f'setting "{name}": "pattern" isn\'t a valid regular expression.'
        stripped[name] = _strip(prop)
    from core.api.schema import check_schema

    try:
        check_schema({**{k: v for k, v in schema.items() if k not in _EXTRA_KEYWORDS}, "properties": stripped})
    except ValueError as e:
        return f"config_schema: {e}."
    for name, prop in properties.items():
        if "default" in prop:
            _, errors = validate_config({"properties": {name: prop}}, {name: prop["default"]})
            if errors:
                return f'setting "{name}": the default doesn\'t fit it ({errors[name]})'
    return ""


def validate_config(schema: dict, config) -> tuple[dict, dict]:
    """``config`` checked against an adapter's flat ``config_schema``.

    Returns ``(cleaned, errors)``: ``cleaned`` keeps the declared settings
    (blank strings and None left out, so a default applies), ``errors`` maps a
    setting (or ``""`` for the whole config) to a sentence.
    """
    if not isinstance(config, dict):
        return {}, {"": "Settings must be an object."}
    properties = schema.get("properties", {})
    errors, cleaned = {}, {}
    for name, value in config.items():
        if name not in properties:
            if not schema.get("additionalProperties", True):
                errors[name] = "Not a setting of this adapter."
            continue
        if value is None or value == "":
            continue
        prop = properties[name]
        try:
            validate(value, _strip(prop), f"$.{name}")
        except ApiError as e:
            errors[name] = _sentence(e)
            continue
        if isinstance(value, str) and len(value) < prop.get("minLength", 0):
            errors[name] = "Too short."
            continue
        if isinstance(value, str) and "pattern" in prop and not re.search(prop["pattern"], value):
            # Say so when the line breaks are what's wrong (an IMAP mailbox
            # or search, 2.5 T52); otherwise the generic sentence.
            unbroken = re.sub(r"[\r\n]", "", value)
            if unbroken != value and re.search(prop["pattern"], unbroken):
                errors[name] = "Line breaks aren't allowed here."
            else:
                errors[name] = "Doesn't have the expected form."
            continue
        cleaned[name] = value
    for name in schema.get("required", []):
        if name not in cleaned and name not in errors and "default" not in properties.get(name, {}):
            errors[name] = "This field is required."
    return cleaned, errors


def with_defaults(schema: dict, config: dict) -> dict:
    """``config`` plus the schema's defaults for the settings it leaves out."""
    merged = {
        name: prop["default"]
        for name, prop in schema.get("properties", {}).items()
        if "default" in prop
    }
    merged.update(config or {})
    return merged


def dig(value, path: str):
    """The value at a dotted ``path`` (``a.b.0.c``) inside ``value``; None if absent.

    An empty path is ``value`` itself. A segment of digits indexes a list.
    """
    if not path:
        return value
    for part in path.split("."):
        if isinstance(value, dict):
            if part not in value:
                return None
            value = value[part]
        elif isinstance(value, list) and part.lstrip("-").isdigit():
            index = int(part)
            if not -len(value) <= index < len(value):
                return None
            value = value[index]
        else:
            return None
    return value
