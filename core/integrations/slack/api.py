"""The slack integration: messages, files and channels in a Slack workspace (see core/integrations/framework.py)."""

from core.integrations.framework import Integration, Smoke


def _smoke_auth(module, connection, config):
    """Who the token is (auth.test, which every token may call), then the channels when its scopes allow."""
    response = module.client(connection).request("POST", "/auth.test")
    body = response.json()
    module._check(response, body)
    scopes = [s.strip() for s in (response.headers.get("x-oauth-scopes") or "").split(",") if s.strip()]
    line = f"auth.test answered for workspace {body.get('team')!r} with {len(scopes)} scope(s)"
    if "channels:read" in scopes:
        channels = list(module.list_channels(limit=5, connection=connection))
        line += f"; list_channels answered with {len(channels)} channel(s) (first 5)"
    return line


EXAMPLE = '''\
from pyrunner_integrations import slack

# A message, then a reply in its thread (the app must be in #alerts: /invite @your-app).
sent = slack.post_message("#alerts", "*Disk almost full* on web-1 (92%)")
slack.post_message("#alerts", "Cleaned up the logs: 61% now.", thread_ts=sent["ts"])

# Edit the first message once it is handled.
slack.update_message(sent["channel"], sent["ts"], ":white_check_mark: Disk on web-1 back to 61%")

# A file, shared in a channel with a comment.
slack.upload_file("/tmp/report.csv", "#reports", comment="This week's signups")

# The last messages of a channel (by id), newest first.
for message in slack.history("C0123ABCD", limit=10):
    print(message["ts"], message.get("text"))
'''

INTEGRATION = Integration(
    key="slack",
    provider="slack",
    title="Slack",
    functions=("post_message", "update_message", "upload_file", "list_channels", "find_channel", "history",
               "list_users", "client"),
    example=EXAMPLE,
    smoke=(Smoke("auth", _smoke_auth),),
)
