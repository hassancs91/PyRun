"""
``rss_new_item``: a run per new item of an RSS or Atom feed.

Reads RSS 2.0 (``channel/item``), RSS 1.0 (``item``) and Atom (``entry``).
An item's id is its ``guid`` / Atom ``id``, else its link, else a hash of its
title and date. Feeds list the newest item first; new items start runs oldest
first.

State: ``{"ids": [<ids seen, newest last>]}``.
"""

import hashlib
import re
import xml.etree.ElementTree as ET

from . import _net
from .base import TEST_ITEMS, Adapter, AdapterError

IDS_MAX = 5000
# Text kept of an item's summary.
SUMMARY_MAX_CHARS = 10_000
_ENTITY_RE = re.compile(rb"<!ENTITY", re.IGNORECASE)


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(element, *names):
    for child in element:
        if _local(child.tag) in names:
            return child
    return None


def _text(element, *names) -> str:
    child = _child(element, *names)
    if child is None:
        return ""
    return "".join(child.itertext()).strip()


def _link(element) -> str:
    # Atom: <link href="..." rel="alternate"/>; RSS: <link>text</link>.
    fallback = ""
    for child in element:
        if _local(child.tag) != "link":
            continue
        href = child.get("href")
        if href is None:
            text = (child.text or "").strip()
            if text:
                return text
            continue
        if child.get("rel", "alternate") == "alternate":
            return href
        fallback = fallback or href
    return fallback


def _item(element) -> dict:
    title = _text(element, "title")
    link = _link(element)
    published = _text(element, "pubDate", "published", "updated", "date")
    summary = _text(element, "description", "summary", "content", "encoded")[:SUMMARY_MAX_CHARS]
    author = _text(element, "author", "creator")
    author_el = _child(element, "author")
    if author_el is not None and _child(author_el, "name") is not None:
        author = _text(author_el, "name")
    categories = []
    for child in element:
        if _local(child.tag) == "category":
            value = child.get("term") or "".join(child.itertext()).strip()
            if value:
                categories.append(value)
    item_id = _text(element, "guid", "id") or link
    if not item_id:
        item_id = "sha256:" + hashlib.sha256(f"{title}\n{published}".encode()).hexdigest()[:16]
    return {
        "id": item_id, "title": title, "link": link, "published": published,
        "author": author, "summary": summary, "categories": categories,
    }


def parse_feed(body: bytes) -> list[dict]:
    """The feed's items as dicts, in document order (newest first in most feeds)."""
    # The whole body (at most MAX_RESPONSE_BYTES): a DOCTYPE can pad its entity
    # declarations past any prefix.
    if _ENTITY_RE.search(body):
        raise AdapterError("The feed declares XML entities, which aren't accepted.")
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise AdapterError("The response isn't an XML feed.") from None
    kind = _local(root.tag)
    if kind == "rss":
        channel = _child(root, "channel")
        elements = [e for e in (channel if channel is not None else []) if _local(e.tag) == "item"]
    elif kind == "feed":
        elements = [e for e in root if _local(e.tag) == "entry"]
    elif kind == "RDF":
        elements = [e for e in root if _local(e.tag) == "item"]
    else:
        raise AdapterError("The response is XML but not an RSS or Atom feed.")
    return [_item(e) for e in elements]


class RssNewItemAdapter(Adapter):
    key = "rss_new_item"
    title = "New item in an RSS or Atom feed"
    description = "Polls a feed and starts a run for each item that wasn't in it before."
    config_schema = {
        "type": "object",
        "required": ["url"],
        "additionalProperties": False,
        "properties": {
            "url": {
                "type": "string", "maxLength": 2000, "title": "Feed URL",
                "description": "An RSS or Atom feed (no sign-in).",
            },
        },
    }

    def _items(self, ctx, config):
        body, _ = _net.get(
            config["url"], accept="application/rss+xml, application/atom+xml, application/xml, text/xml",
            timeout=ctx.timeout,
        )
        # Oldest first.
        return list(reversed(parse_feed(body)))

    def check(self, ctx, config, state):
        items = self._items(ctx, config)
        if not isinstance(state, dict):
            return [], {"ids": [item["id"] for item in items][-IDS_MAX:]}
        known = list(state.get("ids") or [])
        seen = set(known)
        events = []
        for item in items:
            if item["id"] in seen:
                continue
            if ctx.limit is not None and len(events) >= ctx.limit:
                break
            seen.add(item["id"])
            known.append(item["id"])
            events.append({"id": item["id"], "item": item})
        return events, {"ids": known[-IDS_MAX:]}

    def test(self, ctx, config):
        items = self._items(ctx, config)
        if not items:
            return "The feed has no items yet.", []
        count = len(items)
        message = f"Found {count} item{'s' if count != 1 else ''}; the newest is {items[-1]['title'] or items[-1]['id']!r}."
        return message, list(reversed(items[-TEST_ITEMS:]))
