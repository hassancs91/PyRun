"""OpenAPI generated from the same permission-filtered endpoint definitions."""

import copy

from pyrunner.version import __version__
from .registry import registry


def document(ctx):
    paths = {}
    for spec in registry.visible(ctx):
        common = {
            "summary": spec.title,
            "description": spec.description,
            "x-mcp-name": spec.mcp_name,
            "x-permission": spec.permission,
            "responses": {
                "200": (
                    {
                        "description": f"Success: the result's `{spec.text}` field as plain text",
                        "content": {"text/plain": {"schema": {"type": "string"}}},
                    }
                    if spec.text
                    else {"description": "Success: {ok: true, data: object}"}
                ),
                "default": {"description": "Error: {ok: false, error: {code, message}}"},
            },
        }
        methods = {
            "post": {
                **copy.deepcopy(common),
                "operationId": spec.slug,
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {"schema": copy.deepcopy(spec.input)},
                    },
                },
            }
        }
        if spec.access == "read":
            # JSON query content matches the REST adapter's parser for every
            # non-string type (including arrays/objects), without form expansion.
            methods["get"] = {
                **copy.deepcopy(common),
                "operationId": spec.slug + "-get",
                "parameters": [
                    {
                        "name": name,
                        "in": "query",
                        "required": name in spec.input.get("required", []),
                        **(
                            {"schema": copy.deepcopy(schema)}
                            if schema.get("type") == "string"
                            else {
                                "content": {"application/json": {"schema": copy.deepcopy(schema)}}
                            }
                        ),
                    }
                    for name, schema in spec.input.get("properties", {}).items()
                ],
            }
        paths["/" + spec.slug] = methods
    return {
        "openapi": "3.1.0",
        "info": {"title": "PyRun API", "version": __version__},
        "servers": [{"url": "/api/v1"}],
        "security": [{"bearerAuth": []}, {"apiKey": []}],
        "components": {
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer"},
                "apiKey": {"type": "apiKey", "in": "header", "name": "X-API-Key"},
            }
        },
        "paths": paths,
    }
