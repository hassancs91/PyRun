"""
Notion for scripts: query databases, create and update pages, append blocks and search.

Functions take Notion's ids (a page's or database's id, with or without the
dashes; it is the last part of its URL) and return Notion's own objects:
pages with their ``properties``, blocks, databases. Page properties are
written in Notion's shape, for example a database row:

    {"Name": {"title": [{"text": {"content": "Ada"}}]},
     "Status": {"select": {"name": "New"}},
     "Score": {"number": 9}}

Where a function takes blocks, a plain string is a paragraph, so
``notion.append_blocks(page_id, ["First line", "Second line"])`` works. Lists come
page by page as you read them; ``limit`` stops early.

The connection is a Notion integration: an OAuth connection, or an internal
integration's secret pasted as its access token. It only sees the pages and
databases shared with it in Notion. Calls use Notion API version 2022-06-28
(the connection sends it).
"""

from . import _client

PROVIDER = "notion"
PAGE_SIZE = 100  # Notion's maximum page size
MAX_CHILDREN = 100  # blocks per request (Notion's limit)
TEXT_CHUNK = 2000  # characters per rich text object (Notion's limit)


def _rich_text(text) -> list:
    text = str(text)
    return [{"type": "text", "text": {"content": text[i:i + TEXT_CHUNK]}}
            for i in range(0, len(text), TEXT_CHUNK)] or [{"type": "text", "text": {"content": ""}}]


def _block(block) -> dict:
    """A block object; a string becomes a paragraph."""
    if isinstance(block, str):
        return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rich_text(block)}}
    return block


def _chunks(blocks):
    if isinstance(blocks, (str, dict)):
        blocks = [blocks]  # one block
    blocks = [_block(b) for b in blocks]
    for start in range(0, len(blocks), MAX_CHILDREN):
        yield blocks[start:start + MAX_CHILDREN]


def _page_size(limit) -> int:
    return min(PAGE_SIZE, limit) if limit and limit > 0 else PAGE_SIZE


def client(connection=None, *, timeout=30):
    """The shared client of ``connection``, for calls this module has no function for.

    Paths are relative to ``https://api.notion.com``. For example:

        user = notion.client().get("/v1/users/me")
    """
    return _client.Client(connection, provider=PROVIDER, timeout=timeout)


def query_database(database_id, *, filter=None, sorts=None, limit=None, connection=None):
    """Yield the database's pages (its rows) that match ``filter``, fetching pages as it goes.

    ``filter`` and ``sorts`` are Notion's query objects. ``limit`` stops after
    that many. For example:

        open_tasks = notion.query_database(
            database_id,
            filter={"property": "Status", "select": {"equals": "Open"}},
            sorts=[{"property": "Due", "direction": "ascending"}],
        )
        for page in open_tasks:
            print(page["id"], page["properties"]["Name"]["title"][0]["plain_text"])
    """
    body = {"page_size": _page_size(limit)}
    if filter is not None:
        body["filter"] = filter
    if sorts is not None:
        body["sorts"] = [sorts] if isinstance(sorts, dict) else list(sorts)
    return _client.shared(connection, PROVIDER).paginate(
        f"/v1/databases/{_client.quote(database_id)}/query", method="POST", json=body, items="results",
        cursor="next_cursor", cursor_param="start_cursor", limit=limit)


def get_database(database_id, *, connection=None):
    """The database itself: its ``title`` and its ``properties`` (the columns, with their types and options).

    For example:

        columns = notion.get_database(database_id)["properties"]
    """
    return _client.shared(connection, PROVIDER).get(f"/v1/databases/{_client.quote(database_id)}")


def get_page(page_id, *, connection=None):
    """One page with its ``properties`` (its content is in ``list_blocks(page_id)``).

    For example:

        page = notion.get_page(page_id)
        print(page["url"])
    """
    return _client.shared(connection, PROVIDER).get(f"/v1/pages/{_client.quote(page_id)}")


def create_page(*, database_id=None, page_id=None, properties=None, title=None, children=None,
                connection=None):
    """Create a page in a database (a new row) or under a page, and return it.

    Give ``database_id`` or ``page_id``. ``properties`` are the new page's
    values; ``title`` sets its title (the database's title column, whatever its
    name). ``children`` are its content blocks (strings are paragraphs); past
    Notion's 100 per request the rest is appended after it is created. For example:

        page = notion.create_page(
            database_id=database_id,
            title="Call Ada",
            properties={"Status": {"select": {"name": "Open"}}},
            children=["Ask about the invoice."],
        )
    """
    if (database_id is None) == (page_id is None):
        raise ValueError("create_page() needs database_id= or page_id= (one of them)")
    parent = {"database_id": database_id} if database_id is not None else {"page_id": page_id}
    body = {"parent": parent, "properties": dict(properties or {})}
    if title is not None:
        # "title" is the id of every page's title property, in a database too.
        body["properties"]["title"] = {"title": _rich_text(title)}
    chunks = list(_chunks(children or ()))
    if chunks:
        body["children"] = chunks[0]
    api = _client.shared(connection, PROVIDER)
    page = api.post("/v1/pages", json=body)
    for chunk in chunks[1:]:
        api.patch(f"/v1/blocks/{_client.quote(page['id'])}/children", json={"children": chunk})
    return page


def update_page(page_id, properties=None, *, title=None, archived=None, connection=None):
    """Change a page's ``properties`` (only those given) and return it.

    ``title`` sets its title; ``archived=True`` moves it to the trash
    (``False`` restores it). For example:

        notion.update_page(page_id, {"Status": {"select": {"name": "Done"}}})
    """
    changed = dict(properties or {})
    if title is not None:
        changed["title"] = {"title": _rich_text(title)}
    body = {"properties": changed} if changed else {}
    if archived is not None:
        body["archived"] = bool(archived)
    return _client.shared(connection, PROVIDER).patch(f"/v1/pages/{_client.quote(page_id)}", json=body)


def append_blocks(block_id, blocks, *, connection=None):
    """Add ``blocks`` at the end of a page (or of a block that holds others); returns the new blocks.

    ``blocks`` is a list, or one block. Strings are paragraphs; other blocks
    are Notion block objects. More than 100 go in several requests. For example:

        notion.append_blocks(page_id, [
            {"type": "heading_2", "heading_2": {"rich_text": [{"text": {"content": "Summary"}}]}},
            "Everything went fine.",
        ])
    """
    api = _client.shared(connection, PROVIDER)
    out = []
    for chunk in _chunks(blocks):
        out += api.patch(f"/v1/blocks/{_client.quote(block_id)}/children", json={"children": chunk})["results"]
    return out


def list_blocks(block_id, *, limit=None, connection=None):
    """Yield a page's content blocks (or a block's children), in order, fetching pages as it goes.

    Only the first level: a block with ``has_children`` holds more, read them
    with ``list_blocks(block["id"])``. For example:

        for block in notion.list_blocks(page_id):
            if block["type"] == "paragraph":
                print("".join(t["plain_text"] for t in block["paragraph"]["rich_text"]))
    """
    return _client.shared(connection, PROVIDER).paginate(
        f"/v1/blocks/{_client.quote(block_id)}/children", params={"page_size": _page_size(limit)},
        items="results", cursor="next_cursor", cursor_param="start_cursor", limit=limit)


def search(query="", *, kind=None, limit=None, connection=None):
    """Yield the pages and databases shared with the integration whose title matches ``query``.

    ``kind="page"`` or ``kind="database"`` keeps only those. An empty query
    lists everything shared. For example:

        for result in notion.search("Roadmap", kind="database", limit=5):
            print(result["id"])
    """
    if kind not in (None, "page", "database"):
        raise ValueError("search() kind is 'page', 'database' or None")
    body = {"page_size": _page_size(limit)}
    if query:
        body["query"] = str(query)
    if kind:
        body["filter"] = {"property": "object", "value": kind}
    return _client.shared(connection, PROVIDER).paginate(
        "/v1/search", method="POST", json=body, items="results", cursor="next_cursor",
        cursor_param="start_cursor", limit=limit)
