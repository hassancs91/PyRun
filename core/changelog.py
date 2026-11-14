"""
Release notes shown in-app at /cpanel/changelog/.

Keep the newest release first. Each entry groups its changes by a short tag
("Added" / "Improved" / "Fixed" / "Security"); the template maps tags to colors.
When you cut a release, bump ``pyrunner/version.py`` and add an entry here.
"""

CHANGELOG = [
    {
        "version": "2.5.0",
        "date": "Unreleased",
        "headline": (
            "Scripts you can call like functions from the API, MCP, webhooks and chat, "
            "connected to your accounts and started by what happens in them."
        ),
        "changes": [
            {
                "tag": "Changed",
                "title": "Upgrading from 2.0: what behaves differently",
                "body": (
                    "Download a backup before you update: the upgrade changes the "
                    "database, and going back to 2.0 means restoring that backup. "
                    "Stopping a run, or a timeout, now gives the script 10 seconds to "
                    "shut down before it is killed (set its Stop grace period to 0 for "
                    "the old behaviour). Each script's webhook takes 60 calls a minute "
                    "and 30 a second by default, a body over 2.5 MB is refused, and a "
                    "call during a maintenance window gets 503. The forgot-password "
                    "email is sent by the background worker. Uploading a newer version "
                    "of an installed plugin now updates it, with a rollback. The "
                    "changelog's upgrade notes list every change."
                ),
            },
            {
                "tag": "Added",
                "title": "Scripts with inputs and results",
                "body": (
                    "Declare what a script takes on its Inputs tab and Run now asks for "
                    "it in a form. The pyrunner_run helper reads the inputs and the event "
                    "that started the run, sets a result and reports progress, and every "
                    "run page shows them. One event describes every trigger, from Run now "
                    "to a webhook call or a chat message."
                ),
            },
            {
                "tag": "Added",
                "title": "Run control: skip or queue, retries, maintenance windows",
                "body": (
                    "Choose what happens when a script is already running (allow, skip "
                    "or queue), retry a failed run with backoff, give a stopped run time "
                    "to clean up, keep plain settings in per-script env vars, and plan "
                    "maintenance windows that pause schedules, webhooks and chat for the "
                    "whole instance or one workspace."
                ),
            },
            {
                "tag": "Added",
                "title": "Scripts and data over the API and MCP",
                "body": (
                    "Tokens can list, read, run and stop scripts and read runs, data "
                    "stores, tables, databases, vectors and the rest of the workspace. "
                    "Turn on Expose as MCP tool and an AI client calls a script by name, "
                    "with its inputs as the tool's arguments. Data writes exist only while "
                    "Settings → API & MCP → Allow API writes is on (off by default), and a "
                    "token can be bound to one script, data store or table."
                ),
            },
            {
                "tag": "Added",
                "title": "Webhooks that verify and reply",
                "body": (
                    "A webhook run gets the whole request, headers and body of any size. "
                    "Require a Stripe, GitHub, Shopify or custom HMAC signature, set each "
                    "script's rate limits, and add ?sync=1 to wait for the reply the "
                    "script sets. The script page lists the last 20 calls."
                ),
            },
            {
                "tag": "Added",
                "title": "Connections and integrations",
                "body": (
                    "Connect an account once, with an API key or by signing in to Google, "
                    "Microsoft 365, GitHub, Slack or Notion, and grant it to the scripts "
                    "and plugins that need it; OAuth tokens are refreshed for you and "
                    "stored encrypted. pyrunner_integrations gives scripts typed "
                    "helpers for Airtable, Notion, Google Sheets, Drive, Gmail, SMTP, "
                    "Slack, GitHub and any HTTP API."
                ),
            },
            {
                "tag": "Added",
                "title": "Watchers start a script when something new shows up",
                "body": (
                    "A watcher checks a source on an interval, a JSON list, a feed, a "
                    "mailbox, Gmail, a Google Sheet, GitHub or new database rows, and "
                    "runs its script once for each new item, with the item as the run's "
                    "event. Plugins can make watchers and add their own sources."
                ),
            },
            {
                "tag": "Added",
                "title": "WhatsApp channels and a delivery log",
                "body": (
                    "Connect a WhatsApp Business number through Meta's Cloud API: "
                    "notifications, replies, templates, files, buttons and lists, with "
                    "incoming messages starting scripts. Every message a channel sends is "
                    "logged with its status, and a send that fails for a temporary reason "
                    "is retried."
                ),
            },
            {
                "tag": "Added",
                "title": "Tables and one data grid",
                "body": (
                    "Make a table with typed columns in the console and fill it from "
                    "scripts (pyrunner_tables), plugins, the API or a CSV file. The same "
                    "grid now browses data stores, managed database tables and vector "
                    "points, with filters."
                ),
            },
            {
                "tag": "Added",
                "title": "Plugin updates, signatures and a store",
                "body": (
                    "Upload a newer version of an installed plugin to update it, and roll "
                    "back if you need to. Signed plugins show Verified, and PyRun's own "
                    "Verified · PyRun; a trust policy can allow signed plugins only. The "
                    "Store tab installs and updates plugins from a signed index, and "
                    "Install from URL takes a zip link."
                ),
            },
            {
                "tag": "Security",
                "title": "Two-factor sign-in, sign-in alerts and security events",
                "body": (
                    "Add an authenticator app and recovery codes to your account, or "
                    "require two-factor sign-in for everyone under Users → Security. "
                    "Turn on an email after every sign-in. Forgot password no longer "
                    "reveals which accounts exist, admins can hand over a reset link, and "
                    "sign-ins, failures, lockouts and other account changes are listed "
                    "for admins for 90 days."
                ),
            },
            {
                "tag": "Improved",
                "title": "Py AI knows the workspace and the docs",
                "body": (
                    "Py AI reads scripts, runs, data stores, tables, connections, "
                    "watchers and the rest of the workspace, checks whether all is well, "
                    "and answers how-to questions from PyRun's own documentation with a "
                    "link to the right section. It still changes nothing."
                ),
            },
            {
                "tag": "Improved",
                "title": "Backups can include installed plugins",
                "body": (
                    "Tick Include installed plugins in Settings → Backup, for a download "
                    "or the scheduled backup, and a restore installs the plugins the "
                    "server doesn't have, turned off for you to activate. Backups made "
                    "by 2.0 restore as before."
                ),
            },
        ],
    },
    {
        "version": "2.0.0",
        "date": "October 23, 2026",
        "headline": (
            "A versioned Brain for your workspace, one API for your AI clients, "
            "and vector search for scripts and plugins."
        ),
        "changes": [
            {
                "tag": "Changed",
                "title": "Upgrading from 1.x: what behaves differently",
                "body": (
                    "Selected secret injection now means what the script form says: "
                    "a script in Selected mode gets only the secrets attached to it "
                    "(plugin scripts also keep their own plugin's secrets). Before "
                    "2.0 a Selected script also received every unowned workspace "
                    "secret, so attach the ones it uses or switch it to All. "
                    "Each script now runs in its own working folder, "
                    "/app/data/workdir/<script id>/, kept between its runs and removed "
                    "with the script; files written to the shared folder before 2.0 "
                    "stay there, so move any a script still reads into its folder. "
                    "The Coolify compose file now caps the container at 2048 "
                    "processes and threads; the per-run process limit can't stop a "
                    "fork bomb while scripts run as root, and Settings → Isolation "
                    "now says so. Datastore and plugin API tokens are now stored "
                    "hashed, like Unified API tokens: existing tokens keep working, "
                    "the list shows only their first and last characters, and "
                    "going back to 1.x after the upgrade invalidates them."
                ),
            },
            {
                "tag": "Changed",
                "title": "Upgrading from 1.x: new image name, compose file and name",
                "body": (
                    "New releases are published as hasanaboulhasan/pyrun; an install "
                    "that still pulls hasanaboulhasan/pyrunner:latest stays on the "
                    "last image published under that name, so change the image name "
                    "when you update. On Coolify, Redeploy uses the compose file the "
                    "resource already has: a 1.x Docker Compose resource keeps the 1.x "
                    "compose, without the process limit, the trusted-proxy depth (login "
                    "lockout and per-IP limits then see the proxy's address for every "
                    "visitor) or the database and private-network settings. Replace it "
                    "with the 2.0 docker-compose.coolify.yml, keeping your environment "
                    "values, before you redeploy; a Docker Image resource needs the new "
                    "image name and RATELIMIT_TRUSTED_PROXY_DEPTH=1 added by hand. An "
                    "upgraded instance keeps its saved name \"PyRunner\": change it "
                    "under Settings → General → Instance name if you want \"PyRun\"."
                ),
            },
            {
                "tag": "Added",
                "title": "Brains: knowledge with history and explicit access",
                "body": (
                    "Create a Brain from the included template or import a ZIP or "
                    "public GitHub archive. Browse and edit files, compare immutable "
                    "revisions and grant each token, plugin or agent a visibility tier. "
                    "Canonical content lives in the database and travels with backups."
                ),
            },
            {
                "tag": "Added",
                "title": "REST, MCP and Connect share one tool registry",
                "body": (
                    "Create a permission-scoped Unified API token and connect an AI "
                    "client through stateless MCP, or call REST and generated OpenAPI. "
                    "Connect shows the commands. URL-only read credentials are opt-in "
                    "and expire. Existing datastore and plugin token scopes are unchanged."
                ),
            },
            {
                "tag": "Added",
                "title": "Capture, review and ask your Brain",
                "body": (
                    "Capture text or URLs from the console, API, scripts, plugins and "
                    "granted agents. An AI provider prepares a validated proposal; an "
                    "owner or administrator reviews its diff before anything is applied. "
                    "The confined reader returns cited context, optional vector search "
                    "finds related notes, and cost caps bound admitted work."
                ),
            },
            {
                "tag": "Added",
                "title": "GitHub sync keeps human review in the loop",
                "body": (
                    "Pull repository changes into the Brain inbox for review. Export "
                    "an immutable revision to a dedicated branch after acknowledging "
                    "that every visibility tier is copied. Updates never force a moved "
                    "branch; optional polling only queues pulls. Tokens use Secrets."
                ),
            },
            {
                "tag": "Added",
                "title": "Vector stores, embedding providers and SDK 2.7",
                "body": (
                    "Configure embedding providers and Local or Qdrant collections, "
                    "then search, filter and manage points from the console, scripts "
                    "or plugins. Helpers keep credentials out of runs by default. "
                    "The SDK includes VectorAPI and grant-checked BrainAPI."
                ),
            },
            {
                "tag": "Added",
                "title": "More ways to schedule and manage scripts",
                "body": (
                    "Use timezone-aware cron expressions, yearly dates including leap "
                    "days, or any interval from 1 to 32767 minutes. Fork a script with "
                    "its schedule paused, search run output, and compare the last 30 "
                    "run durations. Manage and recover accounts with the user CLI."
                ),
            },
            {
                "tag": "Improved",
                "title": "Large run output keeps its ending",
                "body": (
                    "Each output stream is still stored up to 1 MB, but past that a "
                    "run keeps the first 200 KB and the last 800 KB with a marker "
                    "saying how much was cut, so the result line or the traceback at "
                    "the end is no longer lost."
                ),
            },
            {
                "tag": "Improved",
                "title": "Py AI can say why a script failed",
                "body": (
                    "In the console, Py AI can now read the exit code and the end of "
                    "the error output of a script's most recent failed run, so "
                    "\"why did it fail?\" gets an answer. It stays read-only and "
                    "limited to workspace owners and admins; chat channels don't get "
                    "this tool."
                ),
            },
            {
                "tag": "Fixed",
                "title": "Runs interrupted by a restart fail right away",
                "body": (
                    "When the container or its worker restarts mid-run, the worker "
                    "now marks runs whose process is gone as failed as soon as it "
                    "starts again, instead of showing them as running until their "
                    "timeout plus five minutes had passed."
                ),
            },
            {
                "tag": "Improved",
                "title": "PyRunner is now PyRun",
                "body": (
                    "The product, console and documentation use the PyRun name. "
                    "Existing pyrunner_* helpers, PYRUNNER_* variables, plugin class "
                    "names and data paths remain compatible. The public image moves "
                    "to hasanaboulhasan/pyrun; keep your existing volumes and keys."
                ),
            },
            {
                "tag": "Improved",
                "title": "Console: icons, script search and brain grant pickers",
                "body": (
                    "The sidebar and section cards use Lucide icons, drawn inline. "
                    "The Scripts list shows 25 per page and searches names and "
                    "descriptions; status, tag, owner, search and page stay in the URL "
                    "and combine. On a brain's Grants page, plugins and Py AI (the "
                    "console or a channel) are picked from a list by name, and the Py AI "
                    "page shows which brains it can read."
                ),
            },
            {
                "tag": "Fixed",
                "title": "Safer restores and more resilient workers",
                "body": (
                    "Failed database restores roll back (and one unusable database no "
                    "longer fails the whole restore), Brain history and attribution "
                    "survive backups, and weekly/monthly schedules restore correctly. "
                    "SQLite takes write locks before reads; Qdrant outages share a "
                    "short fail-fast window across workers. Reconciled failed runs "
                    "notify you, package installs terminate cleanly, and partial "
                    "environment creation no longer reserves a broken name. "
                    "manage.py migrate_db copies every table to PostgreSQL, so users, "
                    "API tokens, AI providers and channels come along, brain AI "
                    "calls work on a PostgreSQL core database, a finished run keeps "
                    "its result when that database restarts while it runs, a "
                    "worker killed hard is replaced cleanly instead of running twice, "
                    "and the Logs page keeps every process's lines after the log "
                    "file rotates."
                ),
            },
            {
                "tag": "Fixed",
                "title": "Plugin and console maintenance",
                "body": (
                    "Plugin minimum versions are enforced and unowned stray plugin "
                    "folders no longer prevent upload. The dashboard includes weekly "
                    "and monthly schedules, database-helper dependency guidance is "
                    "accurate, and changing the admin URL explains the required restart."
                ),
            },
            {
                "tag": "Improved",
                "title": "Polish from the 2.0 release test",
                "body": (
                    "Network failures read as one line (host, port and cause) with the "
                    "raw error under Details, and Test results appear next to the "
                    "button. Forms keep what you typed after a refused save, settings "
                    "tabs stay open after saving, and errors name the field. Runs say "
                    "when output was cut or a resource limit stopped them, schedules "
                    "show the next run in their own timezone, and forks are numbered. "
                    "The API tokens list shows bound brains and expired tokens, Connect "
                    "has Copy buttons, brain settings show the search index and "
                    "today's spend, and Logs open a message in full. Phones get "
                    "stacked headers and card lists where tables scrolled sideways, "
                    "the header fits tablet and small-laptop widths, and the Services "
                    "toggles and navigation have accessible names."
                ),
            },
            {
                "tag": "Improved",
                "title": "One console layout on every page",
                "body": (
                    "Every console page shares one shell and page header with a skip "
                    "link. Forms sit at form width with their help in a side panel and "
                    "Cancel beside the primary button; detail pages use the full width. "
                    "Brains get tabs, Services opens only the first unconfigured card "
                    "and has a section index, and Environments and Users are tables. On "
                    "phones, lists drop secondary columns instead of scrolling sideways. "
                    "Members who open an admin-only page see a 403 inside the console, "
                    "and the developer links moved to Settings → System → About PyRun."
                ),
            },
            {
                "tag": "Security",
                "title": "Explicit network access and credential boundaries",
                "body": (
                    "Login failures now enforce the configured account lockout. "
                    "Outbound helpers share SSRF protection; private networks require "
                    "operator opt-in and metadata addresses remain blocked. New API "
                    "tokens are hashed and scoped. Docker builds exclude local "
                    "environment variants and private artifacts."
                ),
            },
            {
                "tag": "Security",
                "title": "Tighter roles and lockout behind a proxy",
                "body": (
                    "Behind Coolify or another reverse proxy, login lockout now uses "
                    "each visitor's address, so failed logins can no longer lock an "
                    "account for everyone. Changing a password asks for the current "
                    "one. Only an Owner can grant or change the Owner role, only "
                    "superusers rename or delete shared tags, and members no longer "
                    "see other workspaces' script names or host details. A run "
                    "started by a plugin always uses its script's workspace."
                ),
            },
            {
                "tag": "Security",
                "title": "Pre-release security review",
                "body": (
                    "The Brain reader can no longer be talked into reading files "
                    "outside its snapshot, and AI agents PyRun starts never hold the "
                    "instance's own secrets; Py AI has no built-in tools. A user "
                    "removed from every workspace sees a \"not in any workspace\" "
                    "page instead of the console, and a run with no workspace no "
                    "longer receives other workspaces' secrets. Outbound fetches "
                    "connect only to the address the SSRF check approved. Escaping "
                    "helpers cover quotes, page source carries no developer notes, "
                    "CDN scripts use Subresource Integrity, and secret values are "
                    "masked until you click Show."
                ),
            },
            {
                "tag": "Security",
                "title": "Hardening from the 2.0 release test",
                "body": (
                    "External secret providers (custom, Vault, Infisical) now apply "
                    "the same SSRF guard as the rest of the app, so one can't be "
                    "pointed at the cloud-metadata endpoint. Delete and archive "
                    "confirmations escape the item's name. A malformed chat webhook "
                    "payload is answered with a 400 instead of an error. A gzip "
                    "backup that would unpack to more than 256 MB is refused "
                    "instead of filling memory (PYRUNNER_BACKUP_MAX_EXPANDED_MB)."
                ),
            },
            {
                "tag": "Fixed",
                "title": "Workspaces stay put",
                "body": (
                    "Inside a second workspace, sidebar links and saves no longer jump "
                    "back to the default workspace, and phones get a workspace list in "
                    "the menu. Members only see the pages they can open, workspace "
                    "owners can reach member management, malformed page filters no "
                    "longer error, and deleting a plugin with its data also removes "
                    "its managed databases."
                ),
            },
        ],
    },
    {
        "version": "1.16.0",
        "date": "July 29, 2026",
        "headline": (
            "Write a helper once and import it from every script, store files "
            "anywhere S3-compatible instead of just backups — and schedules now "
            "keep the timezone you actually picked."
        ),
        "changes": [
            {
                "tag": "Added",
                "title": "Libraries — shared code your scripts import",
                "body": (
                    "Stop pasting the same helper into five scripts. Create a "
                    "library under Libraries, add one or more modules in the "
                    "tabbed editor, attach it to a script from the script's "
                    "form, and import it: from my_helpers.utils import clean. "
                    "Every save that changes code creates a new version, and a "
                    "run is pinned to the version that was current when it was "
                    "queued — so editing a library can never change what an "
                    "already-queued run executes, and each run's page shows "
                    "exactly which versions it used. Full history per library "
                    "with a diff against the current code and one-click "
                    "restore. A library can't be deleted while a script still "
                    "imports it."
                ),
            },
            {
                "tag": "Added",
                "title": "Object storage — more than one bucket, more than backups",
                "body": (
                    "The S3 settings are now a real storage service. Save "
                    "several connections (Cloudflare R2, AWS S3, Backblaze B2, "
                    "DigitalOcean Spaces, MinIO, or any S3-compatible endpoint) "
                    "instead of exactly one, switch which one backups use with "
                    "a click, and point plugins at a different bucket so your "
                    "backup bucket stays private. Your existing configuration "
                    "carries over automatically — backups keep working with "
                    "nothing to re-enter. Scripts can store files with "
                    "import pyrunner_storage, no credentials and nothing to "
                    "install, because the bytes travel over PyRun's internal "
                    "API instead of putting S3 keys in your script."
                ),
            },
            {
                "tag": "Added",
                "title": "Plugins can serve APIs and publish shareable pages",
                "body": (
                    "A plugin can expose read-only HTTP resources that PyRun "
                    "serves for it, handling authentication, rate limiting, and "
                    "workspace scoping itself — the plugin never sees a raw "
                    "request or a token. New API tokens can be scoped to "
                    "exactly one plugin. A plugin can also publish a "
                    "read-only page at an unguessable link that needs no "
                    "login — useful for sharing a report with someone outside "
                    "your team — and every share is listed and revocable under "
                    "Settings, with revoking killing the link for good."
                ),
            },
            {
                "tag": "Fixed",
                "title": "Worker settings now actually apply",
                "body": (
                    "The values saved under Settings → Workers (worker count, "
                    "task timeout, retry delay, queue limit) were never read — "
                    "the cluster always ran on the built-in defaults no matter "
                    "what the page said. \"Restart workers to apply\" is "
                    "finally true."
                ),
            },
            {
                "tag": "Fixed",
                "title": "Long runs no longer get stuck at \"running\"",
                "body": (
                    "A script that ran longer than the worker's re-delivery "
                    "window could be handed to a second worker, which clobbered "
                    "the live run: stuck at running forever, output never "
                    "captured, force-stop broken. Duplicate deliveries are now "
                    "no-ops, the window sizes itself above your longest script "
                    "timeout, and the script form warns you when a timeout "
                    "would outrun it. Runs left behind by a killed worker "
                    "(container restart, out-of-memory) now heal themselves to "
                    "failed with an explanation instead of hanging forever."
                ),
            },
            {
                "tag": "Fixed",
                "title": "Schedules keep the timezone you picked",
                "body": (
                    "The timezone selector appeared once per schedule mode, and "
                    "the browser submitted all of them — so the hidden copy won "
                    "and your choice was silently replaced with UTC. Setting a "
                    "23:30 daily run in Asia/Beirut ran it at 23:30 UTC, with "
                    "no error and the selector reverting on reload. There is "
                    "now one timezone selector, shared by daily, weekly, and "
                    "monthly."
                ),
            },
            {
                "tag": "Fixed",
                "title": "Schedule history shows what actually changed",
                "body": (
                    "Changing a schedule's mode, interval, timezone, or "
                    "active state recorded nothing in its history, and an edit "
                    "that also changed run times recorded the new timezone as "
                    "though it had been the old one. History now captures the "
                    "real before and after."
                ),
            },
        ],
    },
    {
        "version": "1.15.1",
        "date": "July 28, 2026",
        "headline": (
            "Security patch: Docker images no longer contain built-in fallback "
            "keys — every deployment now runs on keys only you know. If you "
            "never set your own keys, this release makes that visible and "
            "walks you through fixing it."
        ),
        "changes": [
            {
                "tag": "Security",
                "title": "Baked-in image keys removed — check your deployment",
                "body": (
                    "Docker images up to 1.15.0 shipped with build-time "
                    "SECRET_KEY and ENCRYPTION_KEY values left inside the "
                    "image. A deployment that didn't set its own keys would "
                    "boot normally but run on those publicly-known values — "
                    "meaning sessions could be forged and secrets stored in "
                    "PyRun could be decrypted by anyone with the public "
                    "image. The keys are now stripped from the image, and "
                    "PyRun refuses to start on the old known values, with "
                    "clear instructions for generating real ones. If your "
                    "deployment never set its own keys: upgrade, generate "
                    "fresh keys, and rotate the secrets you had stored."
                ),
            },
            {
                "tag": "Fixed",
                "title": "Key-generation one-liners no longer hang",
                "body": (
                    "Running a one-off command in the container (docker run "
                    "... python -c \"...\") used to ignore the command and "
                    "boot the whole server, hanging your terminal — which "
                    "broke the documented way to generate keys. The container "
                    "now runs the command you give it and exits."
                ),
            },
        ],
    },
    {
        "version": "1.15.0",
        "date": "July 15, 2026",
        "headline": (
            "PyRun scripts get real SQL: managed PostgreSQL databases with "
            "tables, joins, and transactions — plus secrets you can pull live "
            "from Vault, AWS, Infisical, or Doppler instead of storing them here."
        ),
        "changes": [
            {
                "tag": "Added",
                "title": "Databases — real SQL for scripts & plugins",
                "body": (
                    "Scripts and plugins can now use a real PostgreSQL database "
                    "— tables, joins, indexes, transactions, the whole Python DB "
                    "ecosystem — alongside the simple key-value Data Stores. "
                    "Point PyRun at a data server with a single environment "
                    "variable (it can sit next to a SQLite core), then provision "
                    "databases from the console: each one is its own isolated "
                    "Postgres schema with a dedicated login role, so a script "
                    "granted access gets full SQL inside its own space and cannot "
                    "reach anything else. Scripts connect with the new "
                    "pyrunner_db helper; plugins provision their own through the "
                    "SDK. A read-only explorer lets you browse tables, export "
                    "CSV, and watch live activity — sessions, slow queries, and "
                    "sizes — and everything is included in backups."
                ),
            },
            {
                "tag": "Added",
                "title": "External Secret Providers — bring your own vault",
                "body": (
                    "A secret no longer has to live inside PyRun. Save a "
                    "provider profile for HashiCorp Vault / OpenBao, AWS Secrets "
                    "Manager, Infisical, Doppler, or any HTTP JSON endpoint, then "
                    "point a secret at it with a reference like kv/myapp#API_KEY. "
                    "The real value is fetched fresh at run time, masked in logs "
                    "exactly like a local secret, and never written to disk — so "
                    "rotating it in your vault is picked up automatically, and "
                    "grants, workspaces, and the plugin SDK keep working "
                    "unchanged. If a value can't be fetched the run fails fast "
                    "with a clear error, with an optional last-known-good "
                    "fallback for automation that must not stall."
                ),
            },
            {
                "tag": "Improved",
                "title": "Container-aware resource stats + pinned meters",
                "body": (
                    "When PyRun runs in Docker, the dashboard's CPU and "
                    "memory now show the container's own usage — matching "
                    "docker stats — with the full host/VPS as a secondary line, "
                    "and memory measured against the container's limit when one "
                    "is set. Every console page also gains a compact CPU / Mem / "
                    "Disk mini-meter in the header, updated live and "
                    "color-coded, so you can keep an eye on load from anywhere "
                    "in the app."
                ),
            },
            {
                "tag": "Fixed",
                "title": "Scheduling, workers & Postgres correctness",
                "body": (
                    "Scheduled jobs now run in the timezone you configured "
                    "instead of always UTC (daylight-saving handled), “Pause all "
                    "schedules” no longer knocks out the built-in maintenance "
                    "jobs, and “Restart workers” actually restarts them. Weekly "
                    "and monthly schedules show up in upcoming runs and backups. "
                    "Password-strength rules now apply everywhere a password is "
                    "set, rate limiting is shared across workers and can't be "
                    "starved, and a range of PostgreSQL-specific rough edges "
                    "(datastore writes, the database-size card, setup status) "
                    "are smoothed out."
                ),
            },
            {
                "tag": "Security",
                "title": "Provider-key masking, SSRF & plugin-ZIP hardening",
                "body": (
                    "AI now works with every configured provider and "
                    "third-party provider keys are masked in run output; webhook "
                    "notification URLs that resolve to internal addresses are "
                    "blocked (SSRF); and plugin ZIP installs enforce their size "
                    "limits against real extracted bytes with hardened "
                    "path-traversal checks."
                ),
            },
        ],
    },
    {
        "version": "1.14.0",
        "date": "July 15, 2026",
        "headline": (
            "PyRun now talks to more than Claude: save multiple AI provider "
            "profiles — Anthropic, Z.AI GLM, OpenRouter, even a local Ollama — "
            "and switch the active one with a click. Plus Telegram channels "
            "with an approval inbox, and Py AI, an assistant that knows your "
            "instance."
        ),
        "changes": [
            {
                "tag": "Added",
                "title": "AI Providers — bring any model",
                "body": (
                    "The Claude integration is now a generic AI provider system. "
                    "Save one profile per provider — Anthropic (subscription or "
                    "API key), Z.AI GLM, OpenRouter, a local Ollama, or any "
                    "Anthropic-compatible endpoint — with its own encrypted "
                    "credential and default model, and switch the active one "
                    "with a single click. Every provider gets a real Test "
                    "button: Anthropic runs a live web search, and other "
                    "providers run a real tool-call round-trip, so you know a "
                    "model can drive tools before wiring it into automations. "
                    "Existing Claude setups upgrade automatically — no "
                    "reconfiguration."
                ),
            },
            {
                "tag": "Added",
                "title": "Channels — Telegram, both directions",
                "body": (
                    "Scripts can send Telegram messages with the new "
                    "pyrunner_notify helper, and run-completion notifications "
                    "can be routed to channels. Inbound is protected by a "
                    "deny-by-default approval inbox: unknown senders queue up "
                    "until you approve them, with per-sender rate limits and a "
                    "daily reply cap."
                ),
            },
            {
                "tag": "Added",
                "title": "Py AI — ask your instance anything",
                "body": (
                    "A read-only assistant that answers questions about this "
                    "PyRun instance — “how many scripts do I have?”, "
                    "“did the backup run last night?” — using live "
                    "data from your scripts, runs, schedules, and datastores. "
                    "Chat from the dashboard, or bind it to a Telegram channel. "
                    "It can only read: no run, write, or secret access, by "
                    "construction."
                ),
            },
            {
                "tag": "Improved",
                "title": "Operational hardening",
                "body": (
                    "A shared cache backend (database-backed by default, Redis "
                    "opt-in) makes rate limiting and webhook dedup reliable "
                    "across workers; the Docker image gains a HEALTHCHECK and "
                    "Brotli-compressed static assets; and the repo now ships "
                    "CI (tests + linting), a Brand Tracker example plugin, and "
                    "expanded .env documentation."
                ),
            },
            {
                "tag": "Security",
                "title": "Pre-release security audit fixes",
                "body": (
                    "Removed the passwordless magic-link login, gated all "
                    "settings/environment/log administration strictly on "
                    "superuser, blocked pip option injection in bulk package "
                    "installs, and added a scoped Content-Security-Policy "
                    "header. Third-party AI provider keys are now masked in run "
                    "logs, webhook URLs that resolve to internal addresses are "
                    "blocked (SSRF), and plugin-ZIP installs enforce their size "
                    "limits against actual extracted bytes."
                ),
            },
        ],
    },
    {
        "version": "1.13.0",
        "date": "June 22, 2026",
        "headline": (
            "Operational plugins can now watch and stop the runs they create — "
            "live status, run history, and a Stop button — straight from the "
            "plugin SDK, with no coupling to PyRun's internals."
        ),
        "changes": [
            {
                "tag": "Added",
                "title": "Plugin SDK run-lifecycle surface (API 2.1)",
                "body": (
                    "ScriptAPI gains latest_run(), runs(), and "
                    "cancel_latest_run(), so a plugin can show a live “running…” "
                    "badge, list recent run history, and offer a Stop button for "
                    "the scripts it provisions — all owner- and workspace-scoped, "
                    "and without importing core.models. Runs are returned as a "
                    "small, JSON-serializable RunView read-model rather than live "
                    "database objects. Purely additive; existing plugins are "
                    "unaffected."
                ),
            },
            {
                "tag": "Improved",
                "title": "One shared force-stop path",
                "body": (
                    "Stopping a run from the plugin SDK reuses the exact same "
                    "kill path as the Tasks page Stop button (a running job's "
                    "process tree is killed, a queued one is dequeued, both marked "
                    "cancelled), so there is a single, consistent way a run is "
                    "force-stopped across the whole product."
                ),
            },
        ],
    },
    {
        "version": "1.12.0",
        "date": "June 22, 2026",
        "headline": (
            "Plugins now carry rich, packaged metadata — author, license, an "
            "icon, links, categories, and a declaration of what they create — "
            "shown on a new plugin detail page. Groundwork for a plugin marketplace."
        ),
        "changes": [
            {
                "tag": "Added",
                "title": "Plugin metadata + detail page",
                "body": (
                    "A plugin's plugin.json can now declare an author, license, "
                    "icon, summary, homepage/repository/documentation links, and "
                    "categories. Each plugin gets a detail page that surfaces all "
                    "of it, and the plugins list shows the icon and tagline. Every "
                    "field is optional, so existing plugins are unaffected."
                ),
            },
            {
                "tag": "Added",
                "title": "Bundled plugin icons",
                "body": (
                    "Plugins can ship an icon file (e.g. assets/icon.svg) referenced "
                    "from the manifest. It's served straight from the plugin folder, "
                    "so it renders even before a plugin is activated and needs no "
                    "external hosting, with an emoji fallback when none is provided."
                ),
            },
            {
                "tag": "Added",
                "title": "“What this plugin creates”",
                "body": (
                    "A plugin can declare the resources it provisions (scripts, "
                    "secrets, data stores, schedules) in its manifest. You see "
                    "“creates 1 script, 3 secrets, 1 schedule” at install time "
                    "and on the detail page — before granting it anything."
                ),
            },
            {
                "tag": "Improved",
                "title": "Doctor checks plugin metadata",
                "body": (
                    "The activation doctor now validates manifest metadata: it "
                    "refuses malformed values (bad version, an icon path that "
                    "escapes the plugin folder, a wrong-shaped provisions block) "
                    "and advises when marketplace-recommended fields are missing."
                ),
            },
        ],
    },
    {
        "version": "1.11.0",
        "date": "June 21, 2026",
        "headline": (
            "Plugin Platform v2 — develop plugins live, build them on a stable "
            "SDK, and let them own their own scripts, secrets, and data with no "
            "risk to your core database."
        ),
        "changes": [
            {
                "tag": "Added",
                "title": "Plugin Dev Mode",
                "body": (
                    "Point PYRUNNER_PLUGIN_DEV at a local plugin folder under "
                    "`manage.py runserver` and edit it live — code and templates "
                    "reload instantly, with no upload, preflight, or restart. "
                    "Triple-guarded so it never loads on a production server."
                ),
            },
            {
                "tag": "Added",
                "title": "Plugin SDK (core.plugins.api)",
                "body": (
                    "Plugins now orchestrate scripts, secrets, datastores, "
                    "schedules, and runs through a stable, versioned SDK instead of "
                    "reaching into core internals. It auto-stamps ownership + the "
                    "workspace, is idempotent (re-saving config updates the same "
                    "rows, never duplicates), and runs everything through the real "
                    "execution + scheduling paths."
                ),
            },
            {
                "tag": "Added",
                "title": "Resource ownership + scoped secrets",
                "body": (
                    "Scripts, secrets, and datastores a plugin creates are grouped "
                    "under it with an owner pill, delete-guarded on the generic "
                    "pages (so you can't pull the rug out from under a plugin), and "
                    "removed cleanly on uninstall. Secrets can be injected per "
                    "script (Selected mode) instead of all-at-once — fully opt-in, "
                    "so existing scripts are unchanged."
                ),
            },
            {
                "tag": "Added",
                "title": "Plugin doctor",
                "body": (
                    "Activation now runs a static-lint 'doctor' that refuses a "
                    "rule-breaking plugin with a clear per-rule report — before any "
                    "of its code or migrations can run. Plugins ship no database "
                    "models, so no plugin can ever alter a core table. Authors can "
                    "run `manage.py plugin_doctor` on a folder before shipping."
                ),
            },
            {
                "tag": "Security",
                "title": "No plugin DDL reaches the database",
                "body": (
                    "Plugins persist via owned key-value DataStores, not their own "
                    "tables. With no plugin models or migrations, the entire class "
                    "of 'a plugin migration broke the database' is removed by "
                    "construction — verified live against Postgres."
                ),
            },
        ],
    },
    {
        "version": "1.10.0",
        "date": "June 15, 2026",
        "headline": (
            "Plugins — extend PyRun with self-contained apps you upload, "
            "validate, and activate, with a hard guarantee that a broken plugin "
            "can never take down your site."
        ),
        "changes": [
            {
                "tag": "Added",
                "title": "Plugin system",
                "body": (
                    "Install plugins from the console under Plugins (superuser "
                    "only): upload a .zip, then Activate to validate and enable it. "
                    "A plugin is a self-contained Django app that adds pages to the "
                    "sidebar and serves at /plugins/<slug>/ — no core edits, no "
                    "fork. See docs/plugins.md and examples/example_plugin for how "
                    "to write one."
                ),
            },
            {
                "tag": "Added",
                "title": "Safe by design — a broken plugin can't break the site",
                "body": (
                    "Installed is not active: uploading never imports plugin code. "
                    "Activation validates the plugin in a throwaway subprocess "
                    "first, and every boot re-checks each active plugin in "
                    "isolation, auto-quarantining any that fail. Plugin pages and "
                    "compute are sandboxed, and a kill switch "
                    "(PYRUNNER_DISABLE_PLUGINS=1) guarantees a clean recovery boot."
                ),
            },
            {
                "tag": "Added",
                "title": "Run plugin compute in an environment",
                "body": (
                    "Plugins run heavy or third-party-dependent work inside a "
                    "chosen environment's venv via run_in_environment — an isolated "
                    "subprocess with a timeout and captured output — so extra "
                    "packages never touch the main app."
                ),
            },
        ],
    },
    {
        "version": "1.9.0",
        "date": "June 14, 2026",
        "headline": (
            "A redesigned console, full mobile support, Claude AI in your "
            "scripts, and real control over running jobs."
        ),
        "changes": [
            {
                "tag": "Added",
                "title": "Claude AI integration",
                "body": (
                    "Connect Claude under Services → Claude AI and call it "
                    "straight from your scripts with the bundled pyrunner_ai "
                    "helper. Token usage is tracked per run and per script so you "
                    "can see exactly what each automation costs."
                ),
            },
            {
                "tag": "Added",
                "title": "Force stop running jobs",
                "body": (
                    "Stop a running script for real — the Stop button now "
                    "kills the script's whole process tree (and any child "
                    "processes it spawned), instead of just marking it cancelled "
                    "and waiting for the timeout. The background worker is never "
                    "touched, so the queue keeps processing the moment a job is "
                    "stopped. Stop is available from the Tasks page and the Run "
                    "detail page."
                ),
            },
            {
                "tag": "Added",
                "title": "Per-task detail pages",
                "body": (
                    "Every task is now clickable — drill into queued, "
                    "completed, failed, and system tasks to see the function, "
                    "arguments, result, timing, live PID, and full tracebacks "
                    "for failures. The Run detail page gained task metadata and a "
                    "live PID while running."
                ),
            },
            {
                "tag": "Improved",
                "title": "Redesigned console",
                "body": (
                    "The entire control panel and the authentication screens "
                    "have been rebuilt on a new, consistent design system — "
                    "cleaner panels, clearer status, a refined type scale, and a "
                    "cohesive color and spacing language throughout."
                ),
            },
            {
                "tag": "Improved",
                "title": "Mobile responsive layout",
                "body": (
                    "PyRun now works on phones and tablets. The sidebar "
                    "becomes an off-canvas drawer, and tables, editors, and forms "
                    "reflow to fit small screens so you can manage runs on the go."
                ),
            },
        ],
    },
    {
        "version": "1.8.2",
        "date": "Earlier release",
        "headline": "",
        "changes": [
            {
                "tag": "Added",
                "title": "Admin-configured auth emails",
                "body": "Authentication emails can be configured by an admin.",
            },
            {
                "tag": "Improved",
                "title": "Longer default timeout",
                "body": "Raised the default script execution timeout.",
            },
        ],
    },
    {
        "version": "1.8.1",
        "date": "Earlier release",
        "headline": "",
        "changes": [
            {
                "tag": "Security",
                "title": "S3 hardening",
                "body": "Security improvements for S3 storage and backups.",
            },
        ],
    },
]
