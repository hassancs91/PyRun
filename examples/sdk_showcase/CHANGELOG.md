# Changelog

All notable changes to the SDK Showcase plugin are documented here. This
project adheres to [Semantic Versioning](https://semver.org/).

## [1.3.0] - Unreleased

### Added
- A **Watcher** card. `triggers.py` ships the plugin's own watcher adapter,
  `CounterAdapter` (`sdk_showcase:counter_up`): it watches the demo counter
  and reports each increment (or each multiple of its `every` setting). The
  card turns a watcher on and off with `WatcherAPI.sync` / `pause` / `resume`;
  each increment then starts one run of the demo script, its message filled
  from the item through `input_map`. The adapter also appears in the watcher
  form's picker for any script.
- "Reset demo data" starts a new counter generation, so the watcher's event
  ids stay unique after a reset.

### Notes
- Needs the plugin SDK from PyRun 2.5 (`WatcherAPI`, `TriggerAdapter`).

## [1.2.0] - Unreleased

### Added
- An **Inputs & result** card. The demo script now declares two inputs,
  `message` and `steps`, through `ScriptAPI.upsert(inputs_schema=)`, with the
  saved settings as their defaults, so **Run demo** and the schedule behave as
  before. *Run with inputs* queues one run with `queue_run(inputs=...)`; a
  value the SDK refuses (try 99 steps) shows its reason on the field, and no
  run is created. The card shows the latest run's `RunView.inputs`, its live
  `RunView.progress` and, once it finishes, its `RunView.result`.
- The demo worker reads its inputs with `pyrunner_run.inputs()` (falling back
  to the data store), reports progress with `pyrunner_run.progress()` and
  returns its run summary with `pyrunner_run.set_result()`.

### Changed
- The demo message is limited to 200 characters, the same limit as the
  `message` input.
- The Schedule card shows the SDK's error when a schedule is refused instead
  of failing the request.

### Notes
- Needs the plugin SDK from PyRun 2.5 (`inputs_schema=`, `queue_run(inputs=)`,
  `InputsError`). The manifest's `api` and `min_pyrunner` move to `2.8` and
  `2.5.0` when the SDK version is bumped for that release.

## [1.1.0] - 2026-10-17

### Added
- A **Vectors** card: setup provisions a vectors-only collection
  (`sdk_showcase:demo`) through `VectorAPI`; *Index sample docs* upserts six
  short docs with `add_vectors`, and the search box runs `search_vector` with
  an optional payload filter by topic. The vectors are toy word hashes, so the
  card works with no embedding provider configured and spends nothing.
- The demo worker opens the same collection with the `pyrunner_vectors`
  helper and logs the doc nearest to its configured message.

### Notes
- Targets plugin SDK API `2.6`, which ships with PyRun 2.0.0 — `min_pyrunner`
  moves to `2.0.0` when that release is cut.

## [1.0.0] - 2026-06-23

First release — a reference plugin that demonstrates the whole plugin SDK
(`core.plugins.api`) on one page.

### Added
- One **Set up demo** save that idempotently provisions a data store, an
  owner-scoped secret, a managed script, and a schedule.
- Capability cards, each demonstrating one SDK surface with the code behind it:
  **data store** (counter), **secrets** (encrypted + selected-mode injection),
  **run lifecycle** (`queue_run` / `latest_run` / `runs` / `cancel_latest_run`
  with live progress + Stop), **worker output**, **schedule** (`sync`/`list`),
  and **ownership** (the owned-resource inventory).
- A standard-library-only demo worker, so it runs in any environment.

### Notes
- Requires PyRun **1.13.0+** (plugin SDK API `2.1`).
- The worker needs **no third-party packages**.
