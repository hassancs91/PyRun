"""
API views for PyRun REST API.

The legacy datastore views live in ``core.views.api.datastores``. They aren't
re-exported here: they call the registry, whose error helpers import this
package's ``decorators`` (an eager import here would be circular).
"""
