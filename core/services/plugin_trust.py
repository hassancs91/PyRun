"""
Plugin trust on install, update, rollback and activation (2.5 T43).

``check()`` runs on a staged or kept plugin folder and returns its
TrustResult, or raises ``PluginInstallError`` when:

- a trust file is present but does not check out (core/plugins/trust/signing.py);
- the slug is pinned to a publisher (``Plugin.pinned_publisher``) and the
  folder is unsigned or signed by another publisher;
- ``GlobalSettings.plugin_trust_policy`` is ``signed_only`` and the folder is
  not verified.

``apply()`` copies a result onto a Plugin row (the caller saves).
"""

from core.models import GlobalSettings, Plugin
from core.plugins.trust.signing import TrustError, verify_folder
from core.services.plugin_service import PluginInstallError


def signed_only() -> bool:
    return GlobalSettings.get_settings().plugin_trust_policy == GlobalSettings.PluginTrustPolicy.SIGNED_ONLY


def policy_problem(slug, trust) -> str:
    """The signed_only refusal for an unsigned plugin, or ''."""
    if trust != Plugin.Trust.VERIFIED and signed_only():
        return (
            f"Plugin '{slug}' is not signed, and this PyRun only accepts signed plugins "
            "(Plugins → Trust policy)."
        )
    return ""


def pin_problem(plugin, result) -> str:
    """Why ``result`` may not replace ``plugin``'s pinned publisher, or ''."""
    pinned = plugin.pinned_publisher if plugin is not None else ""
    if not pinned:
        return ""
    if not result.verified:
        return (
            f"Plugin '{plugin.slug}' was installed signed by '{pinned}', so an update must be "
            "signed by that publisher too. This zip is not signed."
        )
    if result.publisher != pinned:
        return (
            f"Plugin '{plugin.slug}' was installed signed by '{pinned}'; this zip is signed by "
            f"'{result.publisher}'. A plugin can't change publisher in an update."
        )
    return ""


def check(folder, slug, manifest, *, plugin=None, include_generated=True, pin=True):
    """TrustResult for ``folder``, or PluginInstallError (see the module docstring)."""
    try:
        result = verify_folder(folder, slug=slug, manifest=manifest, include_generated=include_generated)
    except TrustError as exc:
        raise PluginInstallError(
            f"Refused: plugin '{slug}' failed its signature check: {exc} "
            "Get an untouched copy of the zip from its publisher."
        )
    problem = (pin_problem(plugin, result) if pin else "") or policy_problem(slug, result.trust)
    if problem:
        raise PluginInstallError(problem)
    return result


#: The Plugin fields ``apply`` sets (for ``save(update_fields=...)``).
FIELDS = ["trust", "signed_by", "signing_key_id", "pinned_publisher"]


def apply(plugin, result) -> list:
    """Record ``result`` on ``plugin`` (not saved); returns the field names."""
    plugin.trust = Plugin.Trust.VERIFIED if result.verified else Plugin.Trust.UNSIGNED
    plugin.signed_by = result.publisher
    plugin.signing_key_id = result.key_id
    if result.verified and not plugin.pinned_publisher:
        plugin.pinned_publisher = result.publisher
    return list(FIELDS)
