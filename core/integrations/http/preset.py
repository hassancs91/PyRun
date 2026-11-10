"""Any HTTP API: an API key, a bearer token, or a username and password."""

from core.integrations.presets import API_KEY, BASIC, BEARER, Field, Preset

BASE_URL = Field(
    "base_url",
    "Base URL",
    kind="url",
    placeholder="https://api.example.com",
    help="Requests to this host get the credentials; requests anywhere else don't.",
)
TEST_PATH = Field(
    "test_path",
    "Test path",
    required=False,
    placeholder="/v1/me",
    help="What Test calls (GET, relative to the base URL). Blank: the base URL itself.",
)

PRESET = Preset(
    key="http",
    label="HTTP API",
    description="Any HTTP API that takes an API key, a bearer token or basic auth.",
    fields={
        API_KEY: (
            BASE_URL,
            Field("api_key", "API key", secret=True),
            Field(
                "location",
                "Send the key in",
                kind="choice",
                choices=(("header", "A header"), ("query", "A query parameter")),
                default="header",
            ),
            Field(
                "param_name",
                "Header or parameter name",
                default="X-API-Key",
                placeholder="X-API-Key",
            ),
            Field(
                "prefix",
                "Value prefix",
                required=False,
                placeholder="Token ",
                help="Put in front of the key, e.g. 'Token ' (the space counts). Usually blank.",
                strip=False,
            ),
            TEST_PATH,
        ),
        BEARER: (BASE_URL, Field("token", "Token", secret=True), TEST_PATH),
        BASIC: (
            BASE_URL,
            Field("username", "Username"),
            Field("password", "Password", secret=True),
            TEST_PATH,
        ),
    },
)
