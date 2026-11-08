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
