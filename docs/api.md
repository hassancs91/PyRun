# Unified REST and MCP API

PyRun defines each endpoint once. REST, MCP, script loopback, the plugin SDK and
[Py AI's tools](#py-ai) use the same handlers, schemas and grant checks.

## Authentication and workspace

In **Console → API tokens**, create a **Unified API** token. Choose its workspace,
permissions, an optional binding (one Brain, one script or one datastore) and Brain tier. The secret is shown once; every API
token (Unified, datastore and plugin) is stored only as a digest, and the 2.0
upgrade hashed the datastore and plugin tokens 1.x kept in plaintext. Save it in
your client's secret store.

A backup restore deletes the tokens bound to a Brain, a script or a data store (and
the single-store datastore tokens), even when it restores the same Brain, script or store on the
same instance (the restore preview lists them); unbound tokens are kept. Create new tokens for the
affected clients after a restore. Backups never carry API tokens.

Send `Authorization: Bearer <token>` or `X-API-Key: <token>`. The token selects the
workspace; callers cannot override it in the URL or request body. A token whose
workspace no longer exists is refused (403 `scope_mismatch`). Existing
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
| `/api/v1/_catalog` | Only endpoints permitted for this token, including schemas, and its [script tools](#scripts-as-mcp-tools) |
| `/api/v1/_openapi.json` | Generated OpenAPI document, directly without the REST envelope |

## Endpoint classes and API writes

Each endpoint has an `access` class, shown in `_catalog`:

| Class | Meaning | Available |
|---|---|---|
| `read` | Changes nothing | With its permission |
| `run` | Acts without changing stored data: `scripts.run`, `runs.stop`, and `brain.propose-feed` (a proposal waits for human approval) | With its permission |
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
lists the write endpoints this instance has: today
[`datastores.set` and `datastores.delete`](#datastores). Every write call is recorded
in the token's [Activity](#running-and-stopping-scripts).

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

## Scripts and runs

Read a workspace's scripts and runs, a run's result and its output. **Connect**
has curl and Claude Code examples for this instance.

| REST endpoint | Parameters | Permission |
|---|---|---|
| `scripts.list` | Optional `q` (in the name or description), `tag`, `enabled`, `offset`, `limit` (1–200, default 100) | `scripts:read` |
| `scripts.get` | `script` (its id); optional for a token bound to one script | `scripts:read` |
| `runs.list` | Optional `script`, `status`, `since` (ISO 8601; UTC when it has no zone), `limit` (1–100, default 20) | `runs:read` |
| `runs.get` | `run` (its id) | `runs:read` |
| `runs.output` | `run`; optional `stream` (`stdout`, the default, or `stderr`) and `tail` (keep only the last this many bytes) | `runs:output` |

```sh
curl -H "Authorization: Bearer $PYRUN_TOKEN" \
  "https://pyrun.example/api/v1/runs.list?status=failed&limit=5"

curl -H "Authorization: Bearer $PYRUN_TOKEN" \
  "https://pyrun.example/api/v1/runs.output?run=RUN_ID&stream=stderr&tail=4000"
```

- `scripts.list` and `scripts.get` leave out archived scripts. Neither returns
  a script's code, and `runs.get` doesn't return the code a run executed.
  `scripts.get` adds the inputs schema, the schedule (`mode`, `active`,
  `summary`, `timezone`, `next_run`) and the last run.
- `runs.list` is newest first. Runs of archived scripts stay readable, as on the
  console's Runs page.
- `runs.get` returns the run's status and timing, `attempt`, `cancel_reason`,
  its `inputs`, `result`, `progress` and `event`, and the size of each output
  stream (`output.stdout.chars`, `output.stdout.truncated`). Secret values in the
  inputs and the event are masked, as on the run page. When a secret can't be
  resolved, those two come back `null` and are named in `withheld`. The person who
  started a manual run (or a plugin run a user started) shows as
  `event.user = {"id": 7}`: API and MCP callers get the user id, never the email.
  The script itself and the console still see the email.
- `runs.output` returns exactly what the run page shows: the stored output, with
  the run's secrets masked, at most 1 MB per stream (over that, the start and
  the end are kept with a marker line in between). Output is written when the run
  ends, so a run that is still going has none yet. Over REST the reply is
  `text/plain`, not the JSON envelope (errors keep the envelope; `_catalog` marks
  the endpoint `rest_response: "text/plain"`). Over MCP the tool result is
  `{run, status, stream, text, truncated, tail_cut}`. `tail` cuts at a character
  boundary, so the text can be a few bytes shorter.

**Bind a token to one script** in the token form. That token's `scripts.list`
returns that script alone, `scripts.get` may leave out `script`, and every
`runs.*` call only reaches that script's runs. Any other script or run, like an
unknown id or one in another workspace, answers 404 `not_found`. A token is
bound to one Brain or one script, not both. Deleting the script deletes the
tokens bound to it. A bound token also runs and stops that script only.

### Running and stopping scripts

| REST endpoint | Parameters | Permission |
|---|---|---|
| `scripts.run` | `script` (optional for a token bound to one script), `inputs` (an object), `wait` (default `false`), `wait_seconds` (1–55, default 55, only with `wait`) | `scripts:run` |
| `runs.stop` | `run` | `runs:stop` |

Both are POST only and take 30 calls a minute per token (each its own count);
over that they answer 429 with `Retry-After`. MCP URL tokens can't hold
either permission.

```sh
curl -H "Authorization: Bearer $PYRUN_TOKEN" -H "Content-Type: application/json" \
  -d '{"script":"SCRIPT_ID","inputs":{"city":"Paris"},"wait":true,"wait_seconds":30}' \
  https://pyrun.example/api/v1/scripts.run
```

`scripts.run` checks `inputs` against the script's inputs schema and fills in
defaults, as **Run now** does. A bad input answers 400 `invalid_input` naming
each one, and no run is created. A disabled script answers 409
`script_disabled`. The run is an API run: its event is
`{"kind": "api", "token_id": ..., "via": "rest"}` (or `"mcp"`), and maintenance
windows that block API calls and the script's concurrency policy apply to it.

The answer is the run's summary (`run_id`, `script`, `status`, timing,
`exit_code`, `cancel_reason`), `is_finished`, `is_held`, `result`, and `wait`:

| Case | HTTP | `wait` |
|---|---|---|
| No `wait`, run queued | 202 | `null` |
| The run ended within the wait | 200, with `result` | `finished` |
| The wait ran out; the run keeps going | 202 | `timeout` |
| Every wait slot was taken; the run was queued, not waited for | 202 | `busy` |
| Skipped: the script is already running and its concurrency policy is Skip | 200, `status: "cancelled"`, `cancel_reason: "concurrency_skip"` | as asked |
| No worker is running (only with `wait`); no run is created | 503 `workers_unavailable`, `Retry-After: 60` | |
| A maintenance window blocks API runs; the run is listed as skipped | 503 `maintenance`, `Retry-After` until it ends | |

While `is_finished` is false the answer also has `next`, saying to poll
`runs.get` with the `run_id`. Over MCP the tool result carries the same data
(`scripts-run`); `_catalog` marks the endpoint `rest_accepted`.

A wait holds one of the server's web threads, so it is limited: it fails fast
when no worker has reported in the last 3 minutes, and at most 4 calls wait at
once across the instance (set `PYRUNNER_SYNC_WAIT_SLOTS`; it is capped below
`GUNICORN_WORKERS` × `GUNICORN_THREADS`, so the console always keeps a thread,
and 0 turns waiting off). A wait also ends 10 seconds inside `GUNICORN_TIMEOUT`,
and over MCP 3 seconds before the call's budget (`MCP_CALL_BUDGET_SECONDS`). A retry the script's retry policy
starts after a failure is a new run: the wait returns the failed attempt.

`runs.stop` is the run page's **Stop**. A running script gets the stop signal
and its grace period (the script's `stop_grace_seconds`) to shut down: the
answer says `status: "running"`, `is_stopping: true` with `stop_requested_at`,
and the run ends as `cancelled` once the script has exited. Calling `runs.stop`
again kills it at once (**Kill now**). A pending run is cancelled. A run that has
already ended is left as it is and answers `changed: false`. In 2.5 the stop
reaches runs on this PyRun container's workers only; it is single-container.

**Activity.** Every `scripts.run`, `runs.stop` and write call made with a
token, and every call made with a token that fails (an unknown endpoint, a
refused permission, a bad input, a rate limit...), is recorded with the endpoint, status, error code, time taken, the
caller's IP and the request id (`X-Request-ID`). **Settings → API Tokens →
Activity** lists a token's records; they are kept 30 days. Successful reads
aren't recorded (the token's **Last used** covers them), and neither is a call
whose token is wrong, expired or disabled: it can't be tied to a token (those
are throttled by IP instead).

### Scripts as MCP tools

A script can be a tool of its own in MCP clients. In the script editor's **MCP
tool** card, turn on **Expose as MCP tool** and set a **Tool name**: lowercase
letters, digits and underscores, starting with a letter, up to 40 characters.
Left blank, the name is made from the script's name (`Weekly report` becomes
`weekly_report`). No two exposed scripts in a workspace share a name (archived
ones included); a name kept with the switch off holds nothing. Tool names have
no hyphen, so they never clash with the built-in tools (`scripts-run`, ...).

`tools/list` then adds one tool per exposed script the token may run: the token
needs `scripts:run`, sees its own workspace's scripts only, and a token bound to
one script sees that script's tool only. Disabled and archived scripts aren't
listed. Each tool has:

| Field | Value |
|---|---|
| `name` | The tool name |
| `title` | The script's name |
| `description` | The script's description (or "Runs the PyRun script “name”.") |
| `inputSchema` | The script's inputs schema, as declared and in its order; a script without inputs gets `{"type": "object", "properties": {}, "additionalProperties": false}` |
| `annotations` | `readOnlyHint: false` |

`tools/call` with a script tool is `scripts.run` with `"wait": true` and the
call's `arguments` as `inputs`: the same inputs check (a bad input is a tool
error, `invalid_input`, and no run is created), the same 30 calls a minute
(shared with `scripts.run` over REST), and the same answer as
[`scripts.run`](#running-and-stopping-scripts) in `structuredContent`: the
result when the run ends within the wait (55 seconds at most), else the run id,
`is_finished: false` and `next`, which tells the model to poll `runs.get`; give
the token `runs:read` too so it can. The wait limits of `scripts.run` apply: a
call made while every wait slot is taken gets the run id at once (`wait:
"busy"`), and with no worker running the call fails with `workers_unavailable`
and no run is created. A disabled script answers `script_disabled`; a tool the
token can't see is an unknown tool (JSON-RPC error -32602). The Activity page
records the call as `scripts.run`.

Clients read the tool list when they connect, so reconnect the server after
exposing a script or changing its inputs (in Claude Code, from `/mcp`). Claude Code shows a
tool as `mcp__<server>__<tool name>`, so a script exposed as `weekly_report` on
a server added as `pyrun` is `mcp__pyrun__weekly_report`.

Over REST, run the same script with `scripts.run` and its id. `_catalog` lists a
token's script tools under `script_tools`, next to `endpoints`:

```json
{"kind": "script", "mcp_name": "weekly_report", "title": "Weekly report",
 "description": "...", "inputSchema": {...}, "script": "SCRIPT_ID",
 "rest": "scripts.run", "access": "run", "permission": "scripts:run"}
```

## Datastores

Read a workspace's datastores and their entries, and, while
[API writes](#endpoint-classes-and-api-writes) are on, set and delete entries.

| REST endpoint | Parameters | Permission |
|---|---|---|
| `datastores.list` | None | `datastores:read` |
| `datastores.get` | `name` | `datastores:read` |
| `datastores.entries` | `name`; optional `page` (from 1) and `page_size` (1–100, default 50) | `datastores:read` |
| `datastores.entry` | `name`, `key` | `datastores:read` |
| `datastores.set` | `name`, `key`, `value` (any JSON value) | `datastores:write` |
| `datastores.delete` | `name`, `key` | `datastores:write` |

```sh
curl -H "Authorization: Bearer $PYRUN_TOKEN" \
  "https://pyrun.example/api/v1/datastores.entry?name=results&key=latest"

curl -H "Authorization: Bearer $PYRUN_TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"results","key":"latest","value":{"rows":42}}' \
  https://pyrun.example/api/v1/datastores.set
```

- A datastore is named by its name in the token's workspace. A data store
  that belongs to no workspace is never reached.
  `datastores.list` returns `{"datastores": [...], "count": n}`, each with
  `name`, `description`, `entry_count`, `created_at` and `updated_at`.
- `datastores.entries` returns `entries` (`key`, `value`, `created_at`,
  `updated_at`, by key), `count`, `page`, `page_size` and `total_pages`; a page
  past the end returns the last page. `datastores.entry` returns one entry.
- `datastores.set` stores `value` under `key`, replacing what the key holds, and
  answers `{name, key, created, created_at, updated_at}` (`created` is false when it
  replaced a value). It never creates a datastore: create stores in the console.
  `datastores.delete` deletes one entry and answers `deleted: false` when the key
  wasn't there. Both are POST only and take 30 calls a minute per token (each its
  own count). A request body is limited by the server's upload limit (2.5 MB by
  default; over it, 413 `too_large`).
- Values are returned as stored, as on the console's datastore page: secrets a
  script wrote into a datastore aren't masked.

**Bind a token to one datastore** with the token form's **Datastore** field.
That token's `datastores.list` returns that store alone, the others may leave out
`name`, and any other store in the workspace answers 403 `forbidden` (a store
that doesn't exist, or is in another workspace, answers 404 `not_found`). A
token is bound to one Brain, one script or one datastore, not more. Deleting the
store deletes the tokens bound to it.

**The datastore API** (`/api/v1/datastores/`, `/api/v1/datastores/<name>/`,
`.../entries/` and `.../entries/<key>/`) keeps working for datastore tokens. Its
URLs call the same handlers as `datastores.list`, `.get`, `.entries` and `.entry`
and answer the same data without the `{"ok": true, "data": ...}` envelope; errors
keep that API's shape (`{"error": {"code": "NOT_FOUND", "message": ...}}`). A
single-store datastore token is bound to its store like a Unified API token
bound to it. It is read-only, and Unified API tokens don't work there. A datastore
token whose workspace no longer exists is refused (403 `SCOPE_MISMATCH`). Before
2.5 such a token could still read the data stores that belonged to no workspace,
and any token could read one of those by its name.

## Py AI

Py AI, the built-in read-only assistant, calls the registry's `read` endpoints
in-process, as tools named like their MCP tools (`scripts-get`, `runs-output`),
with the same descriptions, input schemas and answers. It never gets a `run` or
`write` endpoint, whether API writes are on or off; its one action is proposing
a Brain memory (`remember`), which waits for approval (see `docs/plugins.md`).
Every call is scoped to the workspace Py AI answers about, like a token without a
binding, and isn't rate-limited or recorded on an Activity page. An answer over
100,000 characters comes back as a `too_large` error instead, so Py AI asks again
for less (`runs.output` with `tail`, `datastores.entries` with a smaller
`page_size`).

Each endpoint declares a Py AI tier:

| Tier | Who | Endpoints |
|---|---|---|
| `console` | The console's Py AI, open to workspace owners and admins | Every read endpoint |
| `channel` | Py AI on a chat channel (Telegram), whose approved senders need not be owners or admins | `scripts.list`, `scripts.get`, `runs.list`, `runs.get`, the four datastore reads, and the Brain reads |

So a chat channel can't read a run's output (`runs.output`); the console can.
The tier is checked again on every call, not only when the tools are listed.
Datastore values reach both tiers as stored, unmasked, as Py AI's
`query_datastore` tool already gave them to chat channels.
Brain endpoints also need the agent's own Brain grant (`agent:pyai` for the
console, `<provider>:<channel UUID>` for a channel). Like an API token, Py AI
sees the person behind a manual run as `user: {"id"}`, never the email. Py AI's
older tools (`list_scripts`, `query_datastore`, and on the console
`get_run_error`...) keep working beside these.

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
permissions and a mandatory expiry (90 days by default). Read-only means
permissions that guard read endpoints alone: `brain:read`, `scripts:read`,
`runs:read`, `runs:output` and `datastores:read`, not `brain:write`. Its endpoint is
`POST /mcp/k/<url-token>/`. It cannot be used as a REST/bearer token. The URL itself
is a credential: keep connector configuration private and redact that route from
reverse-proxy access logs. Because the token travels in the request path, reverse
proxies, CDNs and PyRun's own access log record it unless you redact it; that is why
URL tokens are read-only and always expire. Use a public HTTPS address for remote connectors.
Disabling URL tokens or revoking a token closes that route immediately.

## Capacity and failures

Calls have a cooperative budget; reader and vector backends receive bounded
timeouts. Every inline MCP call occupies a web-worker slot, and so does a
`scripts.run` that waits (capped, see
[Running and stopping scripts](#running-and-stopping-scripts)). Connect shows the
configured pool and budget. The console reader instead uses the existing worker
queue and a polling result page. Authentication failures are throttled by IP;
endpoint limits use the shared cache and principal ID.

A 404-shaped unknown Brain/note response can mean a missing resource, absent grant,
binding mismatch or insufficient tier. Check those in the console instead of
probing other workspaces. For 429, honor `Retry-After`. For reader/provider errors,
check Services, Brain settings, cost caps and Workers, then retry. Never share
provider credentials or raw authorization headers in logs or support reports.
