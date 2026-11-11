"""
Scaffold a new integration: its preset, api.py, script-facing module, tests, a sample cassette and a docs stub.

    manage.py new_integration acme                    # a new service with its own preset
    manage.py new_integration google_tasks --provider google   # on an existing preset

The files work as they are: the generated module passes the conformance suite
and its cassette test untouched (``manage.py test core.integrations.<key>``),
so you start from green and change the placeholders (base URL, paths, the
functions) one test at a time. A key that is already an integration (shipped,
or with an api.py or a script-facing module) is refused, and so is a key that
names an existing preset unless ``--provider`` is that preset; files left from
an earlier try are kept, never overwritten. Then add
the key to INTEGRATION_MODULES (core/integrations/framework.py), and a new
preset to PRESET_MODULES (core/integrations/presets.py), so PyRun ships them.
"""

import json
import keyword
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.integrations import framework, presets

PRESET = '''\
"""{label}: an API key, sent as a bearer token. Check the base URL, header and whoami against {label}'s API docs."""

from core.integrations.presets import API_KEY, Field, Preset, Whoami

PRESET = Preset(
    key="{provider}",
    label="{label}",
    description="A {label} API key.",
    fields={{API_KEY: (Field("api_key", "API key", secret=True),)}},
    base_url="https://api.{host}.example",
    api_key_header=("Authorization", "Bearer "),
    whoami=Whoami("/v1/me", label_keys=("email", "name")),
)
'''

API = '''\
"""The {key} integration (see core/integrations/framework.py)."""

from core.integrations.framework import Integration, Smoke


def _smoke_list(module, connection, config):
    items = list(module.list_items(connection=connection, limit=1))
    return f"list_items answered with {{len(items)}} item(s)"


EXAMPLE = \'\'\'\\
from pyrunner_integrations import {key}

for item in {key}.list_items(limit=10):
    print(item["id"])
\'\'\'

INTEGRATION = Integration(
    key="{key}",
    provider="{provider}",
    title="{label}",
    functions=("get_item", "list_items", "client"),
    example=EXAMPLE,
    smoke=(Smoke("list_items", _smoke_list),),
)
'''

MODULE = '''\
"""
{label} for scripts: read its items.

Paths are relative to the {provider} connection's base URL. Each function
returns plain data and raises ``IntegrationError`` when the call fails.
"""

from . import _client

PROVIDER = "{provider}"


def client(connection=None, *, timeout=30):
    """The shared client of ``connection``, for calls this module has no function for."""
    return _client.Client(connection, provider=PROVIDER, timeout=timeout)


def get_item(item_id, *, connection=None):
    """One item by its id.

    For example:

        item = {key}.get_item("a1")
    """
    return _client.shared(connection, PROVIDER).get(f"/v1/items/{{_client.quote(item_id)}}")


def list_items(*, connection=None, limit=None):
    """Every item, page by page.

    ``limit`` stops after that many. For example:

        for item in {key}.list_items(limit=50):
            print(item["id"])
    """
    return _client.shared(connection, PROVIDER).paginate(
        "/v1/items", items="items", cursor="next_cursor", cursor_param="cursor", limit=limit)
'''

TESTS = '''\
"""The {key} integration's tests: the conformance suite and recorded exchanges (core/integrations/testing.py)."""

from core.integrations import testing


class {camel}Conformance(testing.ConformanceSuite):
    integration = "{key}"
    pages = [
        {{"json": {{"items": [{{"id": "a1"}}, {{"id": "a2"}}], "next_cursor": "c2"}}}},
        {{"json": {{"items": [{{"id": "a3"}}], "next_cursor": None}}}},
    ]
    page_items = [{{"id": "a1"}}, {{"id": "a2"}}, {{"id": "a3"}}]

    def call(self, module):
        return module.get_item("a1")

    def paginate_call(self, module, limit=None):
        return module.list_items(limit=limit)


class {camel}CassetteTests(testing.IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.module = self.integration("{key}")
        self.use_connection("{provider}", provider="{provider}")

    def test_list_items(self):
        with self.cassette("list_items"):
            items = list(self.module.list_items())
        self.assertEqual([item["id"] for item in items], ["a1", "a2", "a3"])
'''

DOCS = '''\
<!-- A stub from manage.py new_integration: run manage.py integration_docs {key} once {key} is in
INTEGRATION_MODULES (core/integrations/framework.py), and it is replaced by the generated page. -->

# {label} integration (`pyrunner_integrations.{key}`)

Not written yet.
'''


def _cassette():
    page = {"status": 200, "headers": {"content-type": "application/json"}}
    return {
        "pyrunner_cassette": 1,
        "note": "A sample from manage.py new_integration: record the real one with PYRUNNER_RECORD_CASSETTES=1.",
        "interactions": [
            {"request": {"method": "GET", "url": "/v1/items"},
             "response": {**page, "json": {"items": [{"id": "a1"}, {"id": "a2"}], "next_cursor": "c2"}}},
            {"request": {"method": "GET", "url": "/v1/items?cursor=c2"},
             "response": {**page, "json": {"items": [{"id": "a3"}], "next_cursor": None}}},
        ],
    }


class Command(BaseCommand):
    help = "Scaffold core/integrations/<key>/ and pyrunner_integrations/<key>.py, passing conformance as generated."

    def add_arguments(self, parser):
        parser.add_argument("key", help="lower-case letters, digits and '_', e.g. acme or google_tasks")
        parser.add_argument("--provider", help="An existing connection preset to build on (default: a new one "
                                               "named like the key).")
        parser.add_argument("--label", help="The service's name as people write it (default: from the key).")
        parser.add_argument("--root", help="Write under this folder instead of PyRun's (for trying it out).")

    def handle(self, *args, **options):
        key = options["key"]
        if not framework.KEY_RE.match(key) or keyword.iskeyword(key) or key.startswith("_"):
            raise CommandError(f"{key!r}: use 2-40 lower-case letters, digits and '_', starting with a letter "
                               "(and not a Python keyword).")
        provider = options["provider"] or key
        if not framework.KEY_RE.match(provider):
            raise CommandError(f"{provider!r} is not a preset key.")
        label = options["label"] or " ".join(part.capitalize() for part in key.split("_"))
        root = Path(options["root"] or settings.BASE_DIR)
        core = root / "core" / "integrations"
        if options["provider"] and not (Path(settings.BASE_DIR) / "core" / "integrations" / provider /
                                        "preset.py").is_file() and not (core / provider / "preset.py").is_file():
            raise CommandError(f"No preset {provider!r}: core/integrations/{provider}/preset.py doesn't exist.")
        self._refuse_existing(key, options["provider"], root)
        values = {"key": key, "provider": provider, "label": label, "host": key.replace("_", "-"),
                  "camel": "".join(part.capitalize() for part in key.split("_"))}
        files = [
            (core / key / "__init__.py", f'"""The {key} integration (see core/integrations/framework.py)."""\n'),
            (core / key / "api.py", API.format(**values)),
            (core / key / "tests" / "__init__.py", ""),
            (core / key / "tests" / "test_api.py", TESTS.format(**values)),
            (core / key / "tests" / "cassettes" / "list_items.json", json.dumps(_cassette(), indent=2) + "\n"),
            (root / "core" / "script_helpers" / "pyrunner_integrations" / f"{key}.py", MODULE.format(**values)),
            (root / "docs" / "integrations" / f"{key}.md", DOCS.format(**values)),
        ]
        if not options["provider"]:
            files.insert(1, (core / key / "preset.py", PRESET.format(**values)))
        for path, text in files:
            shown = path.relative_to(root).as_posix()
            if path.exists():
                self.stdout.write(f"Kept (exists): {shown}")
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
            self.stdout.write(f"Created: {shown}")
        steps = [
            f"manage.py test core.integrations.{key}   (green as generated)",
            "Replace the placeholders: the preset's base URL and auth, the module's paths and functions "
            "(declare each in api.py), the sample cassette (record it with PYRUNNER_RECORD_CASSETTES=1).",
            f"Add '{key}' to INTEGRATION_MODULES in core/integrations/framework.py"
            + ("" if options["provider"] else f", and '{provider}' to PRESET_MODULES in core/integrations/presets.py")
            + ".",
            f"manage.py integration_docs {key}   (writes docs/integrations/{key}.md)",
        ]
        self.stdout.write("Next:\n" + "\n".join(f"  {n}. {step}" for n, step in enumerate(steps, 1)))

    @staticmethod
    def _refuse_existing(key, provider, root):
        """A CommandError (exit 1) when ``key`` is already an integration, or a preset not named by ``--provider``."""
        roots = dict.fromkeys([Path(settings.BASE_DIR), root])
        if key in framework.INTEGRATION_MODULES or any(
            (r / "core" / "integrations" / key / "api.py").is_file()
            or (r / "core" / "script_helpers" / "pyrunner_integrations" / f"{key}.py").is_file()
            for r in roots
        ):
            raise CommandError(
                f"{key!r} is already an integration: nothing was written. Pick another key, or edit "
                f"core/integrations/{key}/ and pyrunner_integrations/{key}.py."
            )
        is_preset = key in presets.PRESET_MODULES or any(
            (r / "core" / "integrations" / key / "preset.py").is_file() for r in roots
        )
        if is_preset and provider != key:
            raise CommandError(
                f"{key!r} is already a connection preset: nothing was written. To build its integration "
                f"on it, run new_integration {key} --provider {key}."
            )
