# Run context: inputs, results and progress

Every run of a script knows which run it is and what it was started with, and
can report back two things: a **result** (a JSON object) and **progress** (a
percentage and a status line). The `pyrunner_run` helper reads and writes all of
it. It is part of PyRun's script helpers: no install, standard library only.

```python
import pyrunner_run

params = pyrunner_run.inputs()                 # {"city": "Paris", "options": {...}}
pyrunner_run.progress(10, "Fetching the forecast")
forecast = fetch(params["city"], **params.get("options", {}))
pyrunner_run.progress(90, "Saving")
pyrunner_run.set_result({"city": params["city"], "days": len(forecast)})
```

---

## What every run gets

| Variable | Value |
|---|---|
| `PYRUNNER_RUN_ID` | the run's id, 32 hex characters |
| `PYRUNNER_SCRIPT_ID` | the script's id, 32 hex characters |
| `PYRUNNER_SCRIPT_NAME` | the script's name |
| `PYRUNNER_RUN_FILE` | path of the run's `run.json` (below) |
| `INPUT_<NAME>` | each text, number and yes/no input |

These are set for every run, whatever triggered it. (Before 2.5 the three ids
were set only when an AI provider was configured.) They are set after your
secrets, so a secret with the same name can't replace them.

`run.json` is created in the script's working folder when the run starts and
deleted when it ends:

```json
{
  "run": {"id": "3f2c…", "created_at": "2026-11-08T10:00:00+00:00", "started_at": "2026-11-08T10:00:01+00:00"},
  "script": {"id": "9a1b…", "name": "Weather report"},
  "trigger": {"type": "manual"},
  "inputs": {"city": "Paris", "options": {"units": "metric"}}
}
```

`trigger.type` is `manual`, `scheduled` or `api` (webhooks and chat channels).
`inputs` holds every input the run was started with, defaults filled in, JSON
inputs included, and is `{}` for a script that declares none.

---

## `pyrunner_run`

| Function | What it does |
|---|---|
| `inputs()` | The run's inputs as a dict (every input, JSON ones too). `{}` when the script declares none. |
| `run_id()` | The run's id (same as `PYRUNNER_RUN_ID`). |
| `set_result(result)` | Stores `result`, a dict, as the run's result. Calling it again replaces it. |
| `progress(pct=None, message=None)` | Shows how far the run is, live on its run page. |

**`set_result`** takes a dict that `json.dumps` can encode, without NaN or
Infinity, at most **256 KB** as JSON. Large data belongs in a DataStore or in
Storage; return a reference to it. Wrong arguments raise `TypeError` or
`ValueError` before anything is sent. If PyRun refuses the result or can't be
reached, `set_result` raises `pyrunner_run.RunError` (its `code` says why, for
example `RESULT_TOO_LARGE` or `RUN_NOT_RUNNING`), so a lost result never goes
unnoticed.

**`progress`** takes a number from 0 to 100 and/or a status line of at most
500 characters. What you leave out keeps its last value; `message=""` clears
the message. It is best effort: at most one update per second is sent, calls
in between are merged and the latest one is sent when the second is up (or when
the script exits). If PyRun can't take an update, a warning goes to the run's
stderr and the script carries on. Wrong arguments still raise straight away.

Both only work while the run is running. Outside a PyRun run, `inputs()`,
`run_id()` and `set_result()` raise `RunError`, and `progress()` writes its
usual warning to stderr.

---

## Where it shows up

- **Run page:** a **Result** card (formatted JSON, with a download link), and
  while the run is live, its progress bar and status line, updated without
  reloading. A finished run shows its last progress update.
- **Runs list:** a **Result** tag on runs that set one.
- **Notifications:** the webhook notification's JSON payload has `result` (the
  object, or `null`) and `result_too_large` (`true` when the result is over
  16 KB and was left out). Email shows the first 2,000 characters of the
  result, chat channels the first 300.
- **Plugins:** `RunView.result` and `RunView.progress` (see
  [plugins.md](plugins.md)).
- **Backups** include both.

---

## Secrets

PyRun masks secret values in a result and in a progress message **before**
storing them, the same way it masks them in a run's output: a value is
replaced with `[NAME:MASKED]`. Masking uses the secrets the run was given plus
the AI provider credential. If one of the run's secrets comes from an external
provider that can't be reached at that moment, the value can't be checked, so
PyRun refuses to store it and `set_result` raises `RunError` with code
`SECRETS_UNAVAILABLE`.

---

## How it works

`inputs()` and `run_id()` only read the run file and the environment.
`set_result()` and `progress()` POST to PyRun's internal API over loopback
(`/internal/runs/result`, `/internal/runs/progress`) with the run's signed
token (`PYRUNNER_INTERNAL_TOKEN`). The run is the one named in that token,
never one named in the request, so a script can only report on its own run,
and PyRun refuses any write once the run has finished. The run file sits in
the script's working folder, which the sandbox shares at the same path, so
sandboxed runs read it too.
