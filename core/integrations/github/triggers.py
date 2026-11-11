"""
Watcher adapters of the github integration (see ``framework.Trigger``).

``github_new_issue``: a run per issue opened in a repository (optionally only
those with some labels), through ``github.new_issues``. Triggers (2.5 Stage 7)
runs it; until then nothing does.
"""

from core.integrations.framework import Trigger

CONFIG_SCHEMA = {
    "type": "object",
    "required": ["repo"],
    "additionalProperties": False,
    "properties": {
        "repo": {
            "type": "string", "pattern": r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", "title": "Repository",
            "description": "owner/name, as in the repository's URL (\"octo/shop\").",
        },
        "labels": {
            "type": "string", "title": "Labels",
            "description": "Only issues with every one of these labels (comma-separated) when the watcher "
                           "looks. Empty: every new issue.",
        },
    },
}
# What the run's event gets of an issue (the run reads the rest with get_issue).
ITEM_KEYS = ("id", "number", "title", "body", "state", "html_url", "created_at")


def _labels(config):
    return [name.strip() for name in (config.get("labels") or "").split(",") if name.strip()] or None


def check_new_issues(module, connection, config, state, limit=None):
    issues, new_state = module.new_issues(config["repo"], state, labels=_labels(config), limit=limit,
                                          connection=connection)
    events = [{
        "id": str(issue["id"]),
        "item": {
            "repo": config["repo"],
            **{key: issue.get(key) for key in ITEM_KEYS},
            "user": (issue.get("user") or {}).get("login"),
            "labels": [label.get("name") if isinstance(label, dict) else label for label in issue.get("labels") or []],
            "assignees": [person.get("login") for person in issue.get("assignees") or []],
        },
    } for issue in issues]
    return events, new_state


TRIGGERS = (
    Trigger(key="github_new_issue", title="New issue on GitHub", config_schema=CONFIG_SCHEMA,
            check=check_new_issues),
)
