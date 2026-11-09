"""
Vector seam errors — dependency-free so the plugin SDK can re-export them.

``VectorError`` is what every door raises: ``message`` is user-facing and
``code`` is stable, so the loopback maps it onto an HTTP status and a script or
plugin can branch on it. The ``StoreError`` family is what the backends raise;
each class carries the ``VectorError`` code ``VectorService`` translates it to.
"""


class VectorError(Exception):
    """A vector operation failed; ``message`` is user-facing, ``code`` is stable."""

    def __init__(self, message, code="error"):
        super().__init__(message)
        self.code = code


class StoreError(Exception):
    """Base class for backend failures; ``code`` is the VectorError code it maps to."""

    code = "invalid"


class StoreFull(StoreError):
    """The write would push the collection past the backend's point cap."""

    code = "collection_full"


class DimensionMismatch(StoreError):
    """A vector's length does not match the collection's pinned dimension."""

    code = "dimension_mismatch"


class BackendUnavailable(StoreError):
    """The store cannot be reached or opened (missing file, server down, bad key)."""

    code = "backend_unavailable"


# The HTTP status each ``VectorError`` code answers with, on every door that
# speaks HTTP (the run loopback and the unified API).
STATUS_FOR_CODE = {
    "not_found": 404,
    "invalid": 400,
    "unsupported_filter": 400,
    "dimension_mismatch": 400,
    "unsupported": 400,
    "credentials_not_allowed": 403,
    "model_mismatch": 409,
    "not_ready": 409,
    "collection_full": 413,
    "run_budget_exceeded": 429,
    "embedding_failed": 502,
    "backend_unavailable": 503,
}
