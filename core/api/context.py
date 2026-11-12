"""Transport-independent, workspace-scoped call identity."""

import time
import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True)
class CallContext:
    workspace: object
    principal: str
    permissions: frozenset[str]
    principal_id: str = ""
    resource: object = None
    tier: str | None = None
    # The one script a bound token may see (APIToken.script_id); None = any.
    # Handlers look it up inside ``workspace``, so a mismatch sees nothing.
    script_id: object = None
    # The one datastore a bound token may reach (APIToken.datastore_id; a
    # legacy single-store token's store); None = any in ``workspace``.
    datastore_id: object = None
    # The one table a bound token may reach (APIToken.table_id); None = any
    # in ``workspace``.
    table_id: object = None
    # Py AI's tier for an in-process agent call (core.api.tools): "console" or
    # "channel"; the registry refuses an endpoint the tier may not call.
    # None for every other caller.
    pyai_tier: str | None = None
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    transport: str = "in-process"
    deadline: float | None = None

    def remaining_seconds(self):
        return None if self.deadline is None else max(0, self.deadline - time.monotonic())

    def check_budget(self):
        from .errors import ApiError

        if self.deadline is not None and self.remaining_seconds() <= 0:
            raise ApiError("timeout", "The call exceeded its time budget.", status=504)


def token_context(token, *, transport="rest", deadline=None):
    return CallContext(
        workspace=token.workspace,
        principal="token",
        principal_id=str(token.pk),
        permissions=frozenset(token.permissions),
        resource=token.brain,
        tier=token.brain_tier,
        script_id=token.script_id,
        datastore_id=token.datastore_id,
        table_id=token.table_id,
        transport=transport,
        deadline=deadline,
    )
