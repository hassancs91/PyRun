"""
plugin_post_update — run plugins' update steps after a restart (2.5 T42).

An in-place update swaps a plugin's folder while the old code is still loaded,
so the plugin's own steps (``lifecycle.py``: ``reprovision()`` then
``on_update(from_version)``) wait for the restart. The entrypoint runs this
right after the plugin preflight:

    plugin_post_update
        For every ACTIVE plugin with update steps waiting, run them in an
        isolated child that loads only that plugin, and record the outcome on
        its update history. ALWAYS exits 0: it must never stop the container
        from starting. Run it with PYRUNNER_DISABLE_PLUGINS=1. Outside Docker,
        run it once after restarting PyRun by hand.

    plugin_post_update <slug> (--from-version X | --reprovision-only)
        The isolated child itself (PYRUNNER_PREFLIGHT_SLUG=<slug>): imports
        plugins.<slug>.lifecycle and runs the steps. Non-zero exit on failure.
"""

import os
import traceback

from django.core.management.base import BaseCommand, CommandError

from core.services import plugin_updates

PREFLIGHT_SLUG_ENV = "PYRUNNER_PREFLIGHT_SLUG"


class Command(BaseCommand):
    help = "Run plugins' update steps (lifecycle.py) after a restart."

    def add_arguments(self, parser):
        parser.add_argument("slug", nargs="?", help="(child mode) the plugin to run steps for.")
        parser.add_argument("--from-version", default="", help="(child mode) version updated from.")
        parser.add_argument(
            "--reprovision-only", action="store_true",
            help="(child mode) run reprovision() only (after a rollback).",
        )

    def handle(self, *args, **options):
        slug = options.get("slug")
        if not slug:
            results = plugin_updates.run_pending_steps(write=self.stdout.write)
            if not results:
                self.stdout.write("No plugin update steps waiting.")
            return

        if os.environ.get(PREFLIGHT_SLUG_ENV) != slug:
            raise CommandError(
                "Child mode runs only in the isolated process the orchestrator "
                "starts; run `plugin_post_update` without a slug."
            )
        try:
            plugin_updates.run_steps(
                slug,
                options.get("from_version") or "",
                reprovision_only=options.get("reprovision_only"),
                write=self.stdout.write,
            )
        except Exception:
            self.stderr.write(traceback.format_exc())
            raise SystemExit(1)
