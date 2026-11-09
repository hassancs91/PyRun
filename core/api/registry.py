"""One declaration, handler and validation path for REST, MCP and internal tools."""

import copy
import json
import logging
import re
import time
from dataclasses import dataclass

from django.core.serializers.json import DjangoJSONEncoder

from core.ratelimit import rate_limit_exceeded, seconds_to_window_reset
from .errors import ApiError
from .schema import check_schema, validate

logger = logging.getLogger(__name__)
SLUG = re.compile(r"^[a-z][a-z0-9_-]*\.[a-z][a-z0-9_-]*$")

# Endpoint classes (plan D8). ``read`` changes nothing; ``run`` acts without
# changing stored data (brain.propose-feed: a proposal waits for human
# approval; scripts.run and runs.stop) and is always available with its
# permission; ``write`` changes
# stored data and is available only while the superuser setting
# ``GlobalSettings.api_writes_enabled`` is on.
ACCESS_CLASSES = ("read", "run", "write")
WRITES_DISABLED = (
    "API writes are turned off on this instance. A superuser can turn them on "
    "in Settings → API & MCP."
)
# Py AI tiers (plan D9), declared per endpoint (``Endpoint.pyai_tier``). The
# console Py AI, open to workspace owners and admins, may call every read
# endpoint; a chat-channel agent (Telegram), whose approved senders need not
# be either, only the ones declared ``channel``. ``core.api.tools`` builds the
# tools; ``Registry.call`` checks the tier of a Py AI caller again.
PYAI_TIERS = ("console", "channel")


def writes_enabled():
    """Whether write endpoints are on (one settings read, done only when needed)."""
    from core.models import GlobalSettings

    return bool(GlobalSettings.get_settings().api_writes_enabled)


@dataclass(frozen=True)
class Endpoint:
    slug: str
    handler: object
    input: dict
    title: str
    description: str
    access: str
    permission: str
    rate: tuple[int, int]
    # When set, REST answers with this string field of the result as
    # text/plain instead of the JSON envelope (runs.output). MCP and
    # in-process callers still get the whole dict.
    text: str = ""
    # When set, REST answers 202 Accepted instead of 200 while this boolean
    # field of the result is false (scripts.run: a run that hasn't ended).
    done: str = ""
    # The Py AI tiers that may call it (PYAI_TIERS): ``console`` (the default)
    # for the console only, ``channel`` for chat channels too.
    pyai_tier: str = "console"

    def allows_tier(self, tier):
        """Whether a caller of Py AI ``tier`` may call this (``None``: not Py AI)."""
        return tier in (None, "console") or self.pyai_tier == "channel"

    @property
    def mcp_name(self):
        return self.slug.replace(".", "-")

    def catalog_entry(self):
        entry = {
            "slug": self.slug,
            "mcp_name": self.mcp_name,
            "title": self.title,
            "description": self.description,
            "inputSchema": copy.deepcopy(self.input),
            "access": self.access,
            "permission": self.permission,
        }
        if self.text:
            entry["rest_response"] = "text/plain"
        if self.done:
            entry["rest_accepted"] = f"202 while `{self.done}` is false"
        return entry


class Registry:
    def __init__(self):
        self._endpoints = {}

    def register(self, spec):
        if not SLUG.fullmatch(spec.slug) or len(spec.slug) > 64:
            raise ValueError("Endpoint slug must be a namespace.name of at most 64 characters")
        if spec.access not in ACCESS_CLASSES or not spec.permission:
            raise ValueError("Endpoint access (read, run or write) and permission are required")
        if spec.pyai_tier not in PYAI_TIERS:
            raise ValueError("Endpoint pyai_tier must be console or channel")
        # A write permission unlocks writes and nothing else, so refusing it
        # while writes are off never takes a read or run endpoint away.
        writes = {e.permission for e in self._endpoints.values() if e.access == "write"}
        others = {e.permission for e in self._endpoints.values() if e.access != "write"}
        if spec.access == "write" and (
            not spec.permission.endswith(":write") or spec.permission in others
        ):
            raise ValueError("A write endpoint needs its own <namespace>:write permission")
        if spec.access != "write" and spec.permission in writes:
            raise ValueError("A write permission can only guard write endpoints")
        if len(spec.rate) != 2 or any(type(v) is not int or v <= 0 for v in spec.rate):
            raise ValueError("Endpoint rate must be (positive calls, positive seconds)")
        check_schema(spec.input)
        if spec.input["type"] != "object":
            raise ValueError("Endpoint input must be an object")
        if any(e.mcp_name == spec.mcp_name for e in self._endpoints.values()):
            raise ValueError("Duplicate endpoint or MCP tool name")
        self._endpoints[spec.slug] = spec

    def get(self, slug):
        return self._endpoints.get(slug)

    def permitted(self, ctx):
        """Endpoints this caller holds the permission for, writes included."""
        return [
            e
            for e in sorted(self._endpoints.values(), key=lambda e: e.slug)
            if e.permission in ctx.permissions
        ]

    def visible(self, ctx):
        """What a caller may list: its permitted endpoints, minus writes while off."""
        permitted = self.permitted(ctx)
        if any(e.access == "write" for e in permitted) and not writes_enabled():
            return [e for e in permitted if e.access != "write"]
        return permitted

    def write_permissions(self):
        """Permissions that guard write endpoints (not grantable while writes are off)."""
        return frozenset(e.permission for e in self._endpoints.values() if e.access == "write")

    def read_permissions(self):
        """Permissions that guard read endpoints only (all URL tokens may hold)."""
        others = {e.permission for e in self._endpoints.values() if e.access != "read"}
        return frozenset(
            e.permission for e in self._endpoints.values() if e.permission not in others
        )

    def writes(self):
        """The registered write endpoints, by slug."""
        return sorted(
            (e for e in self._endpoints.values() if e.access == "write"), key=lambda e: e.slug
        )

    def reads(self):
        """The registered read endpoints, by slug."""
        return sorted(
            (e for e in self._endpoints.values() if e.access == "read"), key=lambda e: e.slug
        )

    def call(self, slug, ctx, params):
        started, ok = time.monotonic(), False
        try:
            ctx.check_budget()
            spec = self.get(slug)
            if spec is None:
                raise ApiError("not_found", "Unknown endpoint.", status=404)
            if (
                ctx.workspace is None
                or spec.permission not in ctx.permissions
                or not spec.allows_tier(ctx.pyai_tier)
            ):
                raise ApiError("forbidden", "Permission denied.", status=403)
            if spec.access == "write" and not writes_enabled():
                raise ApiError("writes_disabled", WRITES_DISABLED, status=403)
            if ctx.transport in {"rest", "mcp"}:
                key = f"api_call_{ctx.principal}_{ctx.principal_id}_{slug}"
                if rate_limit_exceeded(key, *spec.rate):
                    raise ApiError(
                        "rate_limited",
                        "Rate limit exceeded.",
                        status=429,
                        retry_after=seconds_to_window_reset(spec.rate[1]),
                    )
            validate(params, spec.input)
            result = spec.handler(ctx, params)
            ctx.check_budget()
            if not isinstance(result, dict):
                raise TypeError("Endpoint must return a dict")
            json.dumps(result, cls=DjangoJSONEncoder, allow_nan=False)
            ok = True
            return result
        except ApiError:
            raise
        except Exception as exc:
            logger.exception("api.error request_id=%s slug=%s", ctx.request_id, slug)
            raise ApiError(
                "internal_error", "The request could not be completed.", status=500
            ) from exc
        finally:
            logger.info(
                "api.call slug=%s principal=%s:%s ms=%d ok=%s request_id=%s",
                slug,
                ctx.principal,
                ctx.principal_id,
                int((time.monotonic() - started) * 1000),
                ok,
                ctx.request_id,
            )


registry = Registry()


def endpoint(
    slug, *, title, description="", input, access="read", permission, rate=(60, 60), text="",
    done="", pyai_tier="console",
):
    def decorate(handler):
        schema = copy.deepcopy(input)
        schema.setdefault("additionalProperties", False)
        registry.register(
            Endpoint(
                slug, handler, schema, title, description, access, permission, rate, text, done,
                pyai_tier,
            )
        )
        return handler

    return decorate
