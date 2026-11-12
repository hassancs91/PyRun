"""
plugin_sign — build a signed plugin zip (2.5 T43).

    python manage.py plugin_sign ./examples/brand_tracker --key ~/keys/pyrun.pem --key-id pyrun-2026
    python manage.py plugin_sign --generate-key ~/keys/pyrun.pem --key-id pyrun-2026

Signing writes ``<out>/<slug>-<version>.zip`` (default ``dist/``) holding the
plugin's files plus ``MANIFEST.sha256`` (every file and its sha256) and
``plugin.sig`` (Ed25519 over the manifest, slug, publisher, version and key
id). The plugin folder itself is not changed. Left out of the zip:
``__pycache__``/``.pyc`` and hidden files or folders (names starting with ``.``).

The private key is a PEM file outside this project: the command refuses a key
path inside it, so a key can't end up in the repo or the Docker image.
``--generate-key`` makes a new key pair there and prints the entry to add to
``core/plugins/trust/keys.py``.
"""

import io
import os
import zipfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.plugins import is_valid_plugin_slug
from core.plugins.trust import keys
from core.plugins.trust.signing import (
    MANIFEST_NAME,
    SIGNATURE_NAME,
    TrustError,
    build_manifest,
    load_private_key,
    plugin_files,
    private_key_pem,
    public_key_b64,
    sign_manifest,
)
from core.services.plugin_service import PluginInstallError, PluginService


def _inside_project(path: Path) -> bool:
    base = Path(settings.BASE_DIR).resolve()
    target = path.expanduser().resolve()
    return target == base or base in target.parents


def signable_files(folder: Path) -> list:
    """The files that go into a signed zip (no generated or hidden files)."""
    return [
        rel for rel in plugin_files(folder, include_generated=False)
        if not any(part.startswith(".") for part in rel.split("/"))
    ]


class Command(BaseCommand):
    help = "Build a signed plugin zip (MANIFEST.sha256 + plugin.sig), or generate a signing key."

    def add_arguments(self, parser):
        parser.add_argument("folder", nargs="?", help="The plugin folder (its name is the slug).")
        parser.add_argument("--key", help="Ed25519 private key (PEM) to sign with; kept outside this project.")
        parser.add_argument("--key-id", required=True, help="The key's id, as listed in core/plugins/trust/keys.py.")
        parser.add_argument(
            "--publisher", default=keys.PYRUN_PUBLISHER,
            help=f"Publisher the signature names (default {keys.PYRUN_PUBLISHER!r}).",
        )
        parser.add_argument("--out", default="dist", help="Folder for the zip (default dist/).")
        parser.add_argument(
            "--generate-key", metavar="PATH",
            help="Write a new Ed25519 private key to PATH and print the keys.py entry.",
        )

    def handle(self, *args, **options):
        if options.get("generate_key"):
            if options.get("folder") or options.get("key"):
                raise CommandError("--generate-key takes only --key-id (and --publisher).")
            return self._generate(Path(options["generate_key"]), options["key_id"], options["publisher"])
        if not options.get("folder") or not options.get("key"):
            raise CommandError("Give the plugin folder and --key (or --generate-key PATH).")
        return self._sign(Path(options["folder"]), Path(options["key"]), options)

    def _generate(self, path: Path, key_id, publisher):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        if _inside_project(path):
            raise CommandError("Keep the private key outside this project (it must never enter the repo or image).")
        path = path.expanduser()
        if path.exists():
            raise CommandError(f"{path} already exists; not overwriting a key.")
        private_key = Ed25519PrivateKey.generate()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(private_key_pem(private_key))
        self.stdout.write(self.style.SUCCESS(f"Private key written to {path}. Keep it offline and backed up."))
        self.stdout.write("Add this entry to TRUSTED_KEYS in core/plugins/trust/keys.py:\n")
        self.stdout.write(
            f'    "{key_id}": TrustedKey(publisher="{publisher}", public_key="{public_key_b64(private_key)}"),'
        )

    def _sign(self, folder: Path, key_path: Path, options):
        if not folder.is_dir():
            raise CommandError(f"Not a directory: {folder}")
        folder = folder.resolve()
        slug = folder.name
        if not is_valid_plugin_slug(slug):
            raise CommandError(f"{slug!r} is not a valid plugin slug (the folder name is the slug).")
        if _inside_project(key_path):
            raise CommandError("Keep the private key outside this project (it must never enter the repo or image).")
        try:
            manifest = PluginService._read_manifest(folder, slug)
            private_key = load_private_key(key_path.expanduser().read_bytes())
        except (PluginInstallError, TrustError, OSError) as exc:
            raise CommandError(str(exc))
        version = str(manifest.get("version") or "")
        publisher = options["publisher"]
        declared = manifest.get("publisher")
        if declared and declared != publisher:
            raise CommandError(f"plugin.json names the publisher {declared!r}; sign with --publisher {declared}.")

        key_id = options["key_id"]
        files = signable_files(folder)
        try:
            manifest_bytes = build_manifest(folder, files)
            signature = sign_manifest(
                manifest_bytes, private_key, slug=slug, publisher=publisher, version=version, key_id=key_id,
            )
        except TrustError as exc:
            raise CommandError(str(exc))

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel in files:
                zf.write(folder / rel, f"{slug}/{rel}")
            zf.writestr(f"{slug}/{MANIFEST_NAME}", manifest_bytes)
            zf.writestr(f"{slug}/{SIGNATURE_NAME}", signature)
        out = Path(options["out"])
        out.mkdir(parents=True, exist_ok=True)
        target = out / f"{slug}-{version}.zip"
        target.write_bytes(buf.getvalue())

        self.stdout.write(self.style.SUCCESS(
            f"Signed {slug} {version} as {publisher} (key {key_id}), {len(files)} files: {target}"
        ))
        trusted = keys.trusted_key(key_id)
        if trusted is None:
            self.stdout.write(self.style.WARNING(
                f"Key id {key_id!r} is not in core/plugins/trust/keys.py, so this PyRun refuses the zip."
            ))
        elif trusted.revoked:
            self.stdout.write(self.style.WARNING(
                f"Key id {key_id!r} is revoked in core/plugins/trust/keys.py, so this PyRun refuses the zip."
            ))
        elif trusted.public_key != public_key_b64(private_key) or trusted.publisher != publisher:
            self.stdout.write(self.style.WARNING(
                f"Key id {key_id!r} in core/plugins/trust/keys.py is a different key or publisher, "
                "so this PyRun refuses the zip."
            ))
