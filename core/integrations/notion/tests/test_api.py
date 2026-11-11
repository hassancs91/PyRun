"""The notion integration's tests: the conformance suite and exchanges replayed offline (core/integrations/testing.py).

The cassettes are hand-built from Notion's public API reference
(developers.notion.com/reference, version 2022-06-28): no live workspace was
used. The fake-service tests pin what the cassettes can't show in one place
(more than 100 blocks, long text, the Notion-Version header).
"""

from core.integrations import testing

DB = "8a3c1e2f4b5d4c6e9f0a1b2c3d4e5f60"
PAGE = "1f0e0d0c-0b0a-4908-8706-050403020101"


class NotionConformance(testing.ConformanceSuite):
    integration = "notion"
    pages = [
        {"json": {"object": "list", "results": [{"id": "p1"}, {"id": "p2"}], "next_cursor": "c2",
                  "has_more": True}},
        {"json": {"object": "list", "results": [{"id": "p3"}], "next_cursor": None, "has_more": False}},
    ]
    page_items = [{"id": "p1"}, {"id": "p2"}, {"id": "p3"}]

    def call(self, module):
        return module.get_page("p1")

    def paginate_call(self, module, limit=None):
        return module.query_database(DB, limit=limit)


class _Notion(testing.IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.notion = self.integration("notion")
        self.conn = self.use_connection("notion", provider="notion")

    def _error(self, fn, *args, **kwargs):
        with self.assertRaises(self.integration_client.IntegrationError) as ctx:
            fn(*args, **kwargs)
        self.assertNoSecrets(str(ctx.exception))
        return ctx.exception


class NotionCassetteTests(_Notion):
    def test_query_database_sends_the_cursor_in_the_body(self):
        with self.cassette("query_database"):
            pages = list(self.notion.query_database(
                DB, filter={"property": "Status", "select": {"equals": "Open"}},
                sorts=[{"property": "Due", "direction": "ascending"}]))
        names = [p["properties"]["Name"]["title"][0]["plain_text"] for p in pages]
        self.assertEqual(names, ["Call Ada", "Send invoice", "Book venue"])

    def test_get_database(self):
        with self.cassette("get_database"):
            database = self.notion.get_database(DB)
        self.assertEqual(sorted(database["properties"]), ["Due", "Name", "Status"])

    def test_get_create_update_and_archive_a_page(self):
        with self.cassette("pages"):
            page = self.notion.get_page(PAGE)
            created = self.notion.create_page(database_id=DB, title="Call Ada",
                                              properties={"Status": {"select": {"name": "Open"}}},
                                              children=["Ask about the invoice."])
            done = self.notion.update_page(PAGE, {"Status": {"select": {"name": "Done"}}})
            archived = self.notion.update_page(PAGE, archived=True)
        self.assertEqual(page["id"], PAGE)
        self.assertEqual(created["id"], PAGE)
        self.assertEqual(done["properties"]["Status"]["select"]["name"], "Done")
        self.assertTrue(archived["archived"])

    def test_append_and_list_blocks(self):
        heading = {"type": "heading_2", "heading_2": {"rich_text": [{"text": {"content": "Summary"}}]}}
        with self.cassette("blocks"):
            added = self.notion.append_blocks(PAGE, [heading, "Everything went fine."])
            blocks = list(self.notion.list_blocks(PAGE))
        self.assertEqual([b["type"] for b in added], ["heading_2", "paragraph"])
        texts = [b["paragraph"]["rich_text"][0]["plain_text"] for b in blocks]
        self.assertEqual(texts, ["Everything went fine.", "Second line.", "Third line."])

    def test_search_for_databases(self):
        with self.cassette("search"):
            found = list(self.notion.search("Roadmap", kind="database", limit=5))
        self.assertEqual([r["object"] for r in found], ["database"])

    def test_errors_keep_notions_message_and_a_429_is_waited_out(self):
        with self.cassette("errors"):
            missing = self._error(self.notion.get_page, "0000000000000000000000000000dead")
            invalid = self._error(self.notion.create_page, database_id=DB,
                                  properties={"Stage": {"select": {"name": "Open"}}})
            database = self.notion.get_database(DB)
        self.assertEqual((missing.code, missing.status), ("NOT_FOUND", 404))
        self.assertIn("Make sure the relevant pages and databases are shared with your integration", str(missing))
        self.assertEqual((invalid.code, invalid.status), ("INVALID_REQUEST", 400))
        self.assertIn("Stage is not a property that exists.", str(invalid))
        self.assertEqual((database["object"], self.sleeps), ("database", [1.0]))


class NotionRequestTests(_Notion):
    def test_the_token_and_the_api_version_go_with_every_request(self):
        fake = self.fake_http()
        fake.default(json={"object": "page", "id": PAGE})
        self.notion.get_page(PAGE)
        sent = fake.requests[0]
        self.assertEqual(sent.headers["Authorization"], f"Bearer {self.conn.credentials['access_token']}")
        self.assertEqual(sent.headers["Notion-Version"], "2022-06-28")
        self.assertTrue(sent.url.startswith("https://api.notion.com/v1/pages/"))

    def test_strings_are_paragraphs_and_long_text_is_split(self):
        fake = self.fake_http()
        fake.default(json=lambda request: {"object": "list", "results": request.json()["children"]})
        self.notion.append_blocks(PAGE, ["x" * 4500, ""])
        long, empty = fake.requests[0].json()["children"]
        self.assertEqual([len(t["text"]["content"]) for t in long["paragraph"]["rich_text"]], [2000, 2000, 500])
        self.assertEqual(empty["paragraph"]["rich_text"], [{"type": "text", "text": {"content": ""}}])

    def test_more_than_a_hundred_blocks(self):
        fake = self.fake_http()
        fake.add("POST", "/v1/pages", json={"object": "page", "id": PAGE})
        fake.default(json=lambda request: {"object": "list", "results": request.json()["children"]})
        self.notion.create_page(page_id="parent-page", title="Log", children=[f"line {i}" for i in range(250)])
        create, *appends = fake.requests
        self.assertEqual(create.json()["parent"], {"page_id": "parent-page"})
        self.assertEqual(create.json()["properties"]["title"]["title"][0]["text"]["content"], "Log")
        self.assertEqual(len(create.json()["children"]), 100)
        self.assertEqual([(r.method, r.path, len(r.json()["children"])) for r in appends],
                         [("PATCH", f"/v1/blocks/{PAGE}/children", 100), ("PATCH", f"/v1/blocks/{PAGE}/children", 50)])
        added = self.notion.append_blocks(PAGE, [f"more {i}" for i in range(101)])
        self.assertEqual(len(added), 101)
        self.assertEqual(len(fake.requests), 5)

    def test_create_page_needs_one_parent_and_search_one_kind(self):
        for kwargs in ({}, {"database_id": DB, "page_id": PAGE}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.notion.create_page(**kwargs)
        with self.assertRaises(ValueError):
            list(self.notion.search("x", kind="block"))

    def test_update_page_sends_only_what_changes(self):
        fake = self.fake_http()
        fake.default(json={"object": "page", "id": PAGE})
        self.notion.update_page(PAGE, title="Renamed")
        self.notion.update_page(PAGE, archived=False)
        self.assertEqual(fake.requests[0].json(),
                         {"properties": {"title": {"title": [{"type": "text", "text": {"content": "Renamed"}}]}}})
        self.assertEqual(fake.requests[1].json(), {"archived": False})

    def test_page_size_follows_a_small_limit_and_search_without_a_query(self):
        fake = self.fake_http()
        fake.default(json={"object": "list", "results": [{"id": "a"}, {"id": "b"}], "next_cursor": "c2",
                           "has_more": True})
        self.assertEqual(len(list(self.notion.search(limit=2))), 2)
        self.assertEqual(fake.requests[0].json(), {"page_size": 2})
        self.assertEqual(len(list(self.notion.list_blocks(PAGE, limit=1))), 1)
        self.assertEqual(fake.requests[1].params, {"page_size": "1"})
        self.assertEqual(len(fake.requests), 2)

    def test_append_is_not_repeated_after_a_server_error(self):
        fake = self.fake_http()
        fake.default(status=502, json={"object": "error", "status": 502, "code": "bad_gateway",
                                       "message": "Bad gateway."})
        error = self._error(self.notion.append_blocks, PAGE, ["once"])
        self.assertEqual((error.code, len(fake.requests), self.sleeps), ("SERVER_ERROR", 1, []))

    def test_one_block_or_one_sort_needs_no_list(self):
        fake = self.fake_http()
        fake.default(json=lambda request: {"object": "list", "results": (request.json() or {}).get("children", []),
                                           "next_cursor": None})
        self.assertEqual(len(self.notion.append_blocks(PAGE, "Hello")), 1)
        self.assertEqual(len(self.notion.append_blocks(PAGE, {"type": "divider", "divider": {}})), 1)
        list(self.notion.query_database(DB, sorts={"timestamp": "created_time", "direction": "descending"}))
        self.assertEqual(fake.requests[2].json()["sorts"], [{"timestamp": "created_time", "direction": "descending"}])
