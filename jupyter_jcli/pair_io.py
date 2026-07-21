"""Pure-Python py:percent emitter and ipynb source-only updater."""

from __future__ import annotations

from pathlib import Path

import nbformat

from jupyter_jcli._enums import CellType
from jupyter_jcli.parser import Cell, ParsedFile, comment_ipython_magics


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
            source = comment_ipython_magics(cell.source)
            parts.append(source)
            if not source.endswith("\n"):
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


def update_ipynb_sources(
    ipynb_path: Path, cells: list[Cell], *, clean_outputs: bool = False
) -> None:
    """Rewrite .ipynb so its non-empty cells equal `cells`.

    Outputs follow source-matched cells and otherwise stay at the same position.
    When clean_outputs is true, changed and new cells start with empty outputs.
    """
    import hashlib

    def _src_hash(source: str) -> str:
        return hashlib.md5(source.encode()).hexdigest()

    nb = nbformat.read(str(ipynb_path), as_version=4)
    old_nonempty = [c for c in nb.cells if c.source.strip()]

    # Build hash -> (index, outputs, execution_count) from old code cells.
    # First occurrence wins (avoids duplicate-source ambiguity).
    old_by_hash: dict[str, tuple[int, list, int | None]] = {}
    for index, c in enumerate(old_nonempty):
        if c.cell_type != CellType.CODE:
            continue
        key = _src_hash(c.source)
        if key not in old_by_hash:
            old_by_hash[key] = (
                index,
                c.get("outputs", []),
                c.get("execution_count"),
            )

    matched_outputs: dict[int, tuple[list, int | None]] = {}
    matched_old_indices: set[int] = set()
    for index, cell in enumerate(cells):
        if cell.cell_type != CellType.CODE:
            continue
        if match := old_by_hash.get(_src_hash(cell.source)):
            old_index, outputs, execution_count = match
            matched_outputs[index] = (outputs, execution_count)
            matched_old_indices.add(old_index)

    new_cells = []
    for index, cell in enumerate(cells):
        if cell.cell_type == CellType.CODE:
            nc = nbformat.v4.new_code_cell(cell.source)
            outputs = matched_outputs.get(index)
            if (
                outputs is None
                and not clean_outputs
                and index < len(old_nonempty)
                and index not in matched_old_indices
                and old_nonempty[index].cell_type == CellType.CODE
            ):
                old_cell = old_nonempty[index]
                outputs = (old_cell.get("outputs", []), old_cell.get("execution_count"))
            if outputs is not None:
                nc["outputs"], nc["execution_count"] = outputs
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
