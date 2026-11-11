# Changelog

All notable changes to PyRun are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The authoritative version number lives in `pyrunner/version.py`. This changelog
begins tracking at the current release; earlier history is in the git log.

## [Unreleased]

### Added
- **Script inputs.** A script can declare what it takes on the new **Inputs**
  tab of the editor: text, number, yes/no, select and JSON fields, or a raw
  JSON Schema. **Run now** then asks for them in a form. Inputs are checked
  before a run is created, so a missing or wrong value comes back as an error
  on the field and never as a failed run. Each text, number and yes/no value
  reaches the script as an `INPUT_<NAME>` environment variable (a secret with
  the same name still wins). The run page shows the inputs it ran with, with
  secret values masked, and offers **Re-run with same inputs**. Schedules,
  webhooks and chat channels use each input's default; a schedule can't be
  turned on while a required input has no default, and a webhook call to such
  a script gets a 400 that names the inputs. Scripts without inputs work
  exactly as before.
  The form lists inputs in the order the script declares them, and a run's
  inputs, result and event keep the order they were written in, on PostgreSQL
  as on SQLite. On PostgreSQL the upgrade stores these values as `json` (which
  keeps key order) instead of `jsonb`, which rewrites the scripts and runs
  tables once, so a long run history makes that migration take a little longer.
- **Run results and live progress.** The new `pyrunner_run` script helper
  reads a run's inputs (all of them, JSON inputs included) and its id, and lets
  the script report a structured result (`set_result({...})`, a JSON object up
  to 256 KB) and its progress (`progress(40, "Fetching page 3")`). The run page
  shows the result in a **Result** card with a download link, and the progress
  bar updates while the run is live without reloading the page. The result is
  also in the plugin SDK's `RunView` (with `inputs` and `progress`), in webhook
  notification payloads (`result`, `result_too_large` over 16 KB) and as an
  excerpt in email and chat notifications. Secret values are masked before the
  result or progress message is stored. Every run now gets
  `PYRUNNER_RUN_FILE` (a `run.json` with the run, script, trigger and inputs)
  and `PYRUNNER_RUN_ID`, `PYRUNNER_SCRIPT_ID` and `PYRUNNER_SCRIPT_NAME`; the
  three ids used to be set only when an AI provider was configured. Guide:
  `docs/run-context.md`.
- **One event for every trigger.** Whatever starts a run (Run now or Re-run,
  a schedule, a webhook call, a chat message, a plugin) now records what
  started it, in one shape: `kind`, `received_at`, and the trigger's own data
  (a webhook's method, query, content type and body; the chat message with its
  sender; the schedule and when it fired; who clicked Run now). Scripts read it
  with `pyrunner_run.event()`, and the run page shows it in an **Event** card
  with a download, secret values masked. An event over 1 MB is cut with an
  explicit `truncated` / `body_truncated` flag instead of failing the run.
  `WEBHOOK_*` and `INBOUND_*` variables work exactly as before. Backups include
  the event.
- **Inputs and results in the plugin SDK.** `ScriptAPI.upsert(inputs_schema=)`
  declares the inputs a plugin's script takes, checked the way the editor's
  Inputs tab checks them (`{}` removes them). `ScriptAPI.queue_run(inputs=)`
  starts a run with values for them; bad values raise the new `InputsError`,
  with a reason per input, and create no run. `ScheduleAPI.sync` refuses any
  mode but manual while a required input has no default, and `upsert` refuses
  such a schema while the script has an active schedule. The SDK Showcase
  example gains a **Run with inputs** card that queues a run with inputs and
  shows its live progress and result from `RunView`.
- **What happens when a script is already running.** A new **If it's already
  running** setting in the script editor: **Allow** (the default, today's
  behaviour) starts another run alongside it; **Skip** creates the new run as
  cancelled with a **skipped: already running** chip on the runs list and the
  run page, and runs nothing; **Queue** keeps it waiting and starts it when the
  run ahead of it ends, one at a time and in order, with the inputs, webhook
  body or chat message it arrived with. It applies to every way a script
  starts: Run now, Re-run, schedules, webhooks, chat channels and plugins. A
  skipped webhook call still gets a 200, with `"status": "skipped"`; a skipped
  chat message gets a reply saying so. A waiting run can be cancelled from its
  page. The plugin SDK takes it as `ScriptAPI.upsert(concurrency_policy=)`, and
  `RunView` gains `cancel_reason` and `is_held`. The Brand Tracker and YouTube
  Comments AI examples now use Skip instead of their own "a run is already in
  progress" checks, so their schedules can no longer overlap a manual run
  either. Backups and forks include the setting. Guide: the "When the script
  is still running" section of `docs/scheduling.md`.
- **Retry on failure, with backoff.** New script settings: **Retries on
  failure** (0 by default, up to 10), a **delay** before the first retry
  (60 seconds), a **backoff multiplier** (each retry waits that many times
  longer: 2 gives 1x, 2x, 4x; never more than a day) and **Retry timeouts
  too** (off). A failed run creates its next attempt as a pending run with a
  **retry at HH:MM** chip, carrying the same inputs and webhook or chat data;
  the worker starts it when it is due, checking once a minute, so delays
  under a minute act like about a minute. Retries obey the script's
  concurrency setting. Stopped and skipped runs never retry. Only the last
  failed attempt (or the first success) sends a notification. The run page
  shows **attempt 2 of 3 · retry of #…**, and a waiting retry can be
  cancelled from its page. Scripts can read `run.attempt` from `run.json`.
  Backups and forks include the settings. Guide: the "When a run fails:
  retries" section of `docs/scheduling.md`.
- **Maintenance windows.** Plan a pause instead of remembering to press
  **Pause all**: a one-off window (from one moment to another) or a weekly
  one (a time of day on chosen weekdays, across midnight if you like), in its
  own time zone, with daylight saving handled. Instance admins set global
  windows under **Settings → Schedules**; a workspace's Owners and Admins set
  that workspace's own from **Workspaces → Maintenance**. Each window lists
  its next occurrence. While a window is on, the triggers it blocks don't
  start runs: scheduled runs are skipped (not made up later), webhook calls
  get `503` with `Retry-After`, chat messages get a reply, and, if you tick
  them, manual and API runs show a message. Every refused trigger is listed
  as a cancelled run with a **skipped: maintenance window** chip and a note
  naming the window; it sends no notification and never retries. Runs
  already going are never stopped. The dashboard shows **Maintenance until
  ...** while a window is on. Plugins: `queue_run` returns such a run with
  `cancel_reason == "maintenance"`. Backups include the windows. Guide: the
  "Maintenance windows" section of `docs/scheduling.md`.
- **Graceful stop before kill.** Stopping a run (the Stop button, a plugin's
  `cancel_latest_run`) and a timeout no longer kill the script straight away.
  It first gets SIGTERM (CTRL_BREAK on Windows) and the script's new **Stop
  grace period** (Execution box, 10 seconds by default, 0 to 60) to shut down,
  then whatever is still running is killed. Only the script's own processes
  are signalled, never PyRun's worker. Scripts can register cleanup with
  `pyrunner_run.on_stop(callback)`: save work, report a partial result, then
  exit. A stopped run stays running with a **stopping** chip until the
  script has ended, then turns cancelled and keeps what the script printed
  (before, a stopped run's output was lost); its stderr ends with
  `[Stopped by user after graceful shutdown]` or `[Killed by user after the
  10 s grace period]`, and a timeout adds the same kind of line under its
  TIMEOUT message. **Kill now** on a stopping run kills it at once. The run
  records when the stop was asked for (`stop_requested_at`, also on the
  plugin SDK's `RunView`). A grace period of 0 keeps the old behaviour. The
  setting is forked and backed up. Guide: "Stopping a run" in
  `docs/run-context.md`.
- **Per-script env vars.** A new **Env vars** tab in the script editor holds
  plain settings for the script (a region, a base URL, a feature flag), one
  name and value per row. Every run gets them as environment variables, below
  inputs and secrets: a secret with the same name always wins, and the editor
  warns when a name matches a secret the script gets. Values are shown as
  typed and never masked, so keys and tokens stay in Secrets. Names are
  checked (letters, digits and underscores; not `PATH`, `PYTHONPATH` and the
  other names secrets can't use; nothing starting with `PYRUNNER_`). The
  script page lists the names. Forks and backups include them. Guide: "Script
  env vars" in `docs/run-context.md`.
- **Plugin schedules with several times, and monthly.** The plugin SDK's
  `ScheduleAPI.sync` now takes the same choices as the editor's schedule form:
  `times=["09:00", "18:00"]` for several runs a day, `weekdays=[0, 2, 4]` in
  weekly mode, and monthly mode with `month_days=[1, 15]` (before, a plugin's
  monthly schedule had no days or times and never ran). It saves the same
  schedule the form saves. `time_str` and `weekday` still work and are now
  checked too. A bad value (an invalid time or day, a repeat, a list the mode
  doesn't use, an unknown mode) raises `ValueError` naming it, and nothing is
  saved. Guide: "Scheduling from a plugin" in `docs/scheduling.md`.
- **API writes switch.** Every Unified API / MCP endpoint is now one of three
  classes: `read`, `run` (acts without changing stored data, always available
  with its permission) or `write` (changes stored data). A new superuser
  setting, **Allow API writes** in **Settings → API & MCP** (the API tab is
  renamed), is off by default. While it is off, write endpoints are left out of
  `_catalog`, `_openapi.json`, MCP `tools/list` and Py AI, a call to one gets
  403 `writes_disabled`, and the token form shows write permissions disabled
  with the reason. Turned on, write permissions can be granted per token. A
  token that already holds one keeps it, but its write calls are refused while
  the switch is off. **Connect** shows the current mode. The first write
  endpoints are the datastore ones (`datastores.set` and `datastores.delete`,
  below). `brain.propose-feed` is a `run` endpoint (a proposal waits for
  approval), so it works whatever the switch says, and the catalog now lists
  its `access` as `run` instead of `write`. Backups include the switch; an
  older backup leaves it off. Guide: "Endpoint classes and API writes" in
  `docs/api.md`.
- **Scripts and runs over the API and MCP.** Five new read endpoints:
  `scripts.list` (search by name or description, tag, enabled), `scripts.get`
  (metadata, inputs schema, schedule and last run; never the code),
  `runs.list` (by script, status and start time), `runs.get` (status, timing,
  inputs, result, progress and event) and `runs.output` (stdout or stderr,
  optionally only the last bytes; plain text over REST). They need the new
  token permissions **Read scripts** (`scripts:read`), **Read runs**
  (`runs:read`) and **Read run output** (`runs:output`), which MCP URL tokens
  may hold too. A token can be bound to one script: it then sees that script
  and its runs only, and it is deleted with the script (and by a backup
  restore, which the restore preview says). Output is what the run page shows,
  secrets masked, byte for byte. Inputs and the event are masked like on the
  run page; API and MCP callers see who started a manual run by user id only,
  never the email (scripts and the console still get the email). Connect gains
  curl and Claude Code examples. Guide: "Scripts and runs" in `docs/api.md`.
- **Run and stop scripts over the API and MCP.** `scripts.run` starts a script
  with `inputs`, checked against its inputs schema (a bad input is refused and
  no run is created). With `"wait": true` it waits up to `wait_seconds` (at most
  55) and returns the result when the run ends in time; otherwise, or past the
  wait, it answers HTTP 202 with the run id to poll with `runs.get`. A wait
  fails fast with 503 `workers_unavailable` when no worker is running, and at
  most 4 calls wait at once (`PYRUNNER_SYNC_WAIT_SLOTS`), so waiting calls can't
  take the console's web threads: past the cap a call is queued and answered at
  once. A maintenance window that blocks API runs answers 503 with
  `Retry-After`, as for a webhook. `runs.stop` is the run page's Stop: the
  script gets its grace period, and a second call kills it (runs on this
  container's workers only in 2.5). New token permissions **Run scripts**
  (`scripts:run`) and **Stop runs** (`runs:stop`), never on MCP URL tokens;
  each endpoint takes 30 calls a minute per token. Every run and stop, and
  every failed call made with a token, is recorded (endpoint, status, time, IP,
  request id) and listed on the token's new **Activity** page in Settings →
  API Tokens; records are kept 30 days, deleted by a new daily housekeeping
  task. Guide: "Running and stopping scripts" in `docs/api.md`.
- **Scripts as MCP tools.** Turn on **Expose as MCP tool** in a script's editor
  (new **MCP tool** card) and the script shows up in MCP clients such as Claude
  Code as a tool of its own, named by its tool name (lowercase letters, digits
  and underscores, unique among the workspace's tools; left blank, it comes
  from the script's name). The script's inputs are the tool's parameters.
  Calling the tool runs the script as `scripts.run` with `wait` does and
  returns its result, or, for a run still going after the wait, its run id to
  poll with `runs.get`; tool calls count against the same 30 calls a minute and
  are recorded on the token's Activity page. Only tokens that may run the script
  see the tool: they need **Run scripts** (`scripts:run`), and a token bound to
  one script sees that script's tool only. Disabled and archived scripts aren't
  listed. `_catalog` lists the tools under `script_tools` (`kind: "script"`),
  the script page shows the tool name, a fork is never a tool, and backups keep
  the setting. Connect explains it with a Claude Code example. Guide: "Scripts
  as MCP tools" in `docs/api.md`.
- **Datastores over the API and MCP.** `datastores.list`, `datastores.get`,
  `datastores.entries` and `datastores.entry` read a workspace's datastores and
  entries with the new token permission **Read datastores** (`datastores:read`,
  which MCP URL tokens may hold too). `datastores.set` and `datastores.delete`
  set and delete one entry with **Write datastore entries** (`datastores:write`):
  they are write endpoints, so they work only while API writes are on, and every
  call is recorded on the token's Activity page. A write never creates a
  datastore. A token can be bound to one datastore with the token form's
  **Datastore** field: it then reaches that store only. The datastore API
  (`/api/v1/datastores/…`) keeps working for datastore tokens with the same
  answers; its URLs now run the same code, and a single-store token is bound to
  its store the same way. A datastore token whose workspace no longer exists is
  now refused (403 `SCOPE_MISMATCH`) instead of reading the data stores that
  belong to no workspace, and no token reaches one of those by its name any
  more. Deleting a datastore in the console now says how many API tokens bound
  to it go with it. Guide: "Datastores" in `docs/api.md`.
- **Py AI reads scripts, runs and datastores through the API's endpoints.**
  Py AI now gets the API's read endpoints as tools of its own (`scripts-get`,
  `runs-get`, `runs-output`, `datastores-entries`...), with the same
  descriptions, inputs and answers as over REST and MCP (secrets masked the
  same way), so it can look at a script's inputs and schedule, a run's result
  and progress, and any datastore entry. Each endpoint says which Py AI may
  call it: the console's Py AI (workspace owners and admins) gets every read
  endpoint, while Py AI on a chat channel such as Telegram can't read a run's
  output. It still can't run, change or delete anything, and its older tools
  keep working. Guide: "Py AI" in `docs/api.md`.
- **Databases and vector collections over the API, MCP and Py AI.** New read
  endpoints list a workspace's managed databases (`databases.list`), their
  tables and columns (`databases.tables`), and run one read-only `SELECT`
  (`databases.query`: the database's own role, a read-only transaction, 5
  seconds and 200 rows at most; anything but a single `SELECT` is refused), with
  the new `databases:read` permission. `vectors.list` and `vectors.search` (a
  similarity search by text, embedded with the collection's provider and counted
  on it) take `vectors:read`, and, while API writes are on, `vectors.upsert` and
  `vectors.delete` take `vectors:write`. A collection a Brain uses for search
  isn't reachable through them. Py AI can now answer questions such as "how
  many rows were added to leads this week?": the console's Py AI gets all the
  reads, while Py AI on a chat channel lists databases, tables and collections
  and searches them but can't run a query. On an instance without a data server
  the databases are still listed, with the reason they can't be read. Guide:
  "Databases" and "Vectors" in `docs/api.md`.
- **Py AI and the API know the rest of the workspace, and whether all is well.**
  Nine new read endpoints, each with its own permission: `providers.list` (AI and
  embedding providers, the default one and this workspace's AI usage today),
  `secrets.names` (secret names and the scripts that get each one, or the
  secrets one script gets), `channels.list`, `plugins.list`, `environments.list`
  (with one environment's installed packages), `libraries.list`,
  `schedules.list`, `scripts.code` (`scripts:read_code`, with the values of the
  script's secrets masked) and `system.health` (worker, tasks waiting, paused
  schedules, maintenance windows, the last scheduled backup, version and update,
  plugins that failed to load, failed runs, and a list of issues). None returns
  a secret value, a provider key or a channel token. Py AI can answer "is
  everything OK?" and "does this script have a SERPER key?": the console's Py AI
  gets all of them, while Py AI on a chat channel gets plugins, environments,
  libraries, schedules and system health but never code, secret names,
  providers or channels. As in the console, `providers.list` and `plugins.list`
  answer only a token a superuser created (403 otherwise); `environments.list`
  answers any token with its permission. Guide: "Workspace inventory and
  health" in `docs/api.md`.
- **Py AI and the API search PyRun's own docs.** `help.search` (`help:read`)
  finds the sections of the running version's documentation (the README, the
  guides in `docs/`, the changelog) that answer a question, each with a snippet
  and a link to that section at the running version's tag on GitHub. These docs
  now ship in the Docker image, and the index is built in memory when the
  server starts, with no new dependency. Asked "how do I…?", Py AI searches them
  and answers with the link, on the console and on chat channels. A question
  that names a page ("how do I schedule a script?") gets that page's
  introduction first. Guide: "Documentation search" in `docs/api.md`.
- **Webhook calls hand the script the whole request, any size.** A webhook
  run's event now also has the request's `path`, `remote_ip`, `headers`
  (lower-case names; `Authorization`, `Cookie` and hop-by-hop headers left
  out) and `body_size`, and the body exactly as sent is in a file at
  `PYRUNNER_EVENT_BODY_FILE`, bytes that aren't UTF-8 text included (the event
  says `body_binary`). A body too big for one environment variable (Linux caps
  one at 128 KB) used to stop the run from starting with "Argument list too
  long": the run now starts with `WEBHOOK_BODY` empty and
  `WEBHOOK_BODY_OMITTED=1`, and reads the file. Smaller bodies get the same
  `WEBHOOK_*` variables as before. A body over 2.5 MB is refused with 413
  instead of starting a run with an empty body. The script page's Webhook card
  lists the last 20 calls with their run's status, duration and the HTTP code
  PyRun answered (a call's time is `14:32` today, else `Nov 9`; the full date
  is in its tooltip). Guide: "Webhook calls" in `docs/run-context.md`.
- **Webhooks can require a signature, and each script has its own rate limit.**
  A script's webhook can require every call to be signed the way Stripe,
  GitHub or Shopify sign theirs, or with an HMAC-SHA256 you describe (header,
  prefix, hex or base64, an optional timestamp header and its tolerance).
  PyRun checks it before any run exists: a missing or wrong signature gets 401
  with a `reason` and no run. The signing secret is stored encrypted and never
  shown again. Each script's webhook takes 60 calls a minute and 30 in any one
  second by default, both adjustable; over that, 429 with `Retry-After` and no
  run. The old limit of 30 calls a minute per address is now 300, a floor
  against scanning, while wrong URLs and bad signatures from one address are
  still cut off after 30 a minute. Set both under **Webhook → Signature and
  limits** on the script page. Backups carry the settings, the secret still
  encrypted; restoring a hand-edited one, a header name the console would
  refuse comes back blank (calls are refused until it is set again) and an
  unknown signature comes back as none without its secret, each with a
  warning. Guide: "Webhook signatures" and "Webhook rate limits" in
  `docs/run-context.md`, with recipes for Stripe, GitHub and Shopify.
- **A webhook call can wait for the script's reply.** Add `?sync=1` to the
  URL, or set the script's reply mode to Sync, and the call waits for the run
  and gets the reply the script sets with `pyrunner_run.respond(status, body,
  headers)`: its status (200 to 599), a text or JSON body, and a few safe
  headers. The reply is stored in the run's result, with secret values masked.
  A call waits at most the script's timeout or 55 seconds; past that it gets
  202 with the run's id and a `Location` for the API's `runs.get`. At most 4
  calls wait at once, together with the API's waits (the rest get 202 at
  once); with no worker running a waiting call gets 503, and a script that is
  already running under the Skip policy answers 409. Calls without `?sync=1`
  on a script left in Async mode are answered as before. Set the mode under
  **Webhook → Reply, signature and limits**. Guide: "Synchronous replies" in
  `docs/run-context.md`, with a Slack slash-command example.
- **Connections: call an outside service as an account.** Add an Airtable
  token, an API key, bearer token or username and password for any HTTP API,
  or an SMTP login under the new **Connections** page (sidebar), **Test** it,
  and grant it to scripts: in the script's new **Connections** box, on the
  connection's page, or to all of its creator's scripts at once. Scripts use it
  with the new `pyrunner_connections` helper: `client(name)` gives an HTTP
  client with the credentials attached (a `requests.Session` when `requests` is
  installed), `smtp(name)` a logged-in SMTP connection, `token(name)` the bare
  key. Credentials are encrypted, belong to one workspace, never enter a
  script's environment (PyRun hands them over its internal API after checking
  the grant), are sent only to the connection's base URL, and are masked in
  the run's output, result and progress, including values handed out after the
  run started. Backups carry connections and their grants; the API's new
  `connections.list` (permission `connections:read`) and the console's Py AI
  list them without credentials. Guide: "Connections" in `docs/run-context.md`.
- **OAuth connections: Google, Microsoft 365, GitHub, Slack and Notion.** Add
  the provider's OAuth app (client ID and secret) under **Connections → OAuth
  apps**, for the workspace or, as a superuser, as the instance default, then
  add a connection and tick what it may access (Sheets, Gmail, Drive, Calendar,
  Outlook mail, repositories, posting to Slack...). **Connect** goes through the
  provider's consent screen and back to PyRun's one `/oauth/callback/`, with a
  signed state that expires after 10 minutes and PKCE; **Paste a token** takes
  a refresh token (or a token that doesn't expire) when the instance has no
  callback address your browser can reach, and checks it with the provider
  first. Scripts only ever get the access token (`pyrunner_connections.token()`
  and `client()`, which picks up a refreshed token by itself); the refresh token
  and client secret never leave PyRun. Tokens are refreshed when a script needs
  them, a minute early and one refresh at a time; a refresh the provider refuses
  marks the connection **Expired** and emails its creator. Backups carry the
  apps (secrets still encrypted). A revoked connection's page asks for its
  credentials again instead of offering to keep them. Setup guide for each
  provider, including Google's consent screen:
  `docs/integrations/oauth-apps.md`.
- **Plugins sign in through Connections.** A plugin lists the accounts it needs
  in `plugin.json` (`requires_connections`: provider, scopes, a label), and its
  page in **Settings → Plugins** shows each one with its status (not connected,
  connected as which account, missing access, expired) and a **Connect** button,
  or lets you pick one of the workspace's connections instead (one made by
  pasting a token, for a box with no public callback). The plugin's scripts get
  it through an ordinary grant and only ever see the access token. Plugin
  authors use the new `ConnectionAPI` (`require`, `get`, `grant`,
  `credentials`) and a ready-made card for their own page; the plugin doctor
  checks the manifest entries. **Stop using** (or deleting the plugin with its
  data) lets go of the connection without deleting it. The YouTube Comments AI
  example now posts replies through a Google connection: its own OAuth flow and
  client settings are gone, so after upgrading connect YouTube again on its page
  and save its settings once (that deletes the old OAuth secrets). Guide:
  "Connections" in `docs/plugins.md`.
- **Integrations for scripts.** `pyrunner_integrations` gives scripts typed
  calls per service on top of a connection. The first module, `http`, calls any
  HTTP API with an HTTP API connection: `http.get("/v1/me")`,
  `post`/`put`/`patch`/`delete` and `paginate` (cursor, Link header or page
  numbers), returning parsed JSON; named with `connection=`, an OAuth
  connection works too (its token only to that provider's base URL). Every
  module shares one client: credentials
  only to the connection's base URL (also across redirects), retries with
  backoff on 429 and 5xx (Retry-After honoured), one re-auth after a 401 (PyRun
  refreshes an OAuth token the service refused) and one exception,
  `IntegrationError(code, status, retry_after)`, with no credential in its
  message. Standard library only; `requests` is used when installed. For
  integration and plugin authors there is a testing kit,
  `core.integrations.testing`: stand-in connections, a fake service, cassettes
  recorded from the real service with `PYRUNNER_RECORD_CASSETTES=1` (every
  credential scrubbed) and replayed offline, and a conformance suite every
  module passes; no request leaves a test, a `pyrunner_connections.client()`
  session's included. `manage.py new_integration <key>` scaffolds a module that
  passes it as generated (and refuses a key that is already an integration or,
  without `--provider`, a preset), `manage.py integration_smoke <key>` runs live checks
  with a real connection, and `manage.py integration_docs` writes each module's
  page in `docs/integrations/` from its docstrings (searched by `help.search`).
  Guide: `docs/integrations/framework.md`; plugin tests: "Testing code that uses
  connections" in `docs/plugins.md`.
- **Airtable and Notion integrations.** `pyrunner_integrations.airtable`
  lists, finds, creates, updates and deletes records (`list_records` with view,
  fields, sort and formula, page by page; `find_records(base, table,
  {"Email": "ada@example.com"})`; `create_record(s)`, `update_record(s)` with
  merge, replace or upsert, `delete_records`, in batches of 10; `list_bases`,
  `list_tables`), and waits out Airtable's 30-second pause after a 429.
  `pyrunner_integrations.notion` queries databases, gets, creates, updates and
  archives pages, appends and lists blocks (a plain string is a paragraph, more
  than 100 go in several requests) and searches. Both use a connection of their
  kind, return the service's own objects, and have a generated page with an
  example script in `docs/integrations/` and `manage.py integration_smoke`
  checks. Their tests replay exchanges written from each service's API
  reference. For module authors, the shared client takes `rate_limit_wait`
  for a service that pauses for a fixed time after a 429.
- **Google Sheets and Google Drive integrations.**
  `pyrunner_integrations.google_sheets` reads ranges, appends rows (lists of
  values, or dicts keyed by the header row), updates ranges, finds rows by
  header values (`find_rows(id, "Leads", {"Email": "ada@example.com"})` gives
  each row's number and values), lists a spreadsheet's sheets and creates
  spreadsheets; `new_rows(id, "Leads", state)` returns the rows added since the
  last call, for a scheduled script and for the coming "new row" watcher.
  `pyrunner_integrations.google_drive` lists files (in a folder, with a Drive
  query, page by page), uploads a path or bytes (into a folder, or replacing a
  file's content; over 5 MB as a resumable upload), downloads (Google Docs,
  Sheets and Slides exported as PDF, CSV, XLSX...), makes folders and returns a
  link anyone with it can open. Both use a Google connection (Sheets or Drive
  scope set) and have a generated page with an example script in
  `docs/integrations/` and `manage.py integration_smoke` checks; their tests
  replay exchanges written from Google's API references. For module authors,
  the shared client takes `base_url` for another API host of the same service
  (the token goes there, never to an unrelated host), and an integration can
  declare watcher adapters in its `triggers.py`.
- **Gmail and SMTP integrations.** `pyrunner_integrations.gmail` searches
  mail with Gmail's own search (`gmail.search("is:unread has:attachment")`,
  page by page), reads a message decoded (subject, sender, recipients, the
  text and HTML bodies, its attachments), sends mail with HTML, Cc/Bcc and
  attachments, replies in the thread (to the sender, or everyone with
  `reply_all`), lists, creates, adds and removes labels by name, downloads
  attachments, and `new_messages(state)` returns the mail that came in since
  the last call (with a label, or matching a search), for a scheduled script
  and for the coming "new email" watcher. It uses a Google connection with a
  Gmail scope set; its token goes only to Gmail's API host.
  `pyrunner_integrations.smtp` sends the same kind of message through an SMTP
  connection (`smtp.send(to, subject, text, html=..., attachments=[...])`),
  returns the Message-ID and any refused recipients, and never sends twice.
  Both have a generated page with an example script in `docs/integrations/`
  and `manage.py integration_smoke` checks (SMTP's logs in and sends
  nothing). For module authors, the testing kit gains `fake_smtp()` (a mail
  server with no socket, recording every message) and `SmtpConformanceSuite`,
  and no test reaches a real mail server.
- **Slack and GitHub integrations.** `pyrunner_integrations.slack` posts
  messages (Slack markup or Block Kit, in a thread, to a channel's id or
  `#name`), updates them, uploads files (Slack's three-step upload: the file
  goes to Slack's file host without the token), lists channels and people,
  finds a channel by name and reads a channel's messages. Slack's
  `"ok": false` replies raise `IntegrationError` with a matching code and
  Slack's own error name (`slack_error`), and `invalid_auth` or
  `token_expired` gets new credentials and one retry, as a 401 does.
  `pyrunner_integrations.github` lists, opens, updates and comments on issues,
  reads a file (text, bytes, over 1 MB too) and creates or replaces one with
  a commit, starts a workflow (`dispatch_workflow`) and lists workflow runs;
  GitHub's rate limit raises `RATE_LIMITED` with the seconds until it resets.
  `github.new_issues(state)` returns the issues opened since the last call,
  for a scheduled script and for the coming "new GitHub issue" watcher
  (`github_new_issue`). Both work on a Slack or GitHub connection (Connect or
  a pasted token), have a generated page with an example script in
  `docs/integrations/` and a `manage.py integration_smoke` check.
- **Watchers: start a script when something new shows up.** A watcher
  checks a source on an interval (at least a minute) and starts one run per
  new item, with the item in the run's event (kind `watcher`) and, optionally,
  script inputs mapped from it. Two sources ship: a new entry in a JSON list
  behind a URL (`http_poll`: list path, id path, optional since parameter)
  and a new item in an RSS or Atom feed (`rss_new_item`). The first check
  only marks where the source is; after that each item starts exactly one run,
  across checks, restarts and a worker dying mid-check. Watchers run on their
  own minute task with a fire cap per hour, back off on failures (doubling,
  up to an hour), turn themselves off after 10 failures in a row and email
  their creator; maintenance windows (new ones block watchers by default) and
  Pause all stop the checks, and each run goes through the script's
  concurrency policy. Each script has a Triggers page listing its schedule,
  webhook, watchers and chat channels; the sidebar's Triggers lists the
  workspace's watchers, each with Check now, a Test button on its form and an
  event log of the last 50 events linking to their runs. `watchers.list`
  (`watchers:read`) lists them over the API and MCP, and backups include them.
- **Watchers for email, Google Sheets, Gmail, GitHub and database rows.**
  Five more sources: a new email in an IMAP mailbox (`imap_new_message`, on
  the new **IMAP mailbox** connection: read-only, headers only, optional IMAP
  search keys), a new row from a query over one of the workspace's databases
  (`db_poll`: a read-only SELECT and a cursor column such as an id or a
  created_at, optional id column), and the integrations' triggers: a new row
  in a Google Sheet (`sheets_new_row`, optional key column), a new email in
  Gmail (`gmail_new_message`, label or search) and a new GitHub issue
  (`github_new_issue`). The last three sign in with a Google or GitHub
  connection and call the same `pyrunner_integrations` functions a script
  would. Each has a Test on the watcher form and starts exactly one run per new
  item, across restarts.
- **Watchers for plugins.** A plugin makes watchers for its scripts with
  `WatcherAPI(owner).sync(script, adapter, config, connection, interval)`
  (idempotent by key; also `get`, `list`, `pause`, `resume`, `delete`) and ships
  its own sources in `triggers.py`: each `TriggerAdapter` subclass there is
  offered as `<slug>:<key>` in every workspace's watcher picker, after the
  doctor's new `triggers` rule passes. A plugin's watchers carry its pill; in
  the console they can be turned off and on and checked now, not edited, and
  they are deleted with the plugin. The SDK Showcase example plugin (1.3.0)
  has a Watcher card with its own counter adapter.

### Fixed
- **A schedule time listed twice no longer runs the script twice.** The
  schedule form accepted a repeated time in the daily, weekly and monthly
  modes (`09:00, 9:00`), and each one became its own cron, so the script ran
  twice at 09:00. The form now refuses it ("09:00 is listed twice."), as
  `ScheduleAPI.sync` already did, and a schedule saved with a repeat before
  this is cleaned up by the upgrade (migration 0086): the repeat leaves its
  times and the extra cron it made is deleted, in every time zone, UTC
  included.
- **Brain sources without a title no longer share one source id.** A source's
  id came from its title or URL path only, so two untitled thoughts sent in
  one month (from `brain.propose-feed`, the console or a script) were both
  `thought-2026-11-source`: notes from different sources cited the same
  `source`, and both approvals read "Feed: thought-2026-11-source". An untitled
  source is now named by a short hash of its content, never by its words
  (`thought-2026-11-3f9a1c`), and when the Brain already has the id, the start
  of the new feed's id is appended (`thought-2026-11-3f9a1c-7b2e`). A source
  named by its title or URL path keeps the id it got before unless the Brain
  already has it, and ids already given are not changed.

## [2.0.0] — October 23, 2026

### Upgrade notes (from 1.x)
- **Selected secret injection is now exact.** A user script in *Selected* mode
  receives only the secrets attached to it on the script form. Before 2.0 it also
  received every unowned workspace secret, which is the same set *All* gives, so
  the setting narrowed nothing. After upgrading, open each script that uses
  *Selected*, attach the secrets it reads (*Auto-detect from code* finds them) or
  switch it to *All*. Plugin scripts in *Selected* mode (the SDK default) get
  their own plugin's secrets and the secrets attached to them; a user's unowned
  secret no longer reaches them unless it is attached (the bundled example
  plugins only use their own secrets).
- **Datastore and plugin API tokens are stored hashed.** Like Unified API
  tokens, they are kept only as a SHA-256 digest; the upgrade (migration 0065)
  hashes the existing ones. Tokens keep working unchanged; the list shows the
  first 8 and last 4 characters. A database copy or dump no longer contains
  usable tokens. Rolling back to 1.x after upgrading invalidates them (create
  new ones there).
- **Each script has its own working folder.** A run's current directory is now
  `/app/data/workdir/<script id>/` (the id is in the script's URL) instead of the
  folder every script shared. Files a run writes there are there for the script's
  next run, other scripts no longer see them, and the folder is deleted with the
  script (a backup restore keeps the folders of the scripts it restores; the files
  are not part of backups). Files scripts wrote before 2.0 stay in
  `/app/data/workdir/`; move what a script still reads into its own folder, or
  read it by absolute path.
- **Container process limit.** `docker-compose.coolify.yml` now sets
  `pids_limit: 2048` (processes and threads for the whole container). Scripts
  run as root in the image and the kernel exempts root from the per-run process
  limit (`RLIMIT_NPROC`), so that setting can't stop a fork bomb; Settings →
  Isolation now marks it "not enforced while scripts run as root". Raise the
  limit if your scripts start many processes (several headless browsers), and set
  one yourself on other deployments (`docker run --pids-limit`, or compose
  `pids_limit`).
- **New image name, and Coolify keeps your old compose file.** New releases are
  published as `hasanaboulhasan/pyrun`; an install that still pulls
  `hasanaboulhasan/pyrunner:latest` stays on the last image published under that
  name, so change the image name when you update. On Coolify, **Redeploy** pulls
  the image named in the compose file the resource already has: a 1.x Docker
  Compose resource keeps the 1.x compose, without `pids_limit: 2048`,
  `RATELIMIT_TRUSTED_PROXY_DEPTH=1` (login lockout and per-IP rate limits then
  see Traefik's address for every visitor) or the pass-through for
  `DATABASE_URL`, `PYRUNNER_DATA_DB_URL` and `PYRUNNER_ALLOW_PRIVATE_NETWORK`.
  Replace the resource's compose with the 2.0 `docker-compose.coolify.yml`
  (keep your environment values) before you redeploy. A Docker Image resource
  needs the new image name and `RATELIMIT_TRUSTED_PROXY_DEPTH=1` added by hand.
  The Coolify install guide's *Upgrading from 1.x* section has the steps.
- **An upgraded instance keeps the name "PyRunner".** 1.x saved its default
  instance name in the database, and the upgrade doesn't change a saved name, so
  the header, page titles and emails still say "PyRunner". Change it under
  **Settings → General → Instance name** if you want "PyRun".

### Added
- **Workspace Brains** — immutable revisions and deduplicated text blobs,
  a file browser with visibility and freshness badges, owner/admin edits,
  revision diffs, access grants, template creation, and ZIP or public GitHub
  import/export. Existing BrainOutside 1.0 content is preserved. Backup format
  1.13.0 includes opted-in brains and their full history; token grants must be
  recreated after restore.
- **Brain read and write tools** — shared REST/MCP, signed script loopback,
  plugin `BrainAPI` (SDK 2.7) and grant-filtered agent tools. Captures produce
  validated proposals for owner/admin review, with diffs, conflict re-review,
  idempotent approval and cost caps. The confined reader assembles cited context;
  console requests run asynchronously. Optional vector search verifies results
  against canonical notes and the caller's current tier.
- **GitHub Brain sync** — HTTP Git Data exports with explicit all-tier consent,
  a reviewed initial SHA and non-force updates. Pulls create reviewable proposals;
  optional polling never approves or exports. Credentials use workspace Secrets.
  Restore pauses polling and drops transient jobs while preserving history.
- **MCP and Connect** — stateless Streamable HTTP at `/mcp`, shared API
  handlers, permission-filtered OpenAPI, and workspace connector instructions.
  Expiring read-only URL tokens are opt-in. Supports current per-request
  protocol metadata and older clients' initialization handshake; no sessions
  or streaming worker connections.
- **Unified API registry and REST** — permission-scoped, hashed API tokens;
  one schema and handler per tool-call endpoint, GET for reads, a filtered
  `/api/v1/_catalog`, and `/api/v1/_health`. Existing datastore and plugin token
  scopes retain their reach. Brain endpoints share these handlers; REST names
  use dots (`brain.get-note`), MCP names hyphens (`brain-get-note`).
- **Vectors: embedding providers, vector stores and collections** — Services
  gains two cards: *Embedding Providers*
  (OpenAI, Voyage, Jina, Z.AI, a local Ollama, or any OpenAI-compatible
  `/v1/embeddings` endpoint; Test records the vector dimension) and *Vector
  Stores*: the built-in **Local** store (one SQLite file per collection on the
  data volume, searched exactly with numpy), seeded and set as the default, and
  **Qdrant** servers, where each collection becomes one Qdrant collection named
  after its workspace. A new **Vectors** page per workspace creates collections,
  pins the provider's model and vector dimension to each one, runs real searches
  with JSON payload filters, browses points and deletes with a typed
  confirmation.
  - **Scripts** use the new `pyrunner_vectors` helper (standard library only,
    no keys in the run): `get_collection("docs")` then `add_texts`,
    `add_vectors`, `search`, `search_vector`, `get`, `count`, `scroll`,
    `delete` and `embed`. A plugin's collections are open to that plugin's
    scripts only; every other collection to any script in its workspace. Each
    run is braked at 2 000 embedding calls, 20 MB of embedded text and 200 000
    points written or deleted, so a runaway loop can't run up a provider bill.
  - **Plugins** get `VectorAPI` (plugin SDK 2.7): `provision("docs",
    embedding_provider=..., dimension=..., metric=...)` is idempotent and
    refuses to silently change a collection's model or dimension; `get`,
    `list`, `delete` and the same operations as the helper. Manifests declare
    `provisions.vector_collections`, and the plugin doctor checks it. The SDK
    Showcase example gains a Vectors card.
  - **One filter grammar on both stores** — equality, `$in`, ranges,
    `$prefix`, `must` / `must_not` — with strict types, array fields that match
    on any element, and scores that are always "higher is better", so a
    collection answers the same on Local and on Qdrant.
  - **Native Qdrant access** stays off unless the operator ticks *Share
    credentials with runs* on the connection; then a script's
    `credentials()` returns the URL, API key and physical collection name for
    `qdrant-client`.
  - Backup format 1.13.0 carries providers and connections (upserted by name,
    credentials encrypted as stored) and Local collections with their points
    inline, up to `PYRUNNER_VECTOR_BACKUP_MAX_MB` (default 50) each; Qdrant
    collections are carried as settings and reattach to the server on restore.
    Removing a plugin with *remove data* deletes the collections it owns.
- **Cron expression schedules** — a sixth run mode that takes a standard
  5-field cron expression (`0 9 * * 1-5` for weekday mornings, `*/15 9-17 * * *`
  for every quarter hour during business hours), for anything the guided
  daily/weekly/monthly modes can't say. The form validates as you type and
  previews the next three runs; the plugin SDK gets `ScheduleAPI.sync(...,
  mode="cron", cron="...")`. Like the other clock-based modes the expression
  is written in the schedule's **timezone** and converted to UTC for the
  scheduler (day-of-week/month fields move with a midnight crossing, and the
  daily DST resync covers cron too), so `0 9 * * 1-5` in `Asia/Tokyo` really
  fires at 09:00 Tokyo time. Contributed by @cmcau (#5); timezone conversion
  added on merge. Backup format 1.8.0 carries the new field.
- **Yearly schedules** — a run mode for "once a year on this date": pick a
  month, day, time and timezone. The scheduler stores the next real local
  occurrence (so the right DST offset applies for that date and a schedule
  set late in December can land in the next UTC year), and 29 February runs
  only in leap years — including when the timezone conversion pushes the UTC
  date onto 28 February or 1 March. Available to plugins via
  `ScheduleAPI.sync(..., mode="yearly", month=, day=, time_str=, tz=)` and
  carried by backups (format 1.8.0). Contributed by @djpfs (#16).
- **Account management from the command line** — `python manage.py user` is
  the way back in when the console can't help: no email configured for reset
  links, a login lockout, or no admin who can sign in.
  `user reset-password <email>` sets a new password (prompted, or `--generate`
  to print a random one), signs out that account's sessions and clears its
  lockout. `user create <email> [--admin]`, `user promote`, `user demote`,
  `user unlock` and `user list` cover the rest; the last admin can't be
  demoted. In Docker:
  `docker exec -it pyrunner python manage.py user reset-password you@example.com`.
- **Any interval, not just the preset list** — interval schedules take any
  whole number of minutes from 1 to 32767 (about 22 days) instead of the
  fixed 5/10/15/30/60/120/360/720 menu, so "every 7 minutes" or "every 90
  minutes" is just typed in. Quick-pick buttons cover the common values.
  The plugin SDK's `ScheduleAPI.sync(mode="interval", interval_minutes=…)`
  now validates the same range instead of accepting anything.
- **Fork a script** — a Fork button on the script page duplicates it into the
  same workspace: code, description, environment, timeout, isolation and
  notification settings, tags, notification channels, secret grants and
  library attachments all come along. The copy is a plain user-owned script
  (forking a plugin's script is the supported way to customize it), starts
  with no webhook token, and its schedule is cloned paused so a duplicate
  never fires before you've looked at it. Run history stays with the
  original. You land on the copy's edit page.
- **Search runs by output** — the Runs page has a search box that matches
  script names and error output (stderr), so "which run raised KeyError?"
  is one query instead of opening runs one by one. Tick *Include output* to
  search stdout too (it's the big column, so it's opt-in). The search
  composes with the status tabs and the script picker, and switching those
  keeps your query.
- **Duration chart on the script page** — the last 30 finished runs as a
  bar chart under Statistics, coloured by outcome, with the average drawn
  across them and the slowest run called out. A script that is slowly
  getting slower is visible at a glance instead of buried in the runs
  list. Each bar links to its run. Rendered server-side, no JavaScript.

### Changed
- **Py AI can say why a script failed.** The console Py AI gets a read-only
  `get_run_error` tool: the exit code and the last 2,000 characters of stderr of
  a script's most recent failed or timed-out run, in the current workspace. The
  stored output was already masked for the run's secrets. Py AI stays limited to
  workspace owners and admins; Py AI on chat channels doesn't get this tool.
- **Large run output keeps its ending.** stdout and stderr are each stored up
  to 1 MB per run (unchanged). Beyond that a run used to keep only the first
  1 MB, losing the end of the output, which is where a result line or a
  traceback usually is. It now keeps the first 200 KB and the last ~800 KB, cut
  at line breaks, with a marker in between:
  `[OUTPUT TRUNCATED: N bytes cut from the middle. Kept the first … and the last … of … bytes.]`
- **Rebranded to PyRun** — the product is now called PyRun and lives at
  [pyrun.ai](https://pyrun.ai). Console, emails, setup and error pages, the
  docs links, and the GitHub (`hassancs91/PyRun`) and Docker Hub
  (`hasanaboulhasan/pyrun`) names all follow. Nothing you wrote changes:
  the `pyrunner_*` script helpers, `PYRUNNER_*` environment variables,
  `PyRunnerPlugin` SDK class, and data paths keep their names, so existing
  scripts, plugins, and deployments keep working. Pull the new image name
  when you next update. Page titles now use the instance name from
  Settings → General instead of a hardcoded brand.

### Fixed
- **`manage.py migrate_db` copies the whole instance.** Moving from SQLite to
  PostgreSQL went through backup and restore, which is portable by design and so
  left out users, workspace members, API tokens, AI providers, channels, plugin
  records and library history: the migrated instance came up with no admin
  account and an open `/setup/admin/` page. It now copies every table row for row
  with its original keys and exact timestamps, replaces whatever the target
  database held, and checks every table's row count. Data-server databases,
  vector stores and environments are left as they are. Sessions are not copied,
  so everyone signs in again.
- **One unusable database no longer fails a whole restore.** A database row
  that was never provisioned (no schema, role or password in the backup) made
  the restore stop with "Cannot decrypt empty value", so no backup of that
  instance could be restored. It is now restored in error status with a warning,
  like any other database that can't be provisioned.
- **A database restart no longer loses a finished run's result.** When the
  PostgreSQL server holding the core database restarted while a script ran, the
  script finished but its result could not be saved over the worker's dead
  connection: the run kept showing "running" until the stale-run check failed it
  minutes later, with its output lost. The worker now reconnects and retries the
  save for about a minute.
- **A crashed worker no longer leaves a second one running.** When the worker's
  main process was killed hard (`kill -9`, an out-of-memory kill), its child
  processes kept running and the container's monitor started a new worker next
  to them: two clusters with twice the configured workers, and "Restart
  workers" could only stop the new one. The monitor now stops what is left of
  the old worker before it starts the new one.
- **The Logs page keeps every process's lines after the log rotates.** The web
  workers and the task worker write one `pyrunner.log`; when it reached 10 MB
  the process that rotated it moved on to a new file while the others kept
  writing to the renamed one, so the Logs page showed only part of the log,
  later rotations deleted files still in use, and two processes rotating at once
  printed a "Logging error". Every process now follows the current file and
  only one rotates it at a time.
- **Brain AI works on PostgreSQL.** Every brain feeder extraction and reader
  run failed on a PostgreSQL core database ("Extraction failed. Check the worker
  and retry."): the daily-cap check locked the brain row outside a transaction,
  which PostgreSQL refuses and SQLite ignores. It now runs in one.
- A run that was executing when the container or its worker restarted is now
  marked failed as soon as the worker starts again, if its process is gone. It
  used to show "running" until its timeout plus five minutes had passed (65
  minutes for the default timeout). Workers you start yourself
  (`manage.py pyrunner_qcluster` without `--reconcile-orphans`, e.g. split worker
  containers) keep the deadline rule; set `PYRUNNER_RECONCILE_ON_START=false` if
  several all-in-one containers share one database.
- Backup restore now rolls back database changes when a non-database error
  interrupts the replacement, preserving existing rows after failed restores.
- SQLite transactions acquire their write lock at the start, avoiding immediate
  read-then-write failures while workers update the database.
- Qdrant uses separate 3-second connect and 10-second read timeouts. After a
  backend failure, collections on that connection fail quickly for 30 seconds
  across workers. Services Test bypasses the pause and clears it on recovery;
  server error bodies are no longer exposed to scripts.
- **Weekly and monthly schedules survive a backup/restore** — the export
  omitted `weekly_days`/`weekly_times`/`monthly_days`/`monthly_times`, so a
  restored weekly or monthly schedule came back with no days or times. (#5)
- **Dashboard "upcoming runs" shows weekly and monthly schedules** — the
  widget filtered on interval/daily only, so weekly and monthly schedules
  never appeared even though they fired on time. (#5)
- **CI is green again** — the workflow never passed since it was added:
  tests needed `collectstatic` (the manifest storage rejects unknown static
  files under the test runner's `DEBUG=False`), and an unpinned Ruff picked
  up 0.16's much larger default rule set. Both pinned down; no product code
  changed.
- **A worker dying mid-run now triggers the run's failure notification** —
  runs the stale-run reconciler flipped to *failed* never went through the
  normal finalize path, so the one failure the platform most needs to report
  (your worker was killed or restarted while your script ran) was the one it
  stayed silent about. Reconciled runs, including runs lost from the queue,
  now send the script's configured notifications.
- **Bulk requirements install no longer gets stuck** — the pip subprocess and
  the worker task that hosts it both had a 600 s limit, so on a slow install
  the worker killed the task first, pip kept running orphaned in the venv,
  and the queue re-delivered the same install into it. Every package task
  now gets its own timeout above pip's cap, pip runs in its own process group
  (a timeout kills its build helpers too), with stdin closed and
  `PIP_NO_INPUT` set so it can never wait on a prompt; the worker retry
  window accounts for the longest pip task, and stuck package operations
  are reconciled by the worker heartbeat instead of only when someone opens
  the packages page.
- **A failed environment create no longer poisons its name** — a `python -m
  venv` that failed halfway (typically Debian's `/usr/bin/python3` without
  `python3-venv`) left a folder behind that blocked the name forever with
  "Path already exists" and no way to clean up. Partial venvs are removed on
  failure, an existing folder no environment references is reclaimed, and the
  interpreter picker only lists Pythons that can actually bootstrap pip.
- **A plugin's `min_pyrunner` is now enforced** — the manifest field was
  documented but never checked, so a plugin built for a newer PyRun
  uploaded and activated on an older one and failed later in confusing ways.
  Upload and activation now refuse it with the required version.
- **A stray plugin folder no longer blocks uploads of that slug** — a folder
  under the plugins directory with no plugin record (a delete that crashed
  halfway, or files shipped inside an image) made every upload of the same
  slug fail with "already exists" and nothing in the UI could remove it. It
  is now moved aside (never deleted) and the upload proceeds; a folder loaded
  in dev mode is left alone with a clear message.
- **`pyrunner_db.connect()` tells the truth about `psycopg`** — the error
  claimed the driver "ships with PyRun's runtime"; it doesn't ship inside
  script environments. The message now points at Environments → Packages.
- **Changing the Django admin URL slug says it needs a restart** — routes are
  built at startup, so the old slug stayed live and the new one 404'd until
  the next restart, with nothing telling you so. Saving a changed slug now
  shows exactly that.
- **`run-local-postgres.ps1 -Fresh` really wipes** — `docker compose down -v`
  only removes volumes it can attribute to its own project, so a stack that
  had been brought up under another project name kept its month-old Postgres
  data and the "fresh" boot silently reused it. The launcher now removes the
  three named volumes explicitly and refuses to continue if any survive.

### Improved
- **Console icons.** The sidebar, section cards and empty states use Lucide
  icons (ISC, vendored in `core/icons/lucide/`, rendered inline by the
  `{% icon %}` template tag: no CDN or JavaScript). Plugin nav icons are unchanged.
- **Scripts list.** 25 scripts per page, a search on name and description, and
  status, tag, owner, search and page in the URL so they combine and can be shared.
- **Brain grants.** Plugin and agent grants are picked from a list (installed
  plugins by name; "Py AI (console)" or Py AI on a channel) instead of typing an
  identifier; existing grants show the friendly name. The Py AI page lists the
  brains it can read, with tier and a link to the grants.
- **brand_tracker 1.1.1.** The public report no longer shows its template
  comment as text at the top of the page.
- **Readable failures.** Secret-provider, vector-store and Telegram network
  errors are one line naming the host, port and cause ("doesn't resolve",
  "connection refused", "timed out after 10 s"); the raw text is under
  *Details*. Telegram's bare "Unauthorized" says the bot token was rejected, and
  a network error never echoes the bot token from the request URL. A missing
  bucket is named instead of boto's "HeadBucket … 404".
- **Forms and results.** Services Test results render under the clicked button;
  a refused storage save re-opens the form with its values; settings saves stay
  on their tab and name the field in errors; add forms ask for the key instead of
  "Leave blank to keep current".
- **Runs, scripts and schedules.** A truncation badge, a hint when a configured
  memory/CPU/file-size limit stopped a run, numbered forks, the next run in the
  schedule's own timezone, and an error banner when a script save fails.
- **Data pages.** pip/setuptools can't be uninstalled from an environment, the
  Python picker lists each interpreter once, plugin-owned stores say so, the
  database explorer counts small never-analysed tables exactly, the vector page
  shows "Store unreachable" during an outage, and new collections preselect the
  last tested embedding provider.
- **Tokens, Connect, Brains.** The tokens list shows bound brains, expired
  tokens and inactive plugins (cards on phones); Connect has Copy buttons;
  deleting a token deletes its brain grants; brain settings show the search
  index state and today's spend; Ask brain refuses a cap-blocked query at once;
  an invalid proposal still shows what it would write.
- **Py AI and channels.** Py AI talks about the current workspace, its model hint
  follows the active provider, and answers sent to Telegram are plain text.
- **Admin pages.** Logs open a message in full, keep filters in pager links and
  refresh only the table; Members lists the Owner first and marks "(you)";
  `manage.py recaptcha status|off` switches reCAPTCHA off from the shell; the
  header resource meters are superuser-only; the header Docs link opens the
  GitHub README.
- **Every width fits.** The header no longer runs off the screen at the tablet
  and small-laptop breakpoints (640, 768 and 1024 px): the version label and
  What's new icon appear from 768 px and the Discord, Docs and GitHub icons from
  1280 px. On phones the Workspaces list puts each row's actions under the name,
  and a pending invite's details wrap instead of running under its buttons.
- **Accessibility.** The Services card toggles have accessible names, and the
  header and sidebar navigation are labelled landmarks.
- **One console layout.** Every console page shares one shell and page header
  (title, breadcrumb, actions) with a skip link. Create and edit forms sit at
  form width with their help in a side panel and Cancel next to the primary
  button at the bottom right; detail pages use the full width. Brains get
  segmented tabs, the Services page opens only the first unconfigured card and
  has a sticky section index, and Environments and Users are tables (Users can
  be filtered and shows each user's workspaces). On phones, lists drop their
  secondary columns instead of scrolling sideways, recent runs show status and
  time under the script name, and selects leave room for their arrow. A signed-in
  member who opens an admin-only page gets a 403 inside the console ("Only
  instance admins can open this page.") instead of a redirect. The developer
  links moved from the sidebar footer to an *About PyRun* card under
  Settings → System. Plugin pages keep their own layout for now.

### Security
- **The Brain reader can no longer read files outside its snapshot.** Its file
  guard did not treat a leading `~` the way the Claude CLI does, so a prompt that
  reached `brain.assemble-context` could make the reader open files in the
  server's home directory or `/proc`. `~` paths and escaping Glob/Grep patterns
  are refused, and every AI agent PyRun starts (Brain, Py AI, provider tests)
  now runs without the instance's own secrets (`SECRET_KEY`, `ENCRYPTION_KEY`,
  `DATABASE_URL`, `PYRUNNER_DATA_DB_URL`, `REDIS_URL`, `RESEND_API_KEY` and
  `PYRUNNER_RUN_ENV_DENYLIST`) in its environment. Py AI gets no built-in tools,
  only its own read-only ones.
- **A user removed from every workspace no longer sees across workspaces.** Such
  a user could still open the console, create and run a script with no
  workspace, and that run received every workspace's secrets. The console and
  plugin pages now show "You're not in any workspace" (403) to them; a run with
  no workspace gets only unassigned secrets; and the internal data-store,
  database, vector and channel lookups no longer treat "no workspace" as the
  default workspace. Superusers and upgraded single-workspace instances are
  unaffected (the tenancy backfill assigned every existing row).
- **Outbound fetches are pinned to the address that was checked.** Brain URL
  imports, GitHub import and sync, and plugin answer completion now connect
  only to the IP address the SSRF guard approved, on every redirect, so a
  DNS-rebinding name cannot switch to a private address after the check.
- **Console escaping and page hygiene.** The console's HTML-escaping helpers
  now escape quotes too; developer notes and internal ticket numbers are no
  longer sent in page source; anonymous pages no longer show the version;
  the pinned Monaco loader and Chart.js are loaded with Subresource Integrity;
  secret values are masked until you click Show. `docker-compose.postgres.yml`
  now states that its default database passwords must be changed, and the
  Docker build context excludes every private plugin folder and `node_modules`.
- **Brand Tracker 1.1.2** escapes scraped titles, links and snippets in its email
  digest and keeps its public report share in the workspace its data lives in.
- **External secret providers can no longer reach private hosts.** The custom,
  Vault and Infisical secret backends fetched whatever endpoint their config
  named with no SSRF check, so one pointed at the cloud-metadata address
  (`169.254.169.254`) returned the instance's credentials as "secret values".
  They now apply the same `PYRUNNER_ALLOW_PRIVATE_NETWORK` guard as storage,
  webhooks and vector stores; the metadata range stays blocked either way.
- **Delete and archive confirmations escape the item's name.** The
  confirmation dialogs on scripts, tags, data stores, entries, secrets,
  libraries, packages, channels and invites built their text as a JavaScript
  string with HTML escaping only, so a quote in a name broke the dialog (and a
  crafted name could run in it). Those names are now JavaScript-escaped.
- **A gzip backup can't exhaust memory on restore.** Settings → Backup unpacked
  an uploaded `.json.gz` completely before checking it, so a few hundred KB of
  gzipped zeros grew the web process by hundreds of MB (and the 100 MB upload
  cap allowed far more). The upload is now unpacked in chunks and refused past
  `PYRUNNER_BACKUP_MAX_EXPANDED_MB` (default 256 MB, above the 100 MB upload
  cap, so any backup that loads as plain JSON still loads gzipped); restore a
  larger backup on the server with `python manage.py restore_backup`.
- **A malformed chat webhook payload returns 400, not a 500.** An inbound
  Telegram update with the right secret but an unexpected shape raised an error
  inside the public `/channels/<token>/` endpoint; it is now ignored or answered
  with a 400.
- **A run always uses its script's workspace.** A run started by plugin code
  that set no workspace (the bundled Sales Dashboard's *Run collector now*)
  ran its script with the default workspace's data stores and every
  workspace's secrets. Such runs now take their script's workspace.
- Docker builds exclude every root `.env.*` variant, including local test
  credentials, in addition to `.env` and the existing private artifact rules.
- **The login lockout now actually locks** — the limit of 5 failed sign-ins
  (15-minute cool-off) recorded every failure against no account, so the
  correct password still signed in after any number of wrong guesses and
  password guessing was never stopped. Failures are now counted against the
  email being tried, per address, and a locked account refuses even the
  correct password until the cool-off passes. Locked yourself out? Run
  `python manage.py user unlock <email>` on the server.
- **One SSRF guard, with an operator opt-in for private networks** — every
  URL you type into the console that PyRun then calls out to (S3 endpoints,
  notification webhooks, and the vector stores and Brain imports that follow)
  now goes through a single guard, `core/netguard.py`. Behaviour is unchanged
  by default: hosts that resolve to loopback, private or link-local addresses
  are rejected. New: set `PYRUNNER_ALLOW_PRIVATE_NETWORK=true` to reach a
  Qdrant, MinIO, ntfy or Ollama container on the same Docker network (or
  localhost in development). The cloud-metadata range (`169.254.0.0/16`,
  `fe80::/10`, `fd00:ec2::254`) and the unspecified addresses stay blocked
  whatever the flag says, and rejected URLs now say which flag would allow
  them.

## [1.16.0] — July 29, 2026

### Added
- **Object storage** — the S3 settings became a real storage service. You can now
  save **several connections** (Cloudflare R2, AWS S3, Backblaze B2, DigitalOcean
  Spaces, MinIO, or any S3-compatible endpoint) instead of exactly one, switch
  which one backups use with a click, and point plugins at a *different* bucket so
  your private backup bucket stays private. Your existing S3 configuration is
  carried over automatically as the default connection — backups keep working with
  nothing to re-enter. Each connection can carry a public base URL, so stored files
  get permanent hot-linkable links instead of expiring ones. Plugins can then store
  files (SDK 2.5: `StorageAPI`) — an archived image, a generated PDF, a chart —
  each confined to its own `apps/<plugin>/` space, so one plugin can never read or
  delete another's files. Their scripts get the same thing via
  `import pyrunner_storage`, with no credentials and nothing to install: the bytes
  travel over PyRun's internal loopback API, so S3 keys never enter a script's
  environment. Deleting a plugin with *remove data* clears its files too.
- **Libraries** — shared Python modules that scripts import, instead of the same
  helper copy-pasted into five scripts. Create a library under Libraries, add one
  or more modules in the tabbed editor, attach it to any script from the script's
  form, and import it: `from my_helpers.utils import clean`. Every save that
  changes the code creates a new version, and a run is pinned to the version that
  was current when it was queued — so editing a library can never change what an
  already-queued run executes, and a run's detail page shows exactly which
  versions it used. Full version history per library with a diff against the
  current code and one-click restore (restoring writes the old code forward as a
  new version; history is never rewritten). A library can't be deleted while a
  script still imports it, and if a pinned version is missing at launch the run
  fails with a clear message instead of a confusing `ImportError`. Plugins get
  the same thing through the SDK (2.4: `LibraryAPI`), which is what lets a plugin
  ship one shared pipeline used by many worker scripts.
- **Plugin API** — any plugin can now declare read-only HTTP resources that core
  serves at `/api/v1/plugins/<slug>/<resource>/`, with core owning 100% of
  authentication, rate limiting, and workspace scoping — a plugin never sees a
  raw request, a token, or a tenancy decision. Resources are declared in
  `plugin.json` (`provides.api_resources`) and implemented as small handlers in
  the plugin's `api.py` (SDK 2.3: `@resource`, `APIRequest`, `APIError`); an
  undeclared handler is never served, and the plugin doctor fails
  manifest-vs-code drift before shipping. API tokens gained a scope: existing
  datastore tokens are unchanged (and can never reach plugin APIs), while new
  plugin-scoped tokens grant exactly one plugin's API, least-privilege.
  `GET /api/v1/plugins/` is a self-documenting discovery endpoint. Limits:
  per-token 60/min, per-plugin 300/min (429s carry `Retry-After`), a pre-auth
  per-IP throttle against token scanning, and a 1 MiB response cap — all
  instance-configurable via environment variables.
- **Plugin public pages** — a plugin can publish a shareable, read-only HTML
  page at an unguessable `/p/<token>/` capability URL (no login), created and
  revoked from its dashboard via the SDK (`@page`, `PublicPageAPI`). Pages are
  served script-free under a strict Content-Security-Policy with
  noindex/no-store/no-referrer headers and per-IP rate limiting; revoking (or
  expiry) kills a URL permanently — re-sharing issues a fresh one. All shares
  are listed and revocable under Settings → API Tokens.
- **Brand Tracker 1.1.0** — exposes its mention feed and stats through the new
  plugin API (`mentions` + `stats`, filterable and paginated) and adds a
  "Share report" button that publishes a public, read-only mentions report.
- SDK 2.3 also adds a read-only, workspace-scoped `ChannelAPI.list()` so plugin
  forms can offer a notification-channel picker.

### Fixed
- **Worker settings now actually apply** — the values saved under Settings →
  Workers (worker count, task timeout, retry delay, queue limit) were never
  read: they were loaded at settings-module import, which happens before the
  database is reachable in every Django process, so the cluster always ran on
  the built-in defaults no matter what the page said. The worker now starts
  via `manage.py pyrunner_qcluster`, which applies the saved values once the
  database is available — "restart workers to apply" is finally true.
- **Long runs no longer orphaned by the worker queue** — a run outliving the
  worker re-delivery window (`q_retry`, default 660s) had its task handed to a
  second worker whose early-return path then clobbered the live run row:
  stuck at "running" forever, output never captured, force-stop broken
  (`pid` cleared). Duplicate deliveries are now true no-ops (the run is
  claimed atomically), the re-delivery window automatically floors above the
  largest script timeout at worker start, and the script form warns when a
  timeout would outrun the running workers' window. A worker-side task
  timeout (a `SystemExit`, previously invisible to error handling) now kills
  the script's process tree and records TIMEOUT instead of leaving the
  process running detached.
- **Stale runs self-heal** — a worker killed while busy (container restart,
  OOM) left its run at "running" forever, blocking anything that gates on
  "a run is in flight". Runs stuck past their own timeout (+5 min grace) are
  now reconciled to failed — with an explanatory message — every minute on
  the worker heartbeat and on the runs page; runs queued for over 24 hours
  while workers are alive are reconciled the same way.
- **Datastore API CORS preflight** — browser `OPTIONS` preflights to
  `/api/v1/datastores/…` were rejected with 401 (auth ran before the preflight
  branch) and error responses carried no CORS headers, making cross-origin
  browser calls impossible. Preflights now succeed tokenless and every
  response, including errors, carries CORS headers.
- **Schedule timezone is no longer discarded** — the script form rendered the
  timezone selector three times (once per daily/weekly/monthly panel), all
  sharing one field name. Hidden panels are still submitted, so the browser
  posted three values and Django kept the last one: picking `Asia/Beirut` for a
  daily run silently stored `UTC`, with no error and the selector reverting on
  reload. The selector is now a single control shared by every clock-based mode.
- **Schedule history records mode, timezone, and enabled changes** — the audit
  snapshot of a schedule's previous state was taken after form validation had
  already written the new values onto it, so run-mode, interval, timezone, and
  active-state edits compared equal to themselves. Those edits wrote no history
  entry at all, and an edit that also changed run times *did* write one but
  recorded the new timezone as the previous one. The snapshot is now taken
  before the form touches the schedule, so history shows the real before/after.

## [1.15.1] — July 28, 2026

### Security
- **Docker images no longer ship baked-in keys** — the Dockerfile set
  `SECRET_KEY` and `ENCRYPTION_KEY` as `ENV` for the build-time `collectstatic`
  step, and Docker `ENV` persists into the published image. Every image up to
  and including 1.15.0 therefore contained a publicly-known Django `SECRET_KEY`
  (session/cookie forgery) and a publicly-known constant Fernet
  `ENCRYPTION_KEY` (all "encrypted" secrets decryptable by anyone with the
  image) — and because the variables were never empty, the entrypoint's
  "required keys" check could never fire, so a deployment that set neither key
  booted and ran silently on the known values. The keys are now scoped to the
  single `RUN collectstatic` build step and never persist, the entrypoint
  rejects the two known build-time literal values in addition to empty (so a
  deployment still running on the leaked keys fails fast with instructions
  instead of starting), and the README quickstart now generates and passes both
  required keys. **If you deployed from the Docker Hub image without setting
  your own `SECRET_KEY`/`ENCRYPTION_KEY`, upgrade, generate fresh keys, and
  rotate any secrets stored in PyRun.**

### Fixed
- **`docker run ... <command>` now runs the command** — the entrypoint ignored
  its arguments and always booted the full app, so the documented key-generation
  one-liners (`docker run --rm ... python -c "..."`) hung the terminal on a full
  server boot instead of printing a key. The entrypoint now `exec`s any passed
  command (before the key checks, so generating a key doesn't require one).

## [1.15.0] — July 15, 2026

### Added
- **External Secret Providers** — a secret's value can now be fetched live from
  an external secrets manager instead of stored locally. Configure a provider
  profile under Services → Secret Providers (HashiCorp Vault / OpenBao, AWS
  Secrets Manager, Infisical, Doppler, or any HTTP JSON endpoint), then point a
  secret at it with a reference like `kv/myapp#API_KEY`. The value is fetched at
  run start, cached in-process per a per-profile TTL (never in a shared cache, so
  plaintext is never persisted at rest), and injected **and masked** exactly like
  a local secret — so grants, workspace scoping, and the plugin SDK all keep
  working unchanged. Rotating a value in the provider is picked up automatically
  on the next run past the TTL. Resolution is fail-closed: a value that can't be
  fetched fails the run before it starts with a clear, named error (with an opt-in
  "serve last cached value" fallback for availability-first automation). Adding a
  new backend is one adapter file — no schema, form, or migration changes. Needs
  no new environment variables or dependencies; existing local secrets are
  untouched. Provider profiles and external-secret links are included in instance
  backups (format 1.6.0, relinked by name on restore); older backups still
  restore.
- **Databases** — managed PostgreSQL databases for scripts and plugins,
  complementing the key-value Data Stores with real SQL (tables, joins,
  transactions, the whole Python DB ecosystem). Attach a data server with one
  env var (`PYRUNNER_DATA_DB_URL` — works with a SQLite core, no migration);
  Owners/Admins provision databases from the console, each one a dedicated
  Postgres schema + login role so isolation is enforced by Postgres itself.
  Scripts connect through explicit per-script grants with the new
  `pyrunner_db` helper (`connect()` / `dsn()` / `sqlalchemy_url()`); plugins
  provision their own via the SDK's `DatabaseAPI` (API 2.2). Includes a
  read-only explorer — table browser with CSV export plus a Monitor page
  (live sessions, blocked/long-running queries, sizes, and slow-query history
  via `pg_stat_statements` when available) — and full backup/restore: the
  workspace backup (format 1.5.0) now carries database metadata, grants, and
  per-schema `pg_dump` data. `docker-compose.postgres.yml` sets the data
  server up automatically on first boot.
- **Pinned resource meters** — compact CPU / Mem / Disk mini-bars with live
  percentages on every cpanel page (desktop widths), polled every 30s, colored
  by the same 70/90% thresholds as the dashboard card, with a hover tooltip
  showing exact usage (and VPS totals when running in Docker). Clicking opens
  the dashboard.

### Changed
- Dashboard system resources are now container-aware: when PyRun runs in
  Docker, the primary CPU and Memory numbers are the container's own usage
  (read from cgroups v2/v1, matching `docker stats` semantics), with the full
  host/VPS shown as a smaller secondary line. Memory is reported against the
  container's limit when one is set (labelled "limit"), otherwise against
  host RAM; CPU is normalized to the container's quota when set, otherwise to
  host cores. Non-Docker installs render exactly as before.

### Fixed
- Scheduled jobs now run in the schedule's configured timezone (they previously
  always fired in UTC); daylight-saving shifts are re-synced daily, and backup
  times honor the instance timezone too.
- "Pause all schedules" no longer permanently disables the built-in
  worker-heartbeat, update-check, backup, and cleanup schedules — they are
  re-ensured on resume.
- "Restart workers" now actually restarts the worker and reports failure
  honestly, instead of silently doing nothing and reporting success.
- The General Settings instance name and timezone are now applied everywhere —
  the header, page titles, email subjects, and datetime display; the two
  non-functional date-format/time-format fields were removed.
- Django's configured password validators (length, common-password, numeric,
  attribute-similarity) now run wherever a password is set, and changing your
  password no longer errors.
- Rate limiters use a fixed window that can't be starved by steady sub-limit
  traffic and is shared across workers (database/Redis cache), not per-process.
- Weekly and monthly schedules now appear in the dashboard's upcoming runs, are
  included in backups, and record edit history.
- Backups round-trip weekly/monthly schedule times, script tags, archived
  state, and sandbox isolation mode (backup format 1.4.0; older backups still
  restore).
- The datastore HTTP API used by scripts on Postgres/sandboxed runs now raises
  on server/auth errors instead of silently dropping writes; setup status, the
  queued-tasks list, and the database-size card work correctly on PostgreSQL
  (the size card shows "N/A" rather than a bogus 0 B on non-SQLite engines).
- The last-backup size can exceed 2 GB, and backup file listing is no longer
  capped at 1000 objects.
- The test-email action now requires an administrator; saving AI settings with a
  since-deleted provider shows an error instead of silently disabling AI, and a
  malformed provider id no longer errors.
- Fonts are self-hosted instead of loaded from the Google Fonts CDN; the README
  (invite-only auth, real scheduling modes) and `.env.example` (`DEBUG=False` by
  default) were corrected.
- A missing `SECRET_KEY` now raises a clear `ImproperlyConfigured` with the
  generate command instead of a bare `KeyError` (helps first-run source checkouts).
- Per-IP rate limits (webhooks, inbound channels) can key on the real client IP
  behind a reverse proxy: set `RATELIMIT_TRUSTED_PROXY_DEPTH` to the number of
  trusted proxy hops (default `0` keeps the previous `REMOTE_ADDR` behavior).

### Security
- AI in scripts now works with every configured provider, and third-party
  provider credentials (Z.AI/OpenRouter/custom, carried on
  `ANTHROPIC_AUTH_TOKEN`) are masked in run output.
- Webhook notification URLs are DNS-resolved and rejected when they resolve to
  internal/private addresses (SSRF), matching the S3 endpoint check.
- Plugin ZIP installation enforces its size limits against the actual extracted
  bytes and hardens path-traversal containment (`Path.is_relative_to`).

## [1.14.0] — July 15, 2026

### Added
- **AI Providers** — generic multi-provider AI integration. Saved provider
  profiles (Anthropic Claude, Z.AI GLM, OpenRouter, local Ollama, or any
  Anthropic-compatible endpoint), each with its own encrypted credential and
  default model; switch the active provider with one click. Existing Claude
  configurations migrate automatically (migration 0043). Connection tests are
  provider-aware: Anthropic runs a real web search, other providers run an MCP
  tool-call round-trip that validates the model's tool calling. Usage rows are
  attributed to the serving provider.
- **Channels** — messaging subsystem (Telegram first). Outbound messages from
  scripts via the `pyrunner_notify` helper and `notify_channels` routing;
  inbound webhook with a deny-by-default approval inbox, per-sender rate
  limits, and a dispatch worker (migrations 0039–0040).
- **Py AI** — a read-only in-app assistant that answers questions about this
  instance (scripts, runs, schedules, datastores) via in-process MCP tools.
  Available as a dashboard chat and as a Telegram channel handler
  (migration 0041).
- **Brand Tracker example plugin** — weekly brand-mention tracking (Serper
  web/news, Hacker News, Reddit) with AI sentiment analysis and credit caps.
- Whole-app Beta badge system driven by a single `IS_BETA` flag.
- Shared cache backend: `DatabaseCache` by default (no extra service), with an
  opt-in `REDIS_URL` to use Redis. Fixes rate-limiting and webhook dedup that
  previously used a per-process cache and were not shared across workers.
- `HEALTHCHECK` in the `Dockerfile` (mirrors docker-compose) so `docker run`
  users also get container health reporting.
- Brotli-compressed static assets via WhiteNoise (`.br` in addition to gzip).
- CI: GitHub Actions (tests + Ruff), pre-commit hooks, Dependabot, editorconfig,
  and open-source community files.

### Changed
- Expanded `.env.example` to document the previously-undocumented settings
  (HSTS, DB connection age, gunicorn/PORT, login/API rate limits, per-run
  resource limits).
- README: the Claude AI section is now "AI in your scripts" with a provider
  table and per-provider setup notes.

### Security
- Removed passwordless magic-link login; fixed a channels XSS.
- Gated global-settings handlers, environment/package management, and
  application-log reads on superuser.
- Reject pip option lines in bulk requirements install.
- Added a scoped Content-Security-Policy header (defense-in-depth).

## [1.13.0]

Headline capabilities available in this release:

- **Script management** — create, edit, schedule, and monitor Python scripts.
- **Environments** — isolated per-script virtualenvs with custom pip packages.
- **Secrets** — encrypted environment variables and secrets (Fernet).
- **Claude AI for scripts** — call Claude from scripts (`pyrunner_ai`) using a
  Claude subscription token or an Anthropic API key.
- **Channels** — outbound + inbound messaging (Telegram) with an approval inbox.
- **Py AI** — a read-only in-app Claude assistant over your scripts/runs.
- **Plugins** — installable, self-provisioning Django-app plugins with an SDK.
- **Sandbox & isolation** — opt-in bubblewrap/rlimit isolation for runs.
- **Tenancy** — workspaces with scoped resources and RBAC.
- **Postgres support** — opt-in via `DATABASE_URL`; SQLite stays the default.
- **Notifications** — email, webhook, and Telegram alerts on completion/failure.
