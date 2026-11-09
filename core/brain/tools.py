"""Grant-filtered, in-process tools; each execution rechecks current grants.

Built with the registry bridge (``core.api.tools``), which gives Py AI every
other read endpoint its tier may call.
"""

from core.api.registry import registry
from core.api.tools import as_tool, tier_for, with_tier
from .access import accessible_brains, access_for, principal_context


def build_tools(workspace, *, agent="pyai"):
    ctx = with_tier(principal_context(workspace, "agent", agent), agent)
    if not accessible_brains(ctx).exists():
        return []
    can_propose = any(access_for(ctx, brain).can_propose for brain in accessible_brains(ctx))

    def context():
        return with_tier(principal_context(workspace, "agent", agent), agent)

    tier = tier_for(agent)
    result = [
        as_tool(spec, context)
        for spec in registry.visible(ctx)
        if spec.slug.startswith("brain.")
        and (spec.access == "read" or can_propose)
        and spec.allows_tier(tier)
    ]
    if can_propose:
        result.append(as_tool(registry.get("brain.propose-feed"), context, "remember"))
    return result
