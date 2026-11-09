"""The deliberately small JSON-Schema subset shared by every API transport."""

import json
import math

from .errors import ApiError

TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: type(v) is int or (type(v) is float and math.isfinite(v)),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}
# A schema whose type lists all of these accepts any JSON value (a datastore
# entry's value). It may leave out ``items``: its array items are any value too.
ANY_VALUE = ("object", "array", "string", "number", "boolean", "null")
KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "required",
        "enum",
        "maxLength",
        "minimum",
        "maximum",
        "items",
        "additionalProperties",
        "description",
        "title",
        "default",
        "maxItems",
    }
)


def check_schema(schema, depth=0):
    """Reject unsupported declarations at registration instead of silently ignoring them."""
    if depth > 16 or not isinstance(schema, dict) or set(schema) - KEYWORDS:
        raise ValueError("Unsupported input schema")
    types = schema.get("type")
    types = types if isinstance(types, list) else [types]
    if not types or any(t not in TYPES for t in types):
        raise ValueError("Schema needs a supported type")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise ValueError("Schema properties must be an object")
    if schema.get("additionalProperties", False) not in (True, False):
        raise ValueError("additionalProperties must be boolean")
    required = schema.get("required", [])
    if not isinstance(required, list) or any(k not in properties for k in required):
        raise ValueError("required must name declared properties")
    for key in ("maxLength", "maxItems"):
        if key in schema and (type(schema[key]) is not int or schema[key] < 0):
            raise ValueError(f"{key} must be a non-negative integer")
    for key in ("minimum", "maximum"):
        if key in schema and not TYPES["number"](schema[key]):
            raise ValueError(f"{key} must be a finite number")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise ValueError("enum must be a nonempty list")
    for child in properties.values():
        check_schema(child, depth + 1)
    if "array" in types and ("items" in schema or not set(ANY_VALUE) <= set(types)):
        check_schema(schema.get("items"), depth + 1)


def validate(value, schema, path="$", depth=0):
    def invalid(message):
        raise ApiError("invalid_input", f"{path}: {message}", status=400)

    if depth > 16:
        invalid("input is too deeply nested")
    types = schema["type"]
    types = types if isinstance(types, list) else [types]
    if not any(TYPES[t](value) for t in types):
        invalid(f"expected {' or '.join(types)}")
    if "enum" in schema and not any(type(value) is type(v) and value == v for v in schema["enum"]):
        invalid("value is not in the allowed choices")
    if isinstance(value, str) and len(value) > schema.get("maxLength", float("inf")):
        invalid("text is too long")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema.get("minimum", -float("inf")) or value > schema.get(
            "maximum", float("inf")
        ):
            invalid("number is outside the allowed range")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if any(key not in value for key in schema.get("required", [])):
            invalid("a required property is missing")
        if not schema.get("additionalProperties", True) and set(value) - set(properties):
            invalid("unknown property")
        for key, item in value.items():
            if key in properties:
                validate(item, properties[key], f"{path}.{key}", depth + 1)
    if isinstance(value, list):
        if len(value) > schema.get("maxItems", float("inf")):
            invalid("too many items")
        for i, item in enumerate(value):
            validate(item, schema.get("items", schema), f"{path}[{i}]", depth + 1)
    return value


def loads(raw):
    def non_json(_):
        raise ValueError("Non-finite number")

    try:
        return json.loads(raw, parse_constant=non_json)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ApiError("invalid_input", "Expected valid JSON.", status=400) from exc


def query_params(query, schema):
    result = {}
    for key, values in query.lists():
        if len(values) != 1:
            raise ApiError("invalid_input", "Repeated query parameters are not allowed.")
        prop = schema.get("properties", {}).get(key, {})
        result[key] = values[0] if prop.get("type", "string") == "string" else loads(values[0])
    return result
