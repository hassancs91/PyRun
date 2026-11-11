"""The google_sheets integration: cells and rows of Google Sheets spreadsheets (see core/integrations/framework.py)."""

from core.integrations.framework import Integration, Smoke

# Google's public sample spreadsheet (its Sheets API quickstart reads it): any
# account with the Sheets scope can read it, so the smoke check needs no id of yours.
SAMPLE_SPREADSHEET = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms"


def _smoke_read(module, connection, config):
    """List the sample spreadsheet's sheets, then read its first rows (read-only)."""
    sheets = module.list_sheets(SAMPLE_SPREADSHEET, connection=connection)
    title = (sheets[0]["title"] if sheets else "Sheet1").replace("'", "''")
    rows = module.read_range(SAMPLE_SPREADSHEET, f"'{title}'!A1:F3", connection=connection)
    return (f"list_sheets answered with {len(sheets)} sheet(s); read_range answered with {len(rows)} row(s) "
            "of Google's sample spreadsheet")


EXAMPLE = '''\
from pyrunner_integrations import google_sheets

SHEET = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"   # from the URL: /spreadsheets/d/<id>/edit

# Add a lead unless the email is there already (the first row of "Leads" is its header).
found = google_sheets.find_rows(SHEET, "Leads", {"Email": "ada@example.com"}, limit=1)
if found:
    google_sheets.update_range(SHEET, f"Leads!D{found[0]['row']}", [["Returning"]])
else:
    google_sheets.append_rows(SHEET, "Leads", {"Name": "Ada", "Email": "ada@example.com", "Status": "New"})

# Every row of a range, as lists of cell values.
for row in google_sheets.read_range(SHEET, "Leads!A2:D"):
    print(row)
'''

INTEGRATION = Integration(
    key="google_sheets",
    provider="google",
    title="Google Sheets",
    functions=("read_range", "append_rows", "update_range", "find_rows", "list_sheets", "create_spreadsheet",
               "new_rows", "client"),
    example=EXAMPLE,
    smoke=(Smoke("read", _smoke_read),),
)
