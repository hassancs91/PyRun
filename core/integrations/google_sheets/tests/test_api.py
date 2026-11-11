"""The google_sheets integration's tests: the conformance suite and exchanges replayed offline (core/integrations/testing.py).

The cassettes are hand-built from Google's public Sheets API reference
(developers.google.com/sheets/api/reference/rest): no live account was used.
The fake-service tests pin what the cassettes can't show in one place (the API
host, path quoting, header placement, retries, argument checks).
"""

from unittest import mock

from core.integrations import framework, testing

SID = "1ExampleSheetId0123456789abcdefghijklmnopqrs"
SHEETS = "https://sheets.googleapis.com"
HEADER = ["Name", "Email", "Score", "Status"]


class GoogleSheetsConformance(testing.ConformanceSuite):
    integration = "google_sheets"
    ok_reply = {"json": {"sheets": []}}

    def call(self, module):
        return module.list_sheets(SID)


class _Sheets(testing.IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.sheets = self.integration("google_sheets")
        self.conn = self.use_connection("google", provider="google")

    def _error(self, fn, *args, **kwargs):
        with self.assertRaises(self.integration_client.IntegrationError) as ctx:
            fn(*args, **kwargs)
        self.assertNoSecrets(str(ctx.exception))
        return ctx.exception


class GoogleSheetsCassetteTests(_Sheets):
    def test_read_range_formatted_unformatted_and_empty(self):
        with self.cassette("read_range"):
            rows = self.sheets.read_range(SID, "Leads!A1:D")
            raw = self.sheets.read_range(SID, "Leads!A1:D", value_render="unformatted_value")
            empty = self.sheets.read_range(SID, "Empty")
        self.assertEqual(rows[0], HEADER)
        self.assertEqual((rows[1][2], rows[3], rows[4]), ("36", [], ["Alan", "alan@example.com", "41"]))
        self.assertEqual(raw[1][2], 36)
        self.assertEqual(empty, [])

    def test_append_a_row_of_values_then_dicts_by_header(self):
        with self.cassette("append_rows"):
            one = self.sheets.append_rows(SID, "Leads", ["Linus", "linus@example.com", 7, "New"])
            two = self.sheets.append_rows(SID, "Leads", [
                {"Name": "Margaret", "Status": "New"},
                {"Email": "ken@example.com", "Name": "Ken", "Score": 3},
            ])
        self.assertEqual((one["updatedRange"], one["updatedRows"]), ("Leads!A6:D6", 1))
        self.assertEqual((two["updatedRange"], two["updatedRows"]), ("Leads!A7:D8", 2))

    def test_update_one_cell_and_a_raw_row(self):
        with self.cassette("update_range"):
            cell = self.sheets.update_range(SID, "Leads!D2", [["Contacted"]])
            row = self.sheets.update_range(SID, "Leads!A5:C5", ["Alan", "alan@example.com", "=41"], value_input="RAW")
        self.assertEqual((cell["updatedRange"], cell["updatedCells"]), ("Leads!D2", 1))
        self.assertEqual(row["updatedCells"], 3)

    def test_find_rows_by_header_values_with_sheet_row_numbers(self):
        with self.cassette("find_rows"):
            grace = self.sheets.find_rows(SID, "Leads", {"Email": "grace@example.com", "Score": None})
            # The header in row 3: data rows are numbered from 4.
            new = self.sheets.find_rows(SID, "Leads!A3:D", {"Status": "New"})
        self.assertEqual(grace, [{"row": 3, "values": {"Name": "Grace", "Email": "grace@example.com", "Score": "",
                                                       "Status": "Contacted"}}])
        self.assertEqual([(r["row"], r["values"]["Name"]) for r in new], [(4, "Ada"), (5, "Grace")])

    def test_list_sheets(self):
        with self.cassette("list_sheets"):
            sheets = self.sheets.list_sheets(SID)
        self.assertEqual([(s["title"], s["sheetId"]) for s in sheets], [("Leads", 0), ("Q1 sales", 1820467340)])
        self.assertEqual(sheets[0]["gridProperties"]["rowCount"], 1000)

    def test_create_spreadsheet_with_named_sheets(self):
        with self.cassette("create_spreadsheet"):
            created = self.sheets.create_spreadsheet("Weekly report", sheets=["Summary", "Data"])
        self.assertTrue(created["spreadsheetUrl"].endswith(f"/d/{created['spreadsheetId']}/edit"))
        self.assertEqual([s["properties"]["title"] for s in created["sheets"]], ["Summary", "Data"])

    def test_new_rows_marks_then_reports_rows_once_their_key_is_filled(self):
        with self.cassette("new_rows"):
            first, state = self.sheets.new_rows(SID, "Leads", None, key_column="Email")
            self.assertEqual((first, state), ([], {"last_row": 5}))
            second, state = self.sheets.new_rows(SID, "Leads", state, key_column="Email")
            self.assertEqual(([r["row"] for r in second], state), ([6], {"last_row": 6}))
            third, state = self.sheets.new_rows(SID, "Leads", state, key_column="Email")
        self.assertEqual([(r["row"], r["values"]["Email"]) for r in third],
                         [(7, "margaret@example.com"), (8, "ken@example.com")])
        self.assertEqual(state, {"last_row": 8})

    def test_errors_keep_googles_message(self):
        with self.cassette("errors"):
            missing = self._error(self.sheets.list_sheets, "1Missing")
            bad = self._error(self.sheets.read_range, SID, "Nope!A1")
            denied = self._error(self.sheets.append_rows, SID, "Leads", [["Ada"]])
        self.assertEqual((missing.code, missing.status), ("NOT_FOUND", 404))
        self.assertIn("Requested entity was not found.", str(missing))
        self.assertEqual((bad.code, bad.status), ("INVALID_REQUEST", 400))
        self.assertIn("Unable to parse range: Nope!A1", str(bad))
        self.assertEqual((denied.code, denied.status), ("FORBIDDEN", 403))
        self.assertIn("The caller does not have permission", str(denied))


class GoogleSheetsRequestTests(_Sheets):
    def setUp(self):
        super().setUp()
        self.fake = self.fake_http(SHEETS)

    def test_the_token_goes_to_the_sheets_host_as_a_bearer_header(self):
        self.fake.add("GET", f"/v4/spreadsheets/{SID}", json={"sheets": []})
        self.sheets.list_sheets(SID)
        sent = self.fake.requests[0]
        self.assertTrue(sent.url.startswith(f"{SHEETS}/v4/spreadsheets/{SID}?"), sent.url)
        self.assertEqual(sent.headers["Authorization"], f"Bearer {self.conn.credentials['access_token']}")
        self.assertEqual(self.sheets.client().base_url, SHEETS)

    def test_a_named_connection(self):
        work = self.use_connection("work", provider="google")
        self.fake.add("GET", f"/v4/spreadsheets/{SID}", json={"sheets": []})
        self.sheets.list_sheets(SID, connection="work")
        self.assertEqual(self.fake.requests[0].headers["Authorization"], f"Bearer {work.credentials['access_token']}")

    def test_ranges_and_ids_are_quoted_in_the_path(self):
        self.fake.default(json={"values": [["a"]]})
        self.sheets.read_range("id/with?odd#chars", "'Q1 sales'!A1:B2")
        self.assertEqual(self.fake.requests[0].path,
                         "/v4/spreadsheets/id%2Fwith%3Fodd%23chars/values/%27Q1%20sales%27%21A1%3AB2")

    def test_rows_one_row_or_a_list_and_bad_shapes_refused(self):
        self.fake.default(json={"updates": {}})
        self.sheets.append_rows(SID, "Leads", ("Ada", 36))
        self.sheets.update_range(SID, "Leads!A2", [("Ada", 36), ["Grace"]])
        self.assertEqual(self.fake.requests[0].json()["values"], [["Ada", 36]])
        self.assertEqual(self.fake.requests[1].json()["values"], [["Ada", 36], ["Grace"]])
        for rows in ("Ada", b"Ada", [["Ada"], "Grace"]):
            with self.subTest(rows=rows), self.assertRaises(TypeError):
                self.sheets.append_rows(SID, "Leads", rows)
        with self.assertRaises(TypeError):
            self.sheets.update_range(SID, "Leads!A2", [{"Name": "Ada"}])
        self.assertEqual(len(self.fake.requests), 2)

    def test_appending_nothing_makes_no_request(self):
        self.assertIsNone(self.sheets.append_rows(SID, "Leads", []))
        self.assertEqual(self.fake.requests, [])

    def test_a_dict_row_with_an_unknown_column_writes_nothing(self):
        self.fake.add("GET", None, json={"range": "Leads!A1:Z1", "values": [HEADER]})
        with self.assertRaisesRegex(ValueError, "No column 'Phone' in the first row of 'Leads' "
                                                r"\(Name, Email, Score, Status\)"):
            self.sheets.append_rows(SID, "Leads", {"Name": "Ada", "Phone": "123"})
        self.assertEqual([r.method for r in self.fake.requests], ["GET"])

    def test_dict_rows_follow_the_header_of_the_ranges_first_row_and_column(self):
        self.fake.add("GET", None, json={"range": "Leads!A5:Z5", "values": [["x", "y", "Name", "", "Name", "Email"]]})
        self.fake.add("POST", None, json={"updates": {}})
        self.sheets.append_rows(SID, "Leads!C5", {"Email": "ada@example.com", "Name": "Ada"})
        self.assertEqual(self.fake.requests[0].path, f"/v4/spreadsheets/{SID}/values/Leads%215%3A5")
        # From column C: Name, (empty), Name again (only the first gets it), Email.
        self.assertEqual(self.fake.requests[1].json()["values"], [["Ada", None, None, "ada@example.com"]])
        self.fake.add("GET", None, json={"values": [["Name", "Email"]]})
        self.fake.add("POST", None, json={"updates": {}})
        self.sheets.append_rows(SID, "'Q1 sales'!B2:C", {"Email": "e"})
        self.assertEqual(self.fake.requests[2].path, f"/v4/spreadsheets/{SID}/values/%27Q1%20sales%27%21B2%3AC2")

    def test_choices_are_checked_before_any_request(self):
        with self.assertRaisesRegex(ValueError, "value_input is one of USER_ENTERED, RAW"):
            self.sheets.update_range(SID, "A1", [[1]], value_input="typed")
        with self.assertRaisesRegex(ValueError, "value_render is one of"):
            self.sheets.read_range(SID, "A1", value_render="pretty")
        with self.assertRaisesRegex(ValueError, "needs match="):
            self.sheets.find_rows(SID, "Leads", {})
        self.assertEqual(self.fake.requests, [])

    def test_find_rows_refuses_an_unknown_column_and_stops_at_the_limit(self):
        self.fake.default(json={"range": "Leads!A1:D4", "values": [HEADER, ["Ada", "a", "1", "New"],
                                                                   ["Bo", "b", "1.0", "New"], ["Cy", "c", "1"]]})
        with self.assertRaisesRegex(ValueError, "No column 'Phone'"):
            self.sheets.find_rows(SID, "Leads", {"Phone": "1"})
        found = self.sheets.find_rows(SID, "Leads", {"Score": 1.0})
        self.assertEqual([r["row"] for r in found], [2, 4], "1.0 matches a cell showing 1, not one showing 1.0")
        self.assertEqual(found[1]["values"]["Status"], "", "a short row's missing cells are empty")
        self.assertEqual(len(self.sheets.find_rows(SID, "Leads", {"Status": "New"}, limit=1)), 1)
        self.assertEqual(self.sheets.find_rows(SID, "Leads", {"Status": "New"}, limit=0), [])

    def test_an_append_is_not_repeated_after_a_5xx_but_an_update_is(self):
        self.fake.add("POST", None, status=503)
        error = self._error(self.sheets.append_rows, SID, "Leads", [["Ada"]])
        self.assertEqual((error.code, len(self.fake.requests)), ("SERVER_ERROR", 1))
        self.fake.add("PUT", None, status=502).add("PUT", None, json={"updatedCells": 1})
        self.assertEqual(self.sheets.update_range(SID, "A1", [["x"]])["updatedCells"], 1)
        self.assertEqual(self.sleeps, [1.0])

    def test_new_rows_skips_empty_rows_stops_at_the_limit_and_resets_when_rows_go(self):
        rows = [HEADER, ["Ada", "a"], [], ["Bo", "b"], ["", ""], ["Cy", "c"]]
        self.fake.default(json={"range": "Leads!A1:D6", "values": rows})
        found, state = self.sheets.new_rows(SID, "Leads", {"last_row": 1}, limit=2)
        self.assertEqual(([r["row"] for r in found], state), ([2, 4], {"last_row": 4}))
        found, state = self.sheets.new_rows(SID, "Leads", state)
        self.assertEqual(([r["row"] for r in found], state), ([6], {"last_row": 6}))
        # Two rows deleted: nothing reported, the mark moves back to the new end.
        self.fake.default(json={"range": "Leads!A1:D4", "values": rows[:4]})
        self.assertEqual(self.sheets.new_rows(SID, "Leads", state), ([], {"last_row": 4}))
        for bad in (None, {}, {"last_row": "3"}, {"last_row": True}, "x"):
            with self.subTest(state=bad):
                self.assertEqual(self.sheets.new_rows(SID, "Leads", bad), ([], {"last_row": 4}))
        self.fake.default(json={"range": "Empty!A1:Z1000"})
        self.assertEqual(self.sheets.new_rows(SID, "Empty", {"last_row": 4}), ([], {"last_row": 0}))
        with self.assertRaisesRegex(ValueError, "No column 'Id'"):
            self.sheets.new_rows(SID, "Leads", state, key_column="Id")


class SheetsTriggerTests(_Sheets):
    def test_the_new_row_adapter_turns_new_rows_into_events(self):
        (trigger,) = framework.load_triggers("google_sheets")
        self.assertEqual((trigger.key, trigger.config_schema["required"]), ("sheets_new_row", ["spreadsheet_id", "range"]))
        fake = self.fake_http(SHEETS)
        values = [HEADER, ["Ada", "ada@example.com"]]
        fake.default(json=lambda request: {"range": "Leads!A1:D9", "values": values})
        config = {"spreadsheet_id": SID, "range": "Leads"}
        events, state = trigger.check(self.sheets, None, config, None)
        self.assertEqual((events, state), ([], {"last_row": 2}))
        values += [["Grace", "grace@example.com"], ["Alan", ""]]
        events, state = trigger.check(self.sheets, None, {**config, "key_column": "Email"}, state)
        self.assertEqual([e["id"] for e in events], ["key:grace@example.com"])
        self.assertEqual(events[0]["item"], {"spreadsheet_id": SID, "range": "Leads", "row": 3,
                                             "values": {"Name": "Grace", "Email": "grace@example.com",
                                                        "Score": "", "Status": ""}})
        self.assertEqual(state, {"last_row": 3})
        # By position: the id holds the row number and its content.
        events, _state = trigger.check(self.sheets, None, config, state, limit=5)
        self.assertRegex(events[0]["id"], r"^row:4:[0-9a-f]{12}$")
        with mock.patch.object(self.sheets, "new_rows", return_value=([], {"last_row": 9})) as new_rows:
            trigger.check(self.sheets, "work", {**config, "key_column": ""}, {"last_row": 3}, limit=7)
        new_rows.assert_called_once_with(SID, "Leads", {"last_row": 3}, key_column=None, limit=7, connection="work")

    def test_integrations_without_triggers_have_none(self):
        self.assertEqual(framework.load_triggers("airtable"), ())
        with self.assertRaises(LookupError):
            framework.load_triggers("nope")
