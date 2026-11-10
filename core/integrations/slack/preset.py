"""Slack: a Slack app's bot token, installed in one Slack workspace."""

from core.integrations.presets import OAUTH2, OAuth, Preset, ScopeSet, Whoami, oauth_fields

OAUTH = OAuth(
    authorize_url="https://slack.com/oauth/v2/authorize",
    token_url="https://slack.com/api/oauth.v2.access",
    scope_sets=(
        ScopeSet("chat", "Post messages", ("chat:write",)),
        ScopeSet("channels", "Read public channels", ("channels:read", "channels:history")),
        ScopeSet("users", "Read people's names", ("users:read",)),
        ScopeSet("files", "Upload files", ("files:write",)),
    ),
    default_scope_sets=("chat",),
    scope_separator=",",
    pkce=False,
    console_url="https://api.slack.com/apps",
    paste_help=(
        "A bot token (xoxb-...) from the app's OAuth & Permissions page can be pasted as "
        "the access token; with token rotation on, paste the refresh token instead."
    ),
)

PRESET = Preset(
    key="slack",
    label="Slack",
    description="A Slack app installed in your Slack workspace: post messages, read channels.",
    fields={OAUTH2: oauth_fields(OAUTH)},
    base_url="https://slack.com/api",
    whoami=Whoami("/auth.test", label_keys=("team", "user")),
    docs_url="https://api.slack.com/authentication/oauth-v2",
    oauth=OAUTH,
)
