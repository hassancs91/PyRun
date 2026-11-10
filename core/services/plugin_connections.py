"""
Plugin connections: the accounts a plugin asks for, and which connection serves each (2.5 T25).

A plugin declares the outside accounts it needs in ``plugin.json``::

    "requires_connections": [
        {"key": "gmail", "provider": "google", "scopes": ["gmail_modify"],
         "label": "The mailbox to triage"}
    ]

Each entry is a *requirement*. ``key`` names it inside the plugin (default: the
provider key); ``scopes`` are the provider's scope-set keys (the boxes on the
connection form, e.g. ``gmail_modify``) or raw provider scopes; ``label`` says
what the account is for.

The connection that serves a requirement is the one the plugin holds for that
key in the workspace: ``Connection.owner_plugin`` is the plugin's slug and
``owner_key`` the key (unique per workspace). The plugin page makes one with
its Connect button, or the user picks one of the workspace's own connections
there. Releasing it (Stop using, or deleting the plugin with its data) clears
the two fields: the connection stays in Connections, it is the user's account.

Plugin scripts get the connection through an ordinary ``ConnectionGrant``
(``ConnectionAPI.grant``); the "all my scripts" wildcard never covers them.

This module imports no models at the top: the plugin doctor uses the manifest
checks on untrusted folders.
"""

import re
from dataclasses import dataclass

from core.integrations.presets import OAUTH2, get_preset

KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,49}$")
LABEL_MAX_CHARS = 120
MAX_REQUIREMENTS = 20
_FIELDS = {"key", "provider", "scopes", "label"}

# What a requirement's status means on the plugin page.
MISSING = "missing"
MISSING_SCOPES = "missing_scopes"
STATUS_LABELS = {
    MISSING: "Not connected",
    "ok": "Connected",
    MISSING_SCOPES: "Missing access",
    "expired": "Expired",
    "revoked": "Revoked",
    "error": "Error",
}


@dataclass(frozen=True)
class Requirement:
    """One connection a plugin needs."""

    key: str
    provider: str
    scopes: tuple = ()
    label: str = ""

    @property
    def preset(self):
        return get_preset(self.provider)

    @property
    def is_oauth(self) -> bool:
        preset = self.preset
        return preset is not None and preset.oauth is not None and OAUTH2 in preset.auth_kinds

    def split_scopes(self) -> tuple:
        """(the preset's scope-set keys asked for, the raw scopes asked for)."""
        preset = self.preset
        known = {s.key for s in preset.oauth.scope_sets} if self.is_oauth else set()
        return (
            [s for s in self.scopes if s in known],
            [s for s in self.scopes if s not in known],
        )

    def wanted_scopes(self) -> list:
        """The provider scopes this requirement asks for (base scopes not included)."""
        if not self.is_oauth:
            return []
        sets, extra = self.split_scopes()
        wanted = []
        for scope_set in self.preset.oauth.scope_sets:
            if scope_set.key in sets:
                wanted += list(scope_set.scopes)
        return list(dict.fromkeys(wanted + extra))

    def connect_config(self) -> dict:
        """The connection ``config`` a Connect for this requirement saves."""
        sets, extra = self.split_scopes()
        return {"scope_sets": sets, "extra_scopes": " ".join(extra)}

    def connect_scopes(self) -> list:
        """Everything Connect asks the account for: base scopes, then this requirement's."""
        sets, extra = self.split_scopes()
        return self.preset.oauth.scopes_for(sets, " ".join(extra))

    def as_dict(self) -> dict:
        return {"key": self.key, "provider": self.provider, "scopes": list(self.scopes), "label": self.label}


def parse_requirement(entry) -> Requirement:
    """One ``requires_connections`` entry as a Requirement. Raises ValueError."""
    if not isinstance(entry, dict):
        raise ValueError("each requires_connections entry must be an object.")
    provider = entry.get("provider")
    if not isinstance(provider, str) or not provider:
        raise ValueError("a requires_connections entry needs a 'provider' (a preset key such as 'google').")
    preset = get_preset(provider)
    if preset is None:
        raise ValueError(f"requires_connections: unknown provider {provider!r}.")
    key = entry.get("key", provider)
    if not isinstance(key, str) or not KEY_RE.match(key):
        raise ValueError(
            f"requires_connections: key {key!r} must be lowercase letters, digits, '_' or '-' "
            "(50 at most, starting with a letter or digit)."
        )
    scopes = entry.get("scopes", [])
    if not isinstance(scopes, (list, tuple)) or not all(
        isinstance(s, str) and s and not any(c.isspace() for c in s) and "," not in s for s in scopes
    ):
        raise ValueError(f"requires_connections[{key}]: 'scopes' must be a list of scope names (no spaces).")
    if scopes and (preset.oauth is None or OAUTH2 not in preset.auth_kinds):
        raise ValueError(f"requires_connections[{key}]: {preset.label} connections have no scopes.")
    label = entry.get("label", "")
    if not isinstance(label, str) or len(label) > LABEL_MAX_CHARS:
        raise ValueError(f"requires_connections[{key}]: 'label' must be text ({LABEL_MAX_CHARS} characters at most).")
    return Requirement(key=key, provider=provider, scopes=tuple(dict.fromkeys(scopes)), label=label.strip())


def manifest_problems(raw) -> tuple:
    """(failures, warnings) for a manifest's ``requires_connections`` value (the doctor)."""
    failures, warnings = [], []
    if raw is None:
        return failures, warnings
    if not isinstance(raw, list):
        return ["'requires_connections' must be a list of objects."], warnings
    if len(raw) > MAX_REQUIREMENTS:
        failures.append(f"'requires_connections' lists {len(raw)} entries ({MAX_REQUIREMENTS} at most).")
    seen = set()
    for entry in raw:
        try:
            requirement = parse_requirement(entry)
        except ValueError as e:
            failures.append(str(e))
            continue
        if requirement.key in seen:
            failures.append(
                f"requires_connections: key {requirement.key!r} is used twice "
                "(give two connections of one provider their own 'key')."
            )
        seen.add(requirement.key)
        unknown = sorted(set(entry) - _FIELDS)
        if unknown:
            warnings.append(f"requires_connections[{requirement.key}]: unknown field(s) {', '.join(unknown)}.")
    return failures, warnings


def requirements_from_manifest(manifest) -> list:
    """The manifest's requirements, skipping entries the doctor would refuse."""
    raw = (manifest or {}).get("requires_connections") if isinstance(manifest, dict) else None
    if not isinstance(raw, list):
        return []
    out, seen = [], set()
    for entry in raw[:MAX_REQUIREMENTS]:
        try:
            requirement = parse_requirement(entry)
        except ValueError:
            continue
        if requirement.key not in seen:
            seen.add(requirement.key)
            out.append(requirement)
    return out


def declared_requirement(slug, key):
    """The installed plugin ``slug``'s requirement ``key``, or None."""
    from core.models import Plugin

    plugin = Plugin.objects.filter(slug=slug).first()
    if plugin is None:
        return None
    return next((r for r in requirements_from_manifest(plugin.manifest) if r.key == key), None)


# --------------------------------------------------------------------------- #
# Which connection serves a requirement, and its status
# --------------------------------------------------------------------------- #


def connection_for(workspace, slug, key):
    """The connection plugin ``slug`` holds for ``key`` in ``workspace``, or None."""
    from core.models import Connection

    if workspace is None:
        return None
    return Connection.objects.filter(workspace=workspace, owner_plugin=slug, owner_key=key).first()


def status_of(requirement, connection) -> tuple:
    """(status, the asked-for scopes the account didn't grant)."""
    if connection is None:
        return MISSING, []
    if connection.provider != requirement.provider:
        return "error", []
    if connection.status != "ok":
        return connection.status, []
    granted = set(connection.scopes or [])
    # A pasted access token doesn't say what it may do: nothing to compare.
    missing = [s for s in requirement.wanted_scopes() if granted and s not in granted]
    return (MISSING_SCOPES, missing) if missing else ("ok", [])


def pickable(workspace, requirement):
    """The workspace's connections the plugin page may offer for ``requirement``.

    The provider's connections no plugin holds yet, and not revoked.
    """
    from core.models import Connection

    return list(
        Connection.objects.filter(
            workspace=workspace, provider=requirement.provider, owner_plugin__isnull=True
        ).exclude(status=Connection.Status.REVOKED).order_by("name")
    )


def new_connection_name(workspace, slug, key) -> str:
    """A free connection name for a plugin's new connection: ``<slug>-<key>``, then -2, -3..."""
    from core.models import Connection

    base = f"{slug}-{key}"[:96]  # a slug starts with a letter, so this fits NAME_RE
    taken = set(
        Connection.objects.filter(workspace=workspace, name__startswith=base).values_list("name", flat=True)
    )
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def _drop_plugin_grants(connection, slug) -> None:
    from core.models import ConnectionGrant

    ConnectionGrant.objects.filter(connection=connection, script__owner_plugin=slug).delete()


def release(workspace, slug, key):
    """Stop plugin ``slug`` using its connection for ``key``; returns it (or None).

    The connection stays (it is the user's account); the plugin's scripts lose
    their grants to it.
    """
    from core.models import Connection

    connection = connection_for(workspace, slug, key)
    if connection is None:
        return None
    _drop_plugin_grants(connection, slug)
    Connection.objects.filter(pk=connection.pk).update(owner_plugin=None, owner_key=None)
    connection.owner_plugin = connection.owner_key = None
    return connection


def adopt(connection, slug, key) -> None:
    """Make ``connection`` the one plugin ``slug`` uses for ``key`` (replacing any other).

    Raises ValueError when another plugin already uses it, or when two picks
    for the same key race (the unique constraint keeps one).
    """
    from django.db import IntegrityError, transaction
    from django.db.models import Q

    from core.models import Connection

    if connection.owner_plugin and (connection.owner_plugin, connection.owner_key) != (slug, key):
        raise ValueError(f'Connection "{connection.name}" is already used by the plugin {connection.owner_plugin}.')
    try:
        with transaction.atomic():
            current = connection_for(connection.workspace, slug, key)
            if current is not None and current.pk != connection.pk:
                release(connection.workspace, slug, key)
            # Only if no other plugin took it since it was read.
            updated = Connection.objects.filter(
                Q(owner_plugin__isnull=True) | Q(owner_plugin=slug, owner_key=key), pk=connection.pk
            ).update(owner_plugin=slug, owner_key=key)
            if not updated:
                raise ValueError(f'Connection "{connection.name}" was just taken by another plugin.')
    except IntegrityError:
        raise ValueError(f"Another connection was just picked for this plugin ({key}): reload the page.") from None
    connection.owner_plugin, connection.owner_key = slug, key


def release_all(slug) -> int:
    """Release every connection plugin ``slug`` holds, in every workspace (plugin deleted)."""
    from core.models import Connection, ConnectionGrant

    ConnectionGrant.objects.filter(connection__owner_plugin=slug, script__owner_plugin=slug).delete()
    return Connection.objects.filter(owner_plugin=slug).update(owner_plugin=None, owner_key=None)


@dataclass
class RequirementState:
    """A requirement with what the plugin page shows for it in one workspace."""

    slug: str
    requirement: Requirement
    connection: object
    status: str
    missing_scopes: list
    has_oauth_app: bool
    choices: list

    @property
    def key(self):
        return self.requirement.key

    @property
    def provider_label(self):
        preset = self.requirement.preset
        return preset.label if preset else self.requirement.provider

    @property
    def label(self):
        return self.requirement.label or f"A {self.provider_label} account"

    @property
    def status_label(self):
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def ok(self):
        return self.status == "ok"

    @property
    def is_oauth(self):
        return self.requirement.is_oauth

    @property
    def scope_labels(self):
        """The scopes asked for, as the connection form names them."""
        if not self.requirement.is_oauth:
            return []
        names = {s.key: s.label for s in self.requirement.preset.oauth.scope_sets}
        return [names.get(s, s) for s in self.requirement.scopes]


def state_for(workspace, slug, requirement) -> RequirementState:
    connection = connection_for(workspace, slug, requirement.key)
    status, missing = status_of(requirement, connection)
    has_app = False
    if requirement.is_oauth and workspace is not None:
        from core.services.oauth import client_for

        has_app = client_for(workspace, requirement.provider) is not None
    return RequirementState(
        slug=slug,
        requirement=requirement,
        connection=connection,
        status=status,
        missing_scopes=missing,
        has_oauth_app=has_app,
        choices=pickable(workspace, requirement) if workspace is not None else [],
    )


def states_for(workspace, slug, manifest) -> list:
    """Every declared requirement of a plugin, with its state in ``workspace``."""
    return [state_for(workspace, slug, r) for r in requirements_from_manifest(manifest)]
