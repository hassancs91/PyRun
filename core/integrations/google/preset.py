"""Google: one OAuth connection, with scope sets for Sheets, Gmail, Drive and Calendar."""

from core.integrations.presets import OAUTH2, OAuth, Preset, ScopeSet, Whoami, oauth_fields

_AUTH = "https://www.googleapis.com/auth/"

OAUTH = OAuth(
    authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
    token_url="https://oauth2.googleapis.com/token",
    scope_sets=(
        ScopeSet("sheets", "Sheets", (_AUTH + "spreadsheets",)),
        ScopeSet("gmail_read", "Gmail: read", (_AUTH + "gmail.readonly",)),
        ScopeSet("gmail_send", "Gmail: send", (_AUTH + "gmail.send",)),
        ScopeSet("gmail_modify", "Gmail: read, label and send", (_AUTH + "gmail.modify",)),
        ScopeSet("drive_file", "Drive: files it made or opened", (_AUTH + "drive.file",)),
        ScopeSet("drive", "Drive: all files", (_AUTH + "drive",)),
        ScopeSet("calendar", "Calendar", (_AUTH + "calendar",)),
    ),
    default_scope_sets=("sheets",),
    # What the whoami call (the account's email) needs.
    base_scopes=("openid", "email"),
    # offline: Google gives a refresh token. consent: it gives one every time,
    # not only on the account's first consent to this app.
    authorize_params=(
        ("access_type", "offline"),
        ("prompt", "consent"),
        ("include_granted_scopes", "true"),
    ),
    console_url="https://console.cloud.google.com/apis/credentials",
    paste_help=(
        "No public callback? Get a refresh token for your own OAuth client from the "
        "OAuth 2.0 Playground (developers.google.com/oauthplayground, settings: use your "
        "own OAuth credentials) and paste it here."
    ),
)

PRESET = Preset(
    key="google",
    label="Google",
    description="A Google account: Sheets, Gmail, Drive and Calendar through one OAuth connection.",
    fields={OAUTH2: oauth_fields(OAUTH)},
    base_url="https://www.googleapis.com",
    whoami=Whoami("/oauth2/v3/userinfo", label_keys=("email", "sub")),
    docs_url="https://developers.google.com/identity/protocols/oauth2/web-server",
    oauth=OAUTH,
)
