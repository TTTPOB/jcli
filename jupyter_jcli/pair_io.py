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
    """Update cell.source in an .ipynb file, preserving outputs and metadata.

    Matches the provided cells to non-empty ipynb cells positionally.
    Raises ValueError if the count of non-empty ipynb cells differs from len(cells).
    """
    nb = nbformat.read(str(ipynb_path), as_version=4)

    # Map: position among non-empty cells -> nb.cells index
    nonempty_indices = [i for i, c in enumerate(nb.cells) if c.source.strip()]

    if len(nonempty_indices) != len(cells):
        raise ValueError(
            f"Cell count mismatch: py has {len(cells)} non-empty cells, "
            f"ipynb has {len(nonempty_indices)} non-empty cells"
        )

    for nb_idx, new_cell in zip(nonempty_indices, cells):
        nb.cells[nb_idx].source = new_cell.source

    nbformat.write(nb, str(ipynb_path))


def create_ipynb_from_parsed(parsed: ParsedFile) -> "nbformat.NotebookNode":
    """Create a new NotebookNode from a ParsedFile.

    Caller is responsible for writing to disk with nbformat.write().
    """
    nb = nbformat.v4.new_notebook()

    if parsed.kernel_name:
        nb.metadata["kernelspec"] = {
            "name": parsed.kernel_name,
            "display_name": parsed.kernel_name,
            "language": "python",
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
