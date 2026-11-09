"""Registry endpoints as Py AI tools (2.5 Stage 3): one in-process bridge.

Py AI stays read-only (plan D9), so only ``read`` endpoints become tools. The
one exception is Brain's ``remember`` (a proposal waiting for human approval),
which ``core.brain.tools`` adds for an agent with proposal rights. Brain
endpoints come from there, after the agent's Brain grants are checked; this
module offers every other read endpoint.

Each endpoint declares its Py AI tier (``Endpoint.pyai_tier``). The console Py
AI (agent ``pyai``, open to workspace owners and admins) gets every read
endpoint. A chat-channel agent (``telegram:<channel id>``), whose approved
senders need not be owners or admins, gets the ``channel`` ones only. The call
context carries the tier and its permissions only, so the registry refuses
anything else on every call.

A tool takes its name (``runs-get``), description and input schema from the
registry and answers with the endpoint's result, masked as the endpoint masks
it for any caller. Calls run as principal ``agent``: like an API token, Py AI
sees the user behind a manual run as an id, never an email.
"""

import json
from dataclasses import replace

from asgiref.sync import sync_to_async
from claude_agent_sdk import tool
from django.core.serializers.json import DjangoJSONEncoder

from .context import CallContext
from .errors import ApiError
from .registry import registry

CONSOLE_AGENT = "pyai"
# An agent holds these through Brain grants (core.brain.tools), not its tier.
GRANTED_PERMISSIONS = frozenset({"brain:read", "brain:write"})
# The longest answer a registry tool hands the model, in characters (about 25k
# tokens). A run's output can be 1 MB a stream and a page of datastore entries
# more: a longer answer becomes a ``too_large`` tool error that says how to
# narrow it. Brain tools keep their own handlers' bounds.
MAX_RESULT_CHARS = 100_000
TOO_LARGE = (
    "The answer is {size} characters, more than Py AI reads at once ({limit}). "
    "Ask for less: runs-output takes tail, datastores-entries takes page_size."
)


def tier_for(agent):
    """``console`` for the console Py AI; every other agent is a chat channel."""
    return "console" if agent == CONSOLE_AGENT else "channel"


def permissions_for(tier):
    """The permissions of the read endpoints ``tier`` may call, Brain's aside."""
    return frozenset(
        spec.permission for spec in registry.reads() if spec.allows_tier(tier)
    ) - GRANTED_PERMISSIONS


def agent_context(workspace, agent):
    """A fresh call context for ``agent`` in ``workspace``, workspace-wide."""
    tier = tier_for(agent)
    return CallContext(
        workspace=workspace,
        principal="agent",
        principal_id=agent,
        permissions=permissions_for(tier),
        pyai_tier=tier,
    )


def with_tier(context, agent):
    """``context`` (a Brain principal context) narrowed to ``agent``'s tier."""
    return replace(context, pyai_tier=tier_for(agent))


def tool_error(exc):
    return {
        "isError": True,
        "content": [{"type": "text", "text": json.dumps({"error": exc.as_dict()})}],
    }


def as_tool(spec, context, name=None, max_chars=None):
    """An SDK tool that calls ``spec`` through the registry with ``context()``.

    ``context`` is called on every call, so a call gets its own request id and
    sees the grants as they are then (Brain). An ``ApiError``, or an answer
    longer than ``max_chars`` (when set), comes back as a tool error with a
    code and message.
    """

    @tool(name or spec.mcp_name, spec.description or spec.title, spec.input)
    async def call(params):
        try:
            data = await sync_to_async(registry.call)(spec.slug, context(), params)
        except ApiError as exc:
            return tool_error(exc)
        text = json.dumps(data, cls=DjangoJSONEncoder)
        if max_chars is not None and len(text) > max_chars:
            message = TOO_LARGE.format(size=len(text), limit=max_chars)
            return tool_error(ApiError("too_large", message, status=413))
        return {"content": [{"type": "text", "text": text}]}

    return call


def build_tools(workspace, *, agent=CONSOLE_AGENT):
    """The registry read tools ``agent``'s tier may call in ``workspace`` (no query)."""
    ctx = agent_context(workspace, agent)
    return [
        as_tool(spec, lambda: agent_context(workspace, agent), max_chars=MAX_RESULT_CHARS)
        for spec in registry.visible(ctx)
        if spec.access == "read" and spec.allows_tier(ctx.pyai_tier)
    ]
