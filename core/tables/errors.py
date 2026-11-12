"""Tables errors: one exception with a stable ``code`` every door maps to a status."""


class TableError(Exception):
    """A table operation failed; ``message`` is user-facing, ``code`` is stable."""

    def __init__(self, message, code="invalid"):
        super().__init__(message)
        self.message = message
        self.code = code


# The HTTP status each code answers with on the loopback (and, later, the API).
STATUS_FOR_CODE = {
    "not_found": 404,
    "row_not_found": 404,
    "invalid": 400,
    "unsupported_filter": 400,
    "row_cap": 409,
    "too_large": 413,
}
