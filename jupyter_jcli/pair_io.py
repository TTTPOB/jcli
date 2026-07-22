"""Pure-Python py:percent emitter and ipynb source-only updater."""

from __future__ import annotations

from pathlib import Path

import nbformat

from jupyter_jcli._enums import CellType, OutputPolicy
from jupyter_jcli.cell_alignment import align_cells
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
    ipynb_path: Path,
    cells: list[Cell],
    *,
    output_policy: OutputPolicy = OutputPolicy.PRESERVE,
) -> None:
    """Rewrite .ipynb so its non-empty cells equal `cells`.

    Outputs follow cells through two-way source alignment according to
    ``output_policy``. New cells always start with empty outputs.
    """
    nb = nbformat.read(str(ipynb_path), as_version=4)
    old_nonempty = [c for c in nb.cells if c.source.strip()]
    old_cells = [
        Cell(index=index, cell_type=cell.cell_type, source=cell.source)
        for index, cell in enumerate(old_nonempty)
    ]
    current_cells = [
        Cell(index=index, cell_type=cell.cell_type, source=cell.source)
        for index, cell in enumerate(cells)
    ]
    preserved_outputs: dict[int, tuple[list, int | None]] = {}
    for alignment in align_cells(old_cells, current_cells):
        if (
            alignment.old_index is None
            or alignment.new_index is None
            or alignment.old_cell is None
            or alignment.new_cell is None
            or alignment.old_cell.cell_type != CellType.CODE
            or alignment.new_cell.cell_type != CellType.CODE
            or output_policy == OutputPolicy.CLEAR_ALL
            or (
                output_policy == OutputPolicy.CLEAR_EDITED
                and alignment.kind == "edited"
            )
        ):
            continue
        old_cell = old_nonempty[alignment.old_index]
        preserved_outputs[alignment.new_index] = (
            old_cell.get("outputs", []),
            old_cell.get("execution_count"),
        )

    new_cells = []
    for index, cell in enumerate(cells):
        if cell.cell_type == CellType.CODE:
            nc = nbformat.v4.new_code_cell(cell.source)
            if preserved := preserved_outputs.get(index):
                nc["outputs"], nc["execution_count"] = preserved
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
