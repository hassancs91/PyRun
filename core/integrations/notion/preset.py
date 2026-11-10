"""Notion: a public integration's OAuth token, or an internal integration's secret."""

from core.integrations.presets import OAUTH2, OAuth, Preset, Whoami, oauth_fields

OAUTH = OAuth(
    authorize_url="https://api.notion.com/v1/oauth/authorize",
    token_url="https://api.notion.com/v1/oauth/token",
    # No scopes: what the integration may do is set on the integration, and
    # the person picks the pages it gets on Notion's consent screen.
    authorize_params=(("owner", "user"),),
    pkce=False,
    client_auth="basic",
    token_format="json",
    console_url="https://www.notion.so/profile/integrations",
    paste_help=(
        "An internal integration's secret (ntn_...) doesn't expire: paste it as the access "
        "token, and share the pages it needs with the integration in Notion."
    ),
)

PRESET = Preset(
    key="notion",
    label="Notion",
    description="A Notion integration: the pages and databases it was given.",
    fields={OAUTH2: oauth_fields(OAUTH)},
    base_url="https://api.notion.com",
    whoami=Whoami("/v1/users/me", label_keys=("bot.workspace_name", "name")),
    docs_url="https://developers.notion.com/docs/authorization",
    oauth=OAUTH,
    default_headers=(("Notion-Version", "2022-06-28"),),
)
