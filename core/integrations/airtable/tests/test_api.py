"""The airtable integration's tests: the conformance suite and exchanges replayed offline (core/integrations/testing.py).

The cassettes are hand-built from Airtable's public Web API reference
(airtable.com/developers/web/api): no live account was used. The fake-service
tests pin what the cassettes can't show in one place (batching, the formula
``find_records`` builds, path quoting).
"""

import urllib.parse

from core.integrations import testing

BASE = "appExampleBase01"


def _ids(request):
    """The record ids of a DELETE (records[] repeated in the query)."""
    return urllib.parse.parse_qs(urllib.parse.urlsplit(request.url).query).get("records[]", [])


class AirtableConformance(testing.ConformanceSuite):
    integration = "airtable"
    pages = [
        {"json": {"records": [{"id": "rec1"}, {"id": "rec2"}], "offset": "itr2"}},
        {"json": {"records": [{"id": "rec3"}]}},
    ]
    page_items = [{"id": "rec1"}, {"id": "rec2"}, {"id": "rec3"}]

    def call(self, module):
        return module.get_record(BASE, "Contacts", "rec1")

    def paginate_call(self, module, limit=None):
        return module.list_records(BASE, "Contacts", limit=limit)


class _Airtable(testing.IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.airtable = self.integration("airtable")
        self.conn = self.use_connection("airtable", provider="airtable")

    def _error(self, fn, *args, **kwargs):
        with self.assertRaises(self.integration_client.IntegrationError) as ctx:
            fn(*args, **kwargs)
        self.assertNoSecrets(str(ctx.exception))
        return ctx.exception


class AirtableCassetteTests(_Airtable):
    def test_list_records_follows_the_offset_with_view_fields_and_sort(self):
        with self.cassette("list_records"):
            records = list(self.airtable.list_records(BASE, "Contacts", view="Grid view", fields=["Name", "Email"],
                                                      sort=["-Created"]))
        self.assertEqual([r["id"] for r in records], ["recAda0000000001", "recGrace00000002", "recAlan000000003"])
        self.assertEqual(records[2]["fields"]["Score"], 7)

    def test_find_records_builds_the_formula_and_returns_a_list(self):
        with self.cassette("find_records"):
            found = self.airtable.find_records(BASE, "Contacts", {"Email": "ada@example.com", "Active": True},
                                               limit=1)
        self.assertEqual([r["id"] for r in found], ["recAda0000000001"])

    def test_get_record(self):
        with self.cassette("get_record"):
            record = self.airtable.get_record(BASE, "Contacts", "recAda0000000001")
        self.assertEqual(record["fields"]["Email"], "ada@example.com")

    def test_create_records_in_batches_of_ten_then_one(self):
        leads = [{"Name": f"Lead {i}", "Status": "New"} for i in range(1, 13)]
        with self.cassette("create_records"):
            created = self.airtable.create_records(BASE, "Contacts", leads, typecast=True)
            one = self.airtable.create_record(BASE, "Contacts", {"Name": "Ada", "Email": "ada@example.com"})
        self.assertEqual(len(created), 12)
        self.assertEqual([r["fields"]["Name"] for r in created], [lead["Name"] for lead in leads])
        self.assertEqual(one["id"], "recAda0000000001")

    def test_update_merge_replace_and_upsert(self):
        with self.cassette("update_records"):
            merged = self.airtable.update_record(BASE, "Contacts", "recAda0000000001", {"Status": "Done"})
            replaced = self.airtable.update_record(BASE, "Contacts", "recAda0000000001", {"Name": "Ada L."},
                                                   replace=True)
            upserted = self.airtable.update_records(
                BASE, "Contacts",
                [{"fields": {"Email": "ada@example.com", "Score": 9}},
                 {"fields": {"Email": "new@example.com", "Score": 1}}],
                upsert_on=["Email"], typecast=True)
        self.assertEqual((merged["fields"]["Status"], merged["fields"]["Name"]), ("Done", "Ada"))
        self.assertEqual(replaced["fields"], {"Name": "Ada L."})
        self.assertEqual([r["id"] for r in upserted], ["recAda0000000001", "recNew0000000009"])

    def test_delete_one_id_or_a_list(self):
        with self.cassette("delete_records"):
            one = self.airtable.delete_records(BASE, "Contacts", "recAda0000000001")
            two = self.airtable.delete_records(BASE, "Contacts", ["recGrace00000002", "recAlan000000003"])
        self.assertEqual(one, [{"id": "recAda0000000001", "deleted": True}])
        self.assertEqual([r["id"] for r in two], ["recGrace00000002", "recAlan000000003"])

    def test_bases_and_tables(self):
        with self.cassette("schema"):
            bases = list(self.airtable.list_bases())
            tables = self.airtable.list_tables(BASE)
        self.assertEqual([b["name"] for b in bases], ["CRM", "Content"])
        self.assertEqual([f["name"] for f in tables[0]["fields"]], ["Name", "Email", "Active"])

    def test_errors_keep_airtables_message(self):
        with self.cassette("errors"):
            missing = self._error(self.airtable.get_record, BASE, "Contacts", "recMissing000000")
            invalid = self._error(self.airtable.create_record, BASE, "Contacts", {"Score": "nine"})
            forbidden = self._error(self.airtable.list_tables, BASE)
        self.assertEqual((missing.code, missing.status), ("NOT_FOUND", 404))
        self.assertIn("NOT_FOUND", str(missing))
        self.assertEqual((invalid.code, invalid.status), ("INVALID_REQUEST", 422))
        self.assertIn('Field "Score" cannot accept the provided value', str(invalid))
        self.assertEqual((forbidden.code, forbidden.status), ("FORBIDDEN", 403))
        self.assertIn("Invalid permissions", str(forbidden))


class AirtableRequestTests(_Airtable):
    def test_the_token_goes_as_a_bearer_header(self):
        fake = self.fake_http()
        fake.add("GET", f"/v0/{BASE}/Contacts/rec1", json={"id": "rec1", "fields": {}})
        self.airtable.get_record(BASE, "Contacts", "rec1")
        self.assertEqual(fake.requests[0].headers["Authorization"], f"Bearer {self.conn.credentials['api_key']}")
        self.assertTrue(fake.requests[0].url.startswith("https://api.airtable.com/v0/"))

    def test_table_names_and_ids_are_quoted_as_one_path_segment(self):
        fake = self.fake_http()
        fake.default(json={"id": "rec1", "fields": {}})
        self.airtable.get_record(BASE, "Leads / 2026", "rec1")
        self.assertEqual(fake.requests[0].path, f"/v0/{BASE}/Leads%20%2F%202026/rec1")

    def test_a_small_limit_asks_for_a_small_page(self):
        fake = self.fake_http()
        fake.default(json={"records": [{"id": f"rec{i}"} for i in range(3)], "offset": "itr2"})
        self.assertEqual(len(list(self.airtable.list_records(BASE, "Contacts", limit=3))), 3)
        self.assertEqual((len(fake.requests), fake.requests[0].params["pageSize"]), (1, "3"))
        self.assertEqual(list(self.airtable.list_records(BASE, "Contacts", limit=0)), [])
        self.assertEqual(len(fake.requests), 1, "limit=0 asks for nothing")

    def test_sort_rules_as_names_or_airtables_dicts(self):
        fake = self.fake_http()
        fake.default(json={"records": []})
        list(self.airtable.list_records(BASE, "Contacts", sort=["Name", {"field": "Score", "direction": "desc"}],
                                        formula="{Score}>5"))
        params = fake.requests[0].params
        self.assertEqual((params["sort[0][field]"], params["sort[0][direction]"]), ("Name", "asc"))
        self.assertEqual((params["sort[1][field]"], params["sort[1][direction]"]), ("Score", "desc"))
        self.assertEqual(params["filterByFormula"], "{Score}>5")

    def test_find_records_formulas(self):
        fake = self.fake_http()
        fake.default(json={"records": []})
        cases = [
            ({"Name": 'Ada "the first" \\ L.'}, None, '{Name}="Ada \\"the first\\" \\\\ L."'),
            ({"Score": 9, "Ratio": 0.5}, None, "AND({Score}=9,{Ratio}=0.5)"),
            ({"Done": False, "Notes": None}, None, "AND({Done}=FALSE(),{Notes}=BLANK())"),
            ({"Name": "Ada"}, "{Score}>5", 'AND({Name}="Ada",{Score}>5)'),
            (None, "{Score}>5", "{Score}>5"),
            ({"Città": "Roma"}, None, '{Città}="Roma"'),
        ]
        for match, formula, expected in cases:
            with self.subTest(match=match, formula=formula):
                self.assertEqual(self.airtable.find_records(BASE, "Contacts", match, formula=formula), [])
                self.assertEqual(fake.requests[-1].params["filterByFormula"], expected)

    def test_find_records_refuses_what_it_cant_express(self):
        for match, formula in (({}, None), (None, None), ({"Odd}name": 1}, None), ({"Tags": ["a"]}, None),
                               ({"Score": float("nan")}, None)):
            with self.subTest(match=match), self.assertRaises(ValueError):
                self.airtable.find_records(BASE, "Contacts", match, formula=formula)

    def test_writes_go_in_batches_of_ten(self):
        fake = self.fake_http()
        fake.default(json=lambda request: {"records": [
            {"id": f"rec{i}", "fields": r["fields"]} for i, r in enumerate(request.json()["records"])]})
        created = self.airtable.create_records(BASE, "Contacts", [{"N": i} for i in range(25)])
        self.assertEqual([len(r.json()["records"]) for r in fake.requests], [10, 10, 5])
        self.assertEqual([r["fields"]["N"] for r in created], list(range(25)))
        self.assertNotIn("typecast", fake.requests[0].json())
        self.airtable.update_records(BASE, "Contacts", [{"id": f"rec{i}", "fields": {"N": i}} for i in range(11)],
                                     replace=True)
        self.assertEqual([(r.method, len(r.json()["records"])) for r in fake.requests[3:]], [("PUT", 10), ("PUT", 1)])
        fake.default(json=lambda request: {"records": [{"id": i, "deleted": True}
                                                       for i in _ids(request)]})
        self.airtable.delete_records(BASE, "Contacts", [f"rec{i}" for i in range(12)])
        deletes = [r for r in fake.requests if r.method == "DELETE"]
        self.assertEqual(len(deletes), 2)
        self.assertIn("records%5B%5D=rec0", deletes[0].url)
        self.assertEqual(self.airtable.create_records(BASE, "Contacts", []), [])

    def test_a_failed_batch_keeps_the_ones_before_and_a_post_is_not_repeated(self):
        fake = self.fake_http()
        fake.add("POST", f"/v0/{BASE}/Contacts", json={"records": [{"id": f"rec{i}"} for i in range(10)]})
        fake.add("POST", f"/v0/{BASE}/Contacts", status=502, json={"error": {"message": "upstream"}})
        error = self._error(self.airtable.create_records, BASE, "Contacts", [{"N": i} for i in range(15)])
        self.assertEqual((error.code, len(fake.requests)), ("SERVER_ERROR", 2))
        self.assertEqual(self.sleeps, [], "a POST that may have been saved isn't sent again")

    def test_a_429_is_waited_out(self):
        fake = self.fake_http()
        fake.add("GET", f"/v0/{BASE}/Contacts/rec1", status=429,
                 json={"errors": [{"error": "RATE_LIMIT_REACHED", "message": "Rate limit exceeded."}]})
        fake.add("GET", f"/v0/{BASE}/Contacts/rec1", json={"id": "rec1", "fields": {}})
        self.assertEqual(self.airtable.get_record(BASE, "Contacts", "rec1")["id"], "rec1")
        self.assertEqual(self.sleeps, [30.0], "Airtable refuses for 30 s after a 429")

    def test_a_named_connection(self):
        self.use_connection("crm", provider="airtable", credentials={"api_key": "patT27crm.0123456789"})
        fake = self.fake_http()
        fake.default(json={"tables": []})
        self.assertEqual(self.airtable.list_tables(BASE, connection="crm"), [])
        self.assertEqual(fake.requests[0].headers["Authorization"], "Bearer patT27crm.0123456789")

    def test_one_name_or_a_list_and_a_dict_is_not_a_list_of_records(self):
        fake = self.fake_http()
        fake.default(json={"records": []})
        list(self.airtable.list_records(BASE, "Contacts", fields="Name", sort="-Created"))
        params = fake.requests[0].params
        self.assertEqual((params["fields[]"], params["sort[0][field]"], params["sort[0][direction]"]),
                         ("Name", "Created", "desc"))
        self.assertNotIn("sort[1][field]", params)
        self.airtable.update_records(BASE, "Contacts", [{"fields": {"Email": "a@example.com"}}], upsert_on="Email")
        self.assertEqual(fake.requests[1].json()["performUpsert"], {"fieldsToMergeOn": ["Email"]})
        for fn in (self.airtable.create_records, self.airtable.update_records):
            with self.subTest(fn.__name__), self.assertRaises(TypeError):
                fn(BASE, "Contacts", {"Name": "Ada"})
        self.assertEqual(len(fake.requests), 2)
