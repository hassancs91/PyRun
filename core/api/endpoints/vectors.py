"""Vector collections over REST, MCP and Py AI (2.5 Stage 3, T17).

Reads, ``vectors:read`` (Py AI on the console and on chat channels):
``vectors.list`` and ``vectors.search``. A search embeds the query text with
the collection's own embedding provider through ``VectorService``, like every
other door, so the call is counted on the collection and on the provider (the
tokens and calls the Vectors and Services pages show).

Writes, ``vectors:write``, only while API writes are on (plan D8):
``vectors.upsert`` and ``vectors.delete``. They change the points of an
existing collection; creating or deleting a collection stays in the console.

A collection that a Brain uses for semantic search is left out of all four, as
if it didn't exist: its points hold the Brain's notes at every tier, and only
the brain endpoints, which check the caller's tier, may hand those out.
Points are stored data and come back as stored, like datastore values.
"""

import time

from core.api.errors import ApiError
from core.api.registry import endpoint
from core.models import Brain, VectorCollection
from core.vectors.budget import BudgetSession
from core.vectors.errors import STATUS_FOR_CODE, VectorError
from core.vectors.service import VectorService

MAX_K = 50
MAX_ITEMS = 100
MAX_IDS = 1000
# The longest a provider or vector-store call may take inside one API call.
CALL_SECONDS = 30
NAME = {"type": "string", "maxLength": 64, "description": "The collection's name"}
FILTER = {
    "type": "object",
    "description": (
        'Payload filter: {"field": value}, {"field": {"$in": [...]}}, $gt/$gte/$lt/$lte, '
        '$prefix, and {"must": [...], "must_not": [...]}'
    ),
}
POINT_ID = {"type": "string", "maxLength": 256}


def collections_for(ctx):
    """The workspace's collections, minus those a Brain uses for recall."""
    brain_owned = Brain.objects.filter(vector_collection__isnull=False).values(
        "vector_collection_id"
    )
    return (
        VectorCollection.objects.for_workspace(ctx.workspace)
        .exclude(pk__in=brain_owned)
        .select_related("connection", "embedding_provider")
    )


def resolve_collection(ctx, name):
    collection = collections_for(ctx).filter(name=name).first()
    if collection is None:
        raise ApiError("not_found", f"Collection '{name}' not found", status=404)
    if not collection.is_ready:
        raise ApiError(
            "not_ready",
            f"Collection '{name}' is in an error state; use Retry on its page in the console.",
            status=409,
        )
    return collection


def run(ctx, collection, operation):
    """``operation(handle)`` within the call's budget; a ``VectorError`` → ``ApiError``."""
    now = time.monotonic()
    deadline = min(ctx.deadline or now + CALL_SECONDS, now + CALL_SECONDS)
    try:
        with BudgetSession(deadline) as session:
            return operation(VectorService.handle(collection, session=session))
    except VectorError as exc:
        ctx.check_budget()
        status = STATUS_FOR_CODE.get(exc.code, 500)
        raise ApiError(exc.code, str(exc), status=status) from None


def summary(collection):
    return {
        "name": collection.name,
        "description": collection.description,
        "backend": collection.connection.provider_type,
        "status": collection.status,
        "points": collection.points_count,
        "dimension": collection.dimension,
        "metric": collection.metric,
        "embedding_model": collection.embedding_model,
        "text_search": collection.has_embeddings,
        "created_at": collection.created_at.isoformat(),
        "updated_at": collection.updated_at.isoformat(),
    }


@endpoint(
    "vectors.list",
    title="List vector collections",
    description=(
        "Vector collections in this workspace, by name, with their point counts. "
        "text_search says whether vectors-search can search one by text."
    ),
    input={"type": "object", "properties": {}},
    permission="vectors:read",
    pyai_tier="channel",
)
def list_collections(ctx, params):
    rows = list(collections_for(ctx).order_by("name"))
    return {"collections": [summary(c) for c in rows], "count": len(rows)}


@endpoint(
    "vectors.search",
    title="Search a vector collection",
    description=(
        "Similarity search in a vector collection: the query text is embedded with the "
        f"collection's embedding provider and the k nearest points (1-{MAX_K}, default 5) "
        "come back with their score (higher is closer), payload and text."
    ),
    input={
        "type": "object",
        "properties": {
            "collection": NAME,
            "query": {"type": "string", "maxLength": 8000, "description": "The text to search for"},
            "k": {"type": "integer", "minimum": 1, "maximum": MAX_K},
            "filter": FILTER,
        },
        "required": ["collection", "query"],
    },
    permission="vectors:read",
    rate=(30, 60),
    pyai_tier="channel",
)
def search(ctx, params):
    collection = resolve_collection(ctx, params["collection"])
    results = run(
        ctx,
        collection,
        lambda h: h.search(params["query"], k=params.get("k", 5), filter=params.get("filter")),
    )
    return {"collection": collection.name, "results": results, "count": len(results)}


@endpoint(
    "vectors.upsert",
    title="Upsert vector points",
    description=(
        f"Insert or replace up to {MAX_ITEMS} points of an existing collection, by id. "
        "Give every item a text (embedded with the collection's provider) or every item "
        "a vector of the collection's dimension (text is then optional); payload is an "
        "optional object."
    ),
    input={
        "type": "object",
        "properties": {
            "collection": NAME,
            "items": {
                "type": "array",
                "maxItems": MAX_ITEMS,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": POINT_ID,
                        "text": {"type": "string"},
                        "vector": {"type": "array", "items": {"type": "number"}, "maxItems": 8192},
                        "payload": {"type": "object"},
                    },
                    "required": ["id"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["collection", "items"],
    },
    access="write",
    permission="vectors:write",
    rate=(30, 60),
)
def upsert(ctx, params):
    collection = resolve_collection(ctx, params["collection"])
    items = params["items"]
    with_vectors = sum("vector" in item for item in items)
    if with_vectors not in (0, len(items)):
        raise ApiError(
            "invalid_input",
            "$.items: give every item a vector, or none (their text is embedded).",
            status=400,
        )
    if not with_vectors and any("text" not in item for item in items):
        raise ApiError("invalid_input", "$.items: an item without a vector needs a text.", status=400)

    def write(handle):
        if with_vectors:
            return {"upserted": handle.add_vectors(items)["added"], "tokens": 0}
        added = handle.add_texts(items)
        return {"upserted": added["added"], "tokens": added["tokens"]}

    result = run(ctx, collection, write) if items else {"upserted": 0, "tokens": 0}
    return {"collection": collection.name, **result, "points": collection.points_count}


@endpoint(
    "vectors.delete",
    title="Delete vector points",
    description=(
        f"Delete points of a collection by ids (up to {MAX_IDS}) or by a payload filter "
        "(not both, and not an empty filter). Ids that aren't there are skipped."
    ),
    input={
        "type": "object",
        "properties": {
            "collection": NAME,
            "ids": {"type": "array", "items": POINT_ID, "maxItems": MAX_IDS},
            "filter": FILTER,
        },
        "required": ["collection"],
    },
    access="write",
    permission="vectors:write",
    rate=(30, 60),
)
def delete(ctx, params):
    collection = resolve_collection(ctx, params["collection"])
    if ("ids" in params) == ("filter" in params):
        raise ApiError("invalid_input", "$: pass ids or filter, not both.", status=400)
    if "filter" in params and not params["filter"]:
        raise ApiError(
            "invalid_input", "$.filter: name at least one condition (an empty filter "
            "would match every point).", status=400,
        )
    deleted = run(
        ctx,
        collection,
        lambda h: h.delete(ids=params.get("ids"), filter=params.get("filter"))["deleted"],
    )
    return {"collection": collection.name, "deleted": deleted, "points": collection.points_count}
