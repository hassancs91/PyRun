"""
DataStore explorer (2.5 T40): the store's entries in the shared grid (key
search, filters, sort, pages, type badges, in-place JSON edits for small
values, previews for big ones), the entry page's capped view and download,
and JSON export/import as a previewed upsert (core/services/datastore_io.py).
"""

import datetime
import io
import json
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from core.grid import json_cell_value
from core.models import DataStore, DataStoreEntry, Environment, GlobalSettings, User, Workspace, WorkspaceMembership
from core.services import datastore_io
from core.services.datastore_io import DataStoreImportError
from core.views import datastores as views


def _ws():
    return Workspace.get_default() or Workspace.objects.create(name="Default", is_default=True)


def _put(store, key, value=None, *, raw=None):
    entry = DataStoreEntry(datastore=store, key=key)
    entry.value_json = raw if raw is not None else json.dumps(value)
    entry.save()
    return entry


def _stored(store, key):
    return DataStoreEntry.objects.get(datastore=store, key=key).value_json


class JsonCellValueTests(SimpleTestCase):
    def test_text_wins_and_keeps_exact_numbers(self):
        self.assertEqual(json_cell_value({"value": 1, "text": '{"n": 12345678901234567890, "f": 1.0}'}),
                         {"n": 12345678901234567890, "f": 1.0})
        self.assertIsInstance(json_cell_value({"text": "1.0"}), float)
        self.assertIsNone(json_cell_value({"value": 5, "text": "  "}))
        self.assertEqual(json_cell_value({"value": [1]}), [1])
        with self.assertRaises(ValueError):
            json_cell_value({"text": "{nope"})


class ImportExportTests(TestCase):
    def setUp(self):
        self.store = DataStore.objects.create(name="kv", workspace=_ws())

    def test_parse_refuses_what_it_cannot_import(self):
        for data, needle in [
            (b"{nope", "isn't valid JSON"),
            (b"[1, 2]", "one JSON object"),
            (b"\xff\xfe\x00", "isn't UTF-8"),
            (b'{"": 1}', "A key is empty"),
            (json.dumps({"k" * 256: 1}).encode(), "longer than 255"),
            (b'{"a": 1, "b": {"a": 2, "a": 3}, "a": 4}', 'The key "a" appears more than once'),
        ]:
            with self.subTest(data=data[:20]), self.assertRaises(DataStoreImportError) as ctx:
                datastore_io.parse(data)
            self.assertIn(needle, " ".join([ctx.exception.message, *ctx.exception.errors]))
        # Duplicates inside a nested object are ordinary JSON (the last wins), not an error.
        self.assertEqual(datastore_io.parse(b'\xef\xbb\xbf{"b": {"a": 2, "a": 3}}'), {"b": {"a": 3}})
        with mock.patch.object(datastore_io, "MAX_IMPORT_BYTES", 10), self.assertRaises(DataStoreImportError):
            datastore_io.parse(b'{"a": "0123456789"}')

    def test_preview_counts_and_writes_nothing(self):
        _put(self.store, "same", {"x": 1, "y": [1, 2]})
        _put(self.store, "num", 1)
        _put(self.store, "flag", 1)
        _put(self.store, "keep", "untouched")
        items = {"same": {"y": [1, 2], "x": 1}, "num": 1.0, "flag": True, "fresh": None}
        before = list(DataStoreEntry.objects.order_by("key").values_list("key", "value_json", "updated_at"))
        report = datastore_io.plan_import(self.store, items)
        self.assertEqual((report["total"], report["new"], report["updated"], report["unchanged"]), (4, 1, 2, 1))
        self.assertEqual(report["new_keys"], ["fresh"])
        self.assertEqual(report["updated_keys"], ["flag", "num"])
        self.assertEqual(before, list(DataStoreEntry.objects.order_by("key").values_list("key", "value_json",
                                                                                          "updated_at")))

    def test_apply_upserts_and_leaves_unnamed_and_unchanged_entries(self):
        same = _put(self.store, "same", {"x": 1})
        old = _put(self.store, "old", "v1")
        _put(self.store, "keep", "untouched")
        past = timezone.now() - datetime.timedelta(days=3)
        DataStoreEntry.objects.filter(pk__in=[same.pk, old.pk]).update(updated_at=past, created_at=past)
        report = datastore_io.apply_import(self.store, {"same": {"x": 1}, "old": "v2", "big": 12345678901234567890})
        self.assertEqual((report["new"], report["updated"], report["unchanged"]), (1, 1, 1))
        self.assertEqual(_stored(self.store, "old"), '"v2"')
        self.assertEqual(_stored(self.store, "big"), "12345678901234567890")
        self.assertEqual(_stored(self.store, "keep"), '"untouched"')
        same.refresh_from_db()
        old.refresh_from_db()
        self.assertEqual(same.updated_at, past)  # unchanged: not rewritten
        self.assertGreater(old.updated_at, past)
        self.assertEqual(old.created_at, past)  # an update keeps the entry's creation time
        self.assertEqual(self.store.entries.count(), 4)

    def test_export_streams_one_object_that_imports_back_unchanged(self):
        self.assertEqual(json.loads("".join(datastore_io.stream_json(self.store))), {})
        values = {"b": {"nested": [1, 2.5, None]}, "a": "text with \"quotes\" and ünïcode", "c": False}
        for key, value in values.items():
            _put(self.store, key, value)
        _put(self.store, "pretty", raw='{\n  "n": 12345678901234567890\n}')
        _put(self.store, "raw", raw="not json")  # written around the helpers: exported as a string
        text = "".join(datastore_io.stream_json(self.store))
        self.assertEqual(json.loads(text), {**values, "pretty": {"n": 12345678901234567890}, "raw": "not json"})
        self.store.entries.filter(key="raw").delete()
        text = "".join(datastore_io.stream_json(self.store))
        self.assertLess(text.index('"a"'), text.index('"b"'))  # ordered by key
        report = datastore_io.plan_import(self.store, datastore_io.parse(text.encode()))
        self.assertEqual((report["new"], report["updated"], report["unchanged"]), (0, 0, 4))


class _Console(TestCase):
    def setUp(self):
        gs = GlobalSettings.get_settings()
        gs.setup_completed = True
        gs.save()
        Environment.objects.create(name="denv", path="/tmp/denv", is_default=True)
        User.objects.create_superuser(username="root@example.com", email="root@example.com", password="x-Pass-12345!")
        self.ws = _ws()
        self.user = User.objects.create_user(username="d@example.com", email="d@example.com", password="x-Pass-12345!")
        WorkspaceMembership.ensure(self.user, self.ws, role=WorkspaceMembership.ROLE_MEMBER)
        self.client.force_login(self.user)
        self.store = DataStore.objects.create(name="plugin:cache", workspace=self.ws)

    def url(self, name, *args):
        return reverse(f"cpanel:{name}", args=[self.store.pk, *args])

    def detail(self, query=""):
        resp = self.client.get(self.url("datastore_detail") + query)
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def keys_shown(self, html):
        return [k for k in ("alpha", "beta", "gamma", "delta") if f">{k}</a>" in html]


class ExplorerGridTests(_Console):
    def setUp(self):
        super().setUp()
        now = timezone.now()
        for i, (key, value) in enumerate([("alpha", {"a": 1}), ("beta", "two"), ("gamma", [3]), ("delta", 4)]):
            entry = _put(self.store, key, value)
            DataStoreEntry.objects.filter(pk=entry.pk).update(updated_at=now - datetime.timedelta(hours=4 - i))

    def test_grid_shows_type_badges_links_and_actions(self):
        html = self.detail()
        self.assertIn("data-grid", html)
        for kind in ("object", "string", "array", "number"):
            self.assertIn(f">{kind}</span>", html)
        entry = self.store.entries.get(key="alpha")
        self.assertIn(f'href="{self.url("datastore_entry_edit", entry.pk)}"', html)
        self.assertIn(self.url("datastore_export"), html)
        self.assertIn(self.url("datastore_import"), html)
        self.assertEqual(html.count('data-cell-url="'), 4)
        self.assertEqual(html.count("data-edit "), 4)  # every value is small: edited in place
        self.assertEqual(self.keys_shown(html), ["alpha", "beta", "gamma", "delta"])  # by key: a, b, d, g
        self.assertLess(html.index(">alpha</a>"), html.index(">beta</a>"))
        self.assertLess(html.index(">delta</a>"), html.index(">gamma</a>"))

    def test_key_search_is_kept_by_sort_and_filter_links(self):
        html = self.detail("?q=ET")
        self.assertEqual(self.keys_shown(html), ["beta"])
        self.assertIn('href="?q=ET&amp;sort=-key"', html)  # the sort link keeps the search
        self.assertIn('name="q" value="ET"', html)
        html = self.detail("?q=zzz")
        self.assertIn("No key matches this search.", html)

    def test_filters_on_key_size_and_updated(self):
        self.assertEqual(self.keys_shown(self.detail("?f=key:prefix:G")), ["gamma"])
        self.assertEqual(self.keys_shown(self.detail("?f=key:ne:alpha&f=key:contains:a")), ["beta", "gamma", "delta"])
        self.assertEqual(self.keys_shown(self.detail("?f=key:in:alpha,%20delta")), ["alpha", "delta"])
        self.assertEqual(self.keys_shown(self.detail("?f=size:gt:3")), ["alpha", "beta"])  # {"a": 1}, "two"
        self.assertEqual(self.keys_shown(self.detail("?f=size:eq:1")), ["delta"])
        cutoff = (timezone.now() - datetime.timedelta(hours=2, minutes=30)).strftime("%Y-%m-%dT%H:%M:%S")
        self.assertEqual(self.keys_shown(self.detail(f"?f=updated_at:gt:{cutoff}Z")), ["gamma", "delta"])
        for bad in ("lots", "99999999999999999999", "1,,2"):
            html = self.detail(f"?f=size:gt:{bad}" if bad != "1,,2" else f"?f=size:in:{bad}")
            self.assertIn("compares with a whole number", html)
            self.assertEqual(len(self.keys_shown(html)), 4)  # the bad filter is dropped, not applied

    def test_sort_by_updated_and_size(self):
        html = self.detail("?sort=-updated_at")
        order = sorted(self.keys_shown(html), key=lambda k: html.index(f">{k}</a>"))
        self.assertEqual(order, ["delta", "gamma", "beta", "alpha"])
        html = self.detail("?sort=size")
        order = sorted(self.keys_shown(html), key=lambda k: html.index(f">{k}</a>"))
        self.assertEqual(order[0], "delta")
        self.assertIn("Can&#x27;t sort by &quot;value&quot;", self.detail("?sort=value"))

    def test_pagination(self):
        for i in range(60):
            _put(self.store, f"k{i:03d}", i)
        page1 = self.detail()
        self.assertEqual(page1.count('data-cell-url="'), 50)
        page2 = self.detail("?page=2")
        self.assertEqual(page2.count('data-cell-url="'), 14)
        self.assertIn(">k059</a>", page2)
        self.assertEqual(self.detail("?page=99").count('data-cell-url="'), 14)  # past the end: last page

    def test_big_values_show_a_preview_and_open_on_the_entry_page(self):
        big = {"blob": "x" * (views.GRID_EDIT_CHARS + 10)}
        entry = _put(self.store, "huge", big)
        _put(self.store, "broken", raw="not json at all")
        html = self.detail()
        self.assertNotIn("x" * 300, html)  # the value itself isn't in the page
        self.assertIn('{&quot;blob&quot;: &quot;' + "x" * 50, html)
        self.assertIn("…", html)
        self.assertEqual(html.count("data-edit "), 4)  # huge and broken aren't edited in place
        self.assertIn(f'href="{self.url("datastore_entry_edit", entry.pk)}"', html)
        self.assertIn(">text</span>", html)  # the broken one
        self.assertIn("not json at all", html)

    def test_cell_edit_keeps_exact_numbers_and_refuses_bad_input(self):
        entry = self.store.entries.get(key="alpha")
        url = self.url("datastore_entry_cell", entry.pk)

        def post(body):
            return self.client.post(url, json.dumps(body), content_type="application/json")

        resp = post({"column": "value", "value": {"n": 1.2345678901234567e19}, "text": '{"n": 12345678901234567890}'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(json.loads(_stored(self.store, "alpha")), {"n": 12345678901234567890})
        self.assertIn("12345678901234567890", resp.json()["raw"])
        self.assertIn("12345678901234567890", resp.json()["html"])
        resp = post({"column": "value", "value": "plain"})
        self.assertEqual(_stored(self.store, "alpha"), '"plain"')
        self.assertEqual(post({"column": "value", "value": 1, "text": "{bad"}).status_code, 400)
        self.assertEqual(post({"column": "key", "value": "renamed"}).status_code, 400)
        self.assertEqual(self.client.post(url, "not json", content_type="application/json").status_code, 400)
        other = DataStore.objects.create(name="elsewhere", workspace=Workspace.objects.create(name="Other"))
        resp = self.client.post(reverse("cpanel:datastore_entry_cell", args=[other.pk, entry.pk]),
                                json.dumps({"column": "value", "value": 1}), content_type="application/json")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(_stored(self.store, "alpha"), '"plain"')

    def test_delete_returns_to_the_same_view(self):
        entry = self.store.entries.get(key="beta")
        resp = self.client.post(self.url("datastore_entry_delete", entry.pk), {"next": "?sort=-updated_at&page=1"})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].endswith("?sort=-updated_at&page=1"))
        self.assertFalse(self.store.entries.filter(key="beta").exists())
        entry = self.store.entries.get(key="gamma")
        resp = self.client.post(self.url("datastore_entry_delete", entry.pk), {"next": "https://evil.example/"})
        self.assertEqual(resp["Location"], self.url("datastore_detail"))

    def test_empty_store_shows_the_empty_state(self):
        self.store.entries.all().delete()
        html = self.detail()
        self.assertIn("No entries yet", html)
        self.assertNotIn("data-grid ", html)


class EntryPageTests(_Console):
    def test_small_value_is_pretty_printed_in_the_json_editor(self):
        entry = _put(self.store, "conf", {"a": 1, "b": [1, 2]})
        html = self.client.get(self.url("datastore_entry_edit", entry.pk)).content.decode()
        self.assertIn("data-json-editor", html)
        self.assertIn("data-json-format", html)
        self.assertIn("{\n  &quot;a&quot;: 1,", html)
        self.assertIn(self.url("datastore_entry_download", entry.pk), html)
        # Saved as typed (the existing form's behaviour); invalid JSON is refused.
        resp = self.client.post(self.url("datastore_entry_edit", entry.pk), {"key": "conf", "value": '{"a": 2}'})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(_stored(self.store, "conf"), '{"a": 2}')
        resp = self.client.post(self.url("datastore_entry_edit", entry.pk), {"key": "conf", "value": "{a: 2}"})
        self.assertContains(resp, "Invalid JSON")

    def test_value_over_the_cap_shows_a_preview_and_a_download(self):
        value = {"blob": "y" * (views.VIEW_CAP_CHARS + 1)}
        entry = _put(self.store, "huge/one", value)
        html = self.client.get(self.url("datastore_entry_edit", entry.pk)).content.decode()
        self.assertNotIn("data-json-editor", html)
        self.assertIn("y" * 100, html)
        self.assertNotIn("y" * (views.PREVIEW_CHARS + 1), html)
        self.assertIn("download the value", html)
        resp = self.client.get(self.url("datastore_entry_download", entry.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(json.loads(resp.content), value)
        self.assertEqual(resp["Content-Disposition"], 'attachment; filename="huge_one.json"')

    def test_create_page_has_the_json_editor(self):
        html = self.client.get(self.url("datastore_entry_create")).content.decode()
        self.assertIn("data-json-editor", html)
        self.assertIn("js/json_editor.", html)


class ImportExportViewTests(_Console):
    def upload(self, data, **extra):
        body = {"file": SimpleUploadedFile("in.json", data, content_type="application/json"), **extra}
        return self.client.post(self.url("datastore_import"), body)

    def test_export_view(self):
        _put(self.store, "a", {"x": 1})
        resp = self.client.get(self.url("datastore_export"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(json.loads(b"".join(resp.streaming_content)), {"a": {"x": 1}})
        self.assertEqual(resp["Content-Disposition"], 'attachment; filename="plugin_cache.json"')

    def test_preview_then_confirm(self):
        _put(self.store, "a", 1)
        data = json.dumps({"a": 2, "b": 3}).encode()
        resp = self.upload(data)
        self.assertContains(resp, "data-import-preview")
        self.assertContains(resp, "Nothing has been written yet")
        self.assertEqual(_stored(self.store, "a"), "1")
        resp = self.client.post(self.url("datastore_import"),
                                {"file": SimpleUploadedFile("in.json", data)}, HTTP_ACCEPT="application/json")
        self.assertEqual(resp.json()["changes"], 2)
        self.assertIn("data-import-preview", resp.json()["html"])
        self.assertFalse(self.store.entries.filter(key="b").exists())
        resp = self.upload(data, confirm="1")
        self.assertRedirects(resp, self.url("datastore_detail"), fetch_redirect_response=False)
        self.assertEqual((_stored(self.store, "a"), _stored(self.store, "b")), ("2", "3"))
        msgs = [str(m) for m in resp.wsgi_request._messages]
        self.assertEqual(msgs, ["Import done: 1 new, 1 updated, 0 unchanged (of 2 in the file)."])

    def test_errors_write_nothing(self):
        resp = self.upload(b'{"ok": 1, "": 2}', confirm="1")
        self.assertEqual(resp.status_code, 400)
        self.assertContains(resp, "A key is empty", status_code=400)
        self.assertFalse(self.store.entries.exists())
        resp = self.client.post(self.url("datastore_import"), {"file": SimpleUploadedFile("x.json", b"[]")},
                                HTTP_ACCEPT="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("one JSON object", resp.json()["error"])
        self.assertEqual(self.client.post(self.url("datastore_import"), {}).status_code, 400)

    def test_other_workspace_store_is_404(self):
        other = DataStore.objects.create(name="theirs", workspace=Workspace.objects.create(name="Other"))
        _put(other, "secret", "v")
        for name in ("datastore_export", "datastore_import", "datastore_detail"):
            self.assertEqual(self.client.get(reverse(f"cpanel:{name}", args=[other.pk])).status_code, 404)
        entry = other.entries.get()
        self.assertEqual(self.client.get(reverse("cpanel:datastore_entry_download",
                                                 args=[other.pk, entry.pk])).status_code, 404)
        resp = self.client.post(reverse("cpanel:datastore_import", args=[other.pk]),
                                {"file": io.BytesIO(b'{"secret": "x"}'), "confirm": "1"})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(_stored(other, "secret"), '"v"')


class TableJsonCellTextTests(_Console):
    """The grid's JSON dialog sends the JSON as typed; Tables' JSON cells read it too (no rounding)."""

    def test_table_json_cell_keeps_long_integers(self):
        from core.tables.service import TableService

        table = TableService.create_table(workspace=self.ws, name="Ids", columns=[{"key": "meta", "type": "json"}])
        row = TableService.insert(table, [{"meta": {"id": 1}}])[0]
        url = reverse("cpanel:table_row_cell", args=[table.pk, row.id])
        resp = self.client.post(url, json.dumps({"column": "meta", "value": {"id": 1.2345678901234567e19},
                                                 "text": '{"id": 12345678901234567890}'}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        row.refresh_from_db()
        self.assertEqual(row.data["meta"], {"id": 12345678901234567890})
        resp = self.client.post(url, json.dumps({"column": "meta", "value": None, "text": "{oops"}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Not valid JSON", resp.json()["error"])
