"""Classify REST client failures without confusing transport errors with missing IDs."""

import requests


def server_error_code(error: Exception, not_found: str) -> str:
    """Return a CLI code for a failed server operation."""
    if isinstance(error, requests.exceptions.HTTPError) and error.response is not None:
        status = error.response.status_code
        if status == 404:
            return not_found
        if status in (401, 403):
            return "AUTH_FAILED"
    if isinstance(error, requests.exceptions.Timeout):
        return "TIMEOUT"
    return "CONNECTION_FAILED"
