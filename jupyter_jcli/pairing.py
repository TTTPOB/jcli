"""Synchronization operations for py:percent and notebook pairs."""

from pathlib import Path

import nbformat

from jupyter_jcli._enums import CellType, OutputPolicy
from jupyter_jcli.cell_alignment import align_cells
from jupyter_jcli.formats.model import Cell


def update_ipynb_sources(
    ipynb_path: Path,
    cells: list[Cell],
    *,
    output_policy: OutputPolicy = OutputPolicy.PRESERVE,
) -> None:
    """Rewrite notebook cells while applying the requested output policy."""
    nb = nbformat.read(str(ipynb_path), as_version=4)
    old_nonempty = [cell for cell in nb.cells if cell.source.strip()]
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
            new_cell = nbformat.v4.new_code_cell(cell.source)
            if preserved := preserved_outputs.get(index):
                new_cell["outputs"], new_cell["execution_count"] = preserved
        elif cell.cell_type == CellType.MARKDOWN:
            new_cell = nbformat.v4.new_markdown_cell(cell.source)
        else:
            new_cell = nbformat.v4.new_raw_cell(cell.source)
        new_cells.append(new_cell)

    nb.cells = new_cells
    nbformat.write(nb, str(ipynb_path))
