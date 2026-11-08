"""Brain reads and source intake through shared grant-checked handlers."""

from core.brain.access import accessible_brains, access_for, resolve
from core.brain.reads import read
from core.api.registry import endpoint
from core.brain.intake import INTAKE_PROPERTIES, propose


def string(max_length):
    return {"type": "string", "maxLength": max_length}


def brain_input(properties=None, required=()):
    return {
        "type": "object",
        "properties": {"brain": string(64), **(properties or {})},
        "required": list(required),
    }


@endpoint(
    "brain.list",
    title="List brains",
    description="Brains accessible to this caller.",
    input={
        "type": "object",
        "properties": {
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
        },
    },
    permission="brain:read",
)
def list_brains(ctx, params):
    offset, limit = params.get("offset", 0), params.get("limit", 1000)
    rows = list(accessible_brains(ctx).order_by("slug")[offset : offset + limit + 1])
    return {
        "brains": [
            {"slug": b.slug, "name": b.name, "tier": access_for(ctx, b).tier} for b in rows[:limit]
        ],
        "next_offset": offset + limit if len(rows) > limit else None,
    }


@endpoint(
    "brain.ping",
    title="Ping brain",
    description="Check that this token can reach the brain; returns your tier.",
    input=brain_input(),
    permission="brain:read",
)
def ping(ctx, params):
    _, access = resolve(ctx, params)
    return {"pong": True, "service": "pyrunner", "tier": access.tier}


def register_read(operation, title, description, properties=None, required=()):
    def handler(ctx, params):
        return read(ctx, params, operation)

    return endpoint(
        f"brain.{operation}",
        title=title,
        description=description,
        input=brain_input(properties, required),
        permission="brain:read",
    )(handler)


get_index = register_read("get-index", "Get brain index", "Generated INDEX.md at your tier.")
list_notes = register_read(
    "list-notes",
    "List brain notes",
    "Filter visible notes; current excludes superseded.",
    {key: string(200) for key in ("kind", "topic", "project", "status")},
)
get_note = register_read(
    "get-note",
    "Get brain note",
    "Read a visible note by entity id.",
    {"entity_id": string(200)},
    ("entity_id",),
)
get_lens = register_read(
    "get-lens", "Get brain lens", "Read a visible lens by name.", {"name": string(100)}, ("name",)
)
get_identity = register_read("get-identity", "Get brain identity", "Read visible identity files.")
get_raw = register_read(
    "get-raw",
    "Get brain raw file",
    "Read a raw archive linked by a visible note.",
    {"path": string(300)},
    ("path",),
)


# A proposal waits for a human to approve it, so it is a "run" (always available
# with brain:write and a proposing grant), not a write behind the writes toggle.
@endpoint("brain.propose-feed", title="Propose brain memory",
          description="Capture a source for extraction and human approval. Does not change notes.",
          input=brain_input(INTAKE_PROPERTIES), access="run", permission="brain:write", rate=(20, 60))
def propose_feed(ctx, params):
    return propose(ctx, params)


@endpoint("brain.assemble-context", title="Assemble brain context",
          description="Read a small set of visible notes and assemble a verified context pack.",
          input=brain_input({"task": string(8000), "lens": string(100)}, ("task",)),
          permission="brain:read", rate=(10, 60))
def assemble_context(ctx, params):
    from core.brain.reader import assemble

    return assemble(ctx, params)


@endpoint("brain.search", title="Search brain notes", description="Optional semantic recall at your visibility tier.",
          input=brain_input({"query": string(8000), "k": {"type": "integer", "minimum": 1, "maximum": 20}}, ("query",)),
          permission="brain:read", rate=(20, 60))
def search(ctx, params):
    from core.brain.recall import search as recall

    return recall(ctx, params)
