"""Synchronization operations for py:percent and notebook pairs."""

from copy import deepcopy
from pathlib import Path

import nbformat

from jupyter_jcli._enums import CellChangeKind, CellType, OutputPolicy
from jupyter_jcli.diff import align_cells
from jupyter_jcli.formats import ipynb, percent
from jupyter_jcli.formats.model import Cell, ParsedFile
from jupyter_jcli.pair_state import PairState


def python_text_for_state(template: ParsedFile, state: PairState) -> str:
    """Apply shared state while retaining Python-only header formatting."""
    kernel_changed = template.kernel_name != state.kernel_name
    info = state.kernel_info if kernel_changed else None
    display_name = (
        info.display_name if info is not None else template.kernel_display_name
    )
    language = info.language if info is not None else template.kernel_language
    front_matter = template.front_matter_raw
    if kernel_changed and front_matter is not None:
        front_matter = percent.update_front_matter_kernel(
            front_matter,
            name=state.kernel_name,
            display_name=display_name,
            language=language,
        )

    parsed = ParsedFile(
        kernel_name=state.kernel_name,
        kernel_display_name=display_name,
        kernel_language=language,
        cells=[deepcopy(cell) for cell in state.cells],
        source_path=template.source_path,
        front_matter_raw=front_matter,
    )
    return percent.dumps(
        parsed,
        include_cell_ids=state.include_cell_ids,
        assign_missing_ids=state.include_cell_ids,
    )


def apply_pair_state_to_python(py_path: Path, state: PairState) -> bool:
    """Apply state to a Python file and report whether bytes changed."""
    current = py_path.read_text(encoding="utf-8")
    updated = python_text_for_state(
        percent.loads(current, source_path=str(py_path)), state
    )
    if updated == current:
        return False
    py_path.write_text(updated, encoding="utf-8")
    return True


def apply_pair_state_to_ipynb(
    ipynb_path: Path,
    state: PairState,
    *,
    output_policy: OutputPolicy = OutputPolicy.PRESERVE,
) -> bool:
    """Apply state to a notebook while retaining unrelated notebook data."""
    before = ipynb_path.read_bytes() if ipynb_path.exists() else None
    if ipynb_path.exists():
        nb = nbformat.read(str(ipynb_path), as_version=4)
        nb.cells = _updated_cells(nb.cells, state.cells, output_policy=output_policy)
        _apply_kernel_to_notebook(nb, state)
        nbformat.write(nb, str(ipynb_path))
    else:
        parsed = ParsedFile(
            kernel_name=state.kernel_name,
            kernel_display_name=(
                state.kernel_info.display_name if state.kernel_info else None
            ),
            kernel_language=state.kernel_info.language if state.kernel_info else None,
            cells=[deepcopy(cell) for cell in state.cells],
        )
        _apply_kernel_to_notebook(parsed.notebook, state)
        ipynb.dump(parsed, ipynb_path)
    return before != ipynb_path.read_bytes()


def update_ipynb_sources(
    ipynb_path: Path,
    cells: list[Cell],
    *,
    output_policy: OutputPolicy = OutputPolicy.PRESERVE,
) -> None:
    """Rewrite notebook cells while applying the requested output policy."""
    nb = nbformat.read(str(ipynb_path), as_version=4)
    nb.cells = _updated_cells(nb.cells, cells, output_policy=output_policy)
    nbformat.write(nb, str(ipynb_path))


def _apply_kernel_to_notebook(nb: nbformat.NotebookNode, state: PairState) -> None:
    current_name = nb.metadata.get("kernelspec", {}).get("name")
    if current_name == state.kernel_name:
        return
    if state.kernel_name is None:
        nb.metadata.pop("kernelspec", None)
        return

    info = state.kernel_info
    kernelspec = {
        "name": state.kernel_name,
        "display_name": (
            info.display_name if info and info.display_name else state.kernel_name
        ),
    }
    if info is not None and info.language is not None:
        kernelspec["language"] = info.language
    nb.metadata["kernelspec"] = kernelspec


def _updated_cells(
    old_nodes: list,
    cells: list[Cell],
    *,
    output_policy: OutputPolicy,
) -> list:
    old_nodes = list(old_nodes)
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
                CellChangeKind.EQUAL
                if old_cells[old_index].cell_type == cell.cell_type
                and old_cells[old_index].source == cell.source
                else CellChangeKind.EDITED
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
                and (aligned is None or aligned[1] != CellChangeKind.EQUAL)
            )
        ):
            new_cell.outputs = []
            new_cell.execution_count = None
        new_cells.append(new_cell)
    return new_cells
