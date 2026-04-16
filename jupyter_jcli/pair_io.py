"""Pure-Python py:percent emitter and ipynb source-only updater."""

from __future__ import annotations

from pathlib import Path

import nbformat

from jupyter_jcli._enums import CellType
from jupyter_jcli.parser import Cell, ParsedFile


def emit_py_percent(parsed: ParsedFile) -> str:
    """Emit py:percent text from a ParsedFile.

    Uses front_matter_raw if present; otherwise synthesizes a minimal header
    from parsed.kernel_name. Skips cells with empty source.
    """
    parts: list[str] = []

    # Front matter
    if parsed.front_matter_raw is not None:
        parts.append(parsed.front_matter_raw)
        if not parsed.front_matter_raw.endswith("\n"):
            parts.append("\n")
        parts.append("\n")  # blank line after header
    elif parsed.kernel_name is not None:
        parts.append("# ---\n")
        parts.append("# jupyter:\n")
        parts.append("#   kernelspec:\n")
        if parsed.kernel_display_name is not None:
            parts.append(f"#     display_name: {parsed.kernel_display_name}\n")
        if parsed.kernel_language is not None:
            parts.append(f"#     language: {parsed.kernel_language}\n")
        parts.append(f"#     name: {parsed.kernel_name}\n")
        parts.append("# ---\n")
        parts.append("\n")

    # Cells
    for cell in parsed.cells:
        if not cell.source.strip():
            continue  # skip empty cells

        if cell.cell_type == CellType.CODE:
            parts.append("# %%\n")
            parts.append(cell.source)
            if not cell.source.endswith("\n"):
                parts.append("\n")
            parts.append("\n")

        elif cell.cell_type == CellType.MARKDOWN:
            parts.append("# %% [markdown]\n")
            for line in cell.source.splitlines():
                parts.append(f"# {line}\n" if line else "#\n")
            parts.append("\n")

        else:  # raw or unknown
            parts.append(f"# %% [{cell.cell_type.value}]\n")
            for line in cell.source.splitlines():
                parts.append(f"# {line}\n" if line else "#\n")
            parts.append("\n")

    return "".join(parts)


def update_ipynb_sources(ipynb_path: Path, cells: list[Cell]) -> None:
    """Rewrite .ipynb so its non-empty cells equal `cells`.

    Outputs are preserved for cells whose source exactly matches an old
    non-empty cell (matched by MD5 of source). Changed or new cells start
    with empty outputs — they should be re-run via j-cli exec.
    """
    import hashlib

    def _src_hash(source: str) -> str:
        return hashlib.md5(source.encode()).hexdigest()

    nb = nbformat.read(str(ipynb_path), as_version=4)
    old_nonempty = [c for c in nb.cells if c.source.strip()]

    # Build hash -> (outputs, execution_count) from old non-empty cells.
    # First occurrence wins (avoids duplicate-source ambiguity).
    old_by_hash: dict[str, tuple] = {}
    for c in old_nonempty:
        key = _src_hash(c.source)
        if key not in old_by_hash:
            old_by_hash[key] = (c.get("outputs", []), c.get("execution_count"))

    new_cells = []
    for cell in cells:
        if cell.cell_type == CellType.CODE:
            nc = nbformat.v4.new_code_cell(cell.source)
            key = _src_hash(cell.source)
            if key in old_by_hash:
                nc["outputs"] = old_by_hash[key][0]
                nc["execution_count"] = old_by_hash[key][1]
        elif cell.cell_type == CellType.MARKDOWN:
            nc = nbformat.v4.new_markdown_cell(cell.source)
        else:
            nc = nbformat.v4.new_raw_cell(cell.source)
        new_cells.append(nc)

    nb.cells = new_cells
    nbformat.write(nb, str(ipynb_path))


def create_ipynb_from_parsed(parsed: ParsedFile) -> "nbformat.NotebookNode":
    """Create a new NotebookNode from a ParsedFile.

    Caller is responsible for writing to disk with nbformat.write().
    """
    nb = nbformat.v4.new_notebook()

    if parsed.kernel_name:
        nb.metadata["kernelspec"] = {
            "name": parsed.kernel_name,
            "display_name": parsed.kernel_display_name or parsed.kernel_name,
            "language": parsed.kernel_language or "python",
        }

    for cell in parsed.cells:
        if not cell.source.strip():
            continue  # skip empty cells
        if cell.cell_type == CellType.CODE:
            nb.cells.append(nbformat.v4.new_code_cell(cell.source))
        elif cell.cell_type == CellType.MARKDOWN:
            nb.cells.append(nbformat.v4.new_markdown_cell(cell.source))
        else:
            nb.cells.append(nbformat.v4.new_raw_cell(cell.source))

    return nb
