# Writing a PyRun plugin

A plugin is a self-contained Django app that adds UI and behavior to PyRun
**without editing any core file**. Admins upload it, activate it, and it serves at
`/plugins/<slug>/` and appears in the console sidebar.

The cardinal rule of the system: **a broken plugin can never break the main
site.** "Installed" (files on disk) is not "active" (loaded), nothing risky loads
into the live process without passing an isolated preflight first, and a
pre-activation **doctor** (see below) refuses rule-breakers before they can touch
the live process.

> **Plugin Platform v2** adds four things on top of the v1 system: **Dev Mode**
> (live local iteration), the **SDK** (`core.plugins.api` — orchestrate PyRun
> primitives without importing core internals), **resource ownership + scoped
> secrets** (your scripts/secrets/datastores are grouped, idempotent, and
> delete-guarded), and the **doctor** (a static-lint activation gate). Everything
> is additive — a v1 plugin keeps working.

---

## Folder layout

```
<slug>/                      # e.g. my_flows — a self-contained Django app
  __init__.py
  apps.py                    # subclass core.plugins.PluginAppConfig (import-light!)
  plugin.json                # manifest: slug + marketplace metadata (see below)
  urls.py                    # app_name = "<slug>"; auto-mounted at /plugins/<slug>/
  views.py
  provisioning.py            # optional; your SDK calls (create owned scripts/secrets/…)
  worker_body.py             # optional; the body of a managed Script you provision
  lib/                       # optional; *.py shared by your worker scripts (see Libraries)
  triggers.py                # optional; watcher adapters (see Watchers)
  lifecycle.py               # optional; update steps: reprovision(), on_update(from_version)
  CHANGELOG.md               # recommended; the doctor warns on an update without one
  assets/icon.svg            # optional; the manifest "icon" path (any path under <slug>/)
  templates/<slug>/...       # extend "base.html" so pages match the console
  static/<slug>/...          # optional
```

**Plugins ship NO `models.py` and NO `migrations/`.** Your database is owned
**DataStores** (see *Persistence* below) — so no plugin DDL ever reaches a core
table, and the entire "a plugin migration broke the DB" risk class is gone. The
doctor **rejects** any plugin that ships models/migrations.

The `slug` must match `^[a-z][a-z0-9_]*$` and be identical in three places: the
folder name, `plugin.json`'s `slug`, and `PyRunnerPlugin(slug=...)` in `apps.py`.
It must not be a reserved name (`core`, `theme`, `landing`, `plugins`, `admin`,
`static`, `api`).

## `plugin.json`

The manifest is the single carrier for a plugin's packaged metadata. It is stored
verbatim on install and surfaced on the plugin's detail page — and it is the
contract a future plugin **marketplace** reads. Keep it to **static, packaged,
author-owned** facts: anything that can change without shipping a new version
(ratings, install counts, price) is the marketplace server's job, not the
manifest's.

```json
{
    "manifest_version": 1,
    "slug": "my_flows",
    "publisher": "your_handle",
    "name": "My Flows",
    "version": "1.2.0",

    "summary": "Short one-liner shown on cards.",
    "description": "What this plugin does, in a sentence or two.",
    "icon": "assets/icon.svg",
    "icon_fallback": "🧩",
    "categories": ["automation"],
    "keywords": ["flows", "etl"],

    "author": "Your Name",
    "author_url": "https://example.com",
    "license": "MIT",
    "homepage": "https://example.com/my_flows",
    "repository": "https://github.com/you/my_flows",
    "documentation": "https://example.com/my_flows/docs",

    "api": "2.0",
    "min_pyrunner": "1.11.0",
    "max_pyrunner": "",

    "provisions": {
        "scripts": 1,
        "secrets": 3,
        "datastores": 1,
        "schedules": 1,
        "secret_keys": ["MY_API_KEY"]
    }
}
```

Only `slug` is strictly required; **every other field is optional** and a legacy
manifest with just `slug`/`name`/`version` still installs and activates. Field
reference:

| Field | Notes |
|---|---|
| `manifest_version` | Format version of the manifest itself. Currently `1` (omit ⇒ treated as 1). |
| `slug` | Must match the folder name + `apps.py` `app_name`. Instance-unique. |
| `publisher` | Marketplace namespace → global id is `publisher/slug`. Lowercase letters/digits/`_`/`-`. Local install stays slug-only. |
| `name`, `version` | `version` should be **semver** (`MAJOR.MINOR.PATCH`) — update-detection depends on it. Default to the slug / `0.0.0`. |
| `summary` | Short tagline for list cards (falls back to `description`). |
| `description` | Longer prose for the detail page. |
| `icon` | **Bundled file**, relative path that must stay under `<slug>/`. `.png` / `.svg` / `.webp` / `.jpg` / `.jpeg`. Served from disk, so it shows for installed-but-not-active plugins and works offline. SVG is served only as an `<img>` source, never inlined. |
| `icon_fallback` | Emoji shown when there's no bundled icon (or it fails to load). |
| `categories`, `keywords` | Lists of strings for discovery/filtering. |
| `author`, `author_url`, `license`, `homepage`, `repository`, `documentation` | Authorship + support. `license` is an SPDX id (e.g. `MIT`). |
| `api` | The lowest `core.plugins.api` version you use (see `API_VERSION` and the [SDK changelog](#sdk-changelog)). A plugin whose `api` is newer than this PyRun's SDK is refused at install, activation and update. |
| `min_pyrunner`, `max_pyrunner` | Compatibility bounds. |
| `provisions` | **Declares what the plugin creates** (resource counts + the secret keys it needs). Rendered before install/activation as a trust surface ("creates 1 script, 3 secrets, 1 schedule"). Counts are non-negative integers; `secret_keys` is a list of strings. |
| `requires_connections` | **Declares the accounts the plugin signs in to** (PyRun 2.5): a list of `{provider, key, scopes, label}`. The plugin page offers Connect or a picker for each, with its status (see [Connections](#connections--accounts-on-outside-services-pyrun-25)). |
| `provides` | **Declares what the plugin serves** — `api_resources` and `public_pages` (see [External API](#external-api--apiv1pluginsslug)). Undeclared handlers are never served: the manifest is the truth, same discipline as `provisions`. |

The doctor `warn`s (advisory, never blocks) when the marketplace-recommended
fields — `author`, `license`, `summary`, `icon` — are missing, and `fail`s on
*malformed* values (bad semver, an `icon` that escapes the folder or has an
unsupported extension, an unknown `manifest_version`, or a wrong-shaped
`provisions`). Those recommended fields become **required at marketplace
submission**, not at local install.

## `apps.py` (keep it import-light)

```python
from core.plugins import NavItem, PluginAppConfig, PyRunnerPlugin


class MyFlowsConfig(PluginAppConfig):
    name = "plugins.my_flows"     # always "plugins.<slug>"
    label = "my_flows"            # the app label (use the slug)
    plugin = PyRunnerPlugin(
        slug="my_flows",
        name="My Flows",
        version="1.0.0",
        nav_items=[
            NavItem(label="My Flows", url_name="my_flows:index"),
            # icon_svg="<path ... />"     # optional inline SVG <path>; omit for the default
            # superuser_only=True         # hide this item from non-superusers
        ],
    )
```

`apps.py` is imported **before the app registry is ready** (the boot loader's
light-import pre-check), so it must **not** import `core.models` / `core.tasks` /
`core.services` or any heavy third-party package at module top. Import those
**lazily inside functions**, or use the SDK (`core.plugins.api`), which is
import-light by design. The doctor enforces this (and the preflight asserts it
dynamically).

## `urls.py`

```python
from django.urls import path
from . import views

app_name = "my_flows"          # must match the slug

urlpatterns = [
    path("", views.index, name="index"),
]
```

Auto-mounted at `/plugins/my_flows/`. Reference routes as `{% url 'my_flows:index' %}`.

## Templates

Extend the console base and namespace under your slug so nothing shadows a core
template (the doctor checks this):

```django
{% extends "base.html" %}
{% block title %}My Flows - PyRun{% endblock %}
{% block content %}
<div class="flex">
    {% include "cpanel/_sidebar.html" %}
    <div class="flex-1 min-w-0">
        <div class="px-5 lg:px-8 py-7 max-w-[1440px] mx-auto space-y-5">
            <!-- your page -->
        </div>
    </div>
</div>
{% endblock %}
```

---

## Dev Mode — iterate locally with live reload

Develop a plugin from a local folder under `manage.py runserver`, with Django's
StatReloader reloading your `.py`/templates instantly — no zip, no upload, no
preflight, no restart.

```bash
export DEBUG=True
export PYRUNNER_PLUGIN_DEV=/abs/path/to/my_flows   # the folder IS the slug
python manage.py runserver
```

The dev plugin loads as `plugins.<slug>` (byte-identical to the shipped form), so
your `apps.py` (`name="plugins.<slug>"`) and `urls.py` need no changes between dev
and ship. It's triple-guarded — `DEBUG` **and** `PYRUNNER_PLUGIN_DEV` **and**
`RUN_MAIN` (the reloader child) — so the production WSGI/gunicorn path never loads
it. A dev plugin gets no `plugins`-table row and is invisible to the restart
detector. Validate a folder anytime with `manage.py plugin_doctor --path ./my_flows`.

---

## The plugin SDK — `core.plugins.api`

Orchestrate PyRun primitives (scripts, secrets, datastores, schedules,
environments, runs) **through the SDK** instead of importing `core.models` /
`core.tasks` / `core.services` directly. The SDK auto-stamps **ownership** (your
plugin slug) **and the workspace**, is **idempotent**, auto-names datastores, and
never bypasses the run/sandbox seams.

```python
from core.plugins.api import (
    EnvironmentAPI, ScriptAPI, SecretAPI, DataStoreAPI, DatabaseAPI, ScheduleAPI,
    VectorAPI, API_VERSION,
)

OWNER = "my_flows"   # your slug — passes ownership through every call

# Environments are SELECTED, never created by plugins:
env = EnvironmentAPI().get("data-science")          # read-only; .list() too

# Secrets — owner-scoped, injected under their CLEAN name:
SecretAPI(OWNER).upsert("R2_BUCKET", "my-bucket")   # idempotent by (owner, key)

# DataStores — your database (auto-named "<owner>:<key>"):
store = DataStoreAPI(OWNER).upsert("state")
store.set("config", {"retries": 3})
cfg = DataStoreAPI(OWNER).get("state").get("config")

# Scripts — idempotent on (owner, owner_key); plugin scripts default to
# injection_mode='selected' and isolation_mode='inherit' (the sandbox policy decides):
script = ScriptAPI(OWNER).upsert(
    key="backup", name="My Backup", code=generated_code, environment=env,
    timeout_seconds=3600, notify_on="failure",
)
SecretAPI(OWNER).grant(script, SecretAPI(OWNER).get("R2_BUCKET"))  # selected-mode injection

# Pick a venv once → every owned script follows:
ScriptAPI(OWNER).set_environment(env)

# Schedule + run, through the real RunBackend + scheduler:
ScheduleAPI(OWNER).sync(script, mode="daily", time_str="02:00", tz="UTC")
# Lists, like the editor's schedule form: several times, weekdays (0=Mon) or month days.
# A bad value raises ValueError and saves nothing (rules: docs/scheduling.md):
ScheduleAPI(OWNER).sync(script, mode="daily", times=["09:00", "18:00"], tz="UTC")
ScheduleAPI(OWNER).sync(script, mode="weekly", weekdays=[0, 2, 4], times=["07:30"])
ScheduleAPI(OWNER).sync(script, mode="monthly", month_days=[1, 15], times=["08:00"])
# ...or any 5-field cron expression, written in ``tz`` (converted to UTC for you):
ScheduleAPI(OWNER).sync(script, mode="cron", cron="0 9 * * 1-5", tz="Asia/Tokyo")
# Annual schedules use one calendar date; 29 February runs only in leap years.
ScheduleAPI(OWNER).sync(script, mode="yearly", month=6, day=15, time_str="02:00", tz="UTC")
ScriptAPI(OWNER).queue_run("backup")

# Observe + control that run — no core.models import:
view = ScriptAPI(OWNER).latest_run("backup")     # most recent run, or None
history = ScriptAPI(OWNER).runs("backup", limit=10)  # newest-first RunViews
ScriptAPI(OWNER).cancel_latest_run("backup")     # Stop button → True if cancelled

# Channels — read-only, workspace-scoped picker data (API 2.3):
from core.plugins.api import ChannelAPI, PublicPageAPI
channels = ChannelAPI(OWNER).list()   # [{name, channel_type, is_enabled}]

# Public pages — share/revoke a declared page (API 2.3; see "External API"):
url_path = PublicPageAPI(OWNER).share("report")   # → "/p/<token>/"
PublicPageAPI(OWNER).revoke("report")             # URL 404s permanently
```

Key behaviors:
- **Idempotent upsert.** `upsert(key=...)` keys on `(owner_plugin, owner_key)`, so
  re-saving config updates the same Script/Secret/DataStore — no duplicates on
  re-provision. (You no longer hand-store a `script_id`.)
- **Auto-naming.** A DataStore's stored `name` is `"<owner>:<key>"` (globally/
  per-workspace unique) while you refer to it by the short `key`.
- **Clean secret names.** An owner-scoped secret `R2_BUCKET` injects as
  `R2_BUCKET` into that owner's scripts — two plugins can both define `R2_BUCKET`.
- **Workspace.** Calls default to the default workspace; pass
  `ScriptAPI(OWNER, workspace=ws)` to target another.
- **Legacy lane.** `owner=None` (e.g. `SecretAPI().upsert(...)`) writes an
  unowned, global/user-namespace row — handy for porting old code gradually.
- **No seam bypass.** `queue_run` goes through `queue_script_run` (RunBackend +
  `resolve_isolation`); the SDK never touches raw SQLite or the scheduler directly.
- **Run event.** A run queued with `queue_run` has the event
  `{"kind": "plugin", "plugin": OWNER, "script_key": key, ...}` (plus `user`
  when you pass `triggered_by=`), which the script reads with
  `pyrunner_run.event()` (see [run-context.md](run-context.md#the-run-event)).

### Observe & control runs (API 2.1)

Once you've provisioned and queued a Script, watch and stop its runs through the
SDK — never by importing `core.models.Run` (which couples you to the schema and
trips the doctor's `sdk-usage` warn). Three owner+workspace-scoped methods on
`ScriptAPI`:

| Method | Returns | Use |
|---|---|---|
| `latest_run(key)` | `RunView \| None` | the most recent run of your script `key` |
| `runs(key, *, limit=20)` | `list[RunView]` | recent history, newest first |
| `cancel_latest_run(key)` | `bool` | cancel the latest pending/running run; `True` if one was cancelled or asked to stop |

`cancel_latest_run` reuses the **same** force-stop path as the tasks Stop button
(`TaskService.force_stop_run`). A *pending* run is dequeued and flipped to
`cancelled` at once. A *running* run is stopped gracefully (2.5): its script
gets SIGTERM and its stop grace period to shut down (see
[stopping a run](run-context.md#stopping-a-run)), and the run stays `running`
with `stop_requested_at` set until the process has ended, then turns
`cancelled`. Calling it again while the script shuts down kills it at once. It
returns `False` when nothing is cancellable.

A **`RunView`** is an immutable, ORM-free snapshot — no live `Run` leaks past the
SDK. Fields: `id, status, trigger_type, created_at, started_at, ended_at,
duration, exit_code, pid, task_id, is_finished, is_running, inputs, result,
progress, cancel_reason, is_held, stop_requested_at`. Its `.as_dict()` returns JSON-serializable values (datetimes → ISO
8601), so a status endpoint can return it directly:

```python
from django.http import JsonResponse
from core.plugins.api import ScriptAPI

OWNER = "my_flows"

def backup_status(request):
    """Live status for the plugin's dashboard — poll this from JS."""
    view = ScriptAPI(OWNER).latest_run("backup")
    return JsonResponse({
        "running": bool(view and view.is_running),   # drives a "backup running…" badge
        "latest": view.as_dict() if view else None,
        "history": [v.as_dict() for v in ScriptAPI(OWNER).runs("backup", limit=5)],
    })

def backup_stop(request):
    """A Stop button in the plugin UI."""
    stopped = ScriptAPI(OWNER).cancel_latest_run("backup")
    return JsonResponse({"stopped": stopped})
```

`inputs`, `result` and `progress` (PyRun 2.5) are what the run started with and
what its script reported with [`pyrunner_run`](run-context.md), all with secret
values masked: `inputs` is the dict of the run's inputs (None when the script
declares none, or when a secret can't be resolved, since the values can't be
checked), `result` the dict the script passed to `pyrunner_run.set_result()`,
and `progress` the latest `{"pct", "message", "updated_at"}` from
`pyrunner_run.progress()`. Both are None until the script sets them. A worker
script that calls `pyrunner_run.progress()` gets a progress bar on its run page
for free; a plugin can show the same thing from `RunView.progress`.

### One run at a time (PyRun 2.5)

A script that must never run twice at once doesn't need its own "is a run
already going?" check. Give it a concurrency policy when you provision it:

```python
script = ScriptAPI(OWNER).upsert(key="sync", ..., concurrency_policy="skip")

run = ScriptAPI(OWNER).queue_run("sync")
if run.cancel_reason == "concurrency_skip":
    messages.info(request, "A sync is already running.")
```

| `concurrency_policy` | While the script has a run in flight, a new run is... |
|---|---|
| `"allow"` (default) | started alongside it, as before 2.5 |
| `"skip"` | created already `cancelled`, with `cancel_reason == "concurrency_skip"`, and never executed |
| `"queue"` | held `pending` (`is_held`) and started when the runs ahead of it end, one at a time, oldest first |

It applies to every way the script starts: `queue_run`, its schedule, its
webhook, a chat channel and the console's Run now. That is the point: a
check in the plugin only ever covered the plugin's own button. `None` leaves
the policy as it is; an unknown value raises `ValueError`. Users can change
it in the script editor (**If it's already running**).

A [maintenance window](scheduling.md#maintenance-windows) that blocks manual
runs returns the run from `queue_run` already `cancelled`, with
`cancel_reason == "maintenance"`: check for it before reading any other
`cancel_reason` as "already running".

`latest_run(key)` passes over skipped runs (either reason), so a status badge
keeps showing the run that is actually going; `runs(key)` lists them, with
`cancel_reason` set. See [scheduling.md](scheduling.md#when-the-script-is-still-running) for
how queued runs are released.

### Inputs and results (PyRun 2.5)

A provisioned script can declare its inputs, so the plugin starts it like a
function call and reads back what it returned:

```python
from core.plugins.api import InputsError, ScheduleAPI, ScriptAPI

OWNER = "my_flows"

script = ScriptAPI(OWNER).upsert(
    key="report", code=WORKER_BODY, environment=env,
    inputs_schema={
        "type": "object",
        "properties": {
            "city": {"type": "string", "title": "City", "maxLength": 80},
            "days": {"type": "integer", "minimum": 1, "maximum": 14, "default": 3},
        },
        "required": ["city"],
    },
)

try:
    ScriptAPI(OWNER).queue_run("report", inputs={"city": "Paris"}, triggered_by=request.user)
except InputsError as e:
    for name, reason in e.field_errors.items():   # {"days": "Number is outside the allowed range."}
        form.add_error(name if name in form.fields else None, reason)

view = ScriptAPI(OWNER).latest_run("report")
view.inputs    # {"city": "Paris", "days": 3}  (defaults filled in)
view.progress  # {"pct": 40, "message": "Fetching", "updated_at": "..."}
view.result    # whatever the worker passed to pyrunner_run.set_result()
```

- **`upsert(inputs_schema=)`** takes an object JSON Schema (`"type": "object"`)
  in the small subset the script editor's **Inputs** tab and the API registry
  share: `type` (`string`, `number`, `integer`, `boolean`, `object`, `array`,
  `null`), `enum`, `title`, `description`, `default`, `required`, `maxLength`,
  `minimum`, `maximum`, `items`, `maxItems` and `additionalProperties`. Any
  other keyword is refused. It is checked the same way the editor checks it
  (input names are identifier-like, defaults must fit their input), and a bad
  schema raises `ValueError` before anything is saved. `{}` removes the declaration; leaving the argument out (or
  `None`) keeps the current one, as with every other `upsert` argument.
- **`queue_run(inputs=)`** checks the values before the run exists and fills
  in defaults. Bad values raise **`InputsError`** (a `ValueError`) with
  `.message`, one sentence naming every bad input, and `.field_errors`, a dict
  of input name to reason; no run is created, not even a failed one. Inputs
  for a script that declares none are refused the same way. The worker reads
  them with `pyrunner_run.inputs()`, and text, number and yes/no ones also
  arrive as `INPUT_<NAME>` (see [run-context.md](run-context.md)).
- **Schedules need defaults.** A scheduled run has nobody to supply inputs, so
  `ScheduleAPI.sync` refuses every mode but `manual` with a `ValueError` while
  a required input has no default, and `upsert` refuses such a schema while the
  script has an active schedule. Scheduled runs get each input's default.
- The worker reports back with `pyrunner_run.set_result({...})` and
  `pyrunner_run.progress(pct, message)`; the plugin reads both from `RunView`.

The **SDK Showcase** example's *Run with inputs* card is a working version of
all of this.

`worker_body.py` (the runtime script you provision) reads its credentials from the
injected, masked env vars (clean names), opens datastores with the normal
`from pyrunner_datastore import DataStore`, and can read `PYRUNNER_OWNER_PLUGIN`.

### Databases — real SQL (API 2.2)

When keyed JSON isn't enough (joins, indexes, aggregates over many rows), a
plugin can provision a **managed database**: a real PostgreSQL schema + role on
the instance's attached data server, isolated by Postgres itself. Auto-named
`"<owner>:<key>"` like DataStores.

```python
from core.plugins.api import DatabaseAPI, ScriptAPI

OWNER = "my_flows"

dbs = DatabaseAPI(OWNER)
if dbs.is_available():                       # data server attached?
    db = dbs.provision("metrics")            # idempotent; real schema + role
    dbs.grant(script, db)                    # the worker script may now connect
```

- **Requires a data server.** The instance must set `PYRUNNER_DATA_DB_URL`;
  `provision()` raises `DatabaseProvisionError` otherwise. Check
  `is_available()` and degrade gracefully (or fall back to a DataStore) when
  your plugin can work without SQL.
- **Explicit grants only.** There is no `'all'` mode: without
  `DatabaseAPI(OWNER).grant(script, db)` the worker's `pyrunner_db` calls get a
  `ValueError`.
- **In the worker script**: `import pyrunner_db` then
  `pyrunner_db.connect("my_flows:metrics")` — a plain psycopg connection whose
  `search_path` is preset to your schema. `connect()` needs `psycopg[binary]`
  in the script's *environment*; `pyrunner_db.dsn(...)` is stdlib-only.
- **In your plugin's views** (dashboard reading its tables):
  `DatabaseAPI(OWNER).dsn("metrics")` returns the scoped DSN (or `None` when
  missing/not ready) — connect with psycopg from the web process. Treat it
  like a password; the role can't leave your schema either way.
- **Declare it** in `plugin.json`: `"provisions": {"databases": 1, ...}`.

### Libraries — multi-module plugins (API 2.4)

A `Script` is one file. The moment your plugin is bigger than that — a pipeline
shared by several per-account worker scripts — put the shared code in a
**Library**: a named set of Python modules that scripts *attach* and import at
run time. Same grammar as secrets (create → attach → injected at run), so you
provision it once and attach it to every worker instead of duplicating the code
into each script's body.

Put the modules in a `lib/` folder in your plugin and sync it on provision:

```python
import os
from core.plugins.api import LibraryAPI, ScriptAPI

OWNER = "my_flows"
LIB_DIR = os.path.join(os.path.dirname(__file__), "lib")

libs = LibraryAPI(OWNER)
lib = libs.upsert_from_folder("my_flows_lib", LIB_DIR)   # idempotent; new version only on change

for account in accounts:                                  # one worker per account…
    script = ScriptAPI(OWNER).upsert(key=f"worker_{account}", code=WORKER_BODY)
    libs.attach(script, lib)                              # …all sharing ONE library
```

The worker script then imports it like any package:

```python
from my_flows_lib.pipeline import run_for_account   # modules inside use `from .helpers import x`
```

- **Versions are pinned per run.** Every content-changing save creates a new
  revision, and a run records the version that was current *when it was queued* —
  so editing a library can never change what an already-queued run executes. If a
  pinned version is gone at launch the run fails with a named error, never a
  confusing `ImportError`.
- **`upsert_from_folder` is idempotent by content.** Provisioning re-runs on every
  settings-save; identical modules write no revision, so history stays signal.
- **`lib/` is synced by YOUR provisioning, not by core.** Nothing re-reads the
  folder on activation by itself — the modules land in the database when your
  `upsert_from_folder` call runs, which in practice means when an admin saves your
  plugin's settings form (the same place you provision scripts and secrets). That
  is what keeps production runs deterministic from the database.
- **Dev Mode is the exception, and auto-resyncs.** With your plugin loaded via
  `PYRUNNER_PLUGIN_DEV`, editing a file in `lib/` is picked up by the next run
  automatically — no re-provision step, no settings-save. (One `lib/` folder maps
  to one library: if your plugin owns several in a workspace, the auto-resync can't
  tell which folder is which and skips with a warning — re-provision instead.)
- **The import name is the key**, so unlike DataStores it is *not* auto-prefixed
  with your slug (`"my_flows:lib"` isn't importable). Keys are unique per
  workspace — prefix yours with the plugin slug (`my_flows_lib`) to stay
  collision-free. Reserved: anything starting `pyrunner`, and every standard
  library name (`json`, `email`, `types`, …) — those would shadow the real module.
- **Modules are flat** (`helpers.py`, not `sub/helpers.py`); an `__init__.py` is
  generated if you don't ship one. Caps: 64 modules / 512 KB per version.
- **Declare it** in `plugin.json`: `"provisions": {"libraries": 1, ...}`.
- Users see the library under **Libraries** in the console, badged as managed by
  your plugin, with its version history and diffs. It is editable there — and your
  next provision overwrites it, same as an owned script.

### Object storage — files (API 2.5)

DataStores hold keyed JSON and Databases hold rows; neither is the place for a
**file**. When your plugin needs to keep bytes — an archived avatar, a generated
PDF, a chart PNG, a scraped image — use `StorageAPI`, which puts them in the
instance's S3-compatible assets bucket (Cloudflare R2, AWS S3, Backblaze B2,
DigitalOcean Spaces, MinIO, …).

```python
from core.plugins.api import StorageAPI

OWNER = "my_flows"

storage = StorageAPI(OWNER)
if storage.is_available():                       # assets connection configured?
    storage.put("avatars/abc.jpg", data, content_type="image/jpeg")
    link = storage.url("avatars/abc.jpg")        # public or presigned
```

In your **worker script**, the same space is reachable through a helper — no
credentials, no boto3, nothing to install:

```python
import pyrunner_storage

pyrunner_storage.put("avatars/abc.jpg", data, content_type="image/jpeg")
raw = pyrunner_storage.get("avatars/abc.jpg")    # None when missing
for obj in pyrunner_storage.list("avatars/"):
    print(obj["key"], obj["size"], obj["last_modified"], obj["etag"])
```

- **Requires an assets connection.** An admin picks one under **Services → Object
  Storage**; `is_available()` is `False` until they do and every call raises
  `StorageError`. Check it and degrade gracefully. It deliberately does *not*
  fall back to the backup bucket.
- **You are confined to `apps/<your-slug>/<workspace-id>/`, and you never write
  that prefix.** Every key you pass is relative to it, and `list()` hands keys
  back the same way. You cannot name another plugin's object — or another
  workspace's — so you cannot read or delete one. `StorageAPI(OWNER)` uses the
  default workspace and `StorageAPI(OWNER, workspace=ws)` scopes to `ws`, exactly
  like `SecretAPI` and `DatabaseAPI`; in a worker script the workspace comes from
  the run itself. Traversal (`../`, absolute paths, backslashes) raises
  `StorageKeyError` rather than being quietly rewritten.
- **`url()` has two modes, and the difference bites.** If the connection has a
  *public base URL*, you get a permanent hot-linkable URL. If not, you get a
  **presigned URL that expires** (`expires_in`, default 1 hour, 7-day ceiling) —
  fine for a redirect, wrong to bake into a stored page or an email. Ask the
  admin for a public base URL when you need durable links.
- **In the worker, objects are capped at 25 MB per call.** The bytes travel over
  PyRun's internal loopback API, which is what keeps S3 credentials out of
  your run. Storage in a worker is available to **plugin-owned scripts only**; a
  user's own script gets a 403.
- **Uninstall cleans up.** Deleting your plugin with *remove data* clears
  `apps/<your-slug>/` — every workspace's files — along with your scripts,
  secrets, and stores.

### Vector collections — search by meaning (API 2.6)

When your plugin needs semantic search — "find the five help articles closest to
this question", a RAG chatbot, de-duplicating similar items — use `VectorAPI`.
A collection holds points (an id, a vector, a JSON payload and optional text);
with an **embedding provider** attached, you hand it plain text and PyRun calls
the provider for you.

```python
from core.plugins.api import VectorAPI, VectorError

OWNER = "my_flows"

vectors = VectorAPI(OWNER)
if vectors.is_available():                      # a vector store is enabled
    docs = vectors.provision("docs", embedding_provider="OpenAI")   # by name or row
    docs.add_texts([
        {"id": "kb-12", "text": "How to reset a password", "payload": {"lang": "en"}},
    ])
    hits = docs.search("I forgot my password", k=5, filter={"lang": "en"})
    # → [{"id", "score", "payload", "text"}, ...], best first

    raw = vectors.provision("embeddings", dimension=384, metric="dot")  # bring your own vectors
    raw.add_vectors([{"id": "a", "vector": [...], "payload": {}}])
    raw.search_vector([...], k=10)
```

In your **worker script** the same collection is one import away — stdlib only,
no API key in the run:

```python
import os
from pyrunner_vectors import get_collection

OWNER = os.environ["PYRUNNER_OWNER_PLUGIN"]
docs = get_collection(f"{OWNER}:docs")
docs.add_texts([{"id": "kb-13", "text": "...", "payload": {"lang": "en"}}])
page, cursor = docs.scroll(limit=100)
```

- **Names.** `provision("docs")` stores the collection as `"<owner>:<key>"`, like
  DataStores and Databases; the worker opens it by that full name. The whole
  name fits 64 characters.
- **The shape is pinned.** The provider's model and vector dimension are fixed
  on the collection when it is created. Re-provisioning with the same arguments
  returns the same collection; a different provider, model, dimension, metric
  or store raises `VectorError` — a new model is a new key and a re-embed, never
  a silent change of space. `description=` is updated in place.
- **Filters** are JSON on top-level payload fields: `{"field": value}`,
  `{"field": {"$in": [...]}}`, `$gt` / `$gte` / `$lt` / `$lte`,
  `{"field": {"$prefix": "x"}}`, and `{"must": [...], "must_not": [...]}`.
  Types are strict (`1` never matches `true` or `"1"`), an array field matches
  when any element does, and `{"field": None}` means missing or null. Scores are
  always "higher is better", whatever the metric. Payload keys and filter
  fields starting with `_pyrun_` are reserved. On a Qdrant store, `$prefix`
  and string ranges are checked by PyRun, not the server, so on a large
  collection pair them with an equality or `$in` the server can narrow on.
- **Where it lives.** `provision()` uses the instance's default vector store
  (the built-in Local one unless an admin changed it) or `connection="name"`.
  Local is one SQLite file per collection — comfortable to ~50 000 points,
  refused above 200 000; a Qdrant connection has no such cap.
- **Access.** Your collections are open to your plugin's scripts only; a
  collection without an owner is open to every script in its workspace. Each
  run is braked at 2 000 embedding calls, 20 MB of embedded text and 200 000
  points written or deleted (`VectorError` code `run_budget_exceeded`).
- **Errors** are `VectorError` with a stable `code`: `invalid`,
  `unsupported_filter`, `dimension_mismatch`, `model_mismatch`,
  `collection_full`, `embedding_failed`, `backend_unavailable`, `not_ready`.
- **Qdrant outages** retain `503 backend_unavailable`. After a backend failure,
  subsequent calls on that connection fail immediately for 30 seconds across
  workers. Retry after the pause, or ask an admin to use Services Test; a
  successful test clears it. Requests use 3-second connect / 10-second read
  timeouts and retain the three retries for transient failures.
- **Uninstall cleans up.** Deleting your plugin with *remove data* deletes the
  collections it owns, points included. Declare them in `plugin.json` as
  `"provisions": {"vector_collections": 1}` (needs `"api": "2.6"`).

### Tables — typed rows (PyRun 2.5)

For records your plugin and its scripts share (leads, orders, a sync log), use a
[table](run-context.md#tables): typed columns, rows anyone in the workspace can
browse and edit on the **Tables** page, no SQL.

```python
from core.plugins.api import TableAPI, TableError

leads = TableAPI(OWNER).provision("leads", [
    {"key": "email", "type": "text", "required": True},
    {"key": "score", "type": "number"},
    {"key": "source", "type": "text", "default": "web"},
])
leads.insert({"email": "ana@example.com", "score": 7})
hot = leads.rows(where={"score": {"$gte": 5}}, order="-created_at", limit=50)
leads.import_csv("/path/to/leads.csv", dry_run=True)
```

- `provision(key, columns, *, name=None, description=None, remove_missing=False)`
  makes the table once and returns its handle; calling it again (every `ready()`)
  returns the same table, found by your plugin and `key`, so a rename on the
  Tables page doesn't break it. It is named `"<owner>:<key>"` with the slug
  `"<owner>-<key>"`; `key` uses lowercase letters, digits, `-` and `_`.
- Changed columns are applied the way the console's editor applies them: new
  columns are added, a changed type converts every value (refused whole, with a
  `TableError`, if one doesn't convert), a newly required column is refused while
  rows lack it. A column missing from your list is **kept** unless you pass
  `remove_missing=True`, which deletes its values.
- The handle has the same methods as the scripts' `pyrunner_tables` helper:
  `insert`, `rows`, `get`, `update`, `upsert`, `delete`, `count`, `import_csv`,
  `export_csv` (plus `model`, `name`, `slug`, `columns`). Errors raise
  `TableError` with a stable `code`. `TableAPI(OWNER).get(key)`, `.list()` and
  `.delete(key)` find, list and delete your tables.
- Your plugin's scripts open the table with `table("<owner>-<key>")`; other
  scripts get "not found". On the Tables page it carries your plugin's pill:
  members can edit its rows (and import CSV), not its columns. API tokens with
  `tables:read` see it too.
- Deleting your plugin with *remove data* deletes its tables, rows included.
  Declare them as `"provisions": {"tables": 1}`.

---

## Brains (SDK 2.7)

Use `BrainAPI` for owned brains or shared brains granted to your plugin. Every
read uses the same handler as REST, MCP and the worker helper. No grant means
unknown, including at the public tier.

```python
from core.plugins.api import BrainAPI, BrainError

brains = BrainAPI(OWNER, workspace=workspace)
brain = brains.provision("knowledge", name="Site knowledge")
brain.index()                    # {tier, index}: generated from current notes
brain.notes(kind="take")         # {tier, count, notes}
brain.note("take-example")       # {tier, entity_id, path, content}
brain.identity()                 # {tier, files}
brain.lens("default")            # {tier, name, content}
brain.raw("raw/archive.md")      # {tier, path, content}; visible linked raw only
brain.search("Useful context", k=5)  # {tier, hits, index_current}; optional vectors
brain.assemble_context(task="Draft a reply", lens="default")
                                # context_pack, verified sources, entity_ids_used, gaps
brain.propose(title="A thought", content="Source text", source_kind="thought")
                                # {feed_id, source_id, status, message, error}

brains.grant("knowledge", subject_kind="agent", subject_id="pyai",
             max_tier="agents-only")
shared = brains.get_brain("team-mind")  # requires BrainGrant(plugin, OWNER)
```

`provision(key, slug=None, name=None, created_by=None)` is idempotent by owner
and key; the default slug is `<owner>-<key>` (at most 64 slug characters). It
never reseeds existing content. An explicit conflicting slug is refused.
`list(offset=0, limit=1000)` returns accessible brains and `next_offset`; the
public `brain.list` endpoint accepts the same pagination. Grant management is
restricted to brains owned by this plugin; script/token subjects must belong to
the same workspace. Plugin-owned brains grant their owner private access.
Owned scripts inherit that access and the plugin's explicit grants.

Scripts use `from pyrunner_brain import get_brain`, then the same read methods.
The helper's `list_brains(offset=0, limit=1000)` discovers granted brains.
They call the signed loopback API even on SQLite. SDK read errors raise
`BrainError` with `code`/`status`; scripts import their `BrainError` from the
helper. Above-tier, ungranted and missing notes all return the same unknown
error. Py AI receives these tools only with an explicit `agent:pyai` grant;
execution rechecks grants, so revocation takes effect without a restart.

`assemble_context(task, lens="")` uses the brain's AI profile and daily cap.
Sources list only notes the confined reader actually opened. Changes to the
brain or access during the call discard the result and require a retry. Console
queries use Ask brain's background queue; scripts have a 45-second call budget.
MCP uses the configured MCP call budget. No personal CLI settings are loaded.

Semantic search is optional. Select a workspace vector collection with an
embedding provider in Brain settings. New revisions queue indexing; search
filters and verifies every hit against canonical tier text while it catches up.
The collection contains separate public, agents-only and private chunks. Use
a dedicated collection and a trusted provider: direct collection access is
broader than a Brain grant. `index_current=False` reports pending indexing.
Saving Brain settings retries indexing; canonical notes remain usable on errors.

`propose(title="", url="", content="", notes="", source_kind="doc")` captures
a source for the approval inbox. Owned brains permit proposals; shared brains
require an agents-only/private grant with `can_propose=True`. REST/MCP tokens
also need `brain:write`. A proposal never writes notes. The feeder runs through
the brain's AI provider, then an owner/admin approves the validated diff.
Payloads are bounded to 512 KB UTF-8; URL-only sources are fetched server-side
with the network guard, a 15-second budget and at most three redirects.
Fetch failures remain captured with an error for correction in the inbox.
Py AI exposes `remember` only with proposal rights. Channel agents use
`<provider>:<channel UUID>` instead of `pyai`; Telegram supports
`/remember <brain-slug> <text>` with that channel-specific grant.

Declare `"api": "2.7", "provisions": {"brains": 1}` in `plugin.json`.
Uninstall with *remove data* deletes owned brains and their revision history;
the delete preview includes them. Shared user brains are retained.

## Connections — accounts on outside services (PyRun 2.5)

A plugin that signs in to Google, GitHub, Slack, Notion, Microsoft 365, Airtable,
an HTTP API or an SMTP server doesn't run its own OAuth flow or keep tokens in
secrets. It declares the accounts it needs, the user connects them on the plugin
page, and its scripts get them through an ordinary grant, read with
[`pyrunner_connections`](run-context.md#connections) like any script. Refresh
tokens and client secrets never leave PyRun: a script, or plugin code, only
ever gets a short-lived access token.

Declare them in `plugin.json`:

```json
"requires_connections": [
    {"key": "mail", "provider": "google", "scopes": ["gmail_modify"], "label": "The mailbox to triage"},
    {"key": "youtube", "provider": "google",
     "scopes": ["https://www.googleapis.com/auth/youtube.force-ssl"]}
]
```

| Field | Notes |
|---|---|
| `provider` | Required. A preset key: `google`, `microsoft`, `github`, `slack`, `notion`, `airtable`, `http`, `smtp`, `imap`. |
| `key` | How your code names this connection. Lowercase letters, digits, `_`, `-`; defaults to the provider. Two connections of one provider need their own keys. |
| `scopes` | OAuth providers only. The preset's scope-set keys (the boxes on the connection form, e.g. `sheets`, `gmail_read`, `gmail_modify`, `drive_file`, `calendar` for Google) or raw provider scopes. Connect asks for them plus what the account lookup needs. |
| `label` | What the account is for, shown on the plugin page. |

The plugin's page in **Settings → Plugins** shows each entry with its status
(*Not connected*, *Connected* with the account, *Missing access* when the account
didn't allow every scope asked for, *Expired*, *Revoked*, *Error*) and:

- **Connect Google** (OAuth providers, once the workspace or the instance has an
  [OAuth app](integrations/oauth-apps.md)): the usual consent screen, then back to
  the page. It makes a connection named `<slug>-<key>` that this plugin holds;
  **Reconnect** asks again, for the scopes the entry needs as well as the ones
  the connection already had.
- **Use this one**: pick one of the workspace's own connections of that provider
  instead, for example one made by pasting a token on the Connections page (a
  box with no public callback) or an API-key one. It replaces the connection the
  plugin held for that key, if any.
- **Stop using**: the plugin lets go of it. The connection stays in
  **Connections** (it is the user's account), and the plugin's scripts lose
  their grants to it. Deleting the plugin with its data does the same for every
  connection it holds: connections are never deleted with a plugin.

Your own page shows the same card with one include:

```python
from core.plugins.api import ConnectionAPI

def index(request):
    return render(request, "my_flows/index.html", {
        "connection_requirements": ConnectionAPI(OWNER).requirements(),
    })
```

```django
{% include "cpanel/plugins/_connections.html" with requirements=connection_requirements next=request.get_full_path %}
```

In code:

```python
from core.plugins.api import ConnectionAPI, ConnectionUnavailable

conns = ConnectionAPI(OWNER)              # workspace= as everywhere else

req = conns.require("google", ["gmail_modify"], "The mailbox to triage", key="mail")
req.status          # "missing" | "ok" | "missing_scopes" | "expired" | "revoked" | "error"
req.missing_scopes  # the scopes asked for that the account didn't grant
req.connection      # a ConnectionView (name, provider, status, account_label, scopes, key), or None

view = conns.get("mail")                  # the ConnectionView serving "mail", or None
conns.list()                              # every connection this plugin holds

# Give a worker script the connection (idempotent; active=False switches it off):
script = ScriptAPI(OWNER).upsert(key="triage", ...)
if view:
    conns.grant(script, "mail")           # a key, a ConnectionView or a Connection

# Plugin code outside a run (a page's "Send now"): fresh auth material.
try:
    material = conns.credentials("mail")  # headers, params, token, base_url, expires_in
except ConnectionUnavailable as e:
    e.code  # CONNECTION_NOT_FOUND, CONNECTION_EXPIRED, CONNECTION_REVOKED, CONNECTION_REFRESH_FAILED...
```

- **`require()`** checks its arguments as the doctor checks the manifest (a
  `ValueError` for an unknown provider, a bad key or scopes) and saves nothing.
  Only manifest entries get the page's buttons.
- **Grants are yours to make.** The "grant to all my scripts" wildcard never
  covers plugin scripts, so grant each script that needs the account, when you
  provision it and on the page that shows the card (the user may connect after
  provisioning). A script that isn't granted gets `CONNECTION_NOT_FOUND`.
- **In the worker**, name the connection by its provider when it is the only one
  of that provider the script is granted (`pyrunner_connections.token("google")`),
  or pass `conns.get(key).name` to it (as an input, or in its datastore).
- **`credentials()`** refreshes an expiring OAuth token first, like a run's
  `token()`, and returns what `pyrunner_connections.credentials()` returns. It
  records no masks (there is no run): don't log it.

- **Moving off your own OAuth flow**: delete the secrets it stored (client
  secret, refresh token) once the connection replaces them. A plugin's own
  secrets reach every run of its scripts in `selected` mode, granted or not, so
  a stale refresh token would stay in the worker's environment.

The SDK Showcase example has no connection; the **YouTube Comments AI** example
holds a Google one for posting replies (and shows the upgrade from its own OAuth
flow: `provisioning.py`).

### Testing code that uses connections

Code that calls a service through a connection, with
[`pyrunner_integrations`](integrations/framework.md) or
`pyrunner_connections.credentials()` / `token()`, can be tested with no
account and no network with PyRun's testing kit, `core.integrations.testing`.
Put the calls in functions your worker script and your tests both import, then:

```python
from core.integrations import testing

from . import worker  # your plugin's module that calls the service


class FetchTests(testing.IntegrationTestCase):
    def test_new_items_are_fetched(self):
        self.use_connection("crm", provider="http", auth_kind="bearer",
                            config={"base_url": "https://crm.example.com"})
        fake = self.fake_http()
        fake.add("GET", "/v1/items", json={"items": [{"id": 1}, {"id": 2}]})
        self.assertEqual(worker.new_item_ids(), [1, 2])
        self.assertEqual(fake.requests[0].params, {"since": "0"})

    def test_an_expired_connection_is_reported(self):
        self.use_connection("crm", provider="http", error=("CONNECTION_EXPIRED", 409))
        self.assertEqual(worker.new_item_ids(), [])

    def test_against_a_recording(self):
        self.use_connection("crm", provider="http", auth_kind="bearer",
                            config={"base_url": "https://crm.example.com"})
        with self.cassette("items"):  # cassettes/items.json beside this file
            self.assertEqual(len(worker.new_item_ids()), 3)
```

`use_connection()` stands in for PyRun's loopback (made-up credentials unless
you pass some), `fake_http()` for the service, and `cassette()` replays a
recorded exchange; nothing sent through `pyrunner_integrations` or a
`pyrunner_connections.client()` session reaches the network unless a test
records (one nothing answers fails the test). For email, `fake_smtp()` is the
mail server `pyrunner_connections.smtp()` and `pyrunner_integrations.smtp`
talk to (it keeps every message sent); no real one is reached. Import
`testing` before the code under test: it makes `pyrunner_integrations` and
`pyrunner_connections` importable by the names a script uses. Use
`testing.IntegrationTestMixin` with a `TestCase` when the test needs the
database. Run them with `manage.py test plugins.<slug>` (or `examples.<slug>`).
Recording cassettes, the fake service's options and the conformance suite are
in [the integrations guide](integrations/framework.md#the-testing-kit).

---

## Watchers — runs when something new shows up (PyRun 2.5)

A [watcher](scheduling.md#watchers) checks a source on an interval and starts
one run of a script per new item. A plugin can make watchers for its scripts
with `WatcherAPI`, and ship its own sources (adapters) in `triggers.py`.

```python
from core.plugins.api import WatcherAPI

OWNER = "my_flows"
watchers = WatcherAPI(OWNER)          # workspace=None: the default workspace

# Idempotent on the key: call it on every save of your settings page.
watchers.sync(
    "digest",                          # one of your scripts (its key), or any Script of the workspace
    "rss_new_item",                    # an adapter key (the picker shows them on the watcher form)
    config={"url": "https://example.com/feed.xml"},
    interval=600,                      # seconds; at least 60
    key="blog",                        # default "<script key>:<adapter key>"
    input_map={"title": "item.title"}, # optional: script inputs from the event
)
watchers.pause("blog")                 # off; where the source is is kept
watchers.resume("blog")                # on again: failures cleared, checked within a minute
watchers.get("blog"), watchers.list()  # Watcher rows (enabled, last_checked_at, last_error, ...)
watchers.delete("blog")                # the runs it started stay
```

`sync` checks everything before it saves (ValueError: an unknown adapter,
settings its `config_schema` refuses, an interval out of range, an input the
script doesn't declare). An adapter that needs a connection takes one with
`connection=`: one of your `requires_connections` keys, a `ConnectionView` or a
Connection of its provider. A new watcher starts on; `sync` on an existing one
leaves it on or off, and changing its adapter, settings or connection starts
it over (its next check only marks where the source is). Pass `created_by=`
to say who gets the email if it turns itself off after 10 failed checks.

In the console a plugin's watcher carries the plugin's pill. People can turn
it off and on and press **Check now**, but not edit it (its form sends them
back); only a superuser can delete it there. **Deleting the plugin deletes its
watchers**, with or without its data; installing it again and calling `sync`
makes them anew.

### Your own source: `triggers.py`

Every class in `<slug>/triggers.py` that subclasses `TriggerAdapter` is an
adapter, offered as `"<slug>:<key>"` in the watcher form's picker of every
workspace (with the plugin's pill), to people and to `WatcherAPI` alike:

```python
# my_flows/triggers.py
from core.plugins.api import DataStoreAPI, TriggerAdapter, TriggerError


class NewOrder(TriggerAdapter):
    key = "new_order"                  # offered as "my_flows:new_order"
    title = "New order in my shop"
    description = "A run per order placed since the last check."
    config_schema = {                  # flat: string, integer, number, boolean settings
        "type": "object",
        "properties": {
            "min_total": {"type": "number", "minimum": 0, "default": 0, "title": "Smallest total"},
        },
    }

    def check(self, ctx, config, state):
        orders = DataStoreAPI("my_flows", workspace=ctx.workspace).get("orders")
        if orders is None:
            raise TriggerError("Set up the shop first.")    # shown as the watcher's error
        rows = orders.get("list", [])
        if state is None:                                  # the first check only marks
            return [], {"last": len(rows)}
        new = [r for r in rows[state["last"]:] if r["total"] >= config["min_total"]]
        new = new[: ctx.limit] if ctx.limit is not None else new
        events = [{"id": r["id"], "item": r} for r in new]
        last = rows.index(new[-1]) + 1 if new else len(rows)
        return events, {"last": last}                      # move only past what you return

    def test(self, ctx, config):                           # the form's Test button: read only
        orders = DataStoreAPI("my_flows", workspace=ctx.workspace).get("orders")
        rows = orders.get("list", []) if orders else []
        return f"{len(rows)} orders so far.", rows[-3:]
```

- `check(ctx, config, state)` returns `(events, new_state)`: events oldest
  first, at most `ctx.limit`, each `{"id": <stable id>, "item": <JSON>}`
  (the item becomes the run's event); `new_state` is JSON and only moves past
  the events returned. `state` is None on the first check, which reports
  nothing. Raise `TriggerError` for a failure worth showing; any other
  exception counts as a failed check too (and its traceback is logged).
- `test(ctx, config)` returns a short message and a few sample items. It must
  not change anything.
- `config` arrives with the schema's defaults filled in. `ctx` is a
  `TriggerContext`: `workspace` (pass it on to the other SDK classes),
  `watcher_id` (None for a Test of an unsaved form), `limit`, `timeout`, and,
  for an adapter with `requires_connection = True` (and `providers`, e.g.
  `("google",)`), `connection` (a `ConnectionView`) and `credentials()` (its
  auth material; `ConnectionUnavailable` when it can't be used).
- Both run in a worker thread that is waited for `ctx.timeout` + 5 seconds
  (20 + 5 now): keep them short.

`triggers.py` is checked by the doctor (`triggers` rule): each adapter needs a
literal `key` (a lowercase letter, then letters, digits and underscores;
`<slug>:<key>` at most 80 characters, unique), a literal `title`, its own
`check` and `test`, and, when written as literals, a flat `config_schema` and
known connection `providers`. The same rule runs again before a loaded
plugin's adapters are offered (so a dev-mode plugin is held to it too), and
each adapter object is checked once more when it is imported; one that fails
is left out (logged). The file is imported once per process, on first use.
When the file fails the rule or fails to import, the plugin's page shows
**Watcher types unavailable** with the reason (the traceback stays in the
server log). When a plugin goes away, watchers that use its adapters fail with "isn't
available" and, after 10 failed checks, turn off.

---

## Ownership & scoped secrets

Every resource the SDK creates carries your `owner_plugin` slug (a string, not an
FK — it survives plugin deletion) plus a stable `owner_key` handle. Owned
resources are:

- **Grouped & pill-marked** — they show an *owner pill* in the Scripts/Secrets/
  DataStores lists.
- **Delete-guarded** — a user can't delete them from the generic pages (the
  message routes them to your plugin); a superuser can force-delete with explicit
  confirmation, which cleanly drops dangling grants. Archiving is not guarded:
  anyone who can edit scripts in the workspace can archive a plugin-owned one,
  which also pauses its schedule, and restoring it does not resume the schedule
  (tick **Schedule Active** on the script's edit form again).
- **Cleaned up on uninstall** — `Delete plugin → remove data` deletes exactly the
  rows you own; user rows are never touched.

**Scoped secret injection (opt-in).** Every script has an `injection_mode`:
- `'all'` (the default for user scripts, and the literal pre-v2 behavior) — inject
  every user secret in the workspace.
- `'selected'` (the SDK default for plugin scripts) — inject only **granted**
  secrets + **same-owner** secrets, by clean name. Use
  `SecretAPI(OWNER).grant(script, secret)` to attach one. Since 2.0 a user's
  unowned secret reaches a plugin script only when it is attached (before 2.0
  every unowned secret was injected too).

This is purely additive: existing scripts stay `'all'`, byte-for-byte.

---

## Persistence — DataStores or Databases, never models

Plugins persist via **owned DataStores** (a named store × keyed JSON entries,
through `DataStoreAPI`) or — when the data is genuinely relational — an **owned
Database** (a real Postgres schema, through `DatabaseAPI`, API 2.2). What stays
deliberate: no plugin Django models means no plugin migration, so plugin DDL can
never reach a core table — a Database's DDL lives inside its own Postgres
schema, walled off by the database engine itself. The doctor still rejects
`models.py`/`migrations/`.

Rule of thumb: config, state, and small keyed blobs → DataStore (zero-config,
works everywhere). Rows you query, join, or aggregate → Database (requires the
instance to attach a data server).

The runtime `from pyrunner_datastore import DataStore` API is engine-portable
(SQLite direct, or a loopback API on Postgres) and unchanged.

---

## External API — `/api/v1/plugins/<slug>/…`

A plugin can expose **read-only HTTP resources** to the outside world (a
dashboard, a script on another machine, a cron job) without owning a single
URL: core mounts ONE dispatcher at
`/api/v1/plugins/<slug>/<resource>/[<item_id>/]` and owns 100% of auth, rate
limiting, and workspace scoping. Your handler never sees a raw request, a
token, or a tenancy decision.

Two pieces, and both must exist (**declared ∧ registered = servable**):

**1. Declare the resources in `plugin.json`** under `provides` (requires
`"api": "2.3"` or newer):

```json
"api": "2.3",
"provides": {
    "api_resources": [
        {"name": "mentions", "summary": "Deduped mention feed", "methods": ["GET"]},
        {"name": "stats", "summary": "Run/source counters", "methods": ["GET"]}
    ]
}
```

`methods` must be `["GET"]` — the plugin API is read-only in v1 (the shape is
POST-ready; writes become an additive manifest value when they're designed).

**2. Register a handler** in a conventional `<plugin>/api.py`, marked with the
SDK decorator. `api.py` imports **only** `core.plugins.api` (the import-light
lane, like `views.py`):

```python
# my_flows/api.py
from core.plugins.api import APIError, DataStoreAPI, resource


@resource("mentions")
def mentions(req):
    store = DataStoreAPI(owner="my_flows", workspace=req.workspace).get("state")
    if store is None:
        raise APIError("Not configured yet", code="NOT_CONFIGURED", status=409)
    page = max(1, int(req.params.get("page", 1)))
    page_size = min(100, max(1, int(req.params.get("page_size", 50))))
    items = store.get("mentions", [])
    start = (page - 1) * page_size
    return {
        "mentions": items[start:start + page_size],
        "count": len(items),
        "page": page,
        "page_size": page_size,
    }
```

The handler contract:

- **In:** a frozen `APIRequest` — `workspace` (opaque; pass it to SDK
  constructors — it is derived server-side from the token and you cannot widen
  it), `resource`, `item_id` (`None` for list calls), `method`, `params`
  (first value per query key) and `params_list` (every value:
  `?tag=a&tag=b` → `{"tag": ["a", "b"]}`).
- **Out:** a JSON-serializable **dict** that *is* the response body — core
  adds no envelope, so your feed shape stays natural.
- **Errors:** raise `APIError(message, code=..., status=...)` for a clean 4xx
  (status must be 400–499). Any other exception becomes a generic 500 with
  the traceback in the server log — internals never leak to the caller.
- **Pagination convention** (documented, not enforced): accept
  `page`/`page_size` params and include `count`/`page`/`page_size` in the
  body, mirroring the datastore API. Paginate **at the query level** — core
  rejects responses over ~1 MiB (`RESPONSE_TOO_LARGE`) as a backstop.
- **Stay read-bound.** Handlers run synchronously in the web process: read
  your DataStore/Database and return. No network calls (the doctor warns on
  `requests`/`httpx`/`urllib` imports in `api.py`); anything slow belongs in
  a provisioned Script.

**Calling it.** Consumers create a **plugin-scoped API token** (Settings →
API Tokens → scope "Plugin API") — one token per plugin, least privilege;
datastore tokens can't reach plugin APIs and vice versa. Then:

```bash
# Discovery — what can this token reach? (self-documenting integration)
curl -H "Authorization: Bearer $TOKEN" https://your-host/api/v1/plugins/

# The plugin's resource list
curl -H "Authorization: Bearer $TOKEN" https://your-host/api/v1/plugins/my_flows/

# A resource, with filters
curl -H "Authorization: Bearer $TOKEN" \
  "https://your-host/api/v1/plugins/my_flows/mentions/?page=1&page_size=20"
```

Errors use the datastore-API envelope `{"error": {"code", "message"}}`.
Rate limits: per-token 60/min and per-plugin 300/min (429 with `Retry-After`;
instance-configurable — see the deployment guide). CORS is enabled
(`API_CORS_ORIGINS`), preflights included, so browser dashboards work.

### Public pages — `/p/<token>/`

A plugin can also publish a **shareable, read-only HTML page** behind a
capability URL: anyone with the link reads the page, no login (the
`/webhook/<token>/` idiom). Declare it next to your API resources and mark a
handler with `@page`:

```json
"provides": {
    "public_pages": [
        {"name": "report", "summary": "Shareable mentions report"}
    ]
}
```

```python
# my_flows/api.py
from core.plugins.api import DataStoreAPI, page


@page("report")
def report(req):
    from django.template.loader import render_to_string

    store = DataStoreAPI(owner="my_flows", workspace=req.workspace).get("state")
    return render_to_string("my_flows/public_report.html", {
        "items": (store.get("items", []) if store else []),
    })
```

The handler receives a frozen `PageRequest` (`workspace` from the share row —
never from the anonymous caller — plus `page` and `params`) and returns an
**HTML string**.

**Rendering MUST go through Django templates** (auto-escape) — never
string-concatenated HTML. Public pages typically render scraped or
user-influenced content on an unauthenticated URL: an unescaped title is
stored XSS. Core defangs the class by serving every public page
**script-free** — CSP `default-src 'none'; style-src 'unsafe-inline';
img-src * data:` — so use inline `<style>` only, no JS, no external assets.
Pages are also sent `noindex` / `no-store` / `no-referrer` and set no cookies.

Sharing happens from your dashboard through the SDK:

```python
from core.plugins.api import PublicPageAPI

pages = PublicPageAPI("my_flows")          # workspace=None ⇒ default workspace
url_path = pages.share("report")           # idempotent → "/p/<token>/"
pages.get("report")                        # current share state (or None)
pages.revoke("report")                     # the URL 404s permanently
```

Revocation and expiry are **permanent for that URL**: re-sharing a revoked or
expired page reactivates it with a freshly rotated token — a dead link never
comes back. Users can audit and revoke every share under Settings → API
Tokens ("Public pages"). Only render data you would show on your own
dashboard; the per-IP rate limit and size cap are core's, not yours.

---

## Running real work: `run_in_environment`

Keep the web layer thin. Anything that needs third-party packages must run in a
**PyRun environment's venv** as an isolated subprocess — never imported into
the Django process:

```python
from core.plugins.api import EnvironmentAPI
from core.plugins import run_in_environment

env = EnvironmentAPI().get("data-science")
exit_code, stdout, stderr = run_in_environment(env, code="import pandas; print(pandas.__version__)", timeout=30)
# or a bundled file: run_in_environment(env, path="/app/plugins/my_flows/worker.py", args=["--n", "5"])
```

This reuses the hardened executor path (the env's Python, process-group isolation,
a timeout, captured + size-capped output). A bad package fails the *call*, not the
server. For long jobs, provision a Script and `queue_run` it instead.

---

## The plugin "doctor"

A pre-activation rules check (`manage.py plugin_doctor <slug | --path ./folder>`,
and run automatically at activation). Tier-1 is a **static lint** — file checks +
`ast.parse`, no execution — so it's safe on untrusted files. Severity is
`fail` (blocks activation) or `warn` (advisory):

| Check | Severity |
|---|---|
| Valid, non-reserved slug; manifest present + slug matches folder | fail |
| `__init__.py` + `apps.py` present | fail |
| **No `models.py` / `migrations/`** | fail |
| `apps.py` defines one `PluginAppConfig` with `name=="plugins.<slug>"`, `label==slug` | fail |
| `apps.py` imports `core.models` at module top | fail |
| `urls.py` `app_name == slug` | fail |
| Templates/static namespaced under `<slug>/` (no shadowing) | fail |
| Manifest metadata is malformed (bad semver, `icon` escapes folder / bad ext, unknown `manifest_version`, wrong `provisions` shape) | fail |
| `provides.api_resources` drift: declared resource with no `@resource` handler, missing `api.py`, non-GET `methods`, duplicate names, or manifest `api` < `2.3` | fail |
| `provides.public_pages` drift: declared page with no `@page` handler, missing `api.py`, duplicate names, or manifest `api` < `2.3` | fail |
| `requires_connections` malformed: not a list of objects, unknown provider, bad `key`, `scopes` that aren't scope names (or on a provider without OAuth), a too-long `label`, a duplicate key, more than 20 entries | fail |
| `triggers.py` adapters: doesn't parse, a `TriggerAdapter` subclass without a literal valid `key` (or a duplicate, or `<slug>:<key>` over 80 characters), without a literal `title`, without its own `check`/`test`, a literal `config_schema` that isn't flat, unknown `providers` | fail |
| `lifecycle.py` doesn't parse, or `reprovision` / `on_update` is async or can't be called with no argument / one argument | fail |
| Manifest `api` too old for what it declares: `"vector_collections"` in `provisions` needs `2.6`, `"brains"` `2.7`; `requires_connections`, a `triggers.py` or `"tables"` need `2.8` | fail |
| `MANIFEST.sha256` / `plugin.sig` present but a file changed, is missing or unlisted, or the signature doesn't check out (see [Signed plugins](#signed-plugins-and-the-verified-badge-pyrun-25)) | fail |
| `triggers.py` defines no `TriggerAdapter` subclass | warn |
| `lifecycle.py` defines neither `reprovision` nor `on_update` | warn |
| On an update (or `plugin_doctor --update-from <version>`): the version is not higher, or there is no `CHANGELOG.md` | warn |
| `apps.py` has heavy/third-party top-level imports | warn |
| Imports core internals directly instead of `core.plugins.api` | warn |
| Missing recommended marketplace fields (`author` / `license` / `summary` / `icon`) | warn |
| A `requires_connections` entry with a field the doctor doesn't know | warn |
| `@resource` handler not declared in `plugin.json` (the dispatcher will 404 it) | warn |
| `@page` handler not declared in `plugin.json` (the public view will 404 it) | warn |
| `api.py` imports `requests` / `httpx` / `urllib` (slow-handler smell) | warn |

The doctor runs **before** the preflight subprocess at activation, so a
rule-breaker is refused before any plugin code or migration could run. It never
runs on the boot path, so an already-active plugin stays active across an upgrade
even if new rules are added.

---

## Lifecycle (what the admin does)

1. **Upload** the `.zip` (Plugins → Upload). Validated (zip-slip/size safe) and
   unpacked; code is *not* imported. Status becomes `Installed`.
2. **Activate** runs the **doctor** (static lint) and then `plugin_preflight` in a
   throwaway subprocess (import + resolve URLs + assert apps.py didn't import
   `core.models`). On success → `Active`; on failure the per-rule report is shown
   and the live site is untouched.
3. **Restart** (a button appears) applies the change — gunicorn + the worker
   re-import the new active set, preflighting each in isolation first.
4. **Deactivate** keeps your data; **Delete** removes files + row, optionally
   deleting the resources your plugin owns.
5. **Update**: upload a zip of the same slug with a higher version. See
   *Updating a plugin* below.

If a plugin ever fails at boot, it's quarantined as `Errored` and skipped — the
site still boots. Last resort: `PYRUNNER_DISABLE_PLUGINS=1` boots with zero plugins.

**Backups (PyRun 2.5).** What a plugin owns (scripts, secrets, data stores,
tables…) is always in a backup. The plugins themselves are only when the backup
is made with **Include installed plugins** (Settings → Backup, off by default;
the scheduled backup has the same option). Each plugin then goes in with its
files, signature included. Restoring that backup installs the plugins this
server doesn't have, through the same checks as an upload (archive limits,
signature and the trust policy, `min_pyrunner`, `api`), as `Installed`: none is
activated, so an admin activates each one (doctor + preflight + restart). A
plugin already installed here is left as it is, even at another version, and a
restore never removes one. Update history and the kept previous version are not
in backups. Without the option, reinstall your plugins from their zips.

---

## Updating a plugin (PyRun 2.5)

Upload a zip of an installed plugin (Plugins → Upload, or **Update** on its page)
whose `plugin.json` `version` is **higher** (semver: `1.1.3` > `1.1.2`, and a
pre-release such as `1.2.0-rc.1` sorts before `1.2.0`). PyRun updates it in place:

1. The zip is checked like an install and unpacked next to the live folder. Its
   code is not imported.
2. A version that is the same or lower is refused ("Upload a higher version").
3. The **doctor** runs on the unpacked copy, with the update rules (version
   increase, `CHANGELOG.md`). A `fail` refuses the update.
4. `min_pyrunner` and the SDK `api` must fit this PyRun, and the zip must pass
   its [signature check](#signed-plugins-and-the-verified-badge-pyrun-25): a
   plugin installed signed only updates with a zip signed by the same publisher.
5. The live folder moves to `plugins/.previous/<slug>-<old version>/` and the new
   one takes its place. For an **active** plugin the new code must pass the
   isolated preflight; if it doesn't, the old folder goes straight back.
6. The plugin's row gets the new version and keeps the old one for a rollback.
   An active plugin needs a restart (the banner appears).

Every attempt, refused ones too, is listed under **Updates** on the plugin's page:
versions, source, who, outcome and the error. Dev-mode plugins
(`PYRUNNER_PLUGIN_DEV`) are never updated by upload.

**What an update never touches:** everything your plugin owns. Its secrets, data
stores, tables, scripts, schedules, watchers and connections stay as they are,
values included. Only the files change. If a managed script should run the new
version's code, re-provision it in `lifecycle.py`:

```python
# my_flows/lifecycle.py  (optional)
from . import provisioning as prov


def reprovision():
    """Re-apply the saved settings (idempotent, like a settings save)."""
    if prov.get_script() is None:
        return  # never configured: nothing to re-apply
    prov.provision(prov.initial_from_config())


def on_update(from_version):
    """Migrate stored data from an older version, once."""
    if from_version.startswith("1."):
        ...
```

Both functions are optional. They run **after the restart that loads the new
version**, while the plugin is active: `reprovision()` first, then
`on_update(from_version)`, once, in an isolated process that loads only your
plugin (`manage.py plugin_post_update`; the Docker entrypoint runs it after the
plugin preflight, never failing the boot; outside Docker run it yourself after
restarting PyRun). Several updates before one restart run them once, from the
oldest version. A plugin that is inactive keeps its steps waiting until it is
activated and PyRun restarts. If a step raises, the update is marked **Update
steps failed** with its output (your plugin's secret values masked); fix it with
a newer version or roll back. Brand Tracker 1.1.3's `lifecycle.py` is a working
example.

**Rollback.** **Roll back to <version>** on the plugin's page puts the kept
folder back, after checking it against the checksum taken when it was replaced,
restores the row and asks for a restart. After that restart `reprovision()` runs
(the old version's, if it has one), so provisioned code matches the files again;
`on_update` doesn't run for a rollback. The removed version's files are deleted;
upload it again to go forward. Only the latest previous version is kept, until
the next successful update; deleting the plugin deletes it too. Under the
**Signed plugins only** policy you can't roll back to an unsigned version.

## Signed plugins and the Verified badge (PyRun 2.5)

A plugin zip can carry two more files in its top folder, next to `plugin.json`:

- `MANIFEST.sha256`: every file of the plugin with its SHA-256, one
  `<sha256>  <path>` line per file, sorted by path (the `sha256sum` format, so
  `sha256sum -c MANIFEST.sha256` checks it too).
- `plugin.sig`: JSON with `format` (1), `slug`, `publisher`, `version`, `key_id`
  and `signature`, an Ed25519 signature over the manifest plus the slug,
  publisher, version and key id.

On install and update PyRun checks them on the unpacked copy, before anything
reads the plugin:

| The zip has | Result |
| --- | --- |
| Neither file | Installs as **Unsigned** (allowed by default). |
| `MANIFEST.sha256` only | Every file must match it, and no file may be missing or unlisted; installs as Unsigned. |
| Both, and they check out | Installs as **Verified**. Signed with PyRun's key, the plugin shows **Verified · PyRun** in the plugin list and on its page. |
| A signature that doesn't check out | **Refused**: a file changed, missing or unlisted, a bad signature, a key id this PyRun doesn't trust or that was revoked, or a signature for another slug, version or publisher (`plugin.json`'s `publisher`, when set, must be the signing one). |

PyRun trusts only its own keys, listed by key id in
`core/plugins/trust/keys.py` (a new key id is how a key is rotated). Signatures
from other publishers are refused for now; the format already carries the
publisher and key id they will need. The badge follows the current keys: when
the key that signed an installed plugin is later revoked or removed, the plugin
shows **Signature not trusted** with the doctor's reason instead of Verified.

**Key pinning.** Once a slug is installed signed, every update must be signed by
the same publisher (a new key id of that publisher is fine). An unsigned zip, or
one signed by someone else, is refused and recorded under **Updates**. A
rollback puts back a folder that was already installed here, so it isn't pinned.
Deleting the plugin forgets the pin.

**Trust policy.** Plugins → **Trust policy**: *Allow unsigned plugins* (the
default) or *Signed plugins only*, which refuses installing, updating, rolling
back to or activating an unsigned plugin. Plugins already active keep running.
`plugins.list` (API, MCP, Py AI) reports each plugin's `trust` and `signed_by`.

**The doctor** checks the files too: when a folder has `MANIFEST.sha256` or
`plugin.sig`, a changed or missing file or a bad signature is a `fail`
(`__pycache__` is skipped). So a signed plugin whose files were edited on the
server can't be activated again until it is re-installed.

**Signing (maintainers).** The private key stays offline, outside the repo and
the image; `plugin_sign` refuses a key path inside the project.

```bash
# once: a new key pair, and the entry to add to core/plugins/trust/keys.py
python manage.py plugin_sign --generate-key ~/keys/pyrun-2026.pem --key-id pyrun-2026

# every release: dist/<slug>-<version>.zip with MANIFEST.sha256 and plugin.sig
python manage.py plugin_doctor --path examples/brand_tracker
python manage.py plugin_sign examples/brand_tracker --key ~/keys/pyrun-2026.pem --key-id pyrun-2026
```

The plugin folder isn't changed. `__pycache__`, `.pyc` files and hidden files or
folders (names starting with `.`) are left out of the zip. `--publisher` names the
publisher (default `pyrun`), `--out` the folder for the zip (default `dist/`).

## The plugin store (PyRun 2.5)

Plugins → **Store** lists what a **store index** offers: a signed `index.json`
plus the plugin zips it points at, served as static files. Search, filter by
category, open a plugin to see its description, what it creates (its
`provisions`), its changelog link and its signature, then **Install** (or
**Update**, when an older version is installed). Installed plugins with a newer
version in the index are listed under **Updates available**; the Store tab
shows how many, and superusers see a notice on the dashboard.

**Which index.** Plugins → Store → **Store settings**. Empty means PyRun's
official index. That index isn't published yet, so for now the Store asks for
an index URL. A private index works too, as long as it is signed by a key this
PyRun trusts (`core/plugins/trust/keys.py`). The URL is in backups.

**Fetching.** PyRun reads the index every 12 hours in the background (the
`pyrunner-plugin-store` task, like the PyRun update check) and when you press
**Refresh** or save a new URL; the Store page itself never goes to the network.
The index must be at most 2 MB and pass the SSRF guard
(`PYRUNNER_ALLOW_PRIVATE_NETWORK`, docs/DEPLOYMENT.md), and it is used only
when its signature checks out with a trusted key that isn't revoked. The
cached copy is checked again each time it is read, so revoking a key also stops
an index already fetched. When a refresh fails, the Store says why and keeps
showing the last good copy of the same URL.

**Installing.** The zip is downloaded through the same guard, at most 50 MB
and at most the `size` the index gives, and must match the index's `sha256`
and hold that plugin's slug. Then it goes through the normal upload path: a new
slug installs (as **Installed**; activate it as usual), an installed one
updates in place (the update is recorded with the source *Store*), and the
signature, publisher pin and trust policy checks all apply. Install is
disabled, with the reason, when the plugin needs a newer PyRun (`min_pyrunner`)
or plugin SDK (`api`), or when it is unsigned and the trust policy is
*Signed plugins only*.

**Install from URL.** Plugins → Upload → **Install from URL** takes a zip link
(and, optionally, its sha256) and installs or updates it like an upload, with
the same guard and 50 MB cap. The plugin's page shows the source (*Upload*,
*Store* or *URL*) of its latest install or update; the **Updates** history keeps
each update's own.

**Updates are manual.** Each plugin has an update policy, shown on its page;
2.5 offers only *Manual*. Automatic updates are not available yet.

### The index format

```json
{
  "schema": 1,
  "generated_at": "2026-11-12T10:00:00Z",
  "plugins": [
    {
      "slug": "brand_tracker", "publisher": "pyrun", "name": "Brand Tracker",
      "version": "1.1.3", "summary": "…", "description": "…",
      "categories": ["Marketing"], "icon_url": "https://…/icon.png",
      "min_pyrunner": "2.5.0", "api": "2.8", "provisions": {"scripts": 1},
      "download_url": "zips/brand_tracker-1.1.3.zip",
      "sha256": "…", "size": 48213, "signed": true,
      "changelog_url": "https://…", "homepage": "https://…",
      "tier": "", "requires_license": false
    }
  ],
  "signature": {"publisher": "pyrun", "key_id": "pyrun-2026", "value": "<base64>"}
}
```

`slug`, `publisher`, `name`, `version` (semver), `download_url` and `sha256`
are required. A relative `download_url` (or `changelog_url`) is resolved
against the index's own URL, so a mirror can serve the same files; `icon_url`
must be https. `signed` is informational: the zip's own `plugin.sig` decides,
when it installs. `tier` and `requires_license` are reserved. The signature is
Ed25519 over a context line, the publisher and key id, and everything but
`signature` as canonical JSON (sorted keys, no spaces, ASCII), so whitespace
and key order don't matter but any changed value does. An index with a bad
signature, an unknown schema or any malformed entry is refused as a whole.

**Building an index (maintainers).** Sign each zip with `plugin_sign`, then:

```bash
python manage.py plugin_store_index dist/ --key ~/keys/pyrun-2026.pem --key-id pyrun-2026 \
    --base-url https://example.com/pyrun-plugins/zips/ --out index.json
```

It reads each zip's `plugin.json` (name, version, summary, description,
categories, `min_pyrunner`, `api`, `homepage`, `changelog_url`, `icon_url`,
`provisions`, `tier`, `requires_license`) and `plugin.sig`, adds the sha256 and
size, and signs the index. Without `--base-url` the download URLs are the zip
file names, relative to the index. One zip per plugin; the key path must be
outside the project, as for `plugin_sign`.

---

## SDK changelog

`core.plugins.api.API_VERSION` names the SDK a PyRun serves. Every change so far
is additive: a plugin written for an older version keeps working on a newer
one. Declare the lowest version whose features you use as `"api"` in
`plugin.json`. Install, activation and update refuse a plugin whose `api` is
newer than this PyRun's SDK ("needs plugin SDK 2.9; this PyRun has 2.8"). The
doctor checks `api` only for what a manifest or file declares (the rows that
name a manifest `api` in [the doctor table](#the-plugin-doctor)); a call to a
newer SDK class or kwarg is up to you to declare.

### 2.8 (PyRun 2.5)

Scripts and runs:
- `ScriptAPI.upsert(inputs_schema=...)`: the inputs the script declares (None
  keeps them, `{}` clears them). See [Inputs and results](#inputs-and-results-pyrun-25).
- `ScriptAPI.upsert(concurrency_policy=...)`: `"allow"`, `"skip"` or `"queue"`.
  See [One run at a time](#one-run-at-a-time-pyrun-25).
- `ScriptAPI.queue_run(key, inputs=...)` and the new exception `InputsError`
  (a `ValueError` with `.message` and `.field_errors`; no run is created).
- `RunView` gains `inputs`, `result` and `progress` (masked), `cancel_reason`
  (`"concurrency_skip"`, `"maintenance"` or ""), `is_held` and
  `stop_requested_at`; `as_dict()` carries them too.
- `ScheduleAPI.sync(times=..., weekdays=..., month_days=...)`: several run times
  a day, several weekdays, and the new `"monthly"` mode. The single-value
  `time_str=` / `weekday=` still work. `sync` refuses any mode but manual while
  the script declares a required input with no default.

Connections:
- `ConnectionAPI`, `ConnectionView`, `ConnectionRequirement`,
  `ConnectionUnavailable`, and `requires_connections` in `plugin.json`. See
  [Connections](#connections--accounts-on-outside-services-pyrun-25).

Watchers:
- `WatcherAPI`, `TriggerAdapter`, `TriggerContext`, `TriggerError`, and the
  plugin's own adapters in `triggers.py`. See
  [Watchers](#watchers--runs-when-something-new-shows-up-pyrun-25).

Tables:
- `TableAPI`, `TableHandle` and `TableError`, and `"tables"` in `provisions`.
  See [Tables](#tables--typed-rows-pyrun-25).

Updates:
- The optional `lifecycle.py` with `reprovision()` and `on_update(from_version)`,
  run once after an update's restart. See
  [Updating a plugin](#updating-a-plugin-pyrun-25). Signing (`MANIFEST.sha256`,
  `plugin.sig`, `publisher`) and the store need no SDK version.

The doctor wants `"api": "2.8"` for `requires_connections`, a `triggers.py` and
provisioned tables.

### 2.7
`BrainAPI`: owned brains and grant-checked shared read handlers
([Brains](#brains-sdk-27)); `"brains"` in `provisions` needs `"api": "2.7"`.

### 2.6
`VectorAPI`: owned vector collections on the Local store or Qdrant;
`"vector_collections"` in `provisions` needs `"api": "2.6"`.

### 2.5
`StorageAPI`: owned object storage under `apps/<owner>/` on the assets connection.

### 2.4
`LibraryAPI`: shared multi-module code attached to scripts.

### 2.3
The external API (`@resource`, `APIRequest`, `APIError`), public pages (`@page`,
`PageRequest`, `PublicPageAPI`) and the read-only `ChannelAPI.list()`.

### 2.2
`DatabaseAPI`: owned managed Postgres databases.

### 2.1
`ScriptAPI.latest_run`, `runs`, `cancel_latest_run` and the `RunView` read-model.

---

## Do / don't

**Do**
- Iterate with **Dev Mode**; validate with `plugin_doctor` before you ship.
- Orchestrate via **`core.plugins.api`**; persist via **owned DataStores**.
- Keep `apps.py` import-light; push compute into `run_in_environment` or a `Run`.
- Namespace templates/static/URLs under your slug.

**Don't**
- Ship `models.py` / `migrations/` (the doctor rejects them — use DataStores).
- Import `core.models`/third-party packages at module top in `apps.py`.
- Edit core files or rely on core internals beyond `core.plugins` / `core.plugins.api`.
- Do slow or failure-prone work in `ready()`.

A complete sample lives in [`examples/example_plugin/`](../examples/example_plugin/);
[`examples/qdrant_backup/`](../examples/qdrant_backup/) is a full SDK-based reference (config in an owned
DataStore, owner-scoped secrets, an idempotent managed script + schedule).

## Retrieval consumers (SDK 2.7)

`BrainAPI(owner, workspace, tier="public")` applies an additional visibility
ceiling to the plugin's existing grant; it cannot grant or elevate access.
Isolated scripts can use `pyrunner_brain.get_brain(slug, tier="public")` for
the same ceiling. Omitting the tier preserves the granted tier.

`VectorAPI.choices()` returns enabled provider and connection names for setup
forms, without credentials. A collection handle's `search(..., timeout=8)`
opts into a bounded HTTP budget (up to 30 seconds, no retries); omitting it
preserves the normal vector client behavior.

`AIProviderAPI.list()` returns configured API-key profile names.
`AIProviderAPI.complete(name, system=..., prompt=..., timeout=25)` makes a
tool-free, bounded Messages API call and returns `text`, `model`, and input/output
token `usage`. AI must be enabled and the profile must have a default model.
Credentials remain in core; provider errors are generic and redirects are
rejected. Subscription profiles require the agent runtime and are excluded.
The consumer must validate answers/citations, enforce its own rate and spend
limits, and persist returned usage if needed. This facade does not guess costs.

`PublicPageAPI(owner).resolve_token(token, page_name="chat")` resolves a live,
unexpired capability belonging to that plugin/page into a `PageRequest`, or
returns `None`. Its workspace comes from the capability; never substitute an
anonymous caller's query parameter. Passing a workspace to the API adds an
exact workspace check. Use this for plugin-owned interactive routes, recheck
after slow work to honor revocation, and enforce a separate per-token rate limit
with the shared `core.ratelimit` helper. The shared `/p/` renderer remains
script-free and GET-only.

Scripts that fetch URLs can import the stdlib `pyrunner_netguard` helper. It is
the same policy behind `core.netguard`, with the operator's private-network flag
injected by the executor. Check each redirect, restrict schemes, and bound
response sizes and total time in the consumer; the policy function only checks
whether an endpoint is allowed.
