# Run context: inputs, events, results and progress

Every run of a script knows which run it is, what it was started with (its
**inputs**) and what started it (its **event**: the webhook call, the chat
message, the schedule tick...), and can report back two things: a **result**
(a JSON object) and **progress** (a percentage and a status line). The
`pyrunner_run` helper reads and writes all of
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

### Script env vars

A script can carry plain settings of its own, such as a region, a base URL or
a feature flag: the **Env vars** tab of the script editor, one name and value
per row. Every run of the script gets them as environment variables:

```python
import os

region = os.environ.get("REGION", "eu-west-1")
```

Values are stored and shown as typed, to anyone who can open the script, and
they are **not masked** in a run's output. Keep API keys, tokens and passwords
in **Secrets**.

When the same name is set in more than one place, the later one in this list
wins:

1. PyRun's own environment (the server's variables)
2. the script's env vars
3. inputs (`INPUT_<NAME>`)
4. secrets
5. PyRun's run variables (`PYRUNNER_*` above, `WEBHOOK_*`, `INBOUND_*`)

So a secret with the same name as an env var always wins, and the editor warns
you when one of your env vars has the name of a secret the script gets (in
*All* mode every workspace secret, in *Selected* mode the attached ones). A
name holds letters, digits and underscores and doesn't start with a digit, at
most 100 characters. `PATH`, `HOME`, `PYTHONPATH` and the other names secrets
can't use are refused, and so is anything starting with `PYRUNNER_`. A script
can have up to 100 env vars, 64 KB in all. Env vars are read when the run
starts, so an edit applies to the next run. Forks copy them and backups
include them.

`run.json` is created in the script's working folder when the run starts and
deleted when it ends:

```json
{
  "run": {"id": "3f2c…", "created_at": "2026-11-08T10:00:00+00:00", "started_at": "2026-11-08T10:00:01+00:00", "attempt": 1},
  "script": {"id": "9a1b…", "name": "Weather report"},
  "trigger": {"type": "manual"},
  "inputs": {"city": "Paris", "options": {"units": "metric"}},
  "event": {"kind": "manual", "received_at": "2026-11-08T10:00:00+00:00", "user": {"id": 1, "email": "you@example.com"}}
}
```

`run.attempt` is 1, or 2 and up for an automatic retry of a failed run (see
[retries](scheduling.md#when-a-run-fails-retries)); a retry gets the same
`inputs` and `event` as the attempt it retries.
`trigger.type` is `manual`, `scheduled` or `api` (webhooks, chat channels and `scripts.run`
over the API or MCP).
`inputs` holds every input the run was started with, defaults filled in, JSON
inputs included, and is `{}` for a script that declares none. `event` is
described [below](#the-run-event).

---

## The run event

Whatever starts a run, the script gets the same kind of envelope for it:
`pyrunner_run.event()`. Every event has `kind` and `received_at` (when PyRun
received the call, message or tick, ISO 8601 in UTC); the other keys depend on
the kind.

| `kind` | Started by | Other keys |
|---|---|---|
| `manual` | **Run now**, or **Re-run with same inputs** | `user` (`{id, email}`; API and MCP callers reading the run get `{id}` only); `rerun_of` (the id of the run it copies) for a re-run |
| `schedule` | the script's schedule | `schedule_id`, `run_mode` (`interval`, `daily`, `cron`...), `fired_at` |
| `webhook` | a call to the script's webhook URL | `method`, `path`, `remote_ip`, `query` (each name maps to a list of values), `content_type`, `headers`; for a POST, `body_size`, `body` (the raw text) and, when the body is JSON, `body_json`. See [Webhook calls](#webhook-calls) |
| `channel` | a chat message to a channel linked to the script | `channel` (its name), `channel_id`, `provider`, `text`, `sender`, `reply_ref`, `raw` (the provider's own payload) |
| `plugin` | a plugin (`ScriptAPI.queue_run`) | `plugin`, `script_key`; `user` when the plugin passed one |
| `api` | `scripts.run` over the API or MCP; or code that created the run itself | `token_id` (the API token that started it) and `via` (`rest` or `mcp`) for `scripts.run`; none otherwise |

```python
import pyrunner_run

event = pyrunner_run.event()
if event["kind"] == "webhook":
    order = event.get("body_json") or {}
elif event["kind"] == "channel":
    order = {"note": event["text"]}
```

A webhook event:

```json
{
  "kind": "webhook",
  "received_at": "2026-11-08T10:00:00.412000+00:00",
  "method": "POST",
  "path": "/webhook/Xy3kP9aB.../",
  "remote_ip": "203.0.113.7",
  "query": {"source": ["shop"]},
  "content_type": "application/json",
  "headers": {"content-type": "application/json", "content-length": "15", "user-agent": "Shopify-Captain-Hook"},
  "body_size": 15,
  "body": "{\"order\": 1042}",
  "body_json": {"order": 1042}
}
```

**Size.** An event is at most **1 MB** (as JSON). One that is bigger is cut,
never refused, and says so: it gets `truncated: true` and loses `body_json`
(parse `body` yourself). If it is still too big, `body` is cut to fit, at a
character boundary, and `body_truncated: true` is set. An event with nothing
to cut keeps only `kind`, `received_at` and `truncated`. The run starts either
way.

**Stored as JSON both databases accept.** A NUL character in the event becomes
`U+FFFD` (`�`), a lone surrogate (which a JSON body can spell as `\ud800`)
becomes `?`, and `NaN` or `Infinity` in a JSON body becomes `null` in
`body_json`. `body` is otherwise the text exactly as received.

**`WEBHOOK_*` and `INBOUND_*` are unchanged.** Scripts written before 2.5 keep
reading `WEBHOOK_METHOD`, `WEBHOOK_QUERY`, `WEBHOOK_CONTENT_TYPE`,
`WEBHOOK_BODY`, `WEBHOOK_BODY_JSON` and the `INBOUND_*` variables, with the
same values as before. Those come from the request as they always did, so a
body over the event's 1 MB cap is still whole in `WEBHOOK_BODY`, unless it is
too big for an environment variable (see [below](#webhook-calls)).

A run started before 2.5 has no stored event. If such a run is still queued
during an upgrade, its script gets just `kind` (from its trigger) and
`received_at` (when the run was created).

### Webhook calls

A run started by the script's webhook URL gets the whole request:

- **In the event:** `method`, `path`, `remote_ip`, `query`, `content_type`,
  `headers` and, for a POST, `body_size`, `body` and `body_json`.
  - `headers` has every request header, names in lower case
    (`event["headers"].get("x-github-event")`), except `Authorization`,
    `Cookie`, `Proxy-Authorization` and the hop-by-hop headers (`Connection`,
    `Proxy-Connection`, `Keep-Alive`, `Transfer-Encoding`, `Upgrade`, `TE`,
    `Trailer` and any the `Connection` header names).
  - `path` shows only the first 8 characters of the webhook token: the event is
    shown on the run page and to API callers, and the full URL is what lets
    anyone start the script.
  - `remote_ip` is the caller's address: the direct peer, or the client
    behind your reverse proxy when `RATELIMIT_TRUSTED_PROXY_DEPTH` is set.
  - `body_size` is the body's length in bytes, as received.
- **In a file:** for a POST, `PYRUNNER_EVENT_BODY_FILE` is the path of a file
  holding the body exactly as it was sent, byte for byte. Use it for a body
  that is big, binary, or has to be checked against a signature over its exact
  bytes:

  ```python
  import os

  with open(os.environ["PYRUNNER_EVENT_BODY_FILE"], "rb") as f:
      raw = f.read()
  ```

  It sits next to `run.json`, readable only by the run, and is deleted when the
  run ends. A GET has no body and no file.

**Bodies that aren't plain text.** A body that isn't valid UTF-8 (an image, a
gzip, a Latin-1 form) can't be text: `body` is `""`, the event says
`"body_binary": true`, and the bytes are in the body file.

**Big bodies.** PyRun takes a body of up to 2.5 MB (Django's
`DATA_UPLOAD_MAX_MEMORY_SIZE`); a bigger one is refused with **413** and no
run. Up to that size:

| Body | `body` in the event | Body file | `WEBHOOK_BODY` |
|---|---|---|---|
| up to about 128 KB | whole | whole | whole, as before 2.5 |
| over about 128 KB | whole if the event fits in 1 MB (see **Size** above), else cut (`body_truncated: true`) | whole | `""`, and `WEBHOOK_BODY_OMITTED=1` |

Linux caps one environment variable at 128 KB (the name and `=` included).
Before 2.5 a run whose `WEBHOOK_BODY` or `WEBHOOK_BODY_JSON` was bigger than
that couldn't start ("Argument list too long"). Now it starts, `WEBHOOK_BODY`
is empty, `WEBHOOK_BODY_JSON` is not set, and `WEBHOOK_BODY_OMITTED` is `1`:
read the body file. The same happens for a body that holds a NUL character
(no environment variable can) and for a body that isn't UTF-8 (whose
`WEBHOOK_BODY` was always `""`).

A JSON body gets `body_json` in the event only when it can be stored parsed:
one nested deeper than about 1,000 levels, or with a number over 4,300
digits, stays text only there (`body`, no `body_json`, and not `truncated`).
`WEBHOOK_BODY_JSON` is set as before 2.5, whenever Python's parser reads the
body: that can be a few thousand levels deep, so for a body nested between
the two it is set while the event has no `body_json`.

A body that arrives shorter than its `Content-Length` (the sender hung up
mid-body) gets **400** and no run.

**Recent deliveries.** The script page's Webhook card lists the last 20 calls:
their run's status, how long it ran, and the HTTP status PyRun answered with
(200 queued or skipped, 503 during a maintenance window, 500 if the run
couldn't be queued). Calls refused before a run exists (a wrong URL, a missing
or wrong signature, a rate limit, a disabled script, a required input with no
default, a body cut short, 413) aren't listed. Retries of a failed run aren't calls and aren't
listed either.

#### Webhook signatures

The webhook URL is a secret: anyone who has it can start the script. To also
make sure a call really comes from the service you expect, set a signature:
on the script page, **Webhook → Signature and limits**. PyRun then verifies
each call's signature over the body exactly as it was received, before it
creates a run. A call whose signature is missing or wrong gets **401** and no
run:

```json
{"error": "Invalid webhook signature", "reason": "mismatch"}
```

| `reason` | Meaning |
|---|---|
| `missing` | The signature header (or the custom timestamp header) isn't in the request. |
| `malformed` | The header is there but can't be read (wrong prefix, not hex or base64, no `t=` or `v1=`). |
| `expired` | The signature is right but its timestamp is further from the server's clock than the tolerance: an old call sent again, or a clock that is off. |
| `mismatch` | The signature doesn't match the body: a different secret, or the body was changed on the way. |
| `not_configured` | No secret is saved, or it can't be decrypted because `ENCRYPTION_KEY` changed: save the secret again. |

A refused call also counts as an auth failure for the caller's address (see
[Webhook rate limits](#webhook-rate-limits)).

Every signature is HMAC-SHA256 keyed with the secret's UTF-8 bytes, and
compared in constant time:

| Signature | Header | Signed text | Secret |
|---|---|---|---|
| Stripe | `Stripe-Signature: t=<unix time>,v1=<hex>` | `<t>.<body>`, and `t` must be within the tolerance (default 300 seconds) | the endpoint's signing secret, `whsec_...`, used whole |
| GitHub | `X-Hub-Signature-256: sha256=<hex>` | the body | the webhook's secret |
| Shopify | `X-Shopify-Hmac-Sha256: <base64>` | the body | the app's client secret |
| Custom HMAC-SHA256 | the header you name, with an optional prefix (`sha256=`), hex or base64 | the body; with a timestamp header, `<timestamp>.<body>` | yours |

**Stripe.** In the Stripe dashboard, add a webhook endpoint whose URL is the
script's webhook URL, then reveal the endpoint's signing secret and paste it
into the script's webhook settings with the signature **Stripe**. Stripe sends
`Stripe-Signature: t=1700000000,v1=…`; while it rolls a secret it sends two
`v1` values, and either may match. `stripe listen --forward-to <webhook URL>`
signs with its own secret (it prints it when it starts): use that one while
you forward with the Stripe CLI.

**GitHub.** In the repository (or organization), **Settings → Webhooks → Add
webhook**: the payload URL is the script's webhook URL, the content type
`application/json`, and the secret any long random string; save the same
string with the signature **GitHub**. GitHub signs with `X-Hub-Signature-256`
(the older SHA-1 `X-Hub-Signature` header is ignored). GitHub's own example:
the secret `It's a Secret to Everybody` and the body `Hello, World!` give
`sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17`.

**Shopify.** A webhook an app subscribes to (the Admin API or the app's
configuration) is signed with the app's client secret: choose **Shopify** and
paste it. A webhook made in the Shopify admin (**Settings → Notifications →
Webhooks**) is signed with the key shown under that list ("Your webhooks will
be signed with …"): paste that one.

**Custom HMAC-SHA256.** For your own senders, or a service that signs the same
way: name the header, the prefix written before the signature (if any) and
the encoding. With a timestamp header, the sender signs `<timestamp>.<body>`
with the Unix time in seconds, and PyRun refuses a call whose timestamp is
further off than the tolerance. A Python sender, for the header `X-Signature`,
prefix `sha256=`, hex, and the timestamp header `X-Signature-Timestamp`:

```python
import hashlib, hmac, json, time, urllib.request

secret = b"your signing secret"
body = json.dumps({"order": 1042}).encode()
timestamp = str(int(time.time()))
signature = hmac.new(secret, timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
urllib.request.urlopen(urllib.request.Request(
    "https://pyrun.example.com/webhook/<token>/", data=body, method="POST",
    headers={
        "Content-Type": "application/json",
        "X-Signature": "sha256=" + signature,
        "X-Signature-Timestamp": timestamp,
    },
))
```

The same call from a shell, without the timestamp header:

```bash
body='{"order": 1042}'
sig=$(printf '%s' "$body" | openssl dgst -sha256 -hmac "$SECRET" | sed 's/^.* //')
curl -X POST -H "Content-Type: application/json" -H "X-Signature: sha256=$sig" \
  -d "$body" "https://pyrun.example.com/webhook/<token>/"
```

The script doesn't have to check the signature again. The signature headers
are in the event's `headers` like the others (`x-hub-signature-256`,
`stripe-signature`), so a script that wants to can, over the exact bytes in
`PYRUNNER_EVENT_BODY_FILE`.

#### Webhook rate limits

By default each script's webhook takes at most **60 calls a minute** and **30
calls in any one second**. Change both under **Webhook → Signature and
limits**. A call over either limit gets **429** with a `Retry-After` header
and no run. The
windows are fixed and follow the clock (a minute starts at :00), so up to
twice a limit can pass across the turn of a window.

Two more limits apply per caller address, to every webhook call together:

- **300 calls a minute**, whatever the script: a floor against scanning. A
  script limit above 300 a minute only helps when the calls come from several
  addresses.
- **30 auth failures a minute**: a call to a URL that doesn't exist (404) or
  with a missing or wrong signature (401) counts. After 30, every webhook call
  from that address gets 429 until the minute is over.

Behind a reverse proxy, set `RATELIMIT_TRUSTED_PROXY_DEPTH` so the address is
the caller's and not the proxy's. Before 2.5 the only limit was 30 calls a
minute per address.

---

## `pyrunner_run`

| Function | What it does |
|---|---|
| `inputs()` | The run's inputs as a dict (every input, JSON ones too). `{}` when the script declares none. |
| `event()` | What started the run, as a dict (see [The run event](#the-run-event)). |
| `run_id()` | The run's id (same as `PYRUNNER_RUN_ID`). |
| `set_result(result)` | Stores `result`, a dict, as the run's result. Calling it again replaces it. |
| `progress(pct=None, message=None)` | Shows how far the run is, live on its run page. |
| `on_stop(callback)` | Runs `callback()` when PyRun stops the run, before it is killed (see [Stopping a run](#stopping-a-run)). |

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
`event()`, `run_id()` and `set_result()` raise `RunError`, and `progress()`
writes its usual warning to stderr.

### Stopping a run

PyRun stops a run when someone presses **Stop** on its run page or the tasks
page, when a plugin cancels it (`ScriptAPI.cancel_latest_run`), and when it
hits its timeout. It doesn't kill the script straight away. It first sends the
script's process group SIGTERM (CTRL_BREAK on Windows), then waits the script's
**Stop grace period** (a setting in the editor's Execution box: 10 seconds by
default, 0 to 60) and only then kills whatever is still running. The signal
and the kill reach the script and the processes it started, never PyRun's
worker.

`on_stop` lets the script use that time:

```python
import pyrunner_run

rows = []

@pyrunner_run.on_stop
def save_what_we_have():
    write_rows(rows)                              # flush buffers, close files
    pyrunner_run.set_result({"rows": len(rows), "complete": False})

for row in fetch_rows():
    rows.append(row)
```

- The callbacks run once, in the order they were added, in the script's main
  thread. An exception in one is printed to stderr and the next one still runs.
- When they are done the script exits (`SystemExit` with code 128 + the signal
  number), so `finally` blocks and atexit handlers still run, and a pending
  `progress()` update is still sent. A callback can call `sys.exit()` itself to
  choose the exit code.
- The run is still running while the callbacks work, so `set_result()` and
  `progress()` work there too.
- Call `on_stop` from the main thread; elsewhere it raises `RunError`. It
  replaces a SIGTERM (and on Windows SIGBREAK) handler the script set itself.
- Finish within the grace period: whatever is still running then is killed.
  A bare `except:` that catches the `SystemExit`, or a non-daemon thread that
  keeps going, keeps the script alive until the kill.
- On Windows a signal handler only runs while the main thread is running
  Python code, so a long `time.sleep()` delays it; sleep in short steps.

A script without `on_stop` simply ends on the signal (Python's default for
SIGTERM). How the run ended shows at the end of its stderr:

| Stopped by | Ended within the grace period | Still running when it was up |
|---|---|---|
| Stop button / plugin | `[Stopped by user after graceful shutdown]` | `[Killed by user after the 10 s grace period]` |
| Timeout | `[Stopped after graceful shutdown]` | `[Killed after the 10 s grace period]` |

A stopped run stays **running**, with a **stopping** chip, until the script has
ended, then turns **cancelled** and keeps what the script printed. Its run page
then offers **Kill now**: stopping a run that is already stopping kills it at
once, with the note `[Killed by user]`, and the run keeps what the script
printed before the kill. It is also the way out when the run's worker is gone;
the run then ends without its output, which only the worker had. With a grace
period of 0 the script is killed at once, as before 2.5: the note is
`[Killed by user]`, and a timeout shows only its TIMEOUT line. The run records
when the stop was asked for (`stop_requested_at`, also on the plugin SDK's
`RunView`).

---

## Where it shows up

- **Run page:** a **Result** card (formatted JSON, with a download link), and
  while the run is live, its progress bar and status line, updated without
  reloading. A finished run shows its last progress update. An **Event** card
  shows what started the run (its kind, when it was received and the event's
  JSON, with a download link). Secret values are masked in it; when a secret
  can't be resolved, the event is hidden instead.
- **Runs list:** a **Result** tag on runs that set one.
- **Notifications:** the webhook notification's JSON payload has `result` (the
  object, or `null`) and `result_too_large` (`true` when the result is over
  16 KB and was left out). Email shows the first 2,000 characters of the
  result, chat channels the first 300.
- **Plugins:** `RunView.result` and `RunView.progress` (see
  [plugins.md](plugins.md)).
- **Backups** include the result, progress and event.

Keys keep the order they were written in everywhere above, on SQLite and on
PostgreSQL: the result as the script set it, the event as it arrived, the
inputs in the order the script declares them (and the **Run now** form lists
them in that order too).

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

`inputs()`, `event()` and `run_id()` only read the run file and the
environment. The event is stored as it was received (the script needs the real
values) and masked only where PyRun shows it.
`set_result()` and `progress()` POST to PyRun's internal API over loopback
(`/internal/runs/result`, `/internal/runs/progress`) with the run's signed
token (`PYRUNNER_INTERNAL_TOKEN`). The run is the one named in that token,
never one named in the request, so a script can only report on its own run,
and PyRun refuses any write once the run has finished. The run file sits in
the script's working folder, which the sandbox shares at the same path, so
sandboxed runs read it too.
