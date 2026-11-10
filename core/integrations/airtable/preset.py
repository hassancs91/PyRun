"""Airtable: a personal access token, sent as a bearer token."""

from core.integrations.presets import API_KEY, Field, Preset, Whoami

PRESET = Preset(
    key="airtable",
    label="Airtable",
    description="An Airtable personal access token (airtable.com/create/tokens).",
    fields={
        API_KEY: (
            Field(
                "api_key",
                "Personal access token",
                secret=True,
                placeholder="pat...",
                help="Give it the scopes and bases your scripts need.",
            ),
        ),
    },
    base_url="https://api.airtable.com",
    api_key_header=("Authorization", "Bearer "),
    whoami=Whoami("/v0/meta/whoami", label_keys=("email", "id")),
    docs_url="https://airtable.com/developers/web/api/authentication",
)
