"""The airtable integration: records in Airtable bases (see core/integrations/framework.py)."""

from core.integrations.framework import Integration, Smoke


def _smoke_whoami(module, connection, config):
    """Who the token belongs to, and the bases it reaches when it may list them."""
    me = module.client(connection).get("/v0/meta/whoami")
    scopes = me.get("scopes") or []
    line = f"whoami answered with {len(scopes)} scope(s)"
    if "schema.bases:read" in scopes:
        bases = list(module.list_bases(limit=5, connection=connection))
        line += f"; list_bases answered with {len(bases)} base(s) (first 5)"
    return line


EXAMPLE = '''\
from pyrunner_integrations import airtable

BASE = "appXXXXXXXXXXXXXX"   # the base's id: the part of its URL that starts with "app"

# Add a contact unless one with that email is there already.
found = airtable.find_records(BASE, "Contacts", {"Email": "ada@example.com"}, limit=1)
if found:
    contact = airtable.update_record(BASE, "Contacts", found[0]["id"], {"Status": "Returning"})
else:
    contact = airtable.create_record(BASE, "Contacts", {"Name": "Ada", "Email": "ada@example.com"})

# Every open lead, newest first, page by page.
for record in airtable.list_records(BASE, "Leads", formula="{Status}='Open'", sort=["-Created"]):
    print(record["id"], record["fields"].get("Name"))
'''

INTEGRATION = Integration(
    key="airtable",
    provider="airtable",
    title="Airtable",
    functions=("list_records", "get_record", "find_records", "create_record", "create_records",
               "update_record", "update_records", "delete_records", "list_bases", "list_tables", "client"),
    example=EXAMPLE,
    smoke=(Smoke("whoami", _smoke_whoami),),
)
