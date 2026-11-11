"""
``http_poll``: a run per new entry in a JSON list behind a URL.

The adapter GETs the URL, finds the list (``items_path``, dotted; empty = the
whole response), and reads each entry's id at ``id_path``. Entries whose id it
hasn't seen start runs, oldest first (``newest_first`` says which end of the
list is new). With ``since_param`` set, every request after the first adds
``?<since_param>=<value>``, the value being the ``since_path`` field (default:
the id) of the newest entry reported so far, for APIs that take a cursor.

State: ``{"ids": [<ids seen, newest last>], "since": <value or None>}``.
No authentication in this version: for a source that needs a key, a script
on a schedule with ``pyrunner_integrations.http`` and a Connection is the way.
"""

import json

from . import _net
from .base import TEST_ITEMS, Adapter, AdapterError, dig, with_defaults

# Ids remembered per watcher (the oldest are forgotten first).
IDS_MAX = 5000


def _id_text(value):
    """An entry id as text (None when the entry has none)."""
    if value is None or isinstance(value, (dict, list)):
        return None
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


class HttpPollAdapter(Adapter):
    key = "http_poll"
    title = "New entry in a JSON list (HTTP)"
    description = (
        "Polls a URL that answers with JSON and starts a run for each entry of its list "
        "that wasn't there before."
    )
    config_schema = {
        "type": "object",
        "required": ["url", "id_path"],
        "additionalProperties": False,
        "properties": {
            "url": {
                "type": "string", "maxLength": 2000, "title": "URL",
                "description": "Answers a GET with JSON (no sign-in).",
            },
            "items_path": {
                "type": "string", "maxLength": 200, "title": "List path", "default": "",
                "description": "Where the list is in the JSON, dotted (\"data.items\"). Empty: the "
                               "whole response is the list.",
            },
            "id_path": {
                "type": "string", "maxLength": 200, "title": "Id path", "default": "id",
                "description": "The field of an entry that identifies it, dotted (\"id\", \"meta.uid\").",
            },
            "newest_first": {
                "type": "boolean", "title": "Newest entries first", "default": True,
                "description": "The list starts with the newest entry (most APIs). Off: it ends with it.",
            },
            "since_param": {
                "type": "string", "maxLength": 100, "title": "Since parameter", "default": "",
                "description": "Optional query parameter that asks only for entries after a value "
                               "(\"since\", \"after\").",
            },
            "since_path": {
                "type": "string", "maxLength": 200, "title": "Since value path", "default": "",
                "description": "The entry field sent as the since parameter, dotted. Empty: the id.",
            },
        },
    }

    def _entries(self, ctx, config, since=None):
        config = with_defaults(self.config_schema, config)
        params = None
        if config.get("since_param") and since is not None:
            params = {config["since_param"]: since}
        body, _ = _net.get(config["url"], params=params, accept="application/json", timeout=ctx.timeout)
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise AdapterError("The response isn't JSON.") from None
        entries = dig(data, config.get("items_path") or "")
        if not isinstance(entries, list):
            where = f'at "{config["items_path"]}"' if config.get("items_path") else "as the whole response"
            raise AdapterError(f"No JSON list {where}.")
        if config.get("newest_first", True):
            entries = list(reversed(entries))
        # Oldest first; entries without an id can't be told apart, so skip them.
        keyed = []
        for entry in entries:
            entry_id = _id_text(dig(entry, config["id_path"]))
            if entry_id is not None:
                keyed.append((entry_id, entry))
        return config, keyed

    def _since(self, config, entry):
        return _id_text(dig(entry, config.get("since_path") or config["id_path"]))

    def check(self, ctx, config, state):
        state = state if isinstance(state, dict) else None
        since = state.get("since") if state else None
        config, keyed = self._entries(ctx, config, since)
        if state is None:
            ids = [entry_id for entry_id, _ in keyed][-IDS_MAX:]
            mark = self._since(config, keyed[-1][1]) if keyed else None
            return [], {"ids": ids, "since": mark}
        known = list(state.get("ids") or [])
        seen = set(known)
        events = []
        for entry_id, entry in keyed:
            if entry_id in seen:
                continue
            if ctx.limit is not None and len(events) >= ctx.limit:
                break
            seen.add(entry_id)
            known.append(entry_id)
            events.append({"id": entry_id, "item": entry})
        if events:
            since = self._since(config, events[-1]["item"])
        return events, {"ids": known[-IDS_MAX:], "since": since}

    def test(self, ctx, config):
        config, keyed = self._entries(ctx, config)
        if not keyed:
            return "The list is there but empty (or no entry has an id).", []
        newest = keyed[-1][0]
        count = len(keyed)
        message = f"Found {count} entr{'y' if count == 1 else 'ies'} with an id; the newest is {newest!r}."
        return message, [entry for _, entry in reversed(keyed[-TEST_ITEMS:])]
