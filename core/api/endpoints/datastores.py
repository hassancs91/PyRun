"""Datastores over REST and MCP (2.5 Stage 2): reads, and writes behind the switch.

The legacy ``/api/v1/datastores/…`` URLs are aliases of the four read handlers
(``core.views.api.datastores``): their bodies are these results, unwrapped. A
store is found by name in the caller's workspace only: unlike the run helper's
``DataStore.resolve_for_workspace``, never a store that belongs to no workspace
(fail closed, like the token without a workspace). A token bound to one store
(``APIToken.datastore``: a Unified API binding or a legacy single-store token)
reaches that store alone; any other store in its workspace answers 403.
"""

from django.db import IntegrityError, transaction
from django.db.models import Count

from core.api.errors import ApiError
from core.api.registry import endpoint
from core.api.schema import ANY_VALUE
from core.models import DataStore, DataStoreEntry

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
NAME = {"type": "string", "maxLength": 100,
        "description": "The datastore's name; optional for a token bound to one datastore"}
KEY = {"type": "string", "maxLength": 255, "description": "The entry's key"}


def store_input(properties=None, required=()):
    return {
        "type": "object",
        "properties": {"name": NAME, **(properties or {})},
        "required": list(required),
    }


def stores_for(ctx):
    """The stores this caller may list: its workspace's, or its bound store."""
    stores = DataStore.objects.for_workspace(ctx.workspace)
    if ctx.datastore_id is not None:
        stores = stores.filter(pk=ctx.datastore_id)
    return stores


def resolve_store(ctx, name):
    """The store ``name`` names in the caller's workspace; a bound token may leave it out."""
    if name is None:
        if ctx.datastore_id is None:
            raise ApiError("invalid_input", "$.name: pass the datastore name.", status=400)
        store = stores_for(ctx).first()
    else:
        store = DataStore.objects.for_workspace(ctx.workspace).filter(name=name).first()
    if store is None:
        raise ApiError("not_found", f"Datastore '{name}' not found" if name is not None
                       else "Unknown datastore.", status=404)
    if ctx.datastore_id is not None and store.pk != ctx.datastore_id:
        raise ApiError("forbidden", "Token does not have access to this datastore", status=403)
    return store


def summary(store, entry_count):
    return {
        "name": store.name,
        "description": store.description,
        "entry_count": entry_count,
        "created_at": store.created_at.isoformat(),
        "updated_at": store.updated_at.isoformat(),
    }


def entry_data(entry):
    return {
        "key": entry.key,
        "value": entry.get_value(),
        "created_at": entry.created_at.isoformat(),
        "updated_at": entry.updated_at.isoformat(),
    }


@endpoint(
    "datastores.list",
    title="List datastores",
    description=(
        "Datastores in this workspace (or the one this token is bound to), by name, "
        "with their entry counts."
    ),
    input={"type": "object", "properties": {}},
    permission="datastores:read",
)
def list_datastores(ctx, params):
    stores = list(stores_for(ctx).annotate(n=Count("entries")).order_by("name"))
    return {"datastores": [summary(s, s.n) for s in stores], "count": len(stores)}


@endpoint(
    "datastores.get",
    title="Get datastore",
    description="One datastore's description, entry count and timestamps.",
    input=store_input(),
    permission="datastores:read",
)
def get_datastore(ctx, params):
    store = resolve_store(ctx, params.get("name"))
    return summary(store, store.entries.count())


@endpoint(
    "datastores.entries",
    title="List datastore entries",
    description=(
        "A datastore's entries with their values, by key, a page at a time "
        f"(page_size 1-{MAX_PAGE_SIZE}, default {DEFAULT_PAGE_SIZE}). "
        "A page past the end answers the last page."
    ),
    input=store_input({
        "page": {"type": "integer", "minimum": 1},
        "page_size": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE_SIZE},
    }),
    permission="datastores:read",
)
def list_entries(ctx, params):
    store = resolve_store(ctx, params.get("name"))
    page_size = params.get("page_size", DEFAULT_PAGE_SIZE)
    total = store.entries.count()
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(params.get("page", 1), total_pages)
    offset = (page - 1) * page_size
    rows = store.entries.order_by("key")[offset : offset + page_size]
    return {
        "entries": [entry_data(e) for e in rows],
        "count": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


@endpoint(
    "datastores.entry",
    title="Get datastore entry",
    description="One entry of a datastore by its key, with its value.",
    input=store_input({"key": KEY}, ("key",)),
    permission="datastores:read",
)
def get_entry(ctx, params):
    store = resolve_store(ctx, params.get("name"))
    entry = store.entries.filter(key=params["key"]).first()
    if entry is None:
        raise ApiError("not_found", f"Entry '{params['key']}' not found", status=404)
    return entry_data(entry)


@endpoint(
    "datastores.set",
    title="Set datastore entry",
    description=(
        "Store a JSON value under a key in an existing datastore, replacing any value "
        "the key holds."
    ),
    input=store_input(
        {"key": KEY, "value": {"type": list(ANY_VALUE), "description": "Any JSON value"}},
        ("key", "value"),
    ),
    access="write",
    permission="datastores:write",
    rate=(30, 60),
)
def set_entry(ctx, params):
    store = resolve_store(ctx, params.get("name"))
    key = params["key"]
    if not key:
        raise ApiError("invalid_input", "$.key: pass a non-empty key.", status=400)
    entry = store.entries.filter(key=key).first()
    created = entry is None
    if created:
        entry = DataStoreEntry(datastore=store, key=key)
    entry.set_value(params["value"])
    try:
        with transaction.atomic():
            entry.save()
    except IntegrityError:
        # Another caller created the key between the lookup and the insert:
        # replace its value, as a later set would (last write wins).
        entry = store.entries.get(key=key)
        entry.set_value(params["value"])
        entry.save()
        created = False
    return {
        "name": store.name,
        "key": key,
        "created": created,
        "created_at": entry.created_at.isoformat(),
        "updated_at": entry.updated_at.isoformat(),
    }


@endpoint(
    "datastores.delete",
    title="Delete datastore entry",
    description=(
        "Delete one entry of a datastore by its key. A key that isn't there "
        "answers deleted: false."
    ),
    input=store_input({"key": KEY}, ("key",)),
    access="write",
    permission="datastores:write",
    rate=(30, 60),
)
def delete_entry(ctx, params):
    store = resolve_store(ctx, params.get("name"))
    deleted, _ = store.entries.filter(key=params["key"]).delete()
    return {"name": store.name, "key": params["key"], "deleted": bool(deleted)}
