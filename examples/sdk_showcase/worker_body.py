"""
=============================================================================
 SDK SHOWCASE — demo worker  (managed by the "SDK Showcase" plugin)
=============================================================================

 A deliberately tiny, STANDARD-LIBRARY-ONLY script the plugin provisions to
 demonstrate the *worker side* of the plugin SDK. You do NOT run this by hand —
 the plugin page provisions it, its secret, a data store, and a schedule.

 Every run it:
   1. reads an injected secret from the environment ($DEMO_TOKEN), redacted,
   2. reads its inputs (message, steps) with pyrunner_run.inputs(), falling back
      to the non-secret config in the owned data store,
   3. asks the owned vector collection which sample doc is nearest the message
      (pyrunner_vectors — no API key, nothing to install),
   4. loops `steps` times, reporting live progress each step (pyrunner_run
      progress, plus the older data-store heartbeat),
   5. records a compact run summary into the data store and returns the same
      summary as the run's result (pyrunner_run.set_result).

 It needs NO third-party packages, so it runs in any environment.
=============================================================================
"""

import hashlib
import math
import os
import re
import sys
import time
from datetime import datetime

# Emoji/log lines crash on a non-UTF-8 console; PyRun captures stdout as UTF-8
# but make standalone runs robust too.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# The injected secret (selected-mode grant) — present when run under PyRun.
DEMO_TOKEN = os.environ.get("DEMO_TOKEN", "")

# Derive the owner slug from the env var PyRun injects for owned-script runs,
# so the data-store name is never hardcoded (fall back to the literal slug).
OWNER = os.environ.get("PYRUNNER_OWNER_PLUGIN") or "sdk_showcase"
STATE_STORE = f"{OWNER}:state"
VECTORS = f"{OWNER}:demo"  # the collection the plugin provisioned
RUN_ID = os.environ.get("PYRUNNER_RUN_ID", "")  # ties live progress to this run
HISTORY_LIMIT = 20


def log(message):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}")


def _store():
    """The owned data store handle, or None when not running under PyRun."""
    try:
        from pyrunner_datastore import DataStore

        return DataStore(STATE_STORE)
    except Exception:
        return None  # local/standalone run — degrade gracefully


def load_config():
    """Read non-secret config (message, steps) from the data store."""
    defaults = {"message": "", "steps": 5}
    store = _store()
    if store is None:
        return defaults
    cfg = store.get("config", {}) or {}
    return {
        "message": cfg.get("message", defaults["message"]),
        "steps": int(cfg.get("steps") or defaults["steps"]),
    }


def run_inputs():
    """This run's inputs (defaults filled in by PyRun), or {} for a standalone
    run or a run of an older script that declared none."""
    try:
        import pyrunner_run
    except ImportError:
        return {}  # standalone run: PyRun's helpers aren't on the path
    try:
        return pyrunner_run.inputs()
    except pyrunner_run.RunError as exc:
        log(f"no run inputs ({exc}); using the data store config")
        return {}


def report_progress(pct, message):
    """Core-native live progress: the run page and RunView.progress show it.
    Best effort by design: pyrunner_run warns on stderr if PyRun can't take it."""
    try:
        import pyrunner_run
    except ImportError:
        return  # standalone run: no PyRun to report to
    pyrunner_run.progress(pct, message)


def report_result(result):
    """Store the run's structured result (RunView.result, the run page)."""
    try:
        import pyrunner_run

        pyrunner_run.set_result(result)
    except Exception as exc:
        log(f"could not store the result: {exc}")


def toy_embedding(text, dim=64):
    """The plugin's stand-in embedding (a signed hash per word, unit length) —
    a copy of provisioning.toy_embedding, which this standalone script can't import."""
    vector = [0.0] * dim
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        h = int(hashlib.sha256(word.encode("utf-8")).hexdigest(), 16)
        vector[h % dim] += 1.0 if (h >> 8) & 1 else -1.0
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


def vector_demo(message):
    """Best effort: the nearest sample doc to this run's message, via the loopback
    helper; returns its text, or None. A missing collection or a standalone run
    just logs and moves on."""
    try:
        from pyrunner_vectors import get_collection

        collection = get_collection(VECTORS)
        hits = collection.search_vector(toy_embedding(message or "hello"), k=1)
        nearest = f", nearest doc: {hits[0]['text']!r}" if hits else " (index the sample docs)"
        log(f"  vectors:      {collection.count()} point(s) in '{VECTORS}'{nearest}")
        return hits[0]["text"] if hits else None
    except Exception as exc:
        log(f"  vectors:      skipped ({exc})")
        return None


def write_progress(state, *, index=0, total=0, phase=""):
    """Best-effort live heartbeat the plugin page polls; tagged with this run id.
    A failure here never affects the run."""
    store = _store()
    if store is None:
        return
    try:
        store["progress"] = {
            "run_id": RUN_ID, "state": state, "index": index, "total": total,
            "phase": phase, "ts": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
        }
    except Exception:
        pass


def record_run(status, steps, token_seen, message, duration_s):
    """Append a compact run summary to the data store (best effort)."""
    store = _store()
    if store is None:
        return
    try:
        record = {
            "ts": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
            "status": status, "steps": steps, "token_seen": token_seen,
            "message": message, "duration_s": round(duration_s, 1),
        }
        runs = store.get("runs", [])
        if not isinstance(runs, list):
            runs = []
        runs.append(record)
        store["runs"] = runs[-HISTORY_LIMIT:]
    except Exception as exc:
        log(f"could not record run: {exc}")


def main():
    start = time.time()
    cfg = load_config()
    inputs = run_inputs()
    steps = int(inputs.get("steps") or cfg["steps"])
    message = inputs.get("message", cfg["message"])
    token_seen = bool(DEMO_TOKEN)
    if len(DEMO_TOKEN) >= 2:
        redacted = DEMO_TOKEN[:1] + "***" + DEMO_TOKEN[-1:]
    else:
        redacted = "***" if token_seen else "(none)"

    log("=" * 52)
    log("SDK SHOWCASE — demo worker starting")
    log(f"  owner slug:   {OWNER}")
    log(f"  DEMO_TOKEN:   {redacted}   (injected secret)")
    source = "run inputs" if inputs else "the data store"
    log(f"  message:      {message!r}   (from {source})")
    log(f"  steps:        {steps}")
    nearest = vector_demo(message)
    log("=" * 52)

    write_progress("running", index=0, total=steps, phase="starting")
    report_progress(0, "Starting")
    for i in range(1, steps + 1):
        log(f"  step {i}/{steps} …")
        write_progress("running", index=i, total=steps, phase="working")
        report_progress(round(100 * (i - 1) / steps), f"Step {i} of {steps}")
        time.sleep(1)

    duration = time.time() - start
    record_run("success", steps, token_seen, message, duration)
    write_progress("done", index=steps, total=steps)
    report_progress(100, "Done")
    report_result({
        "message": message,
        "steps": steps,
        "token_seen": token_seen,
        "nearest_doc": nearest,
        "duration_s": round(duration, 1),
    })
    log(f"done in {duration:.1f}s — recorded a run summary to '{STATE_STORE}'.")


if __name__ == "__main__":
    main()
