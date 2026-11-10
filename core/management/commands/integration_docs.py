"""
Write docs/integrations/<key>.md for every integration, from its code (core/integrations/docs.py).

    manage.py integration_docs            # rewrite the pages that changed
    manage.py integration_docs http       # only these integrations
    manage.py integration_docs --check    # change nothing; exit 1 if a page is out of date
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.integrations import docs, framework


class Command(BaseCommand):
    help = "Generate docs/integrations/<key>.md from each integration's api.py and module docstrings."

    def add_arguments(self, parser):
        parser.add_argument("keys", nargs="*", help="Integrations to write (default: every one shipped).")
        parser.add_argument("--check", action="store_true", help="Only report pages that are out of date.")

    def handle(self, *args, **options):
        try:
            chosen = [framework.load(key) for key in options["keys"]] or framework.integrations()
        except (LookupError, ValueError) as e:
            raise CommandError(str(e)) from None
        changed = docs.write(chosen, check=options["check"])
        for path in changed:
            shown = path.relative_to(settings.BASE_DIR) if path.is_relative_to(settings.BASE_DIR) else path
            self.stdout.write(f"{'Out of date' if options['check'] else 'Wrote'}: {shown.as_posix()}")
        if options["check"] and changed:
            raise CommandError(f"{len(changed)} integration page(s) out of date: run manage.py integration_docs.")
        if not changed:
            self.stdout.write("Integration docs are up to date.")
