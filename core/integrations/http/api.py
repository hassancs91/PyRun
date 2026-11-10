"""The http integration: any HTTP API through an HTTP API connection (see core/integrations/framework.py)."""

from core.integrations.framework import Integration, Smoke


def _smoke_get(module, connection, config):
    """GET the connection's test path (its base URL when it has none)."""
    path = (config or {}).get("test_path") or ""
    body = module.get(path, connection=connection)
    kind = type(body).__name__ if body is not None else "an empty body"
    return f"GET {path or '/'} answered with {kind}"


EXAMPLE = '''\
from pyrunner_integrations import http

# The script's one HTTP API connection (or pass connection="name").
me = http.get("/v1/me")
print("Signed in as", me["name"])

created = http.post("/v1/items", json={"name": "Ada"})

for item in http.paginate("/v1/items", items="data", cursor="next_cursor", cursor_param="cursor"):
    print(item["id"])
'''

INTEGRATION = Integration(
    key="http",
    provider="http",
    title="HTTP",
    functions=("get", "post", "put", "patch", "delete", "request", "paginate", "client"),
    example=EXAMPLE,
    smoke=(Smoke("get", _smoke_get),),
)
