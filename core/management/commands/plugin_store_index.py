"""
plugin_store_index — build and sign a plugin store ``index.json`` (2.5 T44).

    python manage.py plugin_store_index dist/*.zip --key ~/keys/pyrun.pem --key-id pyrun-2026 \\
        --base-url https://example.com/pyrun-plugins/zips/ --out index.json

Reads each plugin zip (as ``manage.py plugin_sign`` makes them): its
``plugin.json`` gives the name, version, summary, description, categories,
``min_pyrunner``, ``api``, ``homepage``, ``changelog_url``, ``icon_url`` and
``provisions``; ``plugin.sig`` (when present) gives the publisher and marks the
entry signed. Each entry gets the zip's sha256 and size, and a
``download_url`` of ``--base-url`` + the zip's file name (a relative URL when
``--base-url`` is not given, resolved against the index's own URL). The whole
index is then signed (core/plugins/trust/index.py).

The store repo's ``build_index.py`` runs this. The private key stays outside
this project (a key path inside it is refused).
"""

import hashlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from django.core.management.base import BaseCommand, CommandError

from core.management.commands.plugin_sign import _inside_project
from core.plugins.trust import keys
from core.plugins.trust.index import (
    INDEX_SCHEMA,
    MAX_ZIP_BYTES,
    StoreIndexError,
    entry_from_dict,
    sign_index,
)
from core.plugins.trust.signing import SIGNATURE_NAME, TrustError, load_private_key, public_key_b64
from core.services.plugin_service import PluginInstallError, PluginService

#: plugin.json fields copied into an entry as they are.
_COPIED = (
    "summary", "description", "categories", "min_pyrunner", "api", "homepage",
    "changelog_url", "icon_url", "provisions", "tier", "requires_license",
)


def entry_for_zip(path: Path, *, base_url="", default_publisher=keys.PYRUN_PUBLISHER) -> dict:
    """The index entry (a dict) for one plugin zip; CommandError when it can't be listed."""
    data = path.read_bytes()
    if len(data) > MAX_ZIP_BYTES:
        raise CommandError(f"{path.name} is larger than {MAX_ZIP_BYTES // (1024 * 1024)} MB.")
    try:
        with zipfile.ZipFile(path) as zf:
            slug = PluginService._validate_zip(zf)
            manifest = json.loads(zf.read(f"{slug}/plugin.json").decode("utf-8"))
            names = set(zf.namelist())
            sig = (
                json.loads(zf.read(f"{slug}/{SIGNATURE_NAME}").decode("utf-8"))
                if f"{slug}/{SIGNATURE_NAME}" in names else None
            )
    except (zipfile.BadZipFile, KeyError, ValueError, PluginInstallError) as exc:
        raise CommandError(f"{path.name}: {exc}")
    if not isinstance(manifest, dict):
        raise CommandError(f"{path.name}: plugin.json must be a JSON object.")
    version = str(manifest.get("version") or "")
    if sig is not None:
        if not isinstance(sig, dict) or sig.get("slug") != slug or sig.get("version") != version:
            raise CommandError(f"{path.name}: {SIGNATURE_NAME} is not for {slug} {version}.")
        publisher = sig.get("publisher") or ""
    else:
        publisher = manifest.get("publisher") or default_publisher
    entry = {
        "slug": slug,
        "publisher": publisher,
        "name": manifest.get("name") or slug,
        "version": version,
        "download_url": (base_url.rstrip("/") + "/" if base_url else "") + quote(path.name),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "signed": sig is not None,
    }
    for name in _COPIED:
        if manifest.get(name) not in (None, "", [], {}):
            entry[name] = manifest[name]
    try:
        # A relative download_url is resolved against the index's URL when read.
        entry_from_dict(entry, 0, base_url or "https://index.invalid/index.json")
    except StoreIndexError as exc:
        raise CommandError(f"{path.name}: {exc}")
    return entry


class Command(BaseCommand):
    help = "Build and sign a plugin store index.json from plugin zips."

    def add_arguments(self, parser):
        parser.add_argument("zips", nargs="+", help="Plugin zips, or folders holding them.")
        parser.add_argument("--key", required=True, help="Ed25519 private key (PEM), kept outside this project.")
        parser.add_argument("--key-id", required=True, help="The key's id, as listed in core/plugins/trust/keys.py.")
        parser.add_argument(
            "--publisher", default=keys.PYRUN_PUBLISHER,
            help=f"Publisher the index signature names (default {keys.PYRUN_PUBLISHER!r}).",
        )
        parser.add_argument("--base-url", default="", help="URL the zips are served under (default: next to the index).")
        parser.add_argument("--out", default="index.json", help="Where to write the index (default index.json).")

    def handle(self, *args, **options):
        key_path = Path(options["key"])
        if _inside_project(key_path):
            raise CommandError("Keep the private key outside this project (it must never enter the repo or image).")
        try:
            private_key = load_private_key(key_path.expanduser().read_bytes())
        except (TrustError, OSError) as exc:
            raise CommandError(str(exc))

        paths = []
        for given in options["zips"]:
            path = Path(given)
            paths.extend(sorted(path.glob("*.zip")) if path.is_dir() else [path])
        if not paths:
            raise CommandError("No zips found.")
        entries, seen = [], {}
        for path in paths:
            entry = entry_for_zip(path, base_url=options["base_url"])
            if entry["slug"] in seen:
                raise CommandError(
                    f"{path.name} and {seen[entry['slug']]} are both {entry['slug']!r}; list one zip per plugin."
                )
            seen[entry["slug"]] = path.name
            entries.append(entry)

        body = {
            "schema": INDEX_SCHEMA,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "plugins": sorted(entries, key=lambda e: e["slug"]),
        }
        try:
            signed = sign_index(body, private_key, publisher=options["publisher"], key_id=options["key_id"])
        except StoreIndexError as exc:
            raise CommandError(str(exc))
        out = Path(options["out"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(signed, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"Signed index of {len(entries)} plugins: {out}"))

        trusted = keys.trusted_key(options["key_id"])
        if (
            trusted is None or trusted.revoked or trusted.publisher != options["publisher"]
            or trusted.public_key != public_key_b64(private_key)
        ):
            self.stdout.write(self.style.WARNING(
                f"Key id {options['key_id']!r} is not a trusted, unrevoked key of {options['publisher']!r} "
                "in core/plugins/trust/keys.py, so this PyRun refuses the index."
            ))
