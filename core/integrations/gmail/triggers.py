"""
Watcher adapters of the gmail integration (see ``framework.Trigger``).

``gmail_new_message``: a run per email that comes in (with a label, or
matching a search), through ``gmail.new_messages``. Triggers (2.5 Stage 7)
runs it; until then nothing does.
"""

from core.integrations.framework import Trigger

CONFIG_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "label": {
            "type": "string", "title": "Label",
            "description": "Only mail with this label (a name, or INBOX, STARRED, IMPORTANT...), also mail that "
                           "gets it later. Empty: all mail that comes in.",
        },
        "query": {
            "type": "string", "title": "Search",
            "description": "Only mail matching this Gmail search (\"from:billing@example.com has:attachment\").",
        },
    },
}
# What the run's event gets of a message: no bodies or full header list (the run reads them with get_message).
ITEM_KEYS = ("id", "threadId", "labelIds", "snippet", "internalDate", "subject", "from", "to", "cc", "date",
             "message_id")


def check_new_messages(module, connection, config, state, limit=None):
    messages, new_state = module.new_messages(state, label=config.get("label") or None,
                                              query=config.get("query") or None, limit=limit,
                                              connection=connection)
    events = [{"id": message["id"], "item": {key: message.get(key) for key in ITEM_KEYS}} for message in messages]
    return events, new_state


TRIGGERS = (
    Trigger(key="gmail_new_message", title="New email in Gmail", config_schema=CONFIG_SCHEMA,
            check=check_new_messages),
)
