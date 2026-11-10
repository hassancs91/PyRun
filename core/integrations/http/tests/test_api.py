"""The http integration's tests: the conformance suite and recorded exchanges (core/integrations/testing.py).

The cassettes were recorded (PYRUNNER_RECORD_CASSETTES=1) against a stand-in
API on 127.0.0.1, since a generic HTTP module has no one real service.
"""

from core.integrations import testing


class HttpConformance(testing.ConformanceSuite):
    integration = "http"
    pages = [
        {"json": {"data": [{"id": 1}, {"id": 2}], "next_cursor": "c2"}},
        {"json": {"data": [{"id": 3}], "next_cursor": None}},
    ]
    page_items = [{"id": 1}, {"id": 2}, {"id": 3}]

    def call(self, module):
        return module.get("/conformance")

    def paginate_call(self, module, limit=None):
        return module.paginate("/items", items="data", cursor="next_cursor", cursor_param="cursor", limit=limit)


class HttpCassetteTests(testing.IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.http = self.integration("http")
        self.api = self.use_connection("api", provider="http", auth_kind="bearer",
                                       config={"base_url": "https://api.example.test"})

    def test_get_returns_the_parsed_json(self):
        with self.cassette("get_me"):
            me = self.http.get("/v1/me")
        self.assertEqual(me, {"id": 7, "name": "Ada"})

    def test_post_put_patch_delete(self):
        with self.cassette("crud"):
            created = self.http.post("/v1/items", json={"name": "Ada"})
            replaced = self.http.put(f"/v1/items/{created['id']}", json={"name": "Ada L."})
            patched = self.http.patch(f"/v1/items/{created['id']}", json={"tags": ["math"]})
            gone = self.http.delete(f"/v1/items/{created['id']}")
        self.assertEqual(created, {"id": 11, "name": "Ada"})
        self.assertEqual(replaced["name"], "Ada L.")
        self.assertEqual(patched["tags"], ["math"])
        self.assertIsNone(gone)

    def test_a_text_reply_is_returned_as_text(self):
        with self.cassette("text"):
            self.assertEqual(self.http.get("/v1/ping"), "pong")

    def test_paginate_follows_a_cursor(self):
        with self.cassette("paginate_cursor"):
            ids = [item["id"] for item in self.http.paginate(
                "/v1/items", items="data", cursor="next_cursor", cursor_param="cursor")]
        self.assertEqual(ids, [1, 2, 3])

    def test_paginate_follows_link_headers(self):
        with self.cassette("paginate_link"):
            ids = [item["id"] for item in self.http.paginate("/v1/repos", items=None, link=True)]
        self.assertEqual(ids, [1, 2, 3])

    def test_paginate_by_page_number(self):
        with self.cassette("paginate_pages"):
            ids = [item["id"] for item in self.http.paginate("/v1/users", items="users", page_param="page")]
        self.assertEqual(ids, [1, 2, 3])

    def test_a_404_is_not_found_with_the_services_message(self):
        with self.cassette("not_found"):
            with self.assertRaises(self.integration_client.IntegrationError) as ctx:
                self.http.get("/v1/items/404")
        self.assertEqual((ctx.exception.code, ctx.exception.status), ("NOT_FOUND", 404))
        self.assertIn("No item 404", str(ctx.exception))
