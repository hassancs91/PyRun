"""The notion integration: pages, databases and blocks in Notion (see core/integrations/framework.py)."""

from core.integrations.framework import Integration, Smoke


def _smoke_search(module, connection, config):
    """What the integration was given: a search with no query lists it."""
    found = list(module.search(limit=5, connection=connection))
    return f"search answered with {len(found)} page(s) or database(s) (first 5)"


EXAMPLE = '''\
from pyrunner_integrations import notion

TASKS = "0123456789abcdef0123456789abcdef"   # the database's id: the end of its URL

# Every open task, soonest first.
for page in notion.query_database(TASKS, filter={"property": "Status", "select": {"equals": "Open"}},
                                  sorts=[{"property": "Due", "direction": "ascending"}]):
    print(page["properties"]["Name"]["title"][0]["plain_text"])

# A new row, with a note in its page.
page = notion.create_page(database_id=TASKS, title="Call Ada",
                          properties={"Status": {"select": {"name": "Open"}}})
notion.append_blocks(page["id"], ["Ask about the invoice."])

# Later: mark it done.
notion.update_page(page["id"], {"Status": {"select": {"name": "Done"}}})
'''

INTEGRATION = Integration(
    key="notion",
    provider="notion",
    title="Notion",
    functions=("query_database", "get_database", "get_page", "create_page", "update_page", "append_blocks",
               "list_blocks", "search", "client"),
    example=EXAMPLE,
    smoke=(Smoke("search", _smoke_search),),
)
