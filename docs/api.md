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
[`datastores.set`, `datastores.delete`](#datastores),
[`vectors.upsert` and `vectors.delete`](#vectors), and
[`tables.insert`, `tables.update` and `tables.delete`](#tables). Every write call is recorded
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
schemas and bounds. A Brain's vector collection is reached through `brain.search`
only, never through the [`vectors.*` endpoints](#vectors).

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
once across the instance, synchronous webhook calls included (see
[run-context.md](run-context.md#synchronous-replies); set
`PYRUNNER_SYNC_WAIT_SLOTS`; it is capped below
`GUNICORN_WORKERS` × `GUNICORN_THREADS`, and 0 turns waiting off). One web
process also holds at most `GUNICORN_THREADS` − 1 waits (3 by default), so
each process keeps a thread for the console and for the calls the waited-for
runs make back to PyRun; a call past either limit gets `busy` at once. A wait also ends 10 seconds inside `GUNICORN_TIMEOUT`,
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

## Databases

Read a workspace's managed databases (**Databases** in the console): list them,
see their tables and columns, and run one read-only `SELECT`.

| REST endpoint | Parameters | Permission |
|---|---|---|
| `databases.list` | None | `databases:read` |
| `databases.tables` | `database`; optional `table` (that table, with its columns) | `databases:read` |
| `databases.query` | `database`, `sql` (one statement, up to 20,000 characters) | `databases:read` |

```sh
curl -H "Authorization: Bearer $PYRUN_TOKEN" -H "Content-Type: application/json" \
  -d '{"database":"leads","sql":"SELECT count(*) FROM leads WHERE created_at >= current_date - 7"}' \
  https://pyrun.example/api/v1/databases.query
```

- `databases.list` returns `{"databases": [...], "count": n, "data_server": {...}}`,
  each with `name`, `description`, `status`, `created_at` and `updated_at`.
  `data_server.configured` says whether this instance has a data server
  (`PYRUNNER_DATA_DB_URL`). Without one (a SQLite-only instance, say) the list
  still shows the databases, `data_server.reason` says why they can't be read,
  and `databases.tables` and `databases.query` answer 409 `not_configured`. A
  database that isn't ready answers 409 `not_ready`.
- `databases.tables` returns `tables`, each with `name`, `row_estimate`,
  `row_exact` (false: the planner's estimate) and `size_bytes`. With `table`, it
  returns that one table with its `columns` (`name`, `type`, `nullable`,
  `default`, and `primary`: whether the column is part of the primary key).
- `databases.query` runs `sql` as the database's own role, so tables are named
  without a schema and nothing outside the database is reachable. It accepts one
  statement that starts with `SELECT`, `WITH`, `VALUES` or `TABLE` (a trailing
  semicolon is fine) and refuses anything else, and calls such as `set_config()`
  or `pg_terminate_backend()`, with 400 `invalid_input`. The transaction is
  read-only, so Postgres itself refuses any write. A statement stops after 5
  seconds (400 `query_timeout`); a server error is 400 `query_failed` with
  Postgres's message. It answers `columns`, `rows` (lists, in the order of
  `columns`), `row_count`, `truncated` and `row_cap`: at most 200 rows come back,
  and `truncated` says there were more. Count and filter in SQL. Values come back
  as JSON: `numeric` as text, dates and times as ISO text, `bytea` as `\x` + hex.
  It takes 30 calls a minute per token.
- Rows are returned as stored, like datastore values: they aren't masked.
- A token's binding (one Brain, script or datastore) narrows its own kind only:
  a token with `databases:read` reads every database in its workspace, and one
  with `vectors:read` or `vectors:write` reaches every collection, whatever the
  token is bound to.
- If the data server can't be reached, the answer is 503 `unavailable`; the
  server's own message goes to PyRun's log, not to the caller.

## Vectors

Search a workspace's vector collections and, while
[API writes](#endpoint-classes-and-api-writes) are on, upsert and delete points.

| REST endpoint | Parameters | Permission |
|---|---|---|
| `vectors.list` | None | `vectors:read` |
| `vectors.search` | `collection`, `query` (text); optional `k` (1–50, default 5), `filter` | `vectors:read` |
| `vectors.upsert` | `collection`, `items` (up to 100) | `vectors:write` |
| `vectors.delete` | `collection`, and `ids` (up to 1,000) or `filter` | `vectors:write` |

- `vectors.list` returns `collections`, each with `name`, `description`,
  `backend`, `status`, `points`, `dimension`, `metric`, `embedding_model`,
  `text_search` (whether it can be searched by text), `created_at` and
  `updated_at`.
- `vectors.search` embeds `query` with the collection's own embedding provider
  and returns `results` (`id`, `score`, `payload`, `text`; a higher score is
  closer) and `count`. The embedding call is counted on the collection and its
  provider, as on every other path (the Vectors and Services pages show the
  totals). A collection without an embedding provider answers 400 `invalid`. It
  takes 30 calls a minute per token.
- `filter` uses the payload filter grammar of `pyrunner_vectors`:
  `{"field": value}`, `{"field": {"$in": [...]}}`, `$gt`/`$gte`/`$lt`/`$lte`,
  `$prefix`, and `{"must": [...], "must_not": [...]}` (see `docs/plugins.md`).
- `vectors.upsert` inserts or replaces points by `id`. Give every item a `text`
  (embedded with the collection's provider; the answer's `tokens` counts them)
  or every item a `vector` of the collection's dimension (`text` is then
  optional); `payload` is an optional object. `vectors.delete` deletes by `ids`
  or by a non-empty `filter`, not both. Both answer the collection's `points`
  after the write, take 30 calls a minute per token, and never create or delete a
  collection: do that in the console.
- A collection a Brain uses for semantic search isn't reachable here (404
  `not_found`, as if it didn't exist): its points hold the Brain's notes at every
  tier. Read them through the [Brain operations](#brain-operations), which check
  the tier.
- Points are returned as stored, like datastore values: they aren't masked.
  Errors keep the vector codes (`unsupported_filter`, `dimension_mismatch`,
  `embedding_failed` (502), `backend_unavailable` (503), `not_ready` (409)...).

## Tables

Read a workspace's [tables](run-context.md#tables) and, while
[API writes](#endpoint-classes-and-api-writes) are on, insert, change and delete
their rows. A table is named by its slug (or its name).

| REST endpoint | Parameters | Permission |
|---|---|---|
| `tables.list` | None | `tables:read` |
| `tables.get` | `table` | `tables:read` |
| `tables.rows` | `table`; optional `where`, `order`, `limit` (1–1,000, default 100), `offset` | `tables:read` |
| `tables.insert` | `table`, `rows` (1–1,000 objects of column: value) | `tables:write` |
| `tables.update` | `table`, `id`, `values` (column: value; `null` empties a cell) | `tables:write` |
| `tables.delete` | `table`, `ids` (up to 1,000) | `tables:write` |

- `tables.list` returns `tables`, each with `name`, `slug`, `description`,
  `columns` (`key`, `type`, `required`), `row_count`, `owner_plugin`,
  `created_at` and `updated_at`; `tables.get` adds each column's `default` and
  the table's `row_cap`.
- `tables.rows` returns `rows` (each with `id`, every column, `created_at` and
  `updated_at`, UTC) and `count`, how many rows match in all (`limit: 1` just
  counts). `where` and `order` work as in `pyrunner_tables`:
  `{"field": value}` or `{"field": {"$op": value}}` with `$eq`, `$ne`, `$gt`,
  `$gte`, `$lt`, `$lte`, `$in`, `$contains`, `$prefix`, `$empty`, over the
  column keys plus `id`, `created_at` and `updated_at`; `order` is a field,
  `-field`, or a list. For example, rows added since Monday:
  `{"table": "leads", "where": {"created_at": {"$gte": "2026-11-09"}}, "limit": 1}`.
- Writes go through the same checks as the console and scripts: values are
  converted to their column's type, unknown columns and missing required values
  are refused (400 `invalid`), and rows past the table's row limit answer 409
  `row_cap`. An insert is all or nothing. The writes take 30 calls a minute per
  token and never create, change or delete a table or its columns: do that in the
  console (or, for a plugin's table, in the plugin).
- A token can be bound to one table on the token form: it then reaches that
  table only (`table` may be left out) and gets 403 `forbidden` for any other.
- Plugin tables are listed and writable here, as on the Tables page. Rows are
  returned as stored, like datastore values: they aren't masked.

## Workspace inventory and health

Describe the rest of a workspace, and the instance it runs on: providers, secret
names, chat channels, connections, plugins, environments, libraries, schedules,
watchers, a script's code, and whether everything is OK. All are `read` endpoints, each with its own
permission.

| REST endpoint | Parameters | Permission |
|---|---|---|
| `providers.list` | None | `providers:read`, on a token a superuser created |
| `secrets.names` | Optional `script` (its id: only the secrets it gets) | `secrets:read_names` |
| `channels.list` | None | `channels:read` |
| `connections.list` | Optional `script` (its id: only the connections it may use) | `connections:read` |
| `plugins.list` | None | `plugins:read`, on a token a superuser created |
| `environments.list` | Optional `environment` (a name or id: that one, with its installed packages) | `environments:read` |
| `libraries.list` | None | `libraries:read` |
| `schedules.list` | Optional `active` (only schedules that are on, or off) | `schedules:read` |
| `watchers.list` | Optional `script` (its id: only its watchers) and `enabled` (only watchers that are on, or off) | `watchers:read` |
| `scripts.code` | `script` (its id); optional for a token bound to one script | `scripts:read_code` |
| `system.health` | None | `system:read` |

```sh
curl -H "Authorization: Bearer $PYRUN_TOKEN" https://pyrun.example/api/v1/system.health
```

None of them returns a secret value, a provider's key or base URL, a channel's
bot token, inbound token or signing secret, a connection's key, token or
password, or a library's code. AI and
embedding providers, plugins and environments belong to the instance, so every
workspace lists the same ones; usage, scripts, channels, libraries, schedules,
watchers and runs are the token's workspace's. As in the console, which shows providers
and plugins to superusers only, `providers.list` and `plugins.list` answer only
a token created by a superuser: any other token gets 403 `forbidden`, even with
the permission, and a token stops answering when its creator is no longer a
superuser. `environments.list` answers any token with `environments:read`. Py AI
reads them by its tiers below.

- `providers.list` returns `ai` (`enabled`: the AI switch in Services;
  `default`: the provider scripts, Py AI and Brains use; `providers`, each with
  `name`, `type`, `model`, `default` and `tested_at`; and `usage_today`: the
  calls and tokens this workspace's scripts and Brains used since midnight in the
  instance's time zone, by model; Py AI's own calls aren't counted) and
  `embedding.providers` (`name`, `type`, `model`, `dimension`, `enabled`,
  `tested_at`).
- `secrets.names` returns `secrets`, each with `name`, `owner_plugin` (the
  plugin that owns it, or null), `source` (`local` or `external`),
  `description`, `scripts` (the scripts that get it as an environment variable,
  up to 50) and `script_count`. The selection is the one a run makes: a script
  in *All* mode gets every secret that no plugin owns, a script in *Selected*
  mode its attached secrets and, for a plugin's script, the plugin's own; secrets
  made before workspaces existed (with no workspace) reach every workspace's
  scripts and are listed too. With `script` the list is that script's secrets
  and `script` names it with its `injection_mode`: "does this script have a
  SERPER key?". With no encryption key configured nothing is injected, so every
  `script_count` is 0.
- `channels.list` returns `channels`, each with `name`, `provider`, `enabled`,
  `configured`, `inbound` (`enabled`, `handler`: `script` or `pyai`, the
  `script` it runs, and `access`), `members` (counts by status:
  `pending`, `approved`, `blocked`), `daily_reply_cap`, `daily_cap_reached`,
  `daily_send_cap`, `send_cap_reached` (the cap on every message the channel
  sends a day, notifications included; null = no cap), `last_error` (with the channel's own credentials masked), `last_tested_at`
  and `last_inbound_at`. Members' ids and names aren't returned.
- `connections.list` returns `connections`, each with `name`, `provider`,
  `provider_label`, `auth_kind` (`oauth2`, `api_key`, `bearer`, `basic`, `smtp`),
  `status` (`ok`, `error`, `revoked`, `expired`), `account_label`,
  `last_tested_at`, `last_error` (with the connection's own values masked),
  `grant_all_scripts`, `owner_plugin`, `scripts` (the scripts it is granted to,
  explicitly or by **Grant to all my scripts**, up to 50) and `script_count`.
  With `script`, the list is the connections that script may use. Scripts use
  connections through `pyrunner_connections` (`docs/run-context.md`).
- `plugins.list` returns `plugins`, each with `slug`, `name`, `version`, `status`
  (`active`, `installed`, `disabled`, `errored`), `source`, `summary`, `author`,
  `trust` (`verified` or `unsigned`), `signed_by` (the signing publisher, or
  null), `installed_at` and `activated_at`.
- `environments.list` returns `environments`, each with `id`, `name`,
  `description`, `python_version`, `default`, `active`, `requirements` (the
  saved requirements: PyRun stores the environment's `pip freeze` after every
  package change) and `scripts` (how many of this workspace's scripts use it).
  With `environment`, that one environment also has `packages` (`name`,
  `version`), read from the environment itself (`pip list`, as the console's
  environment page does); two environments with the same name answer 400
  `invalid_input`: pass the id. It takes 30 calls a minute per token.
- `libraries.list` returns `libraries`, each with `key` (its import name),
  `name`, `description`, `owner_plugin`, `version`, `modules` (file names) and
  `scripts` (the scripts it is attached to).
- `schedules.list` returns `schedules` (scripts whose schedule isn't manual),
  each with `script` (`id`, `name`, `enabled`), `mode`, `active`, `summary`,
  `timezone`, `next_run` and `last_run_at`, and `paused`: the **Pause all**
  switch in Settings, which stops every schedule while it is on.
- `watchers.list` returns `watchers` (see [Watchers](scheduling.md#watchers)),
  each with `id`, `name`, `adapter` (`key`, `title`), `script` (`id`, `name`),
  `connection` (its name, or null), `interval_seconds`, `fire_cap_per_hour`,
  `enabled`, `auto_disabled_at` (set when it turned itself off after 10
  failed checks in a row), `last_checked_at`, `last_event_at`, `next_check_at`,
  `error_count` (failed checks in a row) and `owner_plugin`. A watcher's
  settings, state and input map are never returned (a polled URL can hold a
  key), nor its last error text: the console's watcher page shows it.
- `scripts.code` returns the script's `code` as saved, with the values of the
  secrets it gets (and the AI credential its runs get) masked as
  `[NAME:MASKED]`, and `lines`. When a secret can't be resolved (an external
  provider is down), `code` is null and `withheld` says so. `scripts.get` still
  never returns code.
- `system.health` returns `ok`, `issues` (what needs attention, in sentences:
  no worker running, schedules paused, a maintenance window on, the last
  scheduled backup failed, plugins that failed to load, runs that failed or timed
  out in this workspace in the last 24 hours) and the details: `worker`
  (`alive`, `status`, `configured_workers`, `heartbeat_at`, `tasks_waiting`),
  `schedules` (`paused`, `paused_at`), `maintenance` (the windows on now for this
  workspace, `until`, `until_local`, `blocks`), `backup` (the last scheduled
  backup: `scheduled`, `schedule`, `next_run`, `last_run_at`, `last_status`,
  `last_size_bytes`; a backup downloaded from the console isn't recorded),
  `version` (`running`, `latest`, `update_available`, `checked_at`, from the
  daily update check), `plugins.errored` and `runs` (`running`, `waiting`,
  `failed_last_24h`). Error texts aren't returned; the console's pages show them.
- A token bound to one script sees that script only, wherever these name
  scripts (`secrets.names`, `channels.list`, `connections.list`, the `scripts` counts of
  `environments.list`, `libraries.list`, `schedules.list`, `watchers.list`), and `scripts.code`
  reads that script alone. Its binding doesn't narrow anything else, as for
  databases and vectors.

## Documentation search

`help.search` (`help:read`) searches the documentation of the running version:
the README, the guides in `docs/` (`api.md`, `run-context.md`, `scheduling.md`,
`plugins.md`, `brain.md`), the changelog and, when there are any, the
integration pages in `docs/integrations/`. These files ship in the Docker image
for it. Pass `query` (a few words or a question, up to 200 characters) and
optionally `limit` (1 to 10, default 5).

```sh
curl -G -H "Authorization: Bearer $PYRUN_TOKEN" https://pyrun.example/api/v1/help.search \
  --data-urlencode "query=webhook signature"
```

It returns `query`, `version` (the running version), `docs_bundled` and
`results`, best first, each with `title` (the section's heading), `section`
(the headings above it and its own, as in `Scheduling scripts › Maintenance
windows`), `doc` (the file), `link` (that section on GitHub, at the running
version's tag) and `snippet` (the section, or about 1,000 characters of it from
the paragraph that matches best; `…` marks a cut).

The search goes by words: a section ranks higher the more of the query's words
it has, in its heading most of all, and plurals and singulars match each other.
A changelog entry counts half, so a guide comes before the entry that announced
its feature. When the query names a page (every word of the page's title is in the
query, or every word of the query in the title, give or take endings: "how do I
schedule a script?" and "Scheduling scripts"), the page's introduction counts double, so it comes before the page's
own subsections. The index is built once per process (the web server builds it when
it starts, a worker on its first search) and kept in memory; it reads no
database and nothing of the token's workspace. Where the docs aren't there (a
checkout without them), `docs_bundled` is false, `results` is empty and the
server logs it once.

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
| `channel` | Py AI on a chat channel (Telegram, WhatsApp), whose approved senders need not be owners or admins | `scripts.list`, `scripts.get`, `runs.list`, `runs.get`, the four datastore reads, `databases.list`, `databases.tables`, `vectors.list`, `vectors.search`, `plugins.list`, `environments.list`, `libraries.list`, `schedules.list`, `watchers.list`, `system.health`, `help.search`, and the Brain reads |

So a chat channel can't read a run's output (`runs.output`), run a database
query (`databases.query`), read a script's code (`scripts.code`) or list secret
names (`secrets.names`), providers (`providers.list`), channels
(`channels.list`) or connections (`connections.list`); the console can.
The tier is checked again on every call, not only when the tools are listed.
Datastore values reach both tiers as stored, unmasked, as Py AI's
`query_datastore` tool already gave them to chat channels; so do vector points.
A `vectors.search` from Py AI embeds the query like any other call.
Brain endpoints also need the agent's own Brain grant (`agent:pyai` for the
console, `<provider>:<channel UUID>` for a channel). Like an API token, Py AI
sees the person behind a manual run as `user: {"id"}`, never the email. Py AI's
older tools (`list_scripts`, `query_datastore`, and on the console
`get_run_error`...) keep working beside these. Asked how to do something in
PyRun, Py AI searches the docs with `help.search` and gives the link of each
section it answers from.

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
`scripts:read_code`, `runs:read`, `runs:output`, `datastores:read`,
`databases:read`, `vectors:read`, `secrets:read_names`, `providers:read`,
`channels:read`, `plugins:read`, `environments:read`, `libraries:read`,
`schedules:read`, `watchers:read`, `system:read` and `help:read`, not `brain:write`. Its endpoint is
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
