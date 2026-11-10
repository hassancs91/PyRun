"""GitHub: a GitHub account through an OAuth app (or a pasted personal access token)."""

from core.integrations.presets import OAUTH2, OAuth, Preset, ScopeSet, Whoami, oauth_fields

OAUTH = OAuth(
    authorize_url="https://github.com/login/oauth/authorize",
    token_url="https://github.com/login/oauth/access_token",
    scope_sets=(
        ScopeSet("repo", "Repositories, private ones too", ("repo",)),
        ScopeSet("public_repo", "Public repositories", ("public_repo",)),
        ScopeSet("workflow", "Actions workflows", ("workflow",)),
        ScopeSet("gist", "Gists", ("gist",)),
        ScopeSet("notifications", "Notifications", ("notifications",)),
    ),
    default_scope_sets=("public_repo",),
    base_scopes=("read:user",),
    console_url="https://github.com/settings/developers",
    paste_help=(
        "An OAuth app's tokens don't expire, so a personal access token "
        "(github.com/settings/tokens) pasted as the access token works too."
    ),
)

PRESET = Preset(
    key="github",
    label="GitHub",
    description="A GitHub account: repositories, issues, Actions workflows and gists.",
    fields={OAUTH2: oauth_fields(OAUTH)},
    base_url="https://api.github.com",
    whoami=Whoami("/user", label_keys=("login",)),
    docs_url="https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps",
    oauth=OAUTH,
)
