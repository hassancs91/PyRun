"""PyRun's own docs over REST, MCP and Py AI (2.5 Stage 3, T19): ``help.search``.

One read (``help:read``) that searches the documentation bundled with the
running version (``core.services.help_docs``) and answers with the best
sections: title, the headings above it, a snippet and a link to the section on
GitHub at the running version's tag. The docs are public and the same for every
workspace; nothing of the caller's workspace is read. Py AI on a chat channel
may call it (plan D9).
"""

from core.api.errors import ApiError
from core.api.registry import endpoint
from core.services import help_docs


@endpoint(
    "help.search",
    title="Search the PyRun docs",
    description=(
        "Search PyRun's own documentation (README, guides and changelog of the running "
        "version) for how to do something in PyRun. Returns the best sections, best first, "
        "each with its title, the headings above it, a snippet and a link to the section: "
        "answer from the snippet and give the link."
    ),
    input={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "maxLength": 200,
                "description": "A few words or a question, e.g. 'webhook signature'.",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 10,
                      "description": f"Sections to return (default {help_docs.DEFAULT_LIMIT})."},
        },
        "required": ["query"],
    },
    permission="help:read",
    pyai_tier="channel",
)
def search(ctx, params):
    query = params["query"].strip()
    if not query:
        raise ApiError("invalid_input", "$.query: say what to search for.")
    return help_docs.search(query, params.get("limit", help_docs.DEFAULT_LIMIT))
