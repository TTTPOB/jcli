#!/usr/bin/env python3
"""Render .ipynb files as searchable plain text for ripgrep preprocessing."""

from __future__ import annotations

import json
import sys
from pathlib import Path


BINARY_MIME_TYPES = {
    "application/pdf",
    "image/gif",
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/svg+xml",
}

TEXT_MIME_TYPES = {
    "application/javascript",
    "application/json",
    "application/vnd.jupyter.widget-state+json",
    "application/vnd.jupyter.widget-view+json",
    "text/html",
    "text/latex",
    "text/markdown",
    "text/plain",
}


def as_text(value: object) -> str:
    """Convert notebook JSON values to plain text."""
    if isinstance(value, list):
        return "".join(str(item) for item in value)
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def emit_line(text: str = "") -> None:
    """Write one logical line to stdout."""
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")


def emit_block(prefix: str, text: str) -> None:
    """Emit a labeled multi-line block."""
    emit_line(prefix)
    if not text:
        return
    for line in text.splitlines():
        emit_line(line)


def render_output(cell_index: int, output_index: int, output: dict) -> None:
    """Render one cell output."""
    output_type = output.get("output_type", "unknown")
    header = f"## output {cell_index + 1}.{output_index + 1} [{output_type}]"

    if output_type == "stream":
        emit_block(header, as_text(output.get("text", "")))
        return

    if output_type == "error":
        traceback = as_text(output.get("traceback", []))
        if traceback:
            emit_block(header, traceback)
        else:
            emit_line(header)
            emit_line(f"{output.get('ename', 'Error')}: {output.get('evalue', '')}")
        return

    data = output.get("data", {})
    if not isinstance(data, dict):
        emit_block(header, as_text(data))
        return

    emit_line(header)
    for mime_type, value in data.items():
        if mime_type in BINARY_MIME_TYPES:
            payload = as_text(value)
            emit_line(f"[{mime_type} omitted base64 payload of {len(payload)} chars]")
            continue

        if mime_type in TEXT_MIME_TYPES or mime_type.startswith("text/"):
            emit_block(f"[{mime_type}]", as_text(value))
            continue

        if isinstance(value, (dict, list)):
            emit_block(f"[{mime_type}]", json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))
            continue

        emit_block(f"[{mime_type}]", as_text(value))


def render_notebook(path: Path) -> int:
    """Render an .ipynb file into plain text."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            notebook = json.load(handle)
    except FileNotFoundError:
        print(f"Notebook not found: {path}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"Invalid notebook JSON in {path}: {exc}", file=sys.stderr)
        return 1

    emit_line(f"# notebook {path}")

    metadata = notebook.get("metadata", {})
    kernelspec = metadata.get("kernelspec", {})
    if isinstance(kernelspec, dict) and kernelspec.get("name"):
        emit_line(f"# kernelspec {kernelspec['name']}")

    cells = notebook.get("cells", [])
    if not isinstance(cells, list):
        print(f"Notebook cells are not a list in {path}", file=sys.stderr)
        return 1

    for index, cell in enumerate(cells):
        if not isinstance(cell, dict):
            continue

        cell_type = cell.get("cell_type", "unknown")
        cell_id = cell.get("id", "")
        header = f"## cell {index + 1} [{cell_type}]"
        if cell_id:
            header = f"{header} id={cell_id}"
        emit_line("")
        emit_line(header)
        emit_block("## source", as_text(cell.get("source", "")))

        outputs = cell.get("outputs", [])
        if cell_type == "code" and isinstance(outputs, list):
            for output_index, output in enumerate(outputs):
                if isinstance(output, dict):
                    render_output(index, output_index, output)

    return 0


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: rg_ipynb_preprocessor.py <notebook.ipynb>", file=sys.stderr)
        return 1
    return render_notebook(Path(sys.argv[1]))


if __name__ == "__main__":
    sys.exit(main())
