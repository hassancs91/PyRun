"""
Watchers for plugins (2.5 Stage 7, ticket T33).

What this pins, roughly in order of how much it would hurt to get wrong:

- ``WatcherAPI(owner).sync`` is idempotent by owner key (one row however
  often it runs; the database refuses a second one), stamps ownership, checks
  everything before it saves anything, starts the state over only when the
  source changes, and leaves on/off alone; ``pause``/``resume`` work like the
  console's Turn off/on.
- A loaded plugin's ``triggers.py`` adapters join the registry as
  ``<slug>:<key>`` only after the doctor's ``triggers`` rule passes (a file
  that fails it is never imported); an adapter object that fails the checks
  is left out alone; an import that fails is cached, and a plugin that isn't
  loaded offers nothing.
- The runner checks a plugin adapter like a built-in: first check marks,
  then a run per event with the item in its event; ``TriggerError`` and
  ``ConnectionUnavailable`` become the watcher's error text; Test output is
  masked with the connection's values.
- Plugin-owned watchers show the pill and can't be edited in the console
  (pause and Check now still work); deleting the plugin deletes them.
- The doctor's ``triggers`` rule; ``schema_problem`` accepts every shipped
  adapter's schema; backups keep owner keys unique.
"""

import sys
import tempfile
import textwrap
import uuid
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

import plugins as plugins_package
from core.models import Connection, Environment, GlobalSettings, Plugin, Run, Script, Watcher, Workspace
from core.models import WorkspaceMembership
from core.plugins.api import ConnectionView, TriggerAdapter, WatcherAPI
from core.services import plugin_doctor
from core.triggers import registry, runner
from core.triggers.adapters import plugin as plugin_adapters
from core.triggers.adapters.base import schema_problem

User = get_user_model()

SLUG = "t33shop"
SHOP_TOKEN = "t33-shop-access-token-abcdef"

SHOP_TRIGGERS = '''
from core.plugins.api import ConnectionUnavailable, TriggerAdapter, TriggerError

IMPORTS = []
IMPORTS.append(1)


class NewOrder(TriggerAdapter):
    key = "new_order"
    title = "New order"
    description = "Orders from the fake shop."
    config_schema = {
        "type": "object",
        "properties": {"mode": {"type": "string", "maxLength": 20, "default": "ok"}},
    }

    def check(self, ctx, config, state):
        mode = config["mode"]
        if mode == "fail":
            raise TriggerError("The shop is closed.")
        if mode == "conn":
            raise ConnectionUnavailable("Reconnect the shop.", "CONNECTION_EXPIRED")
        if mode == "boom":
            raise RuntimeError("kaboom")
        if state is None:
            return [], {"n": 0}
        n = state["n"] + 1
        return [{"id": f"o{n}", "item": {"order": n, "watcher": ctx.watcher_id}}], {"n": n}

    def test(self, ctx, config):
        return "Shop open.", [{"order": 1, "workspace": ctx.workspace.pk}]


class Signed(TriggerAdapter):
    key = "signed"
    title = "Signed source"
    requires_connection = True
    providers = ("google",)

    def check(self, ctx, config, state):
        return [], state

    def test(self, ctx, config):
        material = ctx.credentials()
        return f"as {ctx.connection.name} ({ctx.connection.provider})", [{"token": material["token"]}]
'''


class _PluginFolder:
    """A plugin package ``plugins.<slug>`` on a temporary folder, loaded or not."""

    def __init__(self, testcase, slug=SLUG, triggers=SHOP_TRIGGERS, loaded=True):
        self.slug = slug
        tmp = tempfile.TemporaryDirectory()
        testcase.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name) / slug
        self.folder.mkdir()
        (self.folder / "__init__.py").write_text("", encoding="utf-8")
        if triggers is not None:
            (self.folder / "triggers.py").write_text(textwrap.dedent(triggers), encoding="utf-8")
        plugins_package.__path__.append(tmp.name)
        testcase.addCleanup(plugins_package.__path__.remove, tmp.name)
        testcase.addCleanup(self._forget)
        plugin_adapters.reset()
        testcase.addCleanup(plugin_adapters.reset)
        if loaded:
            override = override_settings(INSTALLED_PLUGINS=[f"plugins.{slug}"])
            override.enable()
            testcase.addCleanup(override.disable)

    def _forget(self):
        for name in [n for n in sys.modules if n == f"plugins.{self.slug}" or n.startswith(f"plugins.{self.slug}.")]:
            del sys.modules[name]

    @property
    def module(self):
        return sys.modules.get(f"plugins.{self.slug}.triggers")


def _tasks():
    counter = iter(range(1, 10_000))
    return mock.patch("core.tasks.async_task", side_effect=lambda *a, **k: f"task-{next(counter)}")


def _inline():
    """The adapter called on the test's thread (it can then see the TestCase's rows)."""
    return mock.patch.object(runner, "_call_adapter",
                             side_effect=lambda adapter, ctx, config, cursor: adapter.check(ctx, config, cursor))


class _Fixture(TestCase):
    def setUp(self):
        self.ws = Workspace.get_default() or Workspace.objects.create(name="Default", is_default=True)
        self.env = Environment.objects.create(name="t33env", path=f"t33env{uuid.uuid4().hex[:6]}")
        self.user = User.objects.create(email="t33@example.com", username=f"t33{uuid.uuid4().hex[:6]}")
        self.script = self._script(owner_plugin=SLUG, owner_key="worker")

    def _script(self, workspace=None, **kwargs):
        return Script.objects.create(
            name=f"s{uuid.uuid4().hex[:6]}", environment=self.env, workspace=workspace or self.ws,
            is_enabled=True, code="pass", **kwargs,
        )

    def _google(self, name="shop-google", workspace=None, token=SHOP_TOKEN):
        row = Connection(workspace=workspace or self.ws, name=name, provider="google", auth_kind="oauth2",
                         config={}, created_by=self.user)
        row.set_credentials({"access_token": token})
        row.save()
        return row


# --------------------------------------------------------------------------- #
# WatcherAPI
# --------------------------------------------------------------------------- #

class WatcherApiTests(_Fixture):
    def setUp(self):
        super().setUp()
        self.api = WatcherAPI(SLUG)

    def _feed(self, **kwargs):
        return self.api.sync(self.script, "rss_new_item", config={"url": "https://example.com/feed.xml"}, **kwargs)

    def test_sync_creates_an_owned_watcher_with_defaults(self):
        w = self._feed()
        self.assertEqual(
            (w.workspace_id, w.owner_plugin, w.owner_key, w.script_id, w.adapter, w.config, w.connection_id,
             w.interval_seconds, w.fire_cap_per_hour, w.enabled, w.state, w.input_map, w.created_by),
            (self.ws.pk, SLUG, "worker:rss_new_item", self.script.pk, "rss_new_item",
             {"url": "https://example.com/feed.xml"}, None, Watcher.DEFAULT_INTERVAL_SECONDS,
             Watcher.DEFAULT_FIRE_CAP, True, None, {}, None),
        )
        self.assertEqual(w.name, f"New item in an RSS or Atom feed: {self.script.name}")
        self.assertEqual(w.targets, [str(self.script.pk)])

    def test_sync_is_idempotent_by_key(self):
        first = self._feed(interval=600, key="blog", name="Blog")
        again = self._feed(interval=600, key="blog", name="Blog")
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(Watcher.objects.filter(owner_plugin=SLUG).count(), 1)
        other = self._feed(key="news", name="News")
        self.assertNotEqual(other.pk, first.pk)
        self.assertEqual([w.owner_key for w in self.api.list()], ["blog", "news"])
        self.assertEqual(self.api.get("blog").pk, first.pk)
        self.assertIsNone(self.api.get("nope"))

    def test_an_update_keeps_the_state_unless_the_source_changes_and_leaves_on_off_alone(self):
        w = self._feed(key="blog")
        Watcher.objects.filter(pk=w.pk).update(state={"cursor": {"x": 1}, "seen": ["a"]}, enabled=False)
        w = self._feed(key="blog", interval=900, name="Renamed", fire_cap_per_hour=5)
        self.assertEqual((w.state, w.interval_seconds, w.name, w.fire_cap_per_hour, w.enabled),
                         ({"cursor": {"x": 1}, "seen": ["a"]}, 900, "Renamed", 5, False))
        w = self.api.sync(self.script, "rss_new_item", config={"url": "https://example.com/other.xml"}, key="blog")
        self.assertIsNone(w.state)
        self.assertIsNone(w.next_check_at)
        self.assertFalse(w.enabled)

    def test_the_database_refuses_a_second_watcher_with_the_same_key(self):
        self._feed(key="blog")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Watcher.objects.create(workspace=self.ws, name="dup", script=self.script, adapter="rss_new_item",
                                   owner_plugin=SLUG, owner_key="blog")
        # Console watchers (no owner) never clash, nor do two plugins' same key.
        Watcher.objects.create(workspace=self.ws, name="a", script=self.script, adapter="rss_new_item")
        Watcher.objects.create(workspace=self.ws, name="b", script=self.script, adapter="rss_new_item")
        WatcherAPI("otherplug").sync(self.script, "rss_new_item", config={"url": "https://e.com/f"}, key="blog")

    def test_a_race_on_create_updates_the_row_the_other_process_made(self):
        # Another process inserted the key after our lookup found nothing: our
        # insert hits the unique constraint, and the row it made is updated.
        theirs = Watcher.objects.create(workspace=self.ws, name="theirs", script=self.script,
                                        adapter="rss_new_item", owner_plugin=SLUG, owner_key="blog")
        mine = Watcher.objects.filter(workspace=self.ws, owner_plugin=SLUG)
        with mock.patch.object(WatcherAPI, "_qs", side_effect=[Watcher.objects.none(), mine]):
            w = self._feed(key="blog", name="ours")
        self.assertEqual(w.pk, theirs.pk)
        self.assertEqual(w.name, "ours")
        self.assertEqual(Watcher.objects.filter(owner_key="blog").count(), 1)

    def test_bad_arguments_save_nothing(self):
        other_ws = Workspace.objects.create(name="Other")
        foreign = self._script(workspace=other_ws)
        cases = [
            ((self.script, "no_such_adapter"), {}, "No adapter"),
            ((self.script, "rss_new_item"), {"config": {}}, "url"),
            ((self.script, "rss_new_item"), {"config": {"url": 5}}, "url"),
            ((self.script, "rss_new_item"), {"config": {"url": "https://e.com"}, "interval": 59}, "interval"),
            ((self.script, "rss_new_item"), {"config": {"url": "https://e.com"}, "interval": "300"}, "interval"),
            ((self.script, "rss_new_item"), {"config": {"url": "https://e.com"}, "fire_cap_per_hour": 0}, "fire_cap"),
            ((self.script, "rss_new_item"), {"config": {"url": "https://e.com"}, "input_map": {"title": "item.t"}},
             "not an input"),
            ((self.script, "rss_new_item"), {"config": {"url": "https://e.com"}, "key": "k" * 101}, "key"),
            ((foreign, "rss_new_item"), {"config": {"url": "https://e.com"}}, "Script of the workspace"),
            (("missing_key", "rss_new_item"), {"config": {"url": "https://e.com"}}, "no script"),
            ((self.script, "sheets_new_row"), {"config": {"spreadsheet_id": "abc", "range": "Sheet1"}},
             "needs a connection"),
            ((self.script, "rss_new_item"), {"config": {"url": "https://e.com"}, "connection": self._google()},
             "doesn't use a connection"),
        ]
        for args, kwargs, needle in cases:
            with self.subTest(needle=needle):
                with self.assertRaises(ValueError) as cm:
                    self.api.sync(*args, **kwargs)
                self.assertIn(needle, str(cm.exception))
        self.assertFalse(Watcher.objects.exists())
        with self.assertRaises(ValueError):
            WatcherAPI("")

    def test_a_connection_must_match_the_adapter_and_workspace(self):
        http = Connection(workspace=self.ws, name="api", provider="http", auth_kind="bearer",
                          config={"base_url": "https://x.test"}, created_by=self.user)
        http.set_credentials({"token": "t"})
        http.save()
        config = {"spreadsheet_id": "abc", "range": "Sheet1"}
        with self.assertRaisesMessage(ValueError, "needs a google connection, not http"):
            self.api.sync(self.script, "sheets_new_row", config=config, connection=http)
        foreign = self._google(workspace=Workspace.objects.create(name="Other"))
        with self.assertRaisesMessage(ValueError, "watcher's workspace"):
            self.api.sync(self.script, "sheets_new_row", config=config, connection=foreign)
        google = self._google(name="mine")
        w = self.api.sync(self.script, "sheets_new_row", config=config, connection=ConnectionView._from(google))
        self.assertEqual(w.connection_id, google.pk)
        from core.plugins.api import ConnectionUnavailable

        with self.assertRaises(ConnectionUnavailable):
            self.api.sync(self.script, "sheets_new_row", config=config, connection="sheets")

    def test_the_script_by_key_and_an_input_map(self):
        Script.objects.filter(pk=self.script.pk).update(inputs_schema={
            "type": "object", "properties": {"title": {"type": "string"}}})
        w = self.api.sync("worker", "rss_new_item", config={"url": "https://e.com/f"},
                          input_map={"title": " item.title "})
        self.assertEqual((w.script_id, w.input_map), (self.script.pk, {"title": "item.title"}))

    def test_pause_resume_and_delete(self):
        w = self._feed(key="blog")
        Watcher.objects.filter(pk=w.pk).update(enabled=False, auto_disabled_at="2026-11-11T10:00:00Z",
                                               error_count=10)
        w = self.api.resume("blog")
        self.assertEqual((w.enabled, w.auto_disabled_at, w.error_count, w.next_check_at), (True, None, 0, None))
        w = self.api.pause("blog")
        w.refresh_from_db()
        self.assertFalse(w.enabled)
        with self.assertRaisesMessage(ValueError, "no watcher 'nope'"):
            self.api.pause("nope")
        self.assertTrue(self.api.delete("blog"))
        self.assertFalse(self.api.delete("blog"))

    def test_another_plugins_watchers_are_out_of_reach(self):
        WatcherAPI("otherplug").sync(self.script, "rss_new_item", config={"url": "https://e.com/f"}, key="blog")
        self.assertEqual(self.api.list(), [])
        self.assertIsNone(self.api.get("blog"))
        with self.assertRaises(ValueError):
            self.api.pause("blog")
        self.assertFalse(self.api.delete("blog"))


# --------------------------------------------------------------------------- #
# A plugin's triggers.py in the registry, and the runner
# --------------------------------------------------------------------------- #

class PluginAdapterTests(_Fixture):
    def test_a_loaded_plugins_adapters_join_the_registry(self):
        _PluginFolder(self)
        table = registry.adapters()
        self.assertIn("rss_new_item", table)
        order = [key for key in table if key.startswith(f"{SLUG}:")]
        self.assertEqual(order, [f"{SLUG}:new_order", f"{SLUG}:signed"])
        adapter = table[f"{SLUG}:new_order"]
        self.assertEqual((adapter.owner_plugin, adapter.title, adapter.requires_connection, adapter.providers),
                         (SLUG, "New order", False, ()))
        signed = table[f"{SLUG}:signed"]
        self.assertEqual((signed.requires_connection, signed.providers), (True, ("google",)))
        self.assertEqual(signed.description, f'From the "{SLUG}" plugin.')
        # Imported once per process, however often the table is built.
        registry.adapters()
        self.assertEqual(sys.modules[f"plugins.{SLUG}.triggers"].IMPORTS, [1])

    def test_a_plugin_that_isnt_loaded_offers_nothing(self):
        _PluginFolder(self, loaded=False)
        self.assertFalse([k for k in registry.adapters() if k.startswith(f"{SLUG}:")])
        self.assertNotIn(f"plugins.{SLUG}.triggers", sys.modules)

    def test_a_triggers_py_that_fails_the_doctor_is_never_imported(self):
        bad = SHOP_TRIGGERS.replace('title = "New order"', 'title = ""') + "\nraise RuntimeError('imported')\n"
        _PluginFolder(self, triggers=bad)
        with self.assertLogs("core.triggers.adapters.plugin", "WARNING") as logs:
            table = registry.adapters()
        self.assertFalse([k for k in table if k.startswith(f"{SLUG}:")])
        self.assertNotIn(f"plugins.{SLUG}.triggers", sys.modules)
        self.assertIn("fails the doctor", "\n".join(logs.output))

    def test_a_failed_import_is_cached_and_offers_nothing(self):
        _PluginFolder(self, triggers=SHOP_TRIGGERS + "\nraise RuntimeError('broken at import')\n")
        with mock.patch("importlib.import_module", wraps=__import__("importlib").import_module) as imp:
            with self.assertLogs("core.triggers.adapters.plugin", "ERROR"):
                registry.adapters()
            registry.adapters()
        names = [c.args[0] for c in imp.call_args_list]
        self.assertEqual(names.count(f"plugins.{SLUG}.triggers"), 1)
        self.assertFalse([k for k in registry.adapters() if k.startswith(f"{SLUG}:")])

    def test_an_adapter_object_that_fails_the_checks_is_left_out_alone(self):
        code = SHOP_TRIGGERS + textwrap.dedent('''
            NESTED = {"type": "object", "properties": {"deep": {"type": "object", "properties": {}}}}


            class Nested(TriggerAdapter):
                key = "nested"
                title = "Nested settings"
                config_schema = NESTED

                def check(self, ctx, config, state):
                    return [], state

                def test(self, ctx, config):
                    return "", []
        ''')
        _PluginFolder(self, triggers=code)
        with self.assertLogs("core.triggers.adapters.plugin", "WARNING") as logs:
            table = registry.adapters()
        self.assertIn(f"{SLUG}:new_order", table)
        self.assertNotIn(f"{SLUG}:nested", table)
        self.assertIn('setting "deep"', "\n".join(logs.output))

    def test_adapter_problem(self):
        class Good(TriggerAdapter):
            key, title = "ok", "Fine"

            def check(self, ctx, config, state):
                return [], state

            def test(self, ctx, config):
                return "", []

        def variant(**attrs):
            return type("V", (Good,), attrs)()

        self.assertEqual(plugin_adapters.adapter_problem("shop", Good()), "")
        for attrs, needle in [
            ({"key": "Bad-Key"}, "lowercase"),
            ({"key": "k" * 80}, "over 80"),
            ({"title": " "}, "title"),
            ({"description": None}, "description"),
            ({"config_schema": {"type": "array"}}, "object schema"),
            ({"providers": ("nope",)}, "unknown connection provider"),
            ({"providers": "google"}, "tuple"),
        ]:
            with self.subTest(attrs=attrs):
                self.assertIn(needle, plugin_adapters.adapter_problem("shop", variant(**attrs)))

        class NoTest(TriggerAdapter):
            key, title = "nt", "No test"

            def check(self, ctx, config, state):
                return [], state

        self.assertIn("test()", plugin_adapters.adapter_problem("shop", NoTest()))

    def _watcher(self, mode="ok", **kwargs):
        return Watcher.objects.create(workspace=self.ws, name="Orders", script=self.script,
                                      adapter=f"{SLUG}:new_order", config={"mode": mode}, created_by=self.user,
                                      **kwargs)

    def test_the_runner_checks_a_plugin_adapter_like_a_built_in(self):
        _PluginFolder(self)
        w = self._watcher()
        with _inline(), _tasks():
            first = runner.check_watcher(w.pk)
            Watcher.objects.filter(pk=w.pk).update(next_check_at=None)
            second = runner.check_watcher(w.pk)
        self.assertEqual((first.outcome, second.outcome), (runner.OK, runner.OK))
        self.assertEqual((first.runs, second.runs), (0, 1))
        run = Run.objects.get(script=self.script)
        self.assertEqual(run.trigger_type, Run.TriggerType.WATCHER)
        self.assertEqual(run.event["kind"], "watcher")
        self.assertEqual(run.event["watcher"]["adapter"], f"{SLUG}:new_order")
        self.assertEqual(run.event["item"], {"order": 1, "watcher": str(w.pk)})
        self.assertEqual(run.event["event_id"], "o1")
        w.refresh_from_db()
        self.assertEqual((w.error_count, w.last_error), (0, ""))

    def test_plugin_errors_become_the_watchers_error_text(self):
        _PluginFolder(self)
        for mode, text in [("fail", "The shop is closed."),
                           ("conn", "CONNECTION_EXPIRED: Reconnect the shop."),
                           ("boom", "kaboom")]:
            with self.subTest(mode=mode):
                w = self._watcher(mode=mode, state={"cursor": {"n": 0}, "seen": []})
                with _inline(), _tasks(), self.assertLogs("core.triggers.adapters.plugin", "WARNING") as logs:
                    result = runner.check_watcher(w.pk)
                    # Only an exception the plugin didn't mean to show is logged
                    # (assertLogs needs one record: add a marker).
                    plugin_adapters.logger.warning("marker")
                self.assertEqual(len(logs.output), 2 if mode == "boom" else 1, logs.output)
                self.assertEqual(result.outcome, runner.FAILED)
                w.refresh_from_db()
                self.assertEqual((w.error_count, w.last_error), (1, text))
                w.delete()
        self.assertFalse(Run.objects.exists())

    def test_a_watcher_whose_plugin_went_away_fails_as_unavailable(self):
        w = self._watcher(state={"cursor": {"n": 0}, "seen": []})
        with _inline(), _tasks():
            result = runner.check_watcher(w.pk)
        self.assertEqual(result.outcome, runner.FAILED)
        w.refresh_from_db()
        self.assertIn("isn't available", w.last_error)

    def test_test_gets_a_context_and_its_output_is_masked(self):
        _PluginFolder(self)
        adapter = registry.get(f"{SLUG}:new_order")
        ok, message, items = runner.test_adapter(adapter, {}, workspace=self.ws)
        self.assertEqual((ok, message, items), (True, "Shop open.", [{"order": 1, "workspace": self.ws.pk}]))
        signed = registry.get(f"{SLUG}:signed")
        google = self._google()
        ok, message, items = runner.test_adapter(signed, {}, google, workspace=self.ws)
        self.assertTrue(ok, message)
        self.assertEqual(message, "as shop-google (google)")
        self.assertNotIn(SHOP_TOKEN, str(items))
        self.assertIn("MASKED", items[0]["token"])
        ok, message, _ = runner.test_adapter(signed, {}, None, workspace=self.ws)
        self.assertEqual((ok, message), (False, "CONNECTION_NOT_FOUND: This watcher has no connection."))

    def test_watcher_api_takes_the_plugins_own_adapter_by_name_or_class(self):
        folder = _PluginFolder(self)
        api = WatcherAPI(SLUG)
        a = api.sync(self.script, "new_order", key="a")
        self.assertEqual(a.adapter, f"{SLUG}:new_order")
        self.assertEqual(a.config, {})
        cls = registry.get(f"{SLUG}:new_order").plugin_adapter.__class__
        b = api.sync(self.script, cls, config={"mode": "fail"}, key="b")
        self.assertEqual((b.adapter, b.config), (f"{SLUG}:new_order", {"mode": "fail"}))
        # Another plugin names it in full; a bare name is only looked up among one's own.
        c = WatcherAPI("otherplug").sync(self.script, f"{SLUG}:new_order", key="c")
        self.assertEqual(c.adapter, f"{SLUG}:new_order")
        with self.assertRaisesMessage(ValueError, "No adapter 'new_order'"):
            WatcherAPI("otherplug").sync(self.script, "new_order", key="d")
        self.assertIsNotNone(folder.module)


# --------------------------------------------------------------------------- #
# Console
# --------------------------------------------------------------------------- #

class ConsoleTests(_Fixture):
    def setUp(self):
        super().setUp()
        gs = GlobalSettings.get_settings()
        gs.setup_completed = True
        gs.save()
        Environment.objects.filter(pk=self.env.pk).update(is_default=True)
        self.admin = User.objects.create_user(
            username="t33admin@example.com", email="t33admin@example.com", password="x-Pass-12345!",
            is_superuser=True, is_staff=True,
        )
        WorkspaceMembership.ensure(self.admin, self.ws, role=WorkspaceMembership.ROLE_OWNER)
        self.client.force_login(self.admin)
        self.owned = WatcherAPI(SLUG).sync(self.script, "rss_new_item", config={"url": "https://e.com/f"},
                                           key="blog", name="Plugin blog")
        self.mine = Watcher.objects.create(workspace=self.ws, name="My feed", script=self.script,
                                           adapter="rss_new_item", config={"url": "https://e.com/g"})

    def _edit_url(self, watcher):
        return reverse("cpanel:watcher_edit", kwargs={"watcher_id": watcher.pk})

    def test_an_owned_watcher_shows_the_pill_and_no_edit_link(self):
        detail = self.client.get(reverse("cpanel:watcher_detail", kwargs={"watcher_id": self.owned.pk}))
        html = detail.content.decode()
        self.assertIn(f"Managed by the “{SLUG}” plugin", html)
        self.assertNotIn(self._edit_url(self.owned), html)
        self.assertIn(reverse("cpanel:watcher_toggle", kwargs={"watcher_id": self.owned.pk}), html)
        listing = self.client.get(reverse("cpanel:script_triggers", kwargs={"pk": self.script.pk})).content.decode()
        self.assertNotIn(self._edit_url(self.owned), listing)
        self.assertIn(self._edit_url(self.mine), listing)
        self.assertIn(self._edit_url(self.mine), self.client.get(
            reverse("cpanel:watcher_detail", kwargs={"watcher_id": self.mine.pk})).content.decode())

    def test_an_owned_watcher_cant_be_edited_but_can_be_paused(self):
        resp = self.client.get(self._edit_url(self.owned), follow=True)
        self.assertRedirects(resp, reverse("cpanel:watcher_detail", kwargs={"watcher_id": self.owned.pk}))
        self.assertContains(resp, "plugin manages this watcher")
        form = {"name": "Hijacked", "script": str(self.script.pk), "interval_seconds": "60",
                "fire_cap_per_hour": "10", "input_map": "", "enabled": "on", "cfg_url": "https://evil.test/f",
                "action": "save"}
        self.client.post(self._edit_url(self.owned) + "?adapter=rss_new_item", form)
        self.owned.refresh_from_db()
        self.assertEqual((self.owned.name, self.owned.config), ("Plugin blog", {"url": "https://e.com/f"}))
        self.client.post(reverse("cpanel:watcher_toggle", kwargs={"watcher_id": self.owned.pk}))
        self.owned.refresh_from_db()
        self.assertFalse(self.owned.enabled)
        # A console watcher's form still works.
        self.assertEqual(self.client.get(self._edit_url(self.mine)).status_code, 200)

    def test_the_picker_shows_a_plugin_adapter_with_its_pill(self):
        _PluginFolder(self)
        html = self.client.get(reverse("cpanel:watcher_create") + f"?script={self.script.pk}").content.decode()
        self.assertIn("New order", html)
        self.assertIn(f"adapter={SLUG}%3Anew_order", html)
        self.assertIn(f"Managed by the “{SLUG}” plugin", html)
        form = self.client.get(reverse("cpanel:watcher_create")
                               + f"?script={self.script.pk}&adapter={SLUG}:new_order")
        self.assertContains(form, 'name="cfg_mode"')


# --------------------------------------------------------------------------- #
# Uninstall, backups, the doctor, schemas
# --------------------------------------------------------------------------- #

class UninstallTests(_Fixture):
    def test_deleting_the_plugin_deletes_its_watchers_only(self):
        from core.services.plugin_service import PluginService

        plugin = Plugin.objects.create(slug=SLUG, name="Shop", version="1.0.0", manifest={})
        WatcherAPI(SLUG).sync(self.script, "rss_new_item", config={"url": "https://e.com/f"}, key="a")
        WatcherAPI("otherplug").sync(self.script, "rss_new_item", config={"url": "https://e.com/f"}, key="a")
        mine = Watcher.objects.create(workspace=self.ws, name="Mine", script=self.script, adapter="rss_new_item")
        with override_settings(PLUGINS_DIR=tempfile.mkdtemp()):
            PluginService.delete(plugin, remove_data=False)
        self.assertEqual(sorted(Watcher.objects.values_list("owner_plugin", flat=True), key=str),
                         sorted([None, "otherplug"], key=str))
        self.assertTrue(Watcher.objects.filter(pk=mine.pk).exists())
        self.assertTrue(Script.objects.filter(pk=self.script.pk).exists())


class BackupTests(_Fixture):
    def test_restore_leaves_out_a_second_watcher_with_the_same_owner_key(self):
        from core.services.backup_service import BackupService

        WatcherAPI(SLUG).sync(self.script, "rss_new_item", config={"url": "https://e.com/f"}, key="blog")
        data = BackupService.create_backup(include_runs=False)
        row = data["watchers"][0]
        self.assertEqual((row["owner_plugin"], row["owner_key"]), (SLUG, "blog"))
        data["watchers"].append({**row, "id": str(uuid.uuid4()), "name": "Copy"})
        result = BackupService.restore_backup(data, restore_runs=False, current_user=self.user)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["counts"]["watchers"], 1)
        self.assertTrue(any("has its key" in w for w in result.get("warnings", [])), result.get("warnings"))
        w = Watcher.objects.get()
        self.assertEqual((w.owner_plugin, w.owner_key, w.name), (SLUG, "blog", row["name"]))


GOOD_FILE = '''
from core.plugins.api import TriggerAdapter


class A(TriggerAdapter):
    key = "new_order"
    title = "New order"
    providers = ("google",)
    config_schema = {"type": "object", "properties": {"n": {"type": "integer", "minimum": 1}}}

    def check(self, ctx, config, state):
        return [], state

    def test(self, ctx, config):
        return "", []
'''


class DoctorTests(SimpleTestCase):
    def _problems(self, code, slug="shop"):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / slug
            folder.mkdir()
            if code is not None:
                (folder / "triggers.py").write_text(textwrap.dedent(code), encoding="utf-8")
            return plugin_doctor.triggers_problems(folder)

    def test_a_good_file_passes(self):
        self.assertEqual(self._problems(GOOD_FILE), ([], [], ["shop:new_order"]))
        self.assertEqual(self._problems(None), ([], [], []))

    def test_each_rule(self):
        cases = [
            (GOOD_FILE.replace('key = "new_order"', "key = KEY"), "'key' must be a literal string"),
            (GOOD_FILE.replace('"new_order"', '"New-Order"'), "must be a lowercase letter"),
            (GOOD_FILE.replace('"new_order"', '"' + "k" * 76 + '"'), "over 80 characters"),
            (GOOD_FILE + GOOD_FILE.replace("class A(", "class B("), "used by another adapter"),
            (GOOD_FILE.replace('title = "New order"', ""), "'title' must be a literal"),
            (GOOD_FILE.replace("def test(", "def other("), "must define test()"),
            (GOOD_FILE.replace('"integer", "minimum": 1', '"object"'), 'setting "n"'),
            (GOOD_FILE.replace('("google",)', '("nope",)'), "unknown connection provider"),
            (GOOD_FILE.replace('("google",)', '"google"'), "'providers' must be a tuple"),
            ("def broken(:\n", "cannot be parsed"),
        ]
        for code, needle in cases:
            with self.subTest(needle=needle):
                failures, _, _ = self._problems(code)
                self.assertTrue(any(needle in f for f in failures), failures)

    def test_no_adapters_only_warns_and_built_schemas_are_left_to_the_loader(self):
        failures, warnings, keys = self._problems("X = 1\n")
        self.assertEqual((failures, keys), ([], []))
        self.assertIn("no TriggerAdapter subclass", warnings[0])
        built = GOOD_FILE.replace('config_schema = {"type"', 'config_schema = dict(SCHEMA) or {"type"')
        self.assertEqual(self._problems(built)[0], [])

    def test_run_doctor_reports_the_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "shop"
            folder.mkdir()
            (folder / "triggers.py").write_text(GOOD_FILE.replace('title = "New order"', ""), encoding="utf-8")
            report = plugin_doctor.run_doctor(folder)
        rule = [f for f in report.findings if f.rule == "triggers"]
        self.assertEqual([f.severity for f in rule], [plugin_doctor.FAIL])
        self.assertFalse(report.ok)


class SchemaProblemTests(SimpleTestCase):
    def test_every_shipped_adapter_passes(self):
        for adapter in registry.adapters().values():
            with self.subTest(adapter=adapter.key):
                self.assertEqual(schema_problem(adapter.config_schema), "")

    def test_refusals(self):
        for schema, needle in [
            ([], "object schema"),
            ({"type": "object", "properties": []}, "properties must be an object"),
            ({"type": "object", "properties": {"a b": {"type": "string"}}}, "letters, digits"),
            ({"type": "object", "properties": {"a": {"type": "array"}}}, '"type" must be one of'),
            ({"type": "object", "properties": {"a": {"type": "string", "pattern": "("}}}, "regular expression"),
            ({"type": "object", "properties": {"a": {"type": "string", "bogus": 1}}}, "config_schema:"),
            ({"type": "object", "properties": {"a": {"type": "integer", "minimum": 5, "default": 1}}}, "default"),
            ({"type": "object", "properties": {"a": {"type": "string", "minLength": -1}}}, "minLength"),
        ]:
            with self.subTest(needle=needle):
                self.assertIn(needle, schema_problem(schema))

    def test_min_length_is_enforced(self):
        from core.triggers.adapters import validate_config

        schema = {"type": "object", "properties": {"a": {"type": "string", "minLength": 3}}}
        self.assertEqual(validate_config(schema, {"a": "ab"}), ({}, {"a": "Too short."}))
        self.assertEqual(validate_config(schema, {"a": "abc"}), ({"a": "abc"}, {}))
