"""
Run an integration's live smoke checks against a real connection. Opt-in: it calls the real service.

    manage.py integration_smoke http                         # the instance's one http connection
    manage.py integration_smoke http --connection my-api --workspace <workspace id>

It uses a connection made under Connections, with the credentials a run would
get (an OAuth token refreshed first), and runs the integration's read-only
checks (``smoke`` in its api.py) through its script-facing module, from this
shell as a script would (scripts' clients don't go through PyRun's SSRF guard
either). It prints PASS or FAIL for each check, every connection value masked,
and exits 1 when one fails. With no such connection it prints SKIP and exits 0,
so it can run where credentials may be missing.
"""

import hashlib

from django.core.management.base import BaseCommand, CommandError

from core.integrations import framework
from core.models import Connection


class Command(BaseCommand):
    help = "Run an integration's live, read-only smoke checks with a real connection (calls the real service)."

    def add_arguments(self, parser):
        parser.add_argument("key", help="The integration, e.g. http.")
        parser.add_argument("--connection", help="The connection's name (needed when there are several).")
        parser.add_argument("--workspace", help="The connection's workspace id (when names repeat).")

    def handle(self, *args, **options):
        from core.executor import mask_secrets_in_value
        from core.services.connections import ConnectionLookupError, credentials_for, masks_for

        try:
            integration = framework.load(options["key"])
        except (LookupError, ValueError) as e:
            raise CommandError(str(e)) from None
        rows = Connection.objects.filter(provider=integration.provider).exclude(status=Connection.Status.REVOKED)
        if options["workspace"]:
            rows = rows.filter(workspace_id=options["workspace"])
        if options["connection"]:
            rows = rows.filter(name=options["connection"])
        found = list(rows.order_by("name")[:20])
        if not found:
            self.stdout.write(f"SKIP: no {integration.provider} connection to run the {integration.key} smoke checks "
                              "with (add one under Connections, or pass --connection).")
            return
        if len(found) > 1:
            names = ", ".join(sorted({c.name for c in found}))
            raise CommandError(f"Several {integration.provider} connections ({names}): pick one with --connection "
                               "(and --workspace when names repeat).")
        connection = found[0]
        last = {}

        def credentials(name, *, refused=False):
            # Re-read each time, so a token refreshed meanwhile is used.
            digest = None
            if refused and last.get("token"):
                digest = hashlib.sha256(last["token"].encode("utf-8")).hexdigest()
            try:
                material = credentials_for(Connection.objects.get(pk=connection.pk), refused=digest)
            except ConnectionLookupError as e:
                raise pyrunner_connections.ConnectionsError(e.message, e.code, e.status) from None
            last.update(material)
            return material

        def mask(text):
            row = Connection.objects.get(pk=connection.pk)
            return mask_secrets_in_value(str(text), {**masks_for(connection), **masks_for(row)})

        pyrunner_connections, client = framework.script_helpers()
        module = framework.script_module(integration)
        if not integration.smoke:
            self.stdout.write(f"{integration.key} has no smoke checks.")
            return
        original = pyrunner_connections.credentials
        pyrunner_connections.credentials = credentials
        client.reset()
        failures = 0
        try:
            for check in integration.smoke:
                try:
                    line = check.run(module, connection.name, dict(connection.config or {}))
                except pyrunner_connections.ConnectionsError as e:
                    failures += 1
                    self.stdout.write(mask(f"FAIL {check.name}: {e.code}: {e}"))
                else:
                    self.stdout.write(mask(f"PASS {check.name}: {line}"))
        finally:
            pyrunner_connections.credentials = original
            client.reset()
        if failures:
            raise CommandError(f"{failures} of {len(integration.smoke)} {integration.key} smoke checks failed.")
