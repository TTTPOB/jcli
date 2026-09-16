"""Workspace persistence for inline execution outputs."""

from __future__ import annotations

import base64
import binascii
import json
import os
import time
import uuid
import warnings
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jupyter_jcli._enums import OutputType
from jupyter_jcli.executor import summarize_outputs
from jupyter_jcli.outputs.contracts import (
    DEFAULT_TEXT_LIMIT,
    RASTER_MIME_TYPES,
    SCHEMA_VERSION,
    OutputProtocolError,
)
from jupyter_jcli.outputs.core import list_outputs, read_output

INLINE_TEXT_BUDGET = 4_000
_MANIFEST_NAME = "manifest.json"
_IMAGE_SUFFIXES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


class OutputStoreError(RuntimeError):
    """Raised after execution when complete outputs cannot be persisted."""


@dataclass(frozen=True)
class StoredOutputs:
    """Published manifest and CLI summary for one execution."""

    manifest_path: Path
    outputs: list[dict]


def persist_inline_outputs(
    raw_outputs: list[dict],
    *,
    cwd: str | Path | None = None,
    text_budget: int = INLINE_TEXT_BUDGET,
    clock=time.time,
    uuid_factory=uuid.uuid4,
) -> StoredOutputs | None:
    """Persist rich or oversized inline outputs below the effective cwd."""
    if not _requires_storage(raw_outputs, text_budget=text_budget):
        return None

    effective_cwd = Path.cwd() if cwd is None else Path(cwd)
    effective_cwd = effective_cwd.resolve()
    run_id = str(uuid_factory())
    run_dir = effective_cwd / ".j-cli" / "outputs" / run_id
    manifest_path = run_dir / _MANIFEST_NAME
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
        stored_outputs = _write_payloads(raw_outputs, run_dir)
        created_at = float(clock())
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "status": "complete",
            "run_id": run_id,
            "created_at": created_at,
            "cwd": str(effective_cwd),
            "source": {
                "kind": "inline",
                "cwd": str(effective_cwd),
                "run_id": run_id,
            },
            "outputs": stored_outputs,
        }
        temporary_manifest = run_dir / ".manifest.json.tmp"
        temporary_manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary_manifest, manifest_path)
        published = StoredOutputs(
            manifest_path=manifest_path.resolve(),
            outputs=_summary_outputs(stored_outputs),
        )
    except Exception as error:
        raise OutputStoreError(
            f"Execution completed, but outputs could not be saved in {run_dir}: {error}"
        ) from error

    try:
        from jupyter_jcli.outputs.cleanup import cleanup_outputs

        cleanup = cleanup_outputs(
            cwd=effective_cwd, protected_run_ids={run_id}, now=created_at
        )
        if cleanup.failed_runs:
            warnings.warn(
                f"Automatic output cleanup was partial: {cleanup.failed_runs}",
                stacklevel=2,
            )
    except Exception as error:  # noqa: BLE001 - cleanup must not hide saved output
        warnings.warn(f"Automatic output cleanup failed: {error}", stacklevel=2)
    return published


def load_manifest(manifest_path: str | Path) -> dict[str, Any]:
    """Load one complete published manifest without modifying the workspace."""
    path = Path(manifest_path).expanduser().resolve()
    if not path.is_file():
        raise OutputProtocolError("OUTPUT_NOT_FOUND", f"Manifest not found: {path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OutputProtocolError(
            "OUTPUT_DATA_INVALID", f"Could not read output manifest {path}: {error}"
        ) from error
    if not isinstance(manifest, dict) or manifest.get("status") != "complete":
        raise OutputProtocolError(
            "OUTPUT_DATA_INVALID", f"Output manifest is not complete: {path}"
        )
    outputs = manifest.get("outputs")
    source = manifest.get("source")
    if not isinstance(outputs, list) or not isinstance(source, dict):
        raise OutputProtocolError(
            "OUTPUT_DATA_INVALID", f"Invalid output manifest structure: {path}"
        )
    return manifest


def list_stored_outputs(manifest_path: str | Path) -> dict[str, Any]:
    """List every physical output in a published inline run."""
    manifest = load_manifest(manifest_path)
    return list_outputs(
        _materialize_outputs(manifest, Path(manifest_path)), source=manifest["source"]
    )


def read_stored_output(
    manifest_path: str | Path,
    output_index: int,
    *,
    mime_type: str | None = None,
    offset: int = 0,
    limit: int | None = DEFAULT_TEXT_LIMIT,
) -> dict[str, Any]:
    """Read one stored output through the shared output protocol."""
    manifest = load_manifest(manifest_path)
    return read_output(
        _materialize_outputs(manifest, Path(manifest_path)),
        output_index,
        source=manifest["source"],
        mime_type=mime_type,
        offset=offset,
        limit=limit,
    )


def _requires_storage(raw_outputs: list[dict], *, text_budget: int) -> bool:
    text_size = 0
    for output in raw_outputs:
        output_type = output.get("output_type")
        if output_type in ("display_data", "execute_result"):
            data = output.get("data", {})
            if not isinstance(data, dict) or set(data) - {"text/plain"}:
                return True
            text_size += len(_text(data.get("text/plain", "")))
        elif output_type == "stream":
            text_size += len(_text(output.get("text", "")))
        elif output_type == "error":
            text_size += len(str(output.get("ename", "")))
            text_size += len(str(output.get("evalue", "")))
            text_size += sum(len(str(line)) for line in output.get("traceback", []))
        else:
            return True
    return text_size > text_budget


def _write_payloads(raw_outputs: list[dict], run_dir: Path) -> list[dict]:
    stored_outputs = deepcopy(raw_outputs)
    for output_index, output in enumerate(stored_outputs):
        data = output.get("data")
        if not isinstance(data, dict):
            continue
        for mime_type in RASTER_MIME_TYPES:
            if mime_type not in data:
                continue
            encoded = _text(data[mime_type])
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as error:
                raise OutputStoreError(
                    f"Invalid base64 data for {mime_type}"
                ) from error
            image_path = (
                run_dir / f"output-{output_index}{_IMAGE_SUFFIXES[mime_type]}"
            ).resolve()
            image_path.write_bytes(raw)
            data[mime_type] = {
                "type": "file",
                "path": str(image_path),
                "mime": mime_type,
                "bytes": len(raw),
            }
    return stored_outputs


def _summary_outputs(stored_outputs: list[dict]) -> list[dict]:
    summaries = []
    for output in stored_outputs:
        data = output.get("data", {})
        image = next(
            (
                value
                for mime in RASTER_MIME_TYPES
                if isinstance(data, dict)
                and isinstance((value := data.get(mime)), dict)
                and value.get("type") == "file"
            ),
            None,
        )
        if image is not None:
            summaries.append(
                {
                    "type": OutputType.IMAGE,
                    "path": image["path"],
                    "mime": image["mime"],
                }
            )
        else:
            summaries.extend(summarize_outputs([output]))
    return summaries


def _materialize_outputs(manifest: dict[str, Any], manifest_path: Path) -> list[dict]:
    outputs = deepcopy(manifest["outputs"])
    run_dir = manifest_path.expanduser().resolve().parent
    for output in outputs:
        data = output.get("data")
        if not isinstance(data, dict):
            continue
        for mime_type in RASTER_MIME_TYPES:
            reference = data.get(mime_type)
            if not isinstance(reference, dict) or reference.get("type") != "file":
                continue
            image_path = Path(str(reference.get("path", "")))
            resolved = image_path.resolve()
            if (
                not image_path.is_absolute()
                or resolved.parent != run_dir
                or image_path.is_symlink()
                or not resolved.is_file()
                or reference.get("mime") != mime_type
            ):
                raise OutputProtocolError(
                    "OUTPUT_DATA_INVALID",
                    f"Invalid stored image reference: {image_path}",
                )
            data[mime_type] = base64.b64encode(resolved.read_bytes()).decode("ascii")
    return outputs


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(part, str) for part in value):
        return "".join(value)
    return str(value)
