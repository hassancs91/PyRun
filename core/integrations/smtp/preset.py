"""An SMTP server login, for scripts that send email."""

from core.integrations.presets import SMTP, SMTP_SECURITY, Field, Preset

PRESET = Preset(
    key="smtp",
    label="SMTP",
    description="An SMTP server and login, e.g. your mail provider's or a transactional service's.",
    fields={
        SMTP: (
            Field("host", "Host", placeholder="smtp.example.com"),
            Field("port", "Port", kind="int", default=587),
            Field("security", "Security", kind="choice", choices=SMTP_SECURITY, default="starttls"),
            Field("username", "Username", required=False),
            Field("password", "Password", secret=True, required=False),
            Field(
                "from_address",
                "From address",
                kind="email",
                required=False,
                help="The sender scripts use unless they set their own.",
            ),
        ),
    },
)
