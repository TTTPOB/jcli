"""Classify REST client failures without confusing transport errors with missing IDs."""

from jupyter_server_client.exceptions import (
    AuthenticationError,
    ForbiddenError,
    JupyterTimeoutError,
    NotFoundError,
)


def server_error_code(error: Exception, not_found: str) -> str:
    """Return a CLI code for a failed server operation."""
    if isinstance(error, NotFoundError):
        return not_found
    if isinstance(error, (AuthenticationError, ForbiddenError)):
        return "AUTH_FAILED"
    if isinstance(error, JupyterTimeoutError):
        return "TIMEOUT"
    return "CONNECTION_FAILED"
