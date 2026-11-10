"""Microsoft 365: an Entra ID (Azure AD) account, through Microsoft Graph."""

from core.integrations.presets import OAUTH2, Field, OAuth, Preset, ScopeSet, Whoami, oauth_fields

OAUTH = OAuth(
    authorize_url="https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
    token_url="https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
    scope_sets=(
        ScopeSet("mail", "Mail: read and send", ("Mail.ReadWrite", "Mail.Send")),
        ScopeSet("calendars", "Calendars", ("Calendars.ReadWrite",)),
        ScopeSet("files", "OneDrive and Excel files", ("Files.ReadWrite.All",)),
    ),
    default_scope_sets=("mail",),
    # offline_access: a refresh token. User.Read: the whoami call.
    base_scopes=("offline_access", "User.Read"),
    client_fields=(
        Field(
            "tenant",
            "Tenant",
            required=False,
            default="common",
            help="Your directory (tenant) ID for a single-tenant app; blank = common (any account).",
        ),
    ),
    console_url="https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade",
)

PRESET = Preset(
    key="microsoft",
    label="Microsoft 365",
    description="A Microsoft work or personal account: Outlook mail, calendars and OneDrive files.",
    fields={OAUTH2: oauth_fields(OAUTH)},
    base_url="https://graph.microsoft.com",
    whoami=Whoami("/v1.0/me", label_keys=("mail", "userPrincipalName")),
    docs_url="https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow",
    oauth=OAUTH,
)
