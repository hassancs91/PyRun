"""
Publisher keys PyRun trusts for plugin signatures (2.5 T43).

A plugin signed with one of these keys installs as ``trust = verified`` and
shows the "Verified · PyRun" badge. Each key has an id; a signature names the
id it was made with, so a key can be rotated by adding a new id (and, once
nothing signed with the old one is in use, marking the old one revoked).

Only PyRun's own keys live here. The private half never enters the repo or the
image: it stays offline with the maintainer, who signs zips with
``manage.py plugin_sign <folder> --key <path> --key-id <id>``. Third-party
publisher keys are deferred; the signature format already carries the
publisher and key id they will need.

To add a key: ``manage.py plugin_sign --generate-key <path> --key-id <id>``
prints the entry to paste below.
"""

from dataclasses import dataclass

#: The publisher name PyRun's own keys sign as (the badge's "PyRun").
PYRUN_PUBLISHER = "pyrun"


@dataclass(frozen=True)
class TrustedKey:
    """One public key: who it signs for, the raw Ed25519 key (base64), revoked or not."""

    publisher: str
    public_key: str
    revoked: bool = False


#: key id -> TrustedKey. Empty until PyRun's first official key is generated;
#: until then no plugin can be ``verified`` and every plugin is ``unsigned``.
TRUSTED_KEYS: dict = {}


def trusted_key(key_id):
    """The TrustedKey for ``key_id``, or None."""
    return TRUSTED_KEYS.get(key_id)
