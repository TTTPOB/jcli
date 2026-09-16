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

from jupyter_jcli.executor import summarize_outputs, summary_fits
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

    def __init__(self, message: str, outputs: list[dict]) -> None:
        super().__init__(message)
        self.outputs = outputs


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

    effective_cwd = (Path.cwd() if cwd is None else Path(cwd)).expanduser().absolute()
    run_id = str(uuid_factory())
    try:
        outputs_root = _safe_outputs_root(effective_cwd)
        run_dir = outputs_root / run_id
        manifest_path = run_dir / _MANIFEST_NAME
        run_dir.mkdir(parents=True, exist_ok=False)
        stored_outputs = _write_payloads(raw_outputs, run_dir)
        created_at = float(clock())
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "managed_by": "jupyter-jcli",
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
    except Exception as error:
        try:
            diagnostics = summarize_outputs(raw_outputs)
        except Exception:  # noqa: BLE001 - keep the primary persistence failure
            diagnostics = [
                {
                    "type": "summary_notice",
                    "truncated": True,
                    "omitted_items": len(raw_outputs),
                    "message": "Execution output diagnostics could not be summarized.",
                }
            ]
        raise OutputStoreError(
            f"Execution completed, but outputs could not be saved: {error}",
            diagnostics,
        ) from error

    try:
        summary = _summary_outputs(stored_outputs, str(manifest_path.resolve()))
    except Exception:  # noqa: BLE001 - published data remains authoritative
        summary = [
            {
                "type": "summary_notice",
                "truncated": True,
                "omitted_items": len(raw_outputs),
                "message": "Execution summary failed; full outputs remain persisted.",
                "complete_outputs": str(manifest_path.resolve()),
            }
        ]
    published = StoredOutputs(
        manifest_path=manifest_path.resolve(),
        outputs=summary,
    )

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


def _safe_outputs_root(workspace: Path) -> Path:
    cli_dir = workspace / ".j-cli"
    outputs_root = cli_dir / "outputs"
    if cli_dir.is_symlink() or (cli_dir.exists() and not cli_dir.is_dir()):
        raise OutputStoreError(".j-cli is not a real directory", [])
    if outputs_root.is_symlink() or (
        outputs_root.exists() and not outputs_root.is_dir()
    ):
        raise OutputStoreError(".j-cli/outputs is not a real directory", [])
    return outputs_root


def _requires_storage(raw_outputs: list[dict], *, text_budget: int) -> bool:
    if not summary_fits(raw_outputs, text_limit=text_budget):
        return True
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
                raise ValueError(f"Invalid base64 data for {mime_type}") from error
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


def _summary_outputs(stored_outputs: list[dict], manifest_path: str) -> list[dict]:
    return summarize_outputs(stored_outputs, full_output_location=manifest_path)


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
