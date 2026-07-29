"""Synchronization operations for py:percent and notebook pairs."""

from copy import deepcopy
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
    old_nodes = list(nb.cells)
    old_cells = [Cell.from_node(index, cell) for index, cell in enumerate(old_nodes)]
    old_indices_by_id: dict[str, int] = {}
    duplicate_old_ids: set[str] = set()
    for index, cell in enumerate(old_cells):
        if cell.cell_id is None or cell.cell_id in duplicate_old_ids:
            continue
        if cell.cell_id in old_indices_by_id:
            old_indices_by_id.pop(cell.cell_id)
            duplicate_old_ids.add(cell.cell_id)
            continue
        old_indices_by_id[cell.cell_id] = index
    aligned_old_indices = {
        alignment.new_index: (alignment.old_index, alignment.kind)
        for alignment in align_cells(old_cells, cells)
        if alignment.old_index is not None and alignment.new_index is not None
    }
    for index, cell in enumerate(cells):
        if cell.cell_id in old_indices_by_id:
            old_index = old_indices_by_id[cell.cell_id]
            kind = (
                "equal"
                if old_cells[old_index].cell_type == cell.cell_type
                and old_cells[old_index].source == cell.source
                else "edited"
            )
            aligned_old_indices[index] = (old_index, kind)

    new_cells = []
    for index, cell in enumerate(cells):
        aligned = aligned_old_indices.get(index)
        if aligned is not None and old_nodes[aligned[0]].cell_type == cell.cell_type:
            new_cell = deepcopy(old_nodes[aligned[0]])
            new_cell.source = cell.source
            if cell.cell_id is not None:
                new_cell.id = cell.cell_id
        else:
            new_cell = deepcopy(cell.node)

        if new_cell.cell_type == CellType.CODE and (
            output_policy == OutputPolicy.CLEAR_ALL
            or (
                output_policy == OutputPolicy.CLEAR_EDITED
                and (aligned is None or aligned[1] != "equal")
            )
        ):
            new_cell.outputs = []
            new_cell.execution_count = None
        new_cells.append(new_cell)

    nb.cells = new_cells
    nbformat.write(nb, str(ipynb_path))
