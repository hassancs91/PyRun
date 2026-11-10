"""
``help.search`` over the docs bundled with the running version, for REST, MCP and
Py AI (2.5 Stage 3, ticket T19).

What this pins, roughly in order of how much it would hurt to get wrong:

- The corpus is an explicit list of public docs, and those exact files ship in
  the image: ``.dockerignore`` re-includes each of them (and integration pages)
  while internal docs stay out, ``.gitignore`` tracks the same guides, and every
  file is in the checkout. A new public guide must be added in all three places.
- Sections split at headings outside fenced code, with GitHub's anchors (code,
  punctuation, em dashes, duplicates), so each link lands on its section at the
  running version's tag.
- Ranking: heading words weigh most, plurals fold, a changelog entry counts half,
  the share of the query's words found matters; snippets start at the paragraph
  that matches best.
- A missing corpus is not an error: no results, ``docs_bundled`` false, one log
  line per process. Building the index never fails the web boot, which builds it.
- ``help:read`` gates it on every transport; Py AI gets it on the console and on
  chat channels, and its prompt says to answer with the link.

No test calls a model; tools are called through their handlers.
"""

import importlib
import re
import sys
import tempfile
import uuid
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings

from core.api.registry import registry
from core.api.tools import build_tools, permissions_for
from core.models import APIToken, Workspace
from core.services import help_docs
from core.services.pyai import runtime
from core.test_api_inventory import ACCEPT
from core.test_plugin_api import _mock_setup
from core.test_pyai_registry_tools import call, names
from pyrunner.version import __version__

CHANNEL = "telegram:19"
BASE = f"{help_docs.REPO_URL}/blob/v{__version__}/"
SIGNATURE_QUESTION = "How do I set up a webhook with a signature?"
FILLER = " ".join(f"filler{n}" for n in range(60))

RUN_CONTEXT = f"""# Run context

What a run gets. ![diagram](img/run.png) See [the API](api.md) <br>and more.

## Webhooks

Every script has a webhook URL. Call it with a POST and a JSON body; the body
reaches the script as its event.

### Webhook signatures

Set a signature on the script's webhook (Stripe, GitHub, Shopify or HMAC-SHA256)
and PyRun verifies each call before a run starts.

```sh
# Not a heading: a comment in a fence
curl -X POST https://pyrun.example/webhook/abc
```

## Webhooks

A second section under the same heading.

## Retries

A failed run is retried after a delay.

## Long section

{FILLER}

{FILLER}

{FILLER}

A paragraph about cron timezones, deep in the section.

{FILLER}

{FILLER}
"""
CHANGELOG = """# Changelog

## [2.5.0] — November 1, 2026

### Added

- **Webhook signatures** for every script.

### Retries

A failed run is retried after a delay.
"""
SLACK = """# Slack

## Post a message

`slack.post(channel, text)` posts a message to a Slack channel.
"""


def write_corpus(root, files):
    for name, text in files.items():
        path = Path(root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def fixture_index(**extra):
    files = {"docs/run-context.md": RUN_CONTEXT, "CHANGELOG.md": CHANGELOG, **extra}
    with tempfile.TemporaryDirectory() as root:
        write_corpus(root, files)
        return help_docs.build_index(root)


def ignore_patterns(name):
    lines = (Path(settings.BASE_DIR) / name).read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def docker_ignored(path, patterns):
    """Whether a Docker build leaves ``path`` out (the last pattern matching it or a parent wins)."""
    parts = path.split("/")
    candidates = ["/".join(parts[: i + 1]) for i in range(len(parts))]
    ignored = False
    for pattern in patterns:
        negate = pattern.startswith("!")
        regex = (re.escape(pattern.lstrip("!").strip("/"))
                 .replace(r"\*\*/", "(?:.*/)?").replace(r"\*", "[^/]*").replace(r"\?", "[^/]"))
        if any(re.fullmatch(regex, candidate) for candidate in candidates):
            ignored = not negate
    return ignored


class DeclarationTests(SimpleTestCase):
    def test_a_channel_safe_read_with_its_own_url_eligible_permission(self):
        spec = registry.get("help.search")
        self.assertEqual((spec.access, spec.permission, spec.pyai_tier),
                         ("read", "help:read", "channel"))
        self.assertIn("help:read", {p for p, _ in APIToken.API_PERMISSIONS})
        self.assertIn("help:read", registry.read_permissions())
        self.assertIn("help:read", permissions_for("channel"))
        self.assertEqual(spec.input["required"], ["query"])


class CorpusTests(SimpleTestCase):
    """The files help.search reads are the files the image ships."""

    def test_every_corpus_file_is_in_the_checkout(self):
        for doc in help_docs.CORPUS:
            self.assertTrue((Path(settings.BASE_DIR) / doc).is_file(), doc)

    def test_dockerignore_ships_the_corpus_and_nothing_else_of_the_docs(self):
        patterns = ignore_patterns(".dockerignore")
        for doc in (*help_docs.CORPUS, f"{help_docs.INTEGRATIONS_DIR}/slack.md"):
            self.assertFalse(docker_ignored(doc, patterns), doc)
        # Internal docs by file name: core.test_shipped_text flags a literal path
        # to an unpublished doc in tracked files.
        internal_docs = [f"docs/{name}" for name in ("PLAN_v2_5.md", "README.md")]
        for internal in (*internal_docs, "docs/agents/issue-tracker.md", "notes.md",
                         "docs/integrations/cassettes/x.json", "SECURITY_NOTES.md"):
            self.assertTrue(docker_ignored(internal, patterns), internal)

    def test_gitignore_tracks_the_same_guides(self):
        patterns = ignore_patterns(".gitignore")
        tracked = {p[1:] for p in patterns if p.startswith("!docs/") and p.endswith(".md")}
        self.assertEqual(tracked, {d for d in help_docs.CORPUS if d.startswith("docs/")})
        self.assertIn(f"!{help_docs.INTEGRATIONS_DIR}/", patterns)


class ParsingTests(SimpleTestCase):
    def test_github_anchors(self):
        for title, anchor in (
            ("`pyrunner_run`", "pyrunner_run"),
            ("Stopping a run", "stopping-a-run"),
            ("[2.0.0] — October 23, 2026", "200--october-23-2026"),
            ("Pausing & resuming", "pausing--resuming"),
            ("The plugin SDK — `core.plugins.api`", "the-plugin-sdk--corepluginsapi"),
            ("📘 The free book", "-the-free-book"),
            ("Upgrading from 1.x", "upgrading-from-1x"),
        ):
            self.assertEqual(help_docs.anchor_for(title), anchor, title)

    def test_sections_split_at_headings_outside_fences(self):
        sections = help_docs.split_sections("docs/run-context.md", RUN_CONTEXT)
        self.assertEqual(
            [(s.title, s.anchor) for s in sections],
            [("Run context", "run-context"), ("Webhooks", "webhooks"),
             ("Webhook signatures", "webhook-signatures"), ("Webhooks", "webhooks-1"),
             ("Retries", "retries"), ("Long section", "long-section")],
        )
        signatures = sections[2]
        self.assertEqual(signatures.trail, "Run context › Webhooks › Webhook signatures")
        self.assertIn("# Not a heading: a comment in a fence", signatures.text)
        self.assertEqual(sections[3].trail, "Run context › Webhooks")
        # Link targets, images and HTML are left out of the text; their words stay.
        self.assertEqual(sections[0].text, "What a run gets. diagram See the API  and more.")

    def test_text_above_the_first_heading_and_empty_sections(self):
        sections = help_docs.split_sections("docs/integrations/x.md",
                                            "Intro words.\n\n# Title\n\n## Empty\n## Full\nBody.\n")
        self.assertEqual([(s.title, s.anchor, s.text) for s in sections],
                         [("docs/integrations/x.md", "", "Intro words."), ("Full", "full", "Body.")])

    def test_terms(self):
        words = help_docs.terms("How do I use Webhooks and datastores.set with libraries? Status")
        for word in ("webhook", "datastores.set", "datastore", "set", "library", "status"):
            self.assertIn(word, words)
        for word in ("how", "do", "i", "use", "and", "with"):
            self.assertNotIn(word, words)
        self.assertEqual(help_docs.stem("process"), "process")


class SearchTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.index = fixture_index(**{"docs/integrations/slack.md": SLACK})

    def search(self, query, limit=5):
        return help_docs.search(query, limit, index=self.index)

    def test_acceptance_a_webhook_with_a_signature(self):
        data = self.search(SIGNATURE_QUESTION)
        self.assertEqual((data["query"], data["version"], data["docs_bundled"]),
                         (SIGNATURE_QUESTION, __version__, True))
        best = data["results"][0]
        self.assertEqual(best, {
            "title": "Webhook signatures",
            "section": "Run context › Webhooks › Webhook signatures",
            "doc": "docs/run-context.md",
            "link": BASE + "docs/run-context.md#webhook-signatures",
            "snippet": best["snippet"],
        })
        self.assertIn("PyRun verifies each call", best["snippet"])
        # The changelog entry that announced it comes after the guide.
        docs = [r["doc"] for r in data["results"]]
        self.assertLess(docs.index("docs/run-context.md"), docs.index("CHANGELOG.md"))

    def test_a_changelog_entry_counts_half(self):
        results = self.search("retried after a delay")["results"]
        self.assertEqual([(r["doc"], r["link"]) for r in results[:2]], [
            ("docs/run-context.md", BASE + "docs/run-context.md#retries"),
            ("CHANGELOG.md", BASE + "CHANGELOG.md#retries"),
        ])

    def test_plurals_and_limit(self):
        results = self.search("signatures webhooks", limit=2)["results"]
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["title"], "Webhook signatures")

    def test_integration_pages_are_searched(self):
        best = self.search("post a slack message")["results"][0]
        self.assertEqual((best["doc"], best["link"]), (
            "docs/integrations/slack.md", BASE + "docs/integrations/slack.md#post-a-message"))

    def test_a_long_section_is_cut_from_its_best_paragraph(self):
        best = self.search("cron timezones")["results"][0]
        self.assertEqual(best["title"], "Long section")
        snippet = best["snippet"]
        self.assertTrue(snippet.startswith("… A paragraph about cron timezones"), snippet[:80])
        self.assertTrue(snippet.endswith(" …"))
        self.assertLessEqual(len(snippet), help_docs.SNIPPET_CHARS + 4)

    def test_nothing_to_search_for(self):
        self.assertEqual(self.search("how do I")["results"], [])
        self.assertEqual(self.search("zzzunknownword")["results"], [])


class BundledDocsTests(SimpleTestCase):
    """The real corpus of this checkout, as the image ships it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.index = help_docs.build_index(settings.BASE_DIR)

    def test_every_file_is_read_and_anchors_are_unique(self):
        integrations = sorted(
            f"{help_docs.INTEGRATIONS_DIR}/{p.name}"
            for p in (Path(settings.BASE_DIR) / help_docs.INTEGRATIONS_DIR).glob("*.md")
        )
        self.assertIn("docs/integrations/oauth-apps.md", integrations)
        self.assertEqual(self.index.files, list(help_docs.CORPUS) + integrations)
        self.assertEqual(self.index.missing, [])
        for doc in self.index.files:
            anchors = [s.anchor for s in self.index.sections if s.doc == doc]
            self.assertTrue(anchors, doc)
            self.assertEqual(len(anchors), len(set(anchors)), doc)

    def test_how_do_i_questions_find_their_guide(self):
        best = help_docs.search("maintenance window", index=self.index)["results"][0]
        self.assertEqual(best["link"], BASE + "docs/scheduling.md#maintenance-windows")
        results = help_docs.search("how do I set up a webhook", index=self.index)["results"]
        self.assertTrue(results)
        for result in results:
            self.assertTrue(result["link"].startswith(BASE + result["doc"]), result)

    def test_acceptance_a_webhook_with_a_signature(self):
        """T19's acceptance question, over the real docs (T21 wrote the section)."""
        best = help_docs.search(SIGNATURE_QUESTION, index=self.index)["results"][0]
        self.assertEqual(
            (best["title"], best["doc"], best["link"]),
            ("Webhook signatures", "docs/run-context.md",
             BASE + "docs/run-context.md#webhook-signatures"),
        )
        self.assertIn("Reply, signature and limits", best["snippet"])


class MissingCorpusTests(SimpleTestCase):
    def test_no_docs_means_no_results_and_one_log_line(self):
        with tempfile.TemporaryDirectory() as root, override_settings(BASE_DIR=Path(root)), \
                mock.patch.object(help_docs, "_index", None):
            with self.assertLogs("core.services.help_docs", "WARNING") as logs:
                first = help_docs.search("webhook")
                help_docs.search("schedule")
        self.assertEqual(first, {"query": "webhook", "version": __version__,
                                 "docs_bundled": False, "results": []})
        self.assertEqual(len(logs.records), 1)
        self.assertIn("no bundled docs", logs.records[0].getMessage())

    def test_a_partial_corpus_names_what_is_missing(self):
        with tempfile.TemporaryDirectory() as root, override_settings(BASE_DIR=Path(root)), \
                mock.patch.object(help_docs, "_index", None):
            write_corpus(root, {"README.md": "# PyRun\n\nWebhooks and schedules.\n"})
            with self.assertLogs("core.services.help_docs", "INFO") as logs:
                data = help_docs.search("webhooks")
        self.assertTrue(data["docs_bundled"])
        self.assertEqual(data["results"][0]["link"], BASE + "README.md#pyrun")
        messages = [r.getMessage() for r in logs.records]
        self.assertIn("bundled docs missing or unreadable: docs/run-context.md", messages[0])
        self.assertIn("indexed 1 sections from 1 files", messages[1])

    def test_warm_never_raises_and_a_later_search_retries(self):
        with mock.patch.object(help_docs, "_index", None), \
                mock.patch.object(help_docs, "build_index", side_effect=RuntimeError("boom")):
            with self.assertLogs("core.services.help_docs", "ERROR"):
                help_docs.warm()
            self.assertIsNone(help_docs._index)

    def test_the_web_process_builds_it_at_boot(self):
        saved = sys.modules.pop("pyrunner.wsgi", None)
        self.addCleanup(lambda: sys.modules.__setitem__("pyrunner.wsgi", saved) if saved
                        else sys.modules.pop("pyrunner.wsgi", None))
        with mock.patch.object(help_docs, "warm") as warm:
            importlib.import_module("pyrunner.wsgi")
        warm.assert_called_once_with()


class TransportTests(TestCase):
    def setUp(self):
        _mock_setup(self)
        patcher = mock.patch.object(help_docs, "_index", fixture_index())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ws = Workspace.get_default() or Workspace.objects.create(name="T19", is_default=True)
        self.raw = self.api_token(["help:read"])

    def api_token(self, permissions):
        raw = APIToken.generate_token(scope="api")
        APIToken.objects.create(name=f"t{uuid.uuid4().hex[:6]}", scope="api", token=raw,
                                workspace=self.ws, permissions=permissions)
        return raw

    def rest(self, params, raw=None):
        return self.client.get("/api/v1/help.search", params,
                               HTTP_AUTHORIZATION="Bearer " + (raw or self.raw))

    def rpc(self, method, params=None):
        from core.api.mcp import LEGACY

        return self.client.post(
            "/mcp", {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
            content_type="application/json", HTTP_ACCEPT=ACCEPT,
            HTTP_MCP_PROTOCOL_VERSION=LEGACY[0], HTTP_AUTHORIZATION="Bearer " + self.raw,
        )

    def test_rest(self):
        response = self.rest({"query": "webhook signature", "limit": "1"})
        self.assertEqual(response.status_code, 200, response.content)
        results = response.json()["data"]["results"]
        self.assertEqual([r["link"] for r in results],
                         [BASE + "docs/run-context.md#webhook-signatures"])

    def test_refusals(self):
        other = self.api_token(["system:read"])
        for params, raw, status, code in (
            ({"query": "webhook"}, other, 403, "forbidden"),
            ({"query": "   "}, None, 400, "invalid_input"),
            ({"query": "x" * 201}, None, 400, "invalid_input"),
            ({"query": "webhook", "limit": "11"}, None, 400, "invalid_input"),
            ({}, None, 400, "invalid_input"),
        ):
            with self.subTest(params=params):
                response = self.rest(params, raw)
                self.assertEqual((response.status_code, response.json()["error"]["code"]),
                                 (status, code))

    def test_mcp_and_catalog(self):
        tools = {t["name"] for t in self.rpc("tools/list").json()["result"]["tools"]}
        self.assertIn("help-search", tools)
        result = self.rpc("tools/call", {"name": "help-search",
                                         "arguments": {"query": SIGNATURE_QUESTION}}).json()["result"]
        self.assertFalse(result.get("isError"), result)
        self.assertEqual(result["structuredContent"]["results"][0]["title"], "Webhook signatures")
        catalog = self.client.get("/api/v1/_catalog", HTTP_AUTHORIZATION="Bearer " + self.raw)
        self.assertIn("help.search", {e["slug"] for e in catalog.json()["data"]["endpoints"]})

    def test_settings_name_it(self):
        from django.urls import reverse

        from core.models import User

        self.client.force_login(
            User.objects.create(email="t19-settings@example.com", is_superuser=True))
        self.assertContains(self.client.get(reverse("cpanel:settings")), "system.*, help.*")

    def test_py_ai_on_the_console_and_a_channel_answers_with_the_link(self):
        self.assertIn("help-search", names(build_tools(self.ws)))
        tools = build_tools(self.ws, agent=CHANNEL)
        self.assertIn("help-search", names(tools))
        error, data = call(tools, "help-search", {"query": SIGNATURE_QUESTION})
        self.assertFalse(error, data)
        self.assertEqual(data["results"][0]["link"], BASE + "docs/run-context.md#webhook-signatures")
        for plain_text in (False, True):
            prompt = runtime.base_system_prompt(self.ws, plain_text=plain_text)
            for words in ("search its docs with help-search", "giving the link of each section",
                          "if they don't cover it, say so", "You can only READ"):
                self.assertIn(words, prompt)
