"""Declared script inputs: a JSON Schema on the script, values stamped on the run.

A script may declare what it takes as an object JSON Schema
(``Script.inputs_schema``), written in the deliberately small subset
``core.api.schema`` validates, the same one the Unified API registry uses, so a
script's inputs mean the same thing in the console now and over REST/MCP later.

The values a run gets are validated, completed with defaults and stamped on
``Run.inputs`` at QUEUE time (``core.tasks.queue_script_run``, which every
trigger goes through). Bad inputs therefore never become a run, not even a
failed one, and a duplicate task delivery reads the same inputs from the row.
The executor exports each scalar input as an ``INPUT_<NAME>`` environment
variable (``input_env``); objects and arrays stay on the run only.

Names are restricted to identifier-like ASCII and must stay unique ignoring
case, because ``INPUT_<NAME>`` upper-cases them. Every string in a schema or in
inputs must be storable on both engines: no NUL (PostgreSQL refuses
``\\u0000`` in ``jsonb`` and in text taken out of ``json``) and valid Unicode.
"""

import copy
import json
import re
from dataclasses import dataclass, field

from core.api.exceptions import ApiError
from core.api.schema import check_schema, validate

INPUT_ENV_PREFIX = "INPUT_"
NAME_MAX_LENGTH = 64
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
NAME_RULE = (
    "Input names start with a letter or underscore and use only letters, digits "
    f"and underscores (at most {NAME_MAX_LENGTH} characters)."
)
# A schema is a form definition, not data. Inputs are capped so every exported
# value stays far below Linux's 128 KB limit on a single environment string.
SCHEMA_MAX_BYTES = 32 * 1024
INPUTS_MAX_BYTES = 64 * 1024


class ScriptInputsError(ValueError):
    """Inputs a script can't run with. ``field_errors`` maps input name -> reason."""

    def __init__(self, message: str, field_errors: dict | None = None):
        super().__init__(message)
        self.message = message
        self.field_errors = dict(field_errors or {})


def _text_problem(value) -> str:
    """Why some string inside ``value`` can't be stored on both engines, or ""."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, str):
            if "\x00" in item:
                return "Text can't contain NUL characters."
            try:
                item.encode("utf-8")
            except UnicodeEncodeError:
                return "Text must be valid Unicode."
    return ""


def _json_size(value) -> int:
    """Encoded size in bytes. Raises ValueError for NaN/Infinity or non-JSON values."""
    return len(json.dumps(value, allow_nan=False).encode("utf-8"))


def _message(exc: ApiError) -> str:
    """The validator's ``$.path: reason`` as a sentence for one field."""
    text = exc.message
    if text.startswith("$: "):
        text = text[3:]
    elif text.startswith("$."):
        text = text[2:]
    text = text[:1].upper() + text[1:]
    return text if text.endswith(".") else text + "."


def _name_problem(name) -> str:
    if not isinstance(name, str) or len(name) > NAME_MAX_LENGTH or not NAME_RE.match(name):
        return NAME_RULE
    return ""


def check_inputs_schema(schema):
    """Check a declared inputs schema; return it, or None when it declares nothing.

    Raises ValueError with a message fit for the script editor.
    """
    if schema is None or schema == "" or schema == {}:
        return None
    if not isinstance(schema, dict):
        raise ValueError("The inputs schema must be a JSON object.")
    if schema.get("type") != "object":
        raise ValueError('The inputs schema must have "type": "object".')
    try:
        size = _json_size(schema)
    except (TypeError, ValueError):
        raise ValueError("The inputs schema must be plain JSON (no NaN or Infinity).") from None
    if size > SCHEMA_MAX_BYTES:
        raise ValueError(f"The inputs schema is over {SCHEMA_MAX_BYTES // 1024} KB.")
    problem = _text_problem(schema)
    if problem:
        raise ValueError(f"The inputs schema can't be stored: {problem}")
    try:
        check_schema(schema)
    except ValueError as exc:
        raise ValueError(f"Invalid inputs schema: {exc}.") from None

    properties = schema.get("properties", {})
    if not properties:
        return None
    exported = {}
    for name, prop in properties.items():
        problem = _name_problem(name)
        if problem:
            raise ValueError(f'"{name}": {problem}')
        upper = name.upper()
        if upper in exported:
            raise ValueError(
                f'Inputs "{exported[upper]}" and "{name}" would both reach the script '
                f"as {INPUT_ENV_PREFIX}{upper}. Rename one."
            )
        exported[upper] = name
        if "default" in prop:
            try:
                validate(prop["default"], prop)
            except ApiError as exc:
                raise ValueError(
                    f'The default for "{name}" doesn\'t fit the input: {_message(exc)}'
                ) from None
    return schema


def required_without_default(schema) -> list[str]:
    """Required inputs a run started by nobody (a schedule) couldn't fill in."""
    if not schema:
        return []
    properties = schema.get("properties", {})
    return [n for n in schema.get("required", []) if "default" not in properties.get(n, {})]


def schedule_conflict(schema) -> str:
    """Why a script with this schema can't be scheduled, or "" when it can."""
    missing = required_without_default(schema)
    if not missing:
        return ""
    names = ", ".join(f'"{n}"' for n in missing)
    if len(missing) == 1:
        return (
            f"Input {names} is required and has no default, so a scheduled run "
            "would have nothing to use. Give it a default, make it optional, or "
            "keep the schedule on Manual."
        )
    return (
        f"Inputs {names} are required and have no default, so a scheduled run "
        "would have nothing to use. Give them defaults, make them optional, or "
        "keep the schedule on Manual."
    )


def _summary(errors: dict) -> str:
    return "Inputs rejected: " + "; ".join(f"{name}: {reason}" for name, reason in errors.items())


def prepare_inputs(script, inputs):
    """The inputs a run of ``script`` starts with: validated, defaults filled in.

    Returns None for a script that declares no inputs and was given none, else a
    dict in declared order (undeclared extras, when the schema allows them, last).
    Raises ScriptInputsError naming every bad input.
    """
    schema = script.inputs_schema
    if not schema:
        if inputs:
            raise ScriptInputsError("This script doesn't declare any inputs.")
        return None
    if inputs is None:
        inputs = {}
    if not isinstance(inputs, dict):
        raise ScriptInputsError("Inputs must be a JSON object.")

    properties = schema.get("properties", {})
    values = dict(inputs)
    for name, prop in properties.items():
        if name not in values and "default" in prop:
            values[name] = copy.deepcopy(prop["default"])

    errors = {}
    exported = {}
    for name in values:
        problem = _name_problem(name)
        if problem:
            errors[str(name)] = problem
            continue
        if name.upper() in exported:
            errors[name] = (
                f'Clashes with "{exported[name.upper()]}": both would be '
                f"{INPUT_ENV_PREFIX}{name.upper()}."
            )
        exported[name.upper()] = name
    for name in schema.get("required", []):
        if name not in values:
            errors[name] = "This input is required."
    for name, value in values.items():
        if name in errors:
            continue
        prop = properties.get(name)
        if prop is None:
            if schema.get("additionalProperties", True) is False:
                errors[name] = "This script doesn't declare this input."
            continue
        try:
            validate(value, prop)
        except ApiError as exc:
            errors[name] = _message(exc)
            continue
        problem = _text_problem(value)
        if problem:
            errors[name] = problem
    if errors:
        raise ScriptInputsError(_summary(errors), errors)

    ordered = {name: values[name] for name in properties if name in values}
    ordered.update((k, v) for k, v in values.items() if k not in ordered)
    try:
        size = _json_size(ordered)
    except (TypeError, ValueError):
        raise ScriptInputsError("Inputs must be plain JSON values (no NaN or Infinity).") from None
    if size > INPUTS_MAX_BYTES:
        raise ScriptInputsError(f"Inputs are over the {INPUTS_MAX_BYTES // 1024} KB limit.")
    return ordered


def input_env(inputs) -> dict:
    """``INPUT_<NAME>`` environment variables for a run's stamped scalar inputs.

    Text passes through as-is, numbers as JSON (``3``, ``2.5``) and booleans as
    ``true``/``false``. Objects, arrays and nulls are not exported. Names were
    checked when the inputs were stamped, so each maps to one variable.
    """
    env = {}
    for name, value in (inputs or {}).items():
        if not isinstance(name, str) or _name_problem(name):
            continue
        if isinstance(value, bool):
            text = "true" if value else "false"
        elif isinstance(value, (int, float)):
            text = json.dumps(value)
        elif isinstance(value, str):
            text = value
        else:
            continue
        env[INPUT_ENV_PREFIX + name.upper()] = text
    return env


@dataclass
class InputField:
    """One declared input, as a form renders it."""

    name: str
    kind: str  # text | number | integer | boolean | select | json
    label: str
    help: str = ""
    required: bool = False  # required AND without a default: the form must get a value
    has_default: bool = False
    default: object = None
    choices: list = field(default_factory=list)
    max_length: int | None = None
    minimum: float | None = None
    maximum: float | None = None


def _kind(prop: dict) -> str:
    types = prop.get("type")
    if "enum" in prop and not isinstance(types, list):
        return "select"
    if types == "string":
        return "text"
    if types in ("number", "integer", "boolean"):
        return types
    return "json"


def input_fields(schema) -> list[InputField]:
    """The declared inputs of ``schema`` in order, typed for a form."""
    if not schema:
        return []
    required = set(schema.get("required", []))
    fields = []
    for name, prop in schema.get("properties", {}).items():
        fields.append(
            InputField(
                name=name,
                kind=_kind(prop),
                label=prop.get("title") or name,
                help=prop.get("description", ""),
                required=name in required and "default" not in prop,
                has_default="default" in prop,
                default=prop.get("default"),
                choices=list(prop.get("enum", [])),
                max_length=prop.get("maxLength"),
                minimum=prop.get("minimum"),
                maximum=prop.get("maximum"),
            )
        )
    return fields
