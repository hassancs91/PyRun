"""
Update steps for Brand Tracker (PyRun 2.5 in-place updates).

After PyRun updates this plugin in place and restarts, it calls ``reprovision()``
and then ``on_update(from_version)`` in an isolated process, once, while the
plugin is active. Both are optional; see "Updating a plugin" in docs/plugins.md.
"""

from . import provisioning as prov


def reprovision():
    """Re-apply the saved settings, so the managed script runs this version's code.

    Built from what is stored (config entry, script, schedule); secret fields
    are left blank, which keeps every saved secret as it is. A tracker that was
    never configured has nothing to re-apply.
    """
    if prov.get_script() is None:
        return
    data = prov.initial_from_config()
    if not data.get("environment"):
        return  # its environment was removed; the settings page asks for one
    prov.provision(data)


def on_update(from_version):
    """Nothing to migrate between 1.x versions: the stored data keeps its shape."""
