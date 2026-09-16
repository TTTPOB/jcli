"""Pure extraction and MIME selection for Jupyter outputs."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Collection, Mapping, Sequence
from copy import deepcopy
from typing import Any

from jupyter_jcli.outputs.contracts import (
    DEFAULT_TEXT_LIMIT,
    MAX_TRANSPORT_BYTES,
    RASTER_MIME_TYPES,
    SCHEMA_VERSION,
    OutputProtocolError,
    enforce_transport_budget,
)

_BUNDLE_OUTPUT_TYPES = frozenset(("display_data", "execute_result"))
_CANONICAL_OUTPUT_TYPES = frozenset((*_BUNDLE_OUTPUT_TYPES, "stream", "error"))


def list_outputs(
    outputs: Sequence[Mapping[str, Any]],
    *,
    source: Mapping[str, Any],
    max_transport_bytes: int = MAX_TRANSPORT_BYTES,
) -> dict[str, Any]:
    """Build a compact directory without returning output payloads."""
    entries = [
        _directory_entry(output, output_index)
        for output_index, output in enumerate(outputs)
    ]
    response = {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "source": deepcopy(dict(source)),
        "outputs": entries,
    }
    return enforce_transport_budget(response, max_transport_bytes=max_transport_bytes)


def read_output(
    outputs: Sequence[Mapping[str, Any]],
    output_index: int,
    *,
    source: Mapping[str, Any],
    mime_type: str | None = None,
    supported_mime_types: Collection[str] | None = None,
    offset: int = 0,
    limit: int | None = DEFAULT_TEXT_LIMIT,
    max_transport_bytes: int = MAX_TRANSPORT_BYTES,
) -> dict[str, Any]:
    """Read one physical output and preserve its nbformat structure."""
    if output_index < 0 or output_index >= len(outputs):
        raise OutputProtocolError(
            "OUTPUT_NOT_FOUND", f"Output index out of range: {output_index}"
        )
    _validate_window(offset, limit)

    output = outputs[output_index]
    output_type = str(output.get("output_type", ""))
    if output_type not in _CANONICAL_OUTPUT_TYPES:
        raise OutputProtocolError(
            "OUTPUT_TYPE_UNSUPPORTED",
            f"Unsupported output type: {output_type or '<missing>'}",
        )

    response: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "source": {**deepcopy(dict(source)), "output_index": output_index},
        "output_type": output_type,
        "available_mime_types": _available_mime_types(output),
    }

    if output_type in _BUNDLE_OUTPUT_TYPES:
        response["metadata"] = deepcopy(output.get("metadata", {}))
        if output_type == "execute_result":
            response["execution_count"] = output.get("execution_count")
        selected_mime = select_mime_type(
            response["available_mime_types"],
            mime_type=mime_type,
            supported_mime_types=supported_mime_types,
        )
        data = output.get("data", {})
        response["selected"] = _selected_representation(
            selected_mime,
            data[selected_mime],
            offset=offset,
            limit=limit,
        )
    elif output_type == "stream":
        _require_no_mime(mime_type, output_type)
        text = _text_value(output.get("text", ""), field="stream text")
        response["stream"] = {
            "name": str(output.get("name", "stdout")),
            **_text_payload(text, offset=offset, limit=limit),
        }
    else:
        _require_no_mime(mime_type, output_type)
        if offset != 0 or limit != DEFAULT_TEXT_LIMIT:
            raise OutputProtocolError(
                "WINDOW_NOT_SUPPORTED",
                "Text windows are not supported for error outputs",
            )
        traceback = output.get("traceback", [])
        if not isinstance(traceback, list):
            raise OutputProtocolError(
                "OUTPUT_DATA_INVALID", "Error traceback must be a list"
            )
        response["error"] = {
            "ename": str(output.get("ename", "")),
            "evalue": str(output.get("evalue", "")),
            "traceback": [str(line) for line in traceback],
        }

    return enforce_transport_budget(response, max_transport_bytes=max_transport_bytes)


def select_mime_type(
    available_mime_types: Sequence[str],
    *,
    mime_type: str | None = None,
    supported_mime_types: Collection[str] | None = None,
) -> str:
    """Select one MIME representation with deterministic capability filtering."""
    available = list(available_mime_types)
    supported = set(supported_mime_types) if supported_mime_types is not None else None

    if mime_type is not None:
        if mime_type not in available:
            raise OutputProtocolError(
                "MIME_NOT_FOUND", f"MIME representation not found: {mime_type}"
            )
        if supported is not None and mime_type not in supported:
            raise OutputProtocolError(
                "MIME_NOT_SUPPORTED",
                f"MIME representation is not supported: {mime_type}",
            )
        if not _is_extractable_mime(mime_type):
            raise OutputProtocolError(
                "MIME_NOT_SUPPORTED",
                f"MIME representation is not extractable: {mime_type}",
            )
        return mime_type

    candidates = [
        *RASTER_MIME_TYPES,
        "text/html",
        "text/markdown",
        "text/plain",
        *sorted(mime for mime in available if _is_json_mime(mime)),
        "image/svg+xml",
    ]
    for candidate in candidates:
        if candidate in available and (supported is None or candidate in supported):
            return candidate
    raise OutputProtocolError(
        "MIME_NOT_SUPPORTED",
        "Output has no extractable representation for this adapter",
    )


def _directory_entry(output: Mapping[str, Any], output_index: int) -> dict[str, Any]:
    output_type = str(output.get("output_type", ""))
    entry: dict[str, Any] = {
        "output_index": output_index,
        "output_type": output_type,
        "available_mime_types": _available_mime_types(output),
    }
    if output_type in _BUNDLE_OUTPUT_TYPES:
        entry["metadata"] = deepcopy(output.get("metadata", {}))
    if output_type == "execute_result":
        entry["execution_count"] = output.get("execution_count")
    elif output_type == "stream":
        entry["name"] = str(output.get("name", "stdout"))
    elif output_type == "error":
        entry["ename"] = str(output.get("ename", ""))
        entry["evalue"] = str(output.get("evalue", ""))
    return entry


def _available_mime_types(output: Mapping[str, Any]) -> list[str]:
    if output.get("output_type") not in _BUNDLE_OUTPUT_TYPES:
        return []
    data = output.get("data", {})
    if not isinstance(data, Mapping):
        raise OutputProtocolError(
            "OUTPUT_DATA_INVALID", "Output data must be a mapping"
        )
    return [str(mime_type) for mime_type in data]


def _selected_representation(
    mime_type: str, value: Any, *, offset: int, limit: int | None
) -> dict[str, Any]:
    if mime_type in RASTER_MIME_TYPES:
        if offset != 0 or limit != DEFAULT_TEXT_LIMIT:
            raise OutputProtocolError(
                "WINDOW_NOT_SUPPORTED", "Text windows are not supported for images"
            )
        encoded = _text_value(value, field=mime_type)
        raw = _decode_image(encoded, mime_type)
        return {
            "mime_type": mime_type,
            "encoding": "base64",
            "bytes": len(raw),
            "data": encoded,
        }
    if _is_json_mime(mime_type):
        if offset != 0 or limit != DEFAULT_TEXT_LIMIT:
            raise OutputProtocolError(
                "WINDOW_NOT_SUPPORTED", "Text windows are not supported for JSON"
            )
        try:
            raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        except (TypeError, ValueError) as error:
            raise OutputProtocolError(
                "OUTPUT_DATA_INVALID", f"Invalid JSON MIME value: {error}"
            ) from error
        return {
            "mime_type": mime_type,
            "encoding": "json",
            "bytes": len(raw),
            "data": deepcopy(value),
        }
    if mime_type.startswith("text/") or mime_type == "image/svg+xml":
        text = _text_value(value, field=mime_type)
        return {
            "mime_type": mime_type,
            "encoding": "utf-8",
            **_text_payload(text, offset=offset, limit=limit),
        }
    raise OutputProtocolError(
        "MIME_NOT_SUPPORTED", f"MIME representation is not extractable: {mime_type}"
    )


def _text_payload(text: str, *, offset: int, limit: int | None) -> dict[str, Any]:
    end = len(text) if limit is None else min(offset + limit, len(text))
    selected = text[offset:end]
    truncated = offset > 0 or end < len(text)
    payload: dict[str, Any] = {
        "bytes": len(selected.encode("utf-8")),
        "data": selected,
        "offset": offset,
        "returned_characters": len(selected),
        "total_characters": len(text),
        "truncated": truncated,
    }
    if end < len(text):
        payload["next_offset"] = end
    return payload


def _text_value(value: Any, *, field: str) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(part, str) for part in value):
        return "".join(value)
    raise OutputProtocolError("OUTPUT_DATA_INVALID", f"{field} must be text")


def _decode_image(encoded: str, mime_type: str) -> bytes:
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise OutputProtocolError(
            "OUTPUT_DATA_INVALID", f"Invalid base64 data for {mime_type}"
        ) from error
    valid = {
        "image/png": raw.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": raw.startswith(b"\xff\xd8\xff") and raw.endswith(b"\xff\xd9"),
        "image/webp": len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP",
        "image/gif": raw.startswith((b"GIF87a", b"GIF89a")),
    }[mime_type]
    if not valid:
        raise OutputProtocolError(
            "OUTPUT_DATA_INVALID", f"Data does not match declared MIME type {mime_type}"
        )
    return raw


def _is_json_mime(mime_type: str) -> bool:
    return mime_type == "application/json" or mime_type.endswith("+json")


def _is_extractable_mime(mime_type: str) -> bool:
    return (
        mime_type in RASTER_MIME_TYPES
        or mime_type.startswith("text/")
        or mime_type == "image/svg+xml"
        or _is_json_mime(mime_type)
    )


def _validate_window(offset: int, limit: int | None) -> None:
    if offset < 0:
        raise OutputProtocolError("INVALID_WINDOW", "offset must be non-negative")
    if limit is not None and limit <= 0:
        raise OutputProtocolError("INVALID_WINDOW", "limit must be positive")


def _require_no_mime(mime_type: str | None, output_type: str) -> None:
    if mime_type is not None:
        raise OutputProtocolError(
            "MIME_NOT_APPLICABLE", f"--mime is not applicable to {output_type} outputs"
        )
