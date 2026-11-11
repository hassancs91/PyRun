"""A mail server's IMAP login: the imap_new_message watcher reads mail with it."""

from core.integrations.presets import BASIC, SMTP_SECURITY, Field, Preset

PRESET = Preset(
    key="imap",
    label="IMAP mailbox",
    description="A mail server's IMAP login (host, username and password or app password).",
    protocol="imap",
    fields={
        BASIC: (
            Field("host", "Host", placeholder="imap.example.com"),
            Field("port", "Port", kind="int", default=993),
            Field("security", "Security", kind="choice", choices=SMTP_SECURITY, default="ssl"),
            Field("username", "Username"),
            Field(
                "password",
                "Password",
                secret=True,
                help="The account's password, or an app password where the provider asks for one (Gmail, iCloud...).",
            ),
        ),
    },
)
