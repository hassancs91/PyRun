"""
DataStore export and import as one JSON object (``{"key": value, ...}``).

- **Export** streams every entry of a store, ordered by key. Values are written
  as they are stored (the entry's JSON text, checked to parse), not masked: it
  is the store's own data, as the console and the REST API show it. A stored
  text that isn't JSON (written around the helpers) is exported as a JSON
  string, so the file always parses.
- **Import** is an upsert: each key of the file's top-level object is created
  or has its value replaced; keys the file doesn't name are left alone.
  ``plan_import`` checks the whole file and counts what would change (the
  preview); ``apply_import`` writes it in one transaction (all or nothing).
  A value equal to the stored one (same JSON, key order aside) is "unchanged"
  and not rewritten, so its ``updated_at`` stays.
"""

import json

from django.db import transaction
from django.utils import timezone

from core.models import DataStoreEntry

MAX_IMPORT_BYTES = 20 * 1024 * 1024
MAX_KEY_LENGTH = 255
MAX_ERRORS = 20
SAMPLE_KEYS = 20
BATCH = 500
TOO_LARGE = (f"The file is larger than {MAX_IMPORT_BYTES // (1024 * 1024)} MB. Split it, or write the entries "
             "from a script with pyrunner_datastore (no size limit there).")


class DataStoreImportError(Exception):
    """A file that can't be imported (the message is shown as is)."""

    def __init__(self, message: str, errors=None):
        super().__init__(message)
        self.message = message
        self.errors = errors or []


def stream_json(store):
    """The store's entries as one JSON object, a line per entry (for StreamingHttpResponse)."""
    rows = store.entries.order_by("key").values_list("key", "value_json").iterator(chunk_size=BATCH)
    first = True
    for key, value_json in rows:
        try:
            json.loads(value_json)
        except ValueError:
            value_json = json.dumps(value_json, ensure_ascii=False)
        yield ("{\n  " if first else ",\n  ") + json.dumps(key, ensure_ascii=False) + ": " + value_json
        first = False
    yield "{}\n" if first else "\n}\n"


def _canonical(value) -> str:
    # sort_keys: stored objects compare equal whatever their key order (jsonb
    # reorders keys); json.dumps keeps 1, 1.0 and true apart.
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def parse(data: bytes) -> dict:
    """The file's top-level object; raises DataStoreImportError with every problem found (up to MAX_ERRORS)."""
    if len(data) > MAX_IMPORT_BYTES:
        raise DataStoreImportError(TOO_LARGE)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise DataStoreImportError("The file isn't UTF-8 text.") from None
    calls = []

    def pairs_hook(pairs):
        # Called for every object, innermost first; the top-level one is the last.
        calls.append(pairs)
        return dict(pairs)

    try:
        loaded = json.loads(text, object_pairs_hook=pairs_hook)
    except ValueError as exc:
        raise DataStoreImportError(f"The file isn't valid JSON: {exc}.") from None
    if not isinstance(loaded, dict):
        raise DataStoreImportError('The file must hold one JSON object of key: value pairs, like {"my_key": {"any": "value"}}.')
    errors, seen = [], set()
    for key, _ in calls[-1] if calls else []:
        if key in seen:
            errors.append(f'The key "{key[:80]}" appears more than once.')
        seen.add(key)
        if key == "":
            errors.append("A key is empty.")
        elif len(key) > MAX_KEY_LENGTH:
            errors.append(f'The key "{key[:80]}…" is longer than {MAX_KEY_LENGTH} characters.')
    if errors:
        raise DataStoreImportError(
            f"{len(errors)} problem{'s' if len(errors) != 1 else ''} in the file; nothing was imported.",
            errors[:MAX_ERRORS],
        )
    return loaded


def _classify(store, items: dict) -> tuple:
    """(new keys, changed keys, unchanged count) against what the store holds now."""
    keys = list(items)
    stored = {}
    for start in range(0, len(keys), BATCH):
        stored.update(store.entries.filter(key__in=keys[start:start + BATCH]).values_list("key", "value_json"))
    new, changed, unchanged = [], [], 0
    for key in keys:
        if key not in stored:
            new.append(key)
            continue
        try:
            same = _canonical(json.loads(stored[key])) == _canonical(items[key])
        except ValueError:
            same = False
        if same:
            unchanged += 1
        else:
            changed.append(key)
    return new, changed, unchanged


def _report(new, changed, unchanged) -> dict:
    return {
        "total": len(new) + len(changed) + unchanged,
        "new": len(new),
        "updated": len(changed),
        "unchanged": unchanged,
        "new_keys": sorted(new)[:SAMPLE_KEYS],
        "updated_keys": sorted(changed)[:SAMPLE_KEYS],
    }


def plan_import(store, items: dict) -> dict:
    """The preview: counts and sample keys, nothing written."""
    return _report(*_classify(store, items))


def apply_import(store, items: dict) -> dict:
    """Upsert every new or changed key in one transaction; returns the same report as the preview."""
    with transaction.atomic():
        new, changed, unchanged = _classify(store, items)
        now = timezone.now()
        objs = [
            DataStoreEntry(datastore=store, key=key, value_json=json.dumps(items[key]), updated_at=now)
            for key in new + changed
        ]
        # A real upsert: a script that adds one of the new keys meanwhile gets
        # its value replaced (as a later set would), not an IntegrityError.
        DataStoreEntry.objects.bulk_create(
            objs, batch_size=BATCH, update_conflicts=True,
            unique_fields=["datastore", "key"], update_fields=["value_json", "updated_at"],
        )
    return _report(new, changed, unchanged)
