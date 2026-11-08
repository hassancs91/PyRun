# Unified REST and MCP API

PyRun defines each endpoint once. REST, MCP, script loopback, the plugin SDK and
in-process Brain tools use the same handlers, schemas and grant checks.

## Authentication and workspace

In **Console → API tokens**, create a **Unified API** token. Choose its workspace,
permissions and optional Brain binding/tier. The secret is shown once; every API
token (Unified, datastore and plugin) is stored only as a digest, and the 2.0
upgrade hashed the datastore and plugin tokens 1.x kept in plaintext. Save it in
your client's secret store.

A backup restore deletes the tokens bound to a Brain or scoped to a single data
store, even when it restores the same Brain or store on the same instance (the
restore preview lists them); unbound tokens are kept. Create new tokens for the
affected clients after a restore. Backups never carry API tokens.

Send `Authorization: Bearer <token>` or `X-API-Key: <token>`. The token selects the
workspace; callers cannot override it in the URL or request body. Existing
Datastore and plugin token scopes do not authorize this API. Brain operations
also need a Brain grant for the token. No grant means no access, including public
notes. Disabling, expiring or revoking a token takes effect on subsequent checks.

## REST

Read endpoints accept GET query parameters or POST JSON. Run and write endpoints
require POST (see [Endpoint classes](#endpoint-classes-and-api-writes)).
Do not combine a JSON body with query parameters. Use HTTPS outside local testing.

```sh
curl -H "Authorization: Bearer $PYRUN_TOKEN" \
  https://pyrun.example/api/v1/_catalog

curl -H "Authorization: Bearer $PYRUN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"brain":"my-brain","entity_id":"take-example"}' \
  https://pyrun.example/api/v1/brain.get-note
```

Successful calls return `{"ok":true,"data":{...}}`. Errors return
`{"ok":false,"error":{"code":"...","message":"..."}}`, with the appropriate
HTTP status. Rate limits return 429, `Retry-After`, and an optional `retry_after`
field. Keep `X-Request-ID` for debugging; never include tokens in bug reports.

| Discovery route | Result |
|---|---|
| `/api/v1/_health` | Authenticated API status |
| `/api/v1/_catalog` | Only endpoints permitted for this token, including schemas |
| `/api/v1/_openapi.json` | Generated OpenAPI document, directly without the REST envelope |

## Endpoint classes and API writes

Each endpoint has an `access` class, shown in `_catalog`:

| Class | Meaning | Available |
|---|---|---|
| `read` | Changes nothing | With its permission |
| `run` | Acts without changing stored data, for example `brain.propose-feed` (a proposal waits for human approval) | With its permission |
| `write` | Changes stored data | With its permission, and only while API writes are on |

**API writes** is an instance setting, off by default. A superuser turns it on in
**Settings → API & MCP**. **Connect** shows the current mode. While it is off:

- write endpoints are left out of `_catalog`, `_openapi.json`, MCP `tools/list` and
  Py AI's tools;
- calling one returns 403 `writes_disabled` over REST, and a `tools/call` result
  with `isError: true` and the same code over MCP;
- write permissions can't be added to a token: the token form shows them disabled
  with the reason.

Turned on, a write permission can be granted per token, and only tokens that hold
it can call the matching write endpoints. A token that got a write permission while
writes were on keeps it after they are turned off, but its write calls are refused
until they are on again. A token without the permission gets 403 `forbidden`
either way. MCP marks `run` and `write` tools with `readOnlyHint: false`. Settings
lists the write endpoints this instance has. There are none yet; data writes come
with the datastore, table and vector endpoints.

## Brain operations

Pass `brain` as a workspace-local slug, or omit it when the token is bound to one
Brain. The [Brain guide](brain.md) explains grants, tiers and human review.

| REST endpoint | Parameters beyond `brain` | Permission |
|---|---|---|
| `brain.list` | Optional `offset`, `limit`; no `brain` parameter | `brain:read` |
| `brain.ping` | None | `brain:read` |
| `brain.get-index` | None | `brain:read` |
| `brain.list-notes` | Optional `kind`, `topic`, `project`, `status` | `brain:read` |
| `brain.get-note` | `entity_id` | `brain:read` |
| `brain.get-lens` | `name` | `brain:read` |
| `brain.get-identity` | None | `brain:read` |
| `brain.get-raw` | `path` under `raw/`, linked by a visible note | `brain:read` |
| `brain.search` | `query`, optional `k` (1–20); needs configured vectors | `brain:read` |
| `brain.assemble-context` | `task`, optional `lens`; uses an AIProvider | `brain:read` |
| `brain.propose-feed` | Source `content` or `url`; optional `source_kind`, `title`, `notes` | `brain:write` plus proposing grant |

`source_kind` is one of `doc`, `article`, `video`, `podcast`, `thread`, `thought`,
`conversation`, `other`. Proposing requires agents-only or private access and
`can_propose`. It returns a feed identifier, never a committed note. Approval
remains a console owner/admin action. Endpoint discovery is authoritative for
schemas and bounds; there are no `vectors.*` REST/MCP endpoints in this release.

## MCP clients

Open **Console → Connect** for the instance's exact URL and commands. The normal
endpoint is `POST /mcp` using the same bearer token. For Claude Code:

```sh
claude mcp add --transport http pyrun https://pyrun.example/mcp \
  --header "Authorization: Bearer YOUR_TOKEN"
```

Use your client's private configuration for credentials. Reconnect and discover
tools after changing permissions or migrating from BrainOutside. MCP tool names
replace dots in REST names with hyphens: `brain.get-note` becomes
`brain-get-note`, and `brain.assemble-context` becomes `brain-assemble-context`.
The catalog includes both `slug` and `mcp_name`. The endpoint is stateless
Streamable HTTP with JSON responses; it does not open SSE streams or allocate session IDs.
`GET /mcp` returns 405. JSON-RPC batches are rejected; accepted notifications return
202 without a body. Tool failures use `isError: true` with the same application
error code/message as REST. Protocol errors use JSON-RPC errors.

Over a rate limit both answer HTTP 429 with `Retry-After`, in two shapes: a
`tools/call` returns a JSON-RPC result with `isError: true` and the
`rate_limited` error, while `tools/list` and the other methods share a
per-token limit (60 a minute) and return the REST error envelope
(`{"ok":false,"error":{"code":"rate_limited",…}}`) without a JSON-RPC `id`.
Treat any 429 as "wait and retry".

The implementation supports protocol `2026-07-28` discovery/per-request metadata
and legacy initialization for `2025-11-25`, `2025-06-18`, `2025-03-26`. Use a
conforming client to supply the matching version, method/name headers and metadata.
There is no OAuth authorization server.

For clients that can only accept a URL, an instance administrator can enable
**MCP URL tokens** in Connect. Create a separate token of that kind with read-only
permissions and a mandatory expiry (90 days by default). Its endpoint is
`POST /mcp/k/<url-token>/`. It cannot be used as a REST/bearer token. The URL itself
is a credential: keep connector configuration private and redact that route from
reverse-proxy access logs. Because the token travels in the request path, reverse
proxies, CDNs and PyRun's own access log record it unless you redact it; that is why
URL tokens are read-only and always expire. Use a public HTTPS address for remote connectors.
Disabling URL tokens or revoking a token closes that route immediately.

## Capacity and failures

Calls have a cooperative budget; reader and vector backends receive bounded
timeouts. Every inline MCP call occupies a web-worker slot. Connect shows the
configured pool and budget. The console reader instead uses the existing worker
queue and a polling result page. Authentication failures are throttled by IP;
endpoint limits use the shared cache and principal ID.

A 404-shaped unknown Brain/note response can mean a missing resource, absent grant,
binding mismatch or insufficient tier. Check those in the console instead of
probing other workspaces. For 429, honor `Retry-After`. For reader/provider errors,
check Services, Brain settings, cost caps and Workers, then retry. Never share
provider credentials or raw authorization headers in logs or support reports.
