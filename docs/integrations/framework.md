# Integrations: using them, writing them, testing them

An integration is a small Python module with the calls most scripts need for
one service, on top of a [connection](../run-context.md#connections). Scripts
import it from `pyrunner_integrations`:

```python
from pyrunner_integrations import http

me = http.get("/v1/me")                                  # the script's one HTTP API connection
http.post("/v1/items", json={"name": "Ada"}, connection="crm")
```

Each module has its own page here, generated from its code, with an example
script: `http.md` (any HTTP API with an API key, bearer token or basic auth),
`airtable.md` (list, find, create, update and delete records) and `notion.md`
(query databases, create and update pages, append blocks, search). More
services build on the same framework.

## What every integration does

- **Connections by name or by service.** Every function takes `connection=`:
  a connection's name, or nothing for the script's one connection of that
  service. The connection must be granted to the script, as with
  `pyrunner_connections`.
- **Plain data back.** Functions return parsed JSON, text, lists and dicts.
  `<module>.client()` gives the shared client for anything a module has no
  function for; its `request()` returns the reply itself.
- **Credentials only to the service.** They go on requests to the
  connection's base URL; a redirect elsewhere goes on without them. They are
  read before every request, so a token PyRun refreshed is used at once, and
  every value is masked in the run's output as usual.
- **One exception.** Failures raise `pyrunner_integrations.IntegrationError`
  (a `pyrunner_connections.ConnectionsError`) with `code` (`NOT_FOUND`,
  `RATE_LIMITED`, `UNAUTHORIZED`, `CONNECTION_EXPIRED`...), `status` (the HTTP
  status) and `retry_after`. Its message never holds a credential.
- **Retries built in.** A 429, or a 503 with Retry-After, is retried up to 3
  times, waiting what the service asks (up to 60 seconds) or 1, 2 and 4
  seconds (Airtable: 30 seconds, the pause it documents after a 429). Other 5xx replies and lost connections are retried only for calls
  that are safe to repeat (GET, PUT, DELETE). Each retry logs a warning, which
  shows in the run's stderr.
- **One re-auth after a 401.** The client tells PyRun the token was refused
  and asks for it again: an OAuth connection that still holds that token is
  refreshed then (at most once a minute per connection), and the request is
  tried once more with the new one. If nothing changed, or the second try is
  refused too, the error is `UNAUTHORIZED`.
- **Standard library only.** It works in every environment; when `requests`
  is installed it carries the requests.

Function signatures are public API: they only grow (new functions, new keyword
arguments with defaults), and anything deprecated is announced in the
changelog. `pyrunner_integrations.__version__` says which version a script has.

## Where an integration lives

```
core/integrations/<key>/
    preset.py            the connection preset (shared with Connections)
    api.py               INTEGRATION = Integration(...): module, functions, example, smoke checks
    triggers.py          optional, reserved for watcher adapters (nothing reads it yet)
    tests/test_api.py    the conformance suite and cassette tests
    tests/cassettes/     recorded exchanges, replayed offline
core/script_helpers/pyrunner_integrations/<key>.py   what scripts import
docs/integrations/<key>.md                           generated from the two above
```

Several integrations may share one preset (Google's covers Sheets, Gmail and
Drive): `api.py` names it as `provider`. `INTEGRATION_MODULES` in
`core/integrations/framework.py` lists the integrations PyRun ships, as
`PRESET_MODULES` in `core/integrations/presets.py` lists the presets.

The script-facing module:

- uses only the standard library and the shared client in `_client.py`:
  `_client.shared(connection, PROVIDER).get(...)`, `.post(...)`,
  `.paginate(...)`; `_client.quote()` for an id in a path;
- sets `PROVIDER`, has a module docstring (it opens the generated page) and a
  docstring on every public function, with examples after a line of text
  ("For example:"), indented;
- gives every public function a `connection=None` keyword, and declares each
  one in `api.py`'s `functions` (anything else is private: `_name`);
- for a service that refuses for a fixed time after a 429 and sends no
  Retry-After, sets `RATE_LIMIT_WAIT` (seconds) and passes it to its client,
  `_client.shared(connection, PROVIDER, rate_limit_wait=RATE_LIMIT_WAIT)` (and
  in `client()`): the client waits that long instead of 1, 2 and 4 seconds,
  and the generated page says so.

## Starting one

```
manage.py new_integration acme                    # a new service with its own preset
manage.py new_integration google_tasks --provider google   # on an existing preset
```

The scaffold writes every file above with placeholders (an API-key preset, a
module with `get_item` and `list_items`, a sample cassette, a docs stub) that
pass the conformance suite as generated, so you start from green:

```
manage.py test core.integrations.acme
```

It refuses a key that is already an integration (`http`, `airtable`, `notion`,
or one with an `api.py` or a module), and a key that names an existing preset
unless `--provider` is that preset (`new_integration slack --provider slack`):
it exits with an error and writes nothing. Files left from an earlier try are
kept, never overwritten.

Then replace the placeholders one test at a time, add the key to
`INTEGRATION_MODULES` (and a new preset to `PRESET_MODULES`), record the
cassettes and write the page:

```
manage.py integration_docs acme        # docs/integrations/acme.md
manage.py integration_docs --check     # in CI: fails when a page is out of date
```

A test in PyRun's suite fails when a committed page doesn't match the code.

## The testing kit

`core.integrations.testing` tests code that calls a service with no
credentials and no network: an integration module, or a plugin's code that
uses `pyrunner_integrations` or `pyrunner_connections.client()` (see "Testing
code that uses connections" in `docs/plugins.md`). Its test cases run in
PyRun's test process.

```python
from core.integrations import testing


class ItemsTests(testing.IntegrationTestCase):
    def test_list(self):
        crm = self.use_connection("crm", provider="http", auth_kind="bearer",
                                  config={"base_url": "https://crm.example.com"})
        fake = self.fake_http()
        fake.add("GET", "/v1/items", json={"items": [{"id": 1}]})

        http = self.integration("http")
        self.assertEqual(http.get("/v1/items"), {"items": [{"id": 1}]})
        self.assertEqual(fake.requests[0].headers["Authorization"], f"Bearer {crm.credentials['token']}")
```

- **`IntegrationTestCase`** is a `SimpleTestCase` (no database);
  `IntegrationTestMixin` adds the same to a `TestCase`. While a test runs, no
  request made through `pyrunner_integrations` or a
  `pyrunner_connections.client()` session reaches the network (one nothing
  answers fails the test: "The code under test made a real request"), and
  waits don't sleep: they are listed in `self.sleeps`. A `client()` session
  is served by `fake_http()` and cassettes like the modules: it sends the
  connection's credentials and follows redirects as in a run.
- **`use_connection(name, provider, auth_kind=None, config=None,
  credentials=None)`** gives the code a connection, as
  `pyrunner_connections.credentials()`, `token()`, `info()` and `list()` see
  it. Credentials are made up unless you pass some. `error=("CONNECTION_EXPIRED",
  409)` makes asking for it fail as PyRun would; `rotate=False` keeps the same
  credentials when the code reports a 401.
- **`fake_http()`** is a scripted service: `add(method, path, status=200,
  json=..., headers=..., times=1)` answers the next matching request (a path
  is relative to the connection's base URL; None matches anything),
  `default(...)` answers the rest, `error="UNREACHABLE"` or `"TIMEOUT"` fails
  as the network would, and `requests` holds what was sent (`method`, `url`,
  `path`, `params`, `headers`, `body`, `json()`).
- **`cassette(name)`** replays `cassettes/<name>.json` beside the test file:
  each request must be the next one recorded (method, URL, query and body;
  credentials compared masked) and every recorded one must be made.

### Recording cassettes

Run the tests with `PYRUNNER_RECORD_CASSETTES=1` and real credentials in a
JSON file outside the repository, named by `PYRUNNER_RECORD_CREDENTIALS`:

```json
{"crm": {"auth_kind": "bearer",
         "config": {"base_url": "https://crm.example.com"},
         "credentials": {"token": "the real token"}}}
```

(keyed by the connection's name in the test, or by its provider). Each
`cassette()` then calls the real service and writes the file at the end. Every
credential value is replaced by `[MASKED]`, cookies and most reply headers are
left out, and the connection's base URL in a reply (a next-page link) is
written as `{base_url:<name>}`, which replay turns back into the test's own.
Look through a cassette before you commit it: names, emails and other
personal data in the replies are kept as they came (pass `scrub=("value",
...)` to mask more).

A cassette is plain JSON, so it can also be written by hand from the
service's API reference when there is no account to record with: Airtable's
and Notion's were, and their `note` says so. Re-record them with real
credentials when you have them; replay checks the same things either way.

### The conformance suite

Every integration module passes the same checks. Its `tests/test_api.py`
subclasses `ConformanceSuite`:

```python
class AcmeConformance(testing.ConformanceSuite):
    integration = "acme"
    pages = [{"json": {"items": [{"id": 1}], "next_cursor": "c2"}},
             {"json": {"items": [{"id": 2}], "next_cursor": None}}]
    page_items = [{"id": 1}, {"id": 2}]

    def call(self, module):               # one request through the module
        return module.get_item("a1")

    def paginate_call(self, module, limit=None):
        return module.list_items(limit=limit)
```

It checks: the preset is valid (auth kinds, fields, https URLs, scope sets);
the module's public API (`PROVIDER`, docstrings, `connection=`, every public
function declared); for every auth kind, the credentials reach the base URL
and not a host it redirects to; one re-auth after a 401, and none when nothing
changed; 429 and 503 backoff and Retry-After, and no wait for a Retry-After
over a minute; status codes map to error codes, with the service's message
kept and the credentials out of it; no credential in any repr, error or log
line; and, with `pages`, that pagination reads every page once, stops after
the last and stops early at `limit`. `ok_reply` (default `{"json": {}}`) is
what the fake service answers `call()` with when all goes well.

### Smoke checks

```
manage.py integration_smoke http                   # the instance's one http connection
manage.py integration_smoke http --connection my-api
```

runs the integration's live, read-only checks (`smoke` in its `api.py`)
against the real service, with a connection made under Connections, and prints
PASS or FAIL for each, every connection value masked. It exits 1 when one
fails, and prints SKIP and exits 0 when there is no such connection, so it can
run where credentials may be missing. It calls the service from your shell, as
a script would.
