"""Stable contracts and limits for saved output readers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

SCHEMA_VERSION = 1
MAX_TRANSPORT_BYTES = 32 * 1024 * 1024
DEFAULT_TEXT_LIMIT = 100_000

RASTER_MIME_TYPES = (
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
)


class OutputProtocolError(ValueError):
    """A structured failure raised by the output protocol core."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def response_size_bytes(response: Mapping[str, Any]) -> int:
    """Return the compact UTF-8 JSON transport size for a response."""
    try:
        encoded = json.dumps(
            response,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise OutputProtocolError(
            "OUTPUT_DATA_INVALID", f"Output is not JSON serializable: {error}"
        ) from error
    return len(encoded)


def enforce_transport_budget(
    response: dict[str, Any], *, max_transport_bytes: int = MAX_TRANSPORT_BYTES
) -> dict[str, Any]:
    """Reject a response that exceeds the shared serialized transport budget."""
    if max_transport_bytes <= 0:
        raise OutputProtocolError(
            "INVALID_TRANSPORT_LIMIT", "max_transport_bytes must be positive"
        )
    size = response_size_bytes(response)
    if size > max_transport_bytes:
        raise OutputProtocolError(
            "OUTPUT_TOO_LARGE",
            f"Serialized output is {size} bytes; limit is {max_transport_bytes} bytes",
        )
    return response
