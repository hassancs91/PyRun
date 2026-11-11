"""The smtp integration: send email through an SMTP connection (see core/integrations/framework.py)."""

from core.integrations.framework import Integration, Smoke


def _smoke_login(module, connection, config):
    """Connect, upgrade to TLS when set, log in and say NOOP; sends nothing."""
    with module.client(connection=connection) as server:
        server.noop()
    login = " and logged in" if config.get("username") else ""
    return f"connected to {config.get('host')}:{config.get('port')} ({config.get('security')}){login}; nothing sent"


EXAMPLE = '''\
from pyrunner_integrations import smtp

result = smtp.send(
    ["ada@example.com", "Grace Hopper <grace@example.com>"],
    "Monthly report",
    "Hi both,\\n\\nThe report is attached.",
    html="<p>Hi both,</p><p>The report is <b>attached</b>.</p>",
    attachments=["/tmp/report.pdf", ("totals.csv", b"month,total\\n2026-10,1200\\n")],
)
print("sent", result["message_id"], "refused:", result["refused"])
'''

INTEGRATION = Integration(
    key="smtp",
    provider="smtp",
    title="SMTP email",
    functions=("send", "client"),
    example=EXAMPLE,
    smoke=(Smoke("login", _smoke_login),),
)
