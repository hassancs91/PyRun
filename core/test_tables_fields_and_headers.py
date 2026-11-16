"""
Tables: Add row errors under their fields, and CSV header matching (2.5 rc, run 7:
test-pass observations O6 and O10-3; tickets T38 and T39).

What this pins:

- The Add row form reports each refused column under its own field: the error
  text in an element whose id the field names in ``aria-describedby``, the field
  marked ``aria-invalid``; what was typed comes back in the fields, and a number
  or date the server refused comes back in a text field (a number/date field
  can't show "12abc"). Field errors are not messages (toasts); an error that
  isn't one field's (the row limit) still is. The redirect lands on the form.
- CSV headers match column keys ignoring case, spaces, dashes and underscores on
  both sides ("E-Mail" fills ``email``; "Lead Score", "lead-score" and
  "LEADSCORE" fill ``lead_score``). A match that isn't one to one (two headers
  for one key, one header for two keys) is never guessed: without a mapping the
  import is refused naming them, from the console and from ``pyrunner_tables``
  alike (one function, ``csv_io.match_header``); a mapping settles it. The
  Import page's mapping step is given the server's separator pattern.
"""

import io
import json
import os
import tempfile
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from core.models import TableRow
from core.tables import csv_io
from core.tables.errors import TableError
from core.tables.service import TableService
from core.test_tables import _Console, _HelperFixture
from core.test_tables_api import _csv
from core.views.tables import _add_row_errors

HEADER_COLUMNS = [
    {"key": "email", "type": "text"},
    {"key": "lead_score", "type": "number"},
    {"key": "note", "type": "text"},
]


class AddRowFieldErrorTests(_Console):
    def setUp(self):
        super().setUp()
        TableService.insert(self.table, [{"email": "seed@x"}])  # a grid with rows starts the form closed
        self.url = reverse("cpanel:table_row_add", args=[self.table.pk])

    def test_errors_are_returned_per_column(self):
        columns = self.table.columns
        typed, values, errors = _add_row_errors(columns, {
            "c_email": "", "c_score": "12abc", "c_joined": "2026-13-45", "c_meta": "{bad", "c_site": "https://ok.x",
        })
        self.assertEqual(set(errors), {"email", "score", "joined", "meta"})
        self.assertEqual(errors["email"], "Required: give it a value.")
        self.assertIn("expects a number", errors["score"])
        self.assertIn('"joined"', errors["joined"])
        self.assertTrue(errors["meta"].startswith("Not valid JSON ("))
        self.assertEqual(values, {"site": "https://ok.x"})
        self.assertEqual(typed["score"], "12abc")

    def test_each_error_sits_under_its_field_and_the_typed_text_stays(self):
        resp = self.client.post(self.url, {
            "c_email": "", "c_score": "12abc", "c_joined": "2026-13-45", "c_seen": "2026-11-12T09:30:00Z",
            "c_meta": "{bad", "c_active": "false",
        })
        self.assertTrue(resp["Location"].endswith("#add-row"), resp["Location"])
        page = self.client.get(resp["Location"])
        body = page.content.decode()
        self.assertEqual([m for m in page.context["messages"] if m.level_tag == "error"], [])
        self.assertIn("The row wasn't added: fix the fields marked below.", body)
        for key in ("email", "score", "joined", "meta"):
            with self.subTest(key=key):
                self.assertIn(f'id="add_c_{key}" aria-invalid="true" aria-describedby="add_c_{key}_error"', body)
                self.assertIn(f'<p id="add_c_{key}_error" class="text-xs text-fail break-words">', body)
        # A refused number / date comes back as typed, in a text field.
        self.assertRegex(body, r'id="add_c_score"[^>]*\s+type="text" inputmode="decimal"\s+value="12abc"')
        self.assertRegex(body, r'id="add_c_joined"[^>]*\s+type="text" placeholder="YYYY-MM-DD"\s+value="2026-13-45"')
        # Fields that passed keep their value and stay unmarked.
        self.assertNotIn('id="add_c_seen" aria-invalid', body)
        self.assertIn('value="2026-11-12T09:30:00Z"', body)
        self.assertIn('<option value="false" selected>', body)
        self.assertIn("{bad</textarea>", body)
        self.assertEqual(TableRow.objects.filter(table=self.table).count(), 1)
        # Shown once: the next view is the plain form.
        again = self.detail().content.decode()
        self.assertNotIn('aria-invalid="true"', again)
        self.assertRegex(again, r'id="add_c_score"\s+type="number"')
        self.assertRegex(again, r'id="add_c_joined"\s+type="date"')

    def test_one_bad_field_says_field_and_a_required_empty_number_stays_a_number(self):
        table = TableService.create_table(workspace=self.ws, name="Stock", columns=[
            {"key": "sku", "type": "text"}, {"key": "qty", "type": "number", "required": True}])
        self.client.post(reverse("cpanel:table_row_add", args=[table.pk]), {"c_sku": "a", "c_qty": ""})
        body = self.client.get(reverse("cpanel:table_detail", args=[table.pk])).content.decode()
        self.assertIn("fix the field marked below.", body)
        self.assertRegex(body, r'id="add_c_qty" aria-invalid="true" aria-describedby="add_c_qty_error"\s+'
                               r'type="number"')

    def test_an_error_that_is_not_a_fields_stays_a_message(self):
        with mock.patch.object(TableService, "insert", side_effect=TableError("The table is full.", "row_cap")):
            resp = self.client.post(self.url, {"c_email": "a@x", "c_score": "3"}, follow=True)
        self.assertEqual([str(m) for m in resp.context["messages"] if m.level_tag == "error"], ["The table is full."])
        body = resp.content.decode()
        self.assertNotIn('aria-invalid="true"', body)
        self.assertIn('value="a@x"', body)


class HeaderMatchingTests(_Console):
    def test_case_spaces_dashes_and_underscores_are_ignored_on_both_sides(self):
        for name, key in (("E-Mail", "email"), ("e_mail", "email"), (" EMAIL ", "email"),
                          ("Lead Score", "lead_score"), ("lead-score", "lead_score"), ("LEADSCORE", "lead_score"),
                          ("lead__score", "lead_score"), ("Lead - Score", "lead_score"), ("Created At", None),
                          ("ID", None), ("updated-at", None), ("notes", None)):
            with self.subTest(name=name):
                self.assertEqual(csv_io.match_header([name], HEADER_COLUMNS), ([key], []))

    def test_two_headers_for_one_key_are_not_guessed(self):
        mapping, conflicts = csv_io.match_header(["Lead Score", "Email", "lead-score"], HEADER_COLUMNS)
        self.assertEqual(mapping, [None, "email", None])
        self.assertEqual(conflicts, ['CSV columns "Lead Score" and "lead-score" all match the column "lead_score".'])
        # The same header twice is two headers for one key too.
        self.assertEqual(csv_io.match_header(["email", "email"], HEADER_COLUMNS)[0], [None, None])

    def test_one_header_for_two_keys_is_not_guessed(self):
        columns = HEADER_COLUMNS + [{"key": "e_mail", "type": "text"}]
        mapping, conflicts = csv_io.match_header(["E-Mail", "note"], columns)
        self.assertEqual(mapping, [None, "note"])
        self.assertEqual(conflicts, ['CSV column "E-Mail" matches more than one column (email, e_mail).'])

    def test_an_ambiguous_file_is_refused_until_a_mapping_says_which(self):
        table = TableService.create_table(workspace=self.ws, name="Scores", columns=HEADER_COLUMNS)
        text = _csv(["E-Mail", "Lead Score", "lead-score"], ["a@x", "1", "2"])
        with self.assertRaisesMessage(TableError, 'CSV columns "Lead Score" and "lead-score" all match the column '
                                                  '"lead_score". Say which CSV column fills which table column'):
            csv_io.import_csv(table, io.StringIO(text))
        report = csv_io.import_csv(table, io.StringIO(text), mapping={"E-Mail": "email", "lead-score": "lead_score"})
        self.assertEqual(report["inserted"], 1)
        self.assertEqual(TableService.rows(table)[0]["lead_score"], 2)

    def upload(self, table, text, **extra):
        data = {"file": SimpleUploadedFile("rows.csv", text.encode("utf-8"), content_type="text/csv"), **extra}
        return self.client.post(reverse("cpanel:table_import", args=[table.pk]), data,
                                HTTP_ACCEPT="application/json")

    def test_the_console_import_matches_the_same_way(self):
        table = TableService.create_table(workspace=self.ws, name="Scores", columns=HEADER_COLUMNS)
        body = self.upload(table, _csv(["E-Mail", "Lead Score"], ["a@x", "5"])).json()
        self.assertEqual((body["inserted"], body["mapping"]), (1, {"E-Mail": "email", "Lead Score": "lead_score"}))
        refused = self.upload(table, _csv(["Email", "E-Mail"], ["a@x", "b@x"]))
        self.assertEqual(refused.status_code, 400)
        self.assertIn('CSV columns "Email" and "E-Mail" all match the column "email".', refused.json()["error"])
        # The page's mapping step sends a choice; that settles it.
        chosen = self.upload(table, _csv(["Email", "E-Mail"], ["a@x", "b@x"]), mapping=json.dumps([None, "email"]))
        self.assertEqual(chosen.json()["inserted"], 1)
        self.assertEqual(TableService.rows(table, order="-id")[0]["email"], "b@x")

    def test_the_import_page_prefills_with_the_servers_pattern(self):
        page = self.client.get(reverse("cpanel:table_import", args=[self.table.pk]))
        self.assertContains(page, f'data-separators="{csv_io.SEPARATORS}"')
        self.assertContains(page, 'data-system="[&quot;id&quot;, &quot;created_at&quot;, &quot;updated_at&quot;]"')
        self.assertContains(page, "js/table_import.")
        self.assertContains(page, "ignoring case, spaces, dashes and underscores")
        self.assertNotContains(page, "<script>\n(function")


class HelperHeaderMatchingTests(_HelperFixture):
    def test_the_helper_matches_and_refuses_the_same_way(self):
        leads = self.pt.table("leads")
        folder = tempfile.mkdtemp()
        path = os.path.join(folder, "in.csv")
        with open(path, "w", newline="", encoding="utf-8") as fh:
            fh.write(_csv(["E-Mail", "SCORE"], ["h@x", "4"]))
        self.assertEqual(leads.import_csv(path)["inserted"], 1)
        self.assertEqual(TableService.rows(self.table)[0]["score"], 4)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            fh.write(_csv(["Email", "e-mail"], ["a@x", "b@x"]))
        with self.assertRaises(self.pt.TableError) as caught:
            leads.import_csv(path)
        self.assertIn('all match the column "email"', str(caught.exception))
        self.assertEqual(leads.import_csv(path, mapping={"e-mail": "email"})["inserted"], 1)
