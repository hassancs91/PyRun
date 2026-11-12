"""
Plugin store: signed index, browse, install and update (2.5 T44).

What this pins, roughly in order of how much it would hurt to get wrong:

- The index is refused unless its signature checks out with a trusted,
  unrevoked key of the publisher it names; any changed value breaks it, key
  order and whitespace don't. A cached copy is re-verified on every read.
- A store download is SSRF-guarded and size-capped, and installs only when its
  sha256 (and size) match the index and the zip holds that slug; it then goes
  through the normal install / update path (signature, pin, trust policy).
- Install from URL: the same guard and cap, an optional sha256.
- "Update available" comes from the index; an update from the store is a
  PluginUpdate with source store; the dashboard and the Store tab show it.
- update_policy exists with only manual used; auto_update is off behind a
  hidden flag; the 12-hour refresh task and its infra schedule.
- plugin_store_index builds an index this PyRun reads; the store URL setting
  round-trips through a backup.

Everything is served by a local test server; every key is generated here.
"""

import hashlib
import http.server
import json
import shutil
import tempfile
import threading
from pathlib import Path
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from core.models import GlobalSettings, Plugin, PluginStoreIndex, PluginUpdate, User
from core.plugins.trust import keys
from core.plugins.trust.index import StoreIndexError, parse_index, sign_index
from core.plugins.trust.keys import TrustedKey
from core.plugins.trust.signing import private_key_pem, public_key_b64
from core.services import PluginInstallError, plugin_store
from core.services.backup_service import BackupService
from core.test_plugin_signing import KEY, OTHER, STRANGER, demo_files, signed_files, trusted, zip_of
from core.test_plugin_update import SLUG, _Dir

INDEX = "/index.json"


class _Store(http.server.BaseHTTPRequestHandler):
    files = {}
    hits = []

    def do_GET(self):
        _Store.hits.append(self.path)
        body = _Store.files.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def zip_bytes(files, slug=SLUG) -> bytes:
    return zip_of(files, slug).getvalue()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def index_bytes(entries, *, key=KEY, key_id="test-1", publisher="pyrun", **extra) -> bytes:
    body = {"schema": 1, "generated_at": "2026-11-12T10:00:00Z", "plugins": entries, **extra}
    return json.dumps(sign_index(body, key, publisher=publisher, key_id=key_id)).encode()


def entry_dict(data: bytes, *, slug=SLUG, version="1.0.0", url="", **extra) -> dict:
    return {
        "slug": slug, "publisher": "pyrun", "name": "Demo", "version": version,
        "download_url": url or f"zips/{slug}-{version}.zip", "sha256": sha(data),
        "size": len(data), "signed": True, **extra,
    }


# --------------------------------------------------------------------------- #
# The index format
# --------------------------------------------------------------------------- #


class IndexTests(SimpleTestCase):
    BASE = "https://store.example/pyrun/index.json"

    def setUp(self):
        patch = trusted()
        patch.start()
        self.addCleanup(patch.stop)
        self.data = b"zip bytes"

    def parse(self, raw):
        return parse_index(raw, base_url=self.BASE)

    def refused(self, raw, fragment):
        with self.assertRaises(StoreIndexError) as caught:
            self.parse(raw)
        self.assertIn(fragment, str(caught.exception))

    def test_signed_index_round_trips_and_resolves_relative_urls(self):
        raw = index_bytes([entry_dict(self.data, categories=["Data"], tier="pro", requires_license=True,
                                      provisions={"scripts": 1})])
        index = self.parse(raw)
        entry = index.get(SLUG)
        self.assertEqual((index.publisher, index.key_id), ("pyrun", "test-1"))
        self.assertEqual(entry.download_url, f"https://store.example/pyrun/zips/{SLUG}-1.0.0.zip")
        self.assertEqual((entry.sha256, entry.size, entry.signed), (sha(self.data), len(self.data), True))
        self.assertEqual((entry.tier, entry.requires_license, entry.categories), ("pro", True, ("Data",)))
        self.assertEqual(entry.provisions, {"scripts": 1})
        self.assertEqual(index.categories, ["Data"])

    def test_key_order_and_whitespace_do_not_matter_but_values_do(self):
        doc = json.loads(index_bytes([entry_dict(self.data)]))
        reordered = json.dumps(dict(reversed(list(doc.items()))), indent=4).encode()
        self.assertEqual(len(self.parse(reordered).entries), 1)
        doc["plugins"][0]["sha256"] = "0" * 64
        self.refused(json.dumps(doc).encode(), "signature does not match")

    def test_unsigned_untrusted_revoked_and_wrong_publisher_are_refused(self):
        doc = json.loads(index_bytes([entry_dict(self.data)]))
        del doc["signature"]
        self.refused(json.dumps(doc).encode(), "not signed")
        self.refused(index_bytes([], key=STRANGER, key_id="stranger-1"), "does not trust")
        # acme's key claiming to sign as pyrun.
        self.refused(index_bytes([], key=OTHER, key_id="acme-1", publisher="pyrun"), "belongs to the publisher 'acme'")
        with mock.patch.dict(keys.TRUSTED_KEYS, {
            "test-1": TrustedKey(publisher="pyrun", public_key=public_key_b64(KEY), revoked=True),
        }):
            self.refused(index_bytes([]), "revoked")
        # Signed by acme, as acme: a trusted key, so a private index works.
        self.assertEqual(self.parse(index_bytes([], key=OTHER, key_id="acme-1", publisher="acme")).publisher, "acme")

    def test_malformed_entries_refuse_the_whole_index(self):
        cases = [
            ({"schema": 2}, "schema 2"),
            ({"plugins": "x"}, "'plugins' list"),
        ]
        for extra, fragment in cases:
            with self.subTest(extra=extra):
                body = {"schema": 1, "plugins": [], **extra}
                self.refused(json.dumps(sign_index(body, KEY, publisher="pyrun", key_id="test-1")).encode(), fragment)
        bad = [
            (dict(slug="Bad-Slug"), "not a valid plugin slug"),
            (dict(version="one"), "not semver"),
            (dict(sha256="xyz"), "sha256"),
            (dict(size=-1), "size"),
            (dict(size=60 * 1024 * 1024), "size"),
            (dict(download_url="ftp://x/y.zip"), "download_url"),
            (dict(icon_url="http://insecure.example/i.png"), "icon_url"),
            (dict(categories="Data"), "categories"),
            (dict(signed="yes"), "'signed'"),
            (dict(publisher="Py Run"), "publisher"),
        ]
        for change, fragment in bad:
            with self.subTest(change=change):
                self.refused(index_bytes([entry_dict(self.data, **change)]), fragment)
        twice = entry_dict(self.data)
        self.refused(index_bytes([twice, dict(twice)]), "twice")
        self.refused(b"not json", "not valid JSON")


# --------------------------------------------------------------------------- #
# The service against a local store
# --------------------------------------------------------------------------- #


@override_settings(PYRUNNER_ALLOW_PRIVATE_NETWORK=True)
class _StoreBase(_Dir):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Store)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        _Store.files, _Store.hits = {}, []
        patch = trusted()
        patch.start()
        self.addCleanup(patch.stop)
        plugin_store._memo[0] = None
        self.addCleanup(plugin_store._memo.__setitem__, 0, None)
        self.set_url(self.base + INDEX)

    def set_url(self, url):
        settings_obj = GlobalSettings.get_settings()
        settings_obj.plugin_store_url = url
        settings_obj.save(update_fields=["plugin_store_url"])

    def publish(self, *plugins, **index_kw):
        """Serve zips and a signed index. Each plugin: (files, signed, entry overrides)."""
        entries = []
        for files, signed, overrides in plugins:
            data = zip_bytes(signed_files(files) if signed else files)
            version = json.loads(files["plugin.json"])["version"]
            path = f"/zips/{SLUG}-{version}.zip"
            _Store.files[path] = data
            entries.append(entry_dict(data, version=version, signed=signed, **overrides))
        _Store.files[INDEX] = index_bytes(entries, **index_kw)
        return entries


class RefreshTests(_StoreBase):
    def test_no_url_means_no_store(self):
        self.set_url("")
        self.assertEqual(plugin_store.index_url(), plugin_store.OFFICIAL_INDEX_URL)
        with mock.patch.object(plugin_store, "OFFICIAL_INDEX_URL", ""):
            with self.assertRaisesMessage(plugin_store.StoreError, "No plugin store index is set"):
                plugin_store.refresh()

    def test_official_index_is_used_when_the_setting_is_blank(self):
        self.set_url("")
        with mock.patch.object(plugin_store, "OFFICIAL_INDEX_URL", self.base + INDEX):
            self.publish((demo_files(), True, {}))
            self.assertEqual(len(plugin_store.refresh().entries), 1)

    def test_refresh_caches_and_pages_read_without_the_network(self):
        self.publish((demo_files(), True, {}))
        index = plugin_store.refresh()
        self.assertEqual([e.slug for e in index.entries], [SLUG])
        hits = len(_Store.hits)
        state = plugin_store.state()
        self.assertEqual(state.index.get(SLUG).version, "1.0.0")
        self.assertFalse(state.stale)
        self.assertEqual(len(_Store.hits), hits)
        row = PluginStoreIndex.objects.get()
        self.assertEqual((row.url, row.error), (self.base + INDEX, ""))

    def test_a_failed_refresh_keeps_the_last_good_copy_and_records_why(self):
        self.publish((demo_files(), True, {}))
        plugin_store.refresh()
        _Store.files[INDEX] = b'{"schema": 1}'
        with self.assertRaisesMessage(plugin_store.StoreError, "not signed"):
            plugin_store.refresh()
        state = plugin_store.state()
        self.assertIsNotNone(state.index)
        self.assertIn("not signed", state.error)
        # Another URL: the old copy is not shown for it.
        self.set_url(self.base + "/other.json")
        self.assertIsNone(plugin_store.state().index)

    def test_index_over_2_mb_is_refused(self):
        _Store.files[INDEX] = b" " * (plugin_store.INDEX_MAX_BYTES + 1)
        with self.assertRaisesMessage(plugin_store.StoreError, "larger than 2 MB"):
            plugin_store.refresh()

    @override_settings(PYRUNNER_ALLOW_PRIVATE_NETWORK=False)
    def test_ssrf_guard_refuses_a_private_index(self):
        self.publish((demo_files(), True, {}))
        with self.assertRaises(plugin_store.StoreError):
            plugin_store.refresh()
        self.assertEqual(_Store.hits, [])

    def test_cached_index_is_verified_again_on_read(self):
        self.publish((demo_files(), True, {}))
        plugin_store.refresh()
        plugin_store._memo[0] = None
        with mock.patch.dict(keys.TRUSTED_KEYS, {
            "test-1": TrustedKey(publisher="pyrun", public_key=public_key_b64(KEY), revoked=True),
        }):
            state = plugin_store.state()
        self.assertIsNone(state.index)
        self.assertIn("revoked", state.problem)


class InstallTests(_StoreBase):
    def test_install_from_store_verifies_and_records_the_source(self):
        self.publish((demo_files(), True, {}))
        plugin, update, _ = plugin_store.install_from_store(SLUG, user=self.admin)
        self.assertIsNone(update)
        self.assertEqual((plugin.source, plugin.trust, plugin.verified_by_pyrun), ("store", "verified", True))
        self.assertEqual(plugin.update_policy, Plugin.UpdatePolicy.MANUAL)
        self.assertEqual(plugin_store.item_for(plugin_store.state().index.get(SLUG), plugin).action, "installed")
        with self.assertRaisesMessage(plugin_store.StoreError, "nothing newer"):
            plugin_store.install_from_store(SLUG)

    def test_sha256_or_size_mismatch_installs_nothing(self):
        for overrides, fragment in (
            ({"sha256": "0" * 64}, "does not match the sha256"),
            # In bytes against the index, not "larger than 9.54e-06 MB" (2.5 test pass 8).
            ({"size": 10}, "is larger than the 10 bytes the store index says"),
        ):
            with self.subTest(overrides=overrides):
                plugin_store._memo[0] = None
                self.publish((demo_files(), True, overrides))
                plugin_store.refresh()
                with self.assertRaisesMessage(plugin_store.StoreError, fragment):
                    plugin_store.install_from_store(SLUG)
                self.assertFalse(Plugin.objects.exists())
                self.assertFalse((self.root / SLUG).exists())

    def test_a_tampered_zip_with_a_matching_index_is_still_refused_by_the_signature(self):
        files = signed_files(demo_files())
        files["marker.txt"] = "tampered\n"
        data = zip_bytes(files)
        _Store.files[f"/zips/{SLUG}-1.0.0.zip"] = data
        _Store.files[INDEX] = index_bytes([entry_dict(data)])
        with self.assertRaisesMessage(Exception, "failed its signature check"):
            plugin_store.install_from_store(SLUG)
        self.assertFalse(Plugin.objects.exists())

    def test_a_zip_of_another_slug_is_refused(self):
        other = demo_files(slug="t44other")
        data = zip_bytes(other, slug="t44other")
        _Store.files[f"/zips/{SLUG}-1.0.0.zip"] = data
        _Store.files[INDEX] = index_bytes([entry_dict(data)])
        with self.assertRaisesMessage(plugin_store.StoreError, "holds another plugin"):
            plugin_store.install_from_store(SLUG)
        self.assertFalse(Plugin.objects.exists())

    def test_unknown_slug(self):
        self.publish((demo_files(), True, {}))
        with self.assertRaisesMessage(plugin_store.StoreError, "not in the plugin store"):
            plugin_store.install_from_store("t44missing")

    def test_problems_disable_install(self):
        cases = [
            ({"min_pyrunner": "99.0.0"}, "needs PyRun 99.0.0"),
            ({"api": "99.0"}, "needs plugin SDK 99.0"),
        ]
        for overrides, fragment in cases:
            with self.subTest(overrides=overrides):
                plugin_store._memo[0] = None
                self.publish((demo_files(), True, overrides))
                plugin_store.refresh()
                item = plugin_store.items(plugin_store.state().index)[0]
                self.assertIn(fragment, item.problem)
                with self.assertRaisesMessage(plugin_store.StoreError, fragment):
                    plugin_store.install_from_store(SLUG)

    def test_signed_only_disables_unsigned_entries(self):
        self.publish((demo_files(), False, {}))
        plugin_store.refresh()
        GlobalSettings.objects.filter(pk=1).update(plugin_trust_policy="signed_only")
        item = plugin_store.items(plugin_store.state().index)[0]
        self.assertIn("only accepts signed plugins", item.problem)
        with self.assertRaises(plugin_store.StoreError):
            plugin_store.install_from_store(SLUG)
        self.assertFalse(Plugin.objects.exists())

    def test_update_available_then_update_from_the_store(self):
        self.install("1.0.0")
        self.publish((demo_files("1.1.0"), True, {}))
        plugin_store.refresh()
        updates = plugin_store.updates_available()
        self.assertEqual([(u.entry.slug, u.installed.version, u.entry.version) for u in updates],
                         [(SLUG, "1.0.0", "1.1.0")])
        plugin, update, _ = plugin_store.install_from_store(SLUG, user=self.admin)
        self.assertEqual((plugin.version, update.source, update.initiated_by, update.user),
                         ("1.1.0", PluginUpdate.Source.STORE, PluginUpdate.InitiatedBy.USER, self.admin))
        self.assertEqual(self.live(), "version 1.1.0\n")
        self.assertEqual(plugin_store.updates_available(), [])

    def test_install_from_url(self):
        data = zip_bytes(demo_files())
        _Store.files["/my.zip"] = data
        url = self.base + "/my.zip"
        with self.assertRaisesMessage(plugin_store.StoreError, "does not match the sha256 you gave"):
            plugin_store.install_from_url(url, sha256="0" * 64)
        plugin, update, _ = plugin_store.install_from_url(url, sha256=sha(data).upper(), user=self.admin)
        self.assertEqual((plugin.source, update), ("url", None))
        _Store.files["/my2.zip"] = zip_bytes(demo_files("1.2.0"))
        plugin, update, _ = plugin_store.install_from_url(self.base + "/my2.zip", user=self.admin)
        self.assertEqual((plugin.version, update.source), ("1.2.0", PluginUpdate.Source.URL))

    def test_install_from_url_caps_and_guards(self):
        _Store.files["/big.zip"] = b"x" * 2048
        with mock.patch.object(plugin_store, "MAX_ZIP_BYTES", 1024):
            with self.assertRaisesMessage(plugin_store.StoreError, "larger than"):
                plugin_store.install_from_url(self.base + "/big.zip")
        with self.assertRaisesMessage(plugin_store.StoreError, "http:// or https://"):
            plugin_store.install_from_url("file:///etc/passwd")
        with override_settings(PYRUNNER_ALLOW_PRIVATE_NETWORK=False):
            with self.assertRaises(plugin_store.StoreError):
                plugin_store.install_from_url(self.base + "/big.zip")
        _Store.files["/notzip.zip"] = b"not a zip"
        with self.assertRaisesMessage(PluginInstallError, "not a valid .zip"):
            plugin_store.install_from_url(self.base + "/notzip.zip")
        self.assertFalse(Plugin.objects.exists())


class AutoUpdateTests(_StoreBase):
    def test_auto_update_is_off_behind_the_hidden_flag(self):
        self.assertFalse(plugin_store.settings.PYRUNNER_PLUGIN_AUTO_UPDATE)
        self.assertEqual(plugin_store.auto_update(), {"enabled": False})

    @override_settings(PYRUNNER_PLUGIN_AUTO_UPDATE=True)
    def test_the_job_shape_updates_auto_plugins_as_automatic(self):
        plugin = self.install("1.0.0")
        self.publish((demo_files("1.1.0"), True, {}))
        plugin_store.refresh()
        self.assertEqual(plugin_store.auto_update(), {"enabled": True, "updated": [], "failed": {}})
        Plugin.objects.filter(pk=plugin.pk).update(update_policy=Plugin.UpdatePolicy.AUTO)
        self.assertEqual(plugin_store.auto_update()["updated"], [SLUG])
        row = PluginUpdate.objects.get(plugin=plugin)
        self.assertEqual((row.initiated_by, row.source, row.user), ("auto", "store", None))

    def test_refresh_task_and_its_schedule(self):
        from django_q.models import Schedule

        from core.services.schedule_service import (
            INFRA_SCHEDULE_NAMES,
            PLUGIN_STORE_SCHEDULE_NAME,
            ScheduleService,
        )
        from core.tasks import refresh_plugin_store_task

        self.assertIn(PLUGIN_STORE_SCHEDULE_NAME, INFRA_SCHEDULE_NAMES)
        Schedule.objects.filter(name=PLUGIN_STORE_SCHEDULE_NAME).delete()
        self.assertTrue(ScheduleService.ensure_plugin_store_schedule())
        self.assertFalse(ScheduleService.ensure_plugin_store_schedule())
        row = Schedule.objects.get(name=PLUGIN_STORE_SCHEDULE_NAME)
        self.assertEqual((row.func, row.minutes), ("core.tasks.refresh_plugin_store_task", 720))

        self.publish((demo_files(), True, {}))
        self.assertEqual(refresh_plugin_store_task(),
                         {"success": True, "plugins": 1, "auto_update": {"enabled": False}})
        _Store.files[INDEX] = b"{}"
        self.assertFalse(refresh_plugin_store_task()["success"])
        self.set_url("")
        with mock.patch.object(plugin_store, "OFFICIAL_INDEX_URL", ""):
            self.assertEqual(refresh_plugin_store_task(), {"success": True, "skipped": "no store index set"})


# --------------------------------------------------------------------------- #
# Console
# --------------------------------------------------------------------------- #


class ConsoleTests(_StoreBase):
    def setUp(self):
        super().setUp()
        for target in (
            "core.services.setup_service.SetupService.is_setup_needed",
            "core.services.setup_service.SetupService.needs_admin_setup",
        ):
            patch = mock.patch(target, return_value=False)
            patch.start()
            self.addCleanup(patch.stop)
        self.client.force_login(self.admin)

    def store(self, **params):
        return self.client.get(reverse("cpanel:plugin_store"), params)

    def test_empty_states(self):
        self.set_url("")
        with mock.patch.object(plugin_store, "OFFICIAL_INDEX_URL", ""):
            self.assertContains(self.store(), "No store index set")
        self.set_url(self.base + INDEX)
        self.assertContains(self.store(), "Load the store")
        self.assertEqual(_Store.hits, [])  # the page never fetches

    def test_browse_search_category_and_detail(self):
        self.publish((demo_files(), True, {"categories": ["Data"], "summary": "Moves rows",
                                           "provisions": {"scripts": 2, "secret_keys": ["API_KEY"]}}))
        self.client.post(reverse("cpanel:plugin_store_refresh"))
        page = self.store()
        self.assertContains(page, "Verified · PyRun")
        self.assertContains(page, "Creates</span> 2 scripts")
        self.assertContains(self.store(q="rows"), "Moves rows")
        self.assertContains(self.store(q="nothing-like-it"), "No plugin matches")
        self.assertContains(self.store(category="Other"), "No plugin matches")
        self.assertContains(self.store(category="Data"), "Moves rows")
        detail = self.client.get(reverse("cpanel:plugin_store_detail", kwargs={"slug": SLUG}))
        self.assertContains(detail, "API_KEY")
        self.assertContains(detail, ">Install</button>", html=False)
        missing = self.client.get(reverse("cpanel:plugin_store_detail", kwargs={"slug": "t44missing"}))
        self.assertRedirects(missing, reverse("cpanel:plugin_store"))

    def test_install_and_update_buttons(self):
        self.publish((demo_files(), True, {}))
        resp = self.client.post(reverse("cpanel:plugin_store_install", kwargs={"slug": SLUG}))
        self.assertRedirects(resp, reverse("cpanel:plugin_detail", kwargs={"slug": SLUG}), fetch_redirect_response=False)
        self.assertEqual(Plugin.objects.get().source, "store")
        detail = self.client.get(reverse("cpanel:plugin_detail", kwargs={"slug": SLUG}))
        self.assertContains(detail, "Automatic updates are not available yet")
        self.assertContains(detail, "<dd class=\"text-text\">Store</dd>", html=False)

        plugin_store._memo[0] = None
        self.publish((demo_files("1.1.0"), True, {}))
        self.client.post(reverse("cpanel:plugin_store_refresh"))
        self.assertContains(self.store(), "Updates available")
        listing = self.client.get(reverse("cpanel:plugin_list"))
        self.assertContains(listing, 'title="Updates available">1</span>', html=False)
        dashboard = self.client.get(reverse("cpanel:dashboard"))
        self.assertContains(dashboard, "Plugin update available")
        resp = self.client.post(reverse("cpanel:plugin_store_install", kwargs={"slug": SLUG}), follow=True)
        self.assertContains(resp, "updated from 1.0.0 to 1.1.0")
        self.assertNotContains(self.client.get(reverse("cpanel:dashboard")), "Plugin update available")

    def test_an_old_index_says_so(self):
        from datetime import timedelta

        from django.utils import timezone

        self.publish((demo_files(), True, {}))
        plugin_store.refresh()
        self.assertNotContains(self.store(), "PyRun refreshes it every 12 hours")
        PluginStoreIndex.objects.update(checked_at=timezone.now() - timedelta(hours=13))
        self.assertContains(self.store(), "PyRun refreshes it every 12 hours")

    def test_failed_install_shows_why(self):
        self.publish((demo_files(), True, {"sha256": "0" * 64}))
        resp = self.client.post(reverse("cpanel:plugin_store_install", kwargs={"slug": SLUG}), follow=True)
        self.assertContains(resp, "does not match the sha256")

    def test_store_settings(self):
        url = reverse("cpanel:plugin_store_settings")
        resp = self.client.post(url, {"store_url": "ftp://x/index.json"}, follow=True)
        self.assertContains(resp, "Enter an http:// or https:// URL")
        with override_settings(PYRUNNER_ALLOW_PRIVATE_NETWORK=False):
            resp = self.client.post(url, {"store_url": "http://127.0.0.1/index.json"}, follow=True)
        self.assertContains(resp, "Store index URL not saved")
        self.assertEqual(GlobalSettings.get_settings().plugin_store_url, self.base + INDEX)
        self.publish((demo_files(), True, {}))
        resp = self.client.post(url, {"store_url": self.base + INDEX}, follow=True)
        self.assertContains(resp, "Store index URL saved: 1 plugins")
        with mock.patch.object(plugin_store, "OFFICIAL_INDEX_URL", ""):
            resp = self.client.post(url, {"store_url": ""}, follow=True)
        self.assertContains(resp, "Store index URL cleared")
        self.assertEqual(GlobalSettings.get_settings().plugin_store_url, "")

    def test_install_from_url_form(self):
        data = zip_bytes(demo_files())
        _Store.files["/my.zip"] = data
        url = reverse("cpanel:plugin_install_url")
        resp = self.client.post(url, {"url": self.base + "/my.zip", "sha256": "abc"}, follow=True)
        self.assertContains(resp, "64 hexadecimal characters")
        resp = self.client.post(url, {"url": self.base + "/my.zip", "sha256": sha(data)})
        self.assertRedirects(resp, reverse("cpanel:plugin_detail", kwargs={"slug": SLUG}), fetch_redirect_response=False)
        self.assertEqual(Plugin.objects.get().source, "url")
        self.assertContains(self.client.get(reverse("cpanel:plugin_upload")), "Install from URL")

    def test_superuser_only(self):
        member = User.objects.create(email="member@example.com")
        self.client.force_login(member)
        for name, kwargs, method in (
            ("cpanel:plugin_store", {}, "get"),
            ("cpanel:plugin_store_detail", {"slug": SLUG}, "get"),
            ("cpanel:plugin_store_refresh", {}, "post"),
            ("cpanel:plugin_store_settings", {}, "post"),
            ("cpanel:plugin_store_install", {"slug": SLUG}, "post"),
            ("cpanel:plugin_install_url", {}, "post"),
        ):
            with self.subTest(name=name):
                resp = getattr(self.client, method)(reverse(name, kwargs=kwargs))
                self.assertEqual(resp.status_code, 403)
        self.assertEqual(_Store.hits, [])


# --------------------------------------------------------------------------- #
# Building an index; backup
# --------------------------------------------------------------------------- #


class BuildIndexCommandTests(_StoreBase):
    def setUp(self):
        super().setUp()
        self.keydir = Path(tempfile.mkdtemp(prefix="t44-key-"))
        self.addCleanup(shutil.rmtree, self.keydir, True)
        self.key = self.keydir / "pyrun.pem"
        self.key.write_bytes(private_key_pem(KEY))
        self.zips = self.keydir / "zips"
        self.zips.mkdir()

    def test_builds_an_index_this_pyrun_reads_and_installs_from(self):
        signed = zip_bytes(signed_files(demo_files(categories=["Data"], provisions={"scripts": 1})))
        (self.zips / f"{SLUG}-1.0.0.zip").write_bytes(signed)
        (self.zips / "t44plain-0.1.0.zip").write_bytes(zip_bytes(demo_files("0.1.0", slug="t44plain"), slug="t44plain"))
        out = self.keydir / "index.json"
        call_command("plugin_store_index", str(self.zips), key=str(self.key), key_id="test-1", out=str(out),
                     stdout=open(self.keydir / "log.txt", "w"))
        raw = out.read_bytes()
        index = parse_index(raw, base_url=self.base + INDEX)
        entry = index.get(SLUG)
        self.assertEqual((entry.sha256, entry.size, entry.signed, entry.categories),
                         (sha(signed), len(signed), True, ("Data",)))
        self.assertEqual(entry.provisions, {"scripts": 1})
        self.assertFalse(index.get("t44plain").signed)
        self.assertEqual(entry.download_url, f"{self.base}/{SLUG}-1.0.0.zip")
        # Served next to the index, it installs from the store.
        _Store.files[INDEX] = raw
        _Store.files[f"/{SLUG}-1.0.0.zip"] = signed
        plugin, _, _ = plugin_store.install_from_store(SLUG)
        self.assertEqual(plugin.trust, "verified")

    def test_refusals(self):
        (self.zips / "a.zip").write_bytes(zip_bytes(demo_files()))
        (self.zips / "b.zip").write_bytes(zip_bytes(demo_files("1.1.0")))
        with self.assertRaisesMessage(CommandError, "list one zip per plugin"):
            call_command("plugin_store_index", str(self.zips), key=str(self.key), key_id="test-1",
                         out=str(self.keydir / "i.json"))
        from django.conf import settings

        with self.assertRaisesMessage(CommandError, "outside this project"):
            call_command("plugin_store_index", str(self.zips), key=str(Path(settings.BASE_DIR) / "k.pem"),
                         key_id="test-1")


class BackupTests(_StoreBase):
    def restore(self, data):
        BackupService.restore_backup(data, restore_runs=False, current_user=self.admin)
        return GlobalSettings.get_settings().plugin_store_url

    def test_store_url_round_trips_and_bad_values_fall_back_to_official(self):
        self.set_url("https://store.example/index.json")
        data = BackupService.create_backup(include_runs=False)
        self.assertEqual(data["global_settings"]["plugin_store_url"], "https://store.example/index.json")
        self.set_url("")
        self.assertEqual(self.restore(data), "https://store.example/index.json")
        for bad in (None, "javascript:alert(1)", 42, "https://x/" + "a" * 600):
            with self.subTest(bad=bad):
                copy = json.loads(json.dumps(data))
                if bad is None:
                    del copy["global_settings"]["plugin_store_url"]  # an older backup
                else:
                    copy["global_settings"]["plugin_store_url"] = bad
                self.set_url("https://before.example/index.json")
                self.assertEqual(self.restore(copy), "")
