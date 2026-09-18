"""Synchronization operations for py:percent and notebook pairs."""

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import nbformat

from jupyter_jcli._enums import CellChangeKind, CellType, OutputPolicy
from jupyter_jcli.diff import align_cells
from jupyter_jcli.formats import ipynb, percent
from jupyter_jcli.formats.model import Cell, ParsedFile
from jupyter_jcli.pair_state import PairState, metadata_with_local_fields


@dataclass(frozen=True)
class PairSyncResult:
    """Outcome of one shared synchronization execution."""

    state: PairState | None
    drift: object | None
    py_changed: bool = False
    ipynb_changed: bool = False
    baseline_written: bool = False


def synchronize_pair(
    py_path: Path,
    ipynb_path: Path,
    *,
    authoritative: Literal["py", "ipynb"] | None = None,
    output_policy: OutputPolicy = OutputPolicy.PRESERVE,
    strict_baseline: bool = False,
    persist_baseline: bool = True,
    protected_path: Path | None = None,
) -> PairSyncResult:
    """Select, apply, verify, and baseline one target pair state."""
    from jupyter_jcli.diff import (
        BaselineMissing,
        Conflict,
        DriftOnly,
        InSync,
        Merged,
        check_drift,
    )

    if authoritative == "py":
        parsed = percent.load(py_path)
        include_ids = bool(parsed.stable_cell_ids)
        missing_ids = include_ids and any(cell.cell_id is None for cell in parsed.cells)
        if missing_ids and ipynb_path.exists():
            paired = ipynb.load(ipynb_path)
            used_ids = set(parsed.stable_cell_ids)
            for alignment in align_cells(paired, parsed):
                old_cell = alignment.old_cell
                new_cell = alignment.new_cell
                if (
                    old_cell is None
                    or new_cell is None
                    or new_cell.cell_id is not None
                    or old_cell.cell_id is None
                    or old_cell.cell_id in used_ids
                ):
                    continue
                new_cell.node.id = old_cell.cell_id
                parsed.stable_cell_ids.add(old_cell.cell_id)
                used_ids.add(old_cell.cell_id)
        if missing_ids:
            percent.dumps(parsed, include_cell_ids=True, assign_missing_ids=True)
        metadata_changed = _ensure_valid_kernelspec(parsed)
        state = PairState.from_parsed(parsed, include_cell_ids=include_ids)
        py_needs = missing_ids or metadata_changed
        ipynb_needs = True
        drift = None
        should_persist = True
    elif authoritative == "ipynb":
        parsed = ipynb.load(ipynb_path)
        include_ids = bool(parsed.cells) and all(
            cell.cell_id is not None for cell in parsed.cells
        )
        state = PairState.from_parsed(parsed, include_cell_ids=include_ids)
        py_needs = True
        ipynb_needs = False
        drift = None
        should_persist = True
    else:
        drift = check_drift(py_path, ipynb_path, strict_baseline=strict_baseline)
        if isinstance(drift, (Conflict, DriftOnly)):
            return PairSyncResult(state=None, drift=drift)
        if isinstance(drift, InSync):
            parsed = percent.load(py_path)
            state = PairState.from_parsed(
                parsed, include_cell_ids=bool(parsed.stable_cell_ids)
            )
            py_needs = ipynb_needs = False
            should_persist = isinstance(drift.baseline, BaselineMissing)
        elif isinstance(drift, Merged):
            state = drift.target_state
            py_needs = drift.py_needs_update
            ipynb_needs = drift.ipynb_needs_update
            should_persist = True
        else:  # pragma: no cover - closed result union
            raise TypeError(f"Unsupported drift result: {type(drift).__name__}")

    both_need_update = py_needs and ipynb_needs
    if protected_path == py_path and py_needs and not both_need_update:
        raise RuntimeError("edited Python source requires a canonical update")
    if protected_path == ipynb_path and ipynb_needs and not both_need_update:
        raise RuntimeError("edited notebook source requires a canonical update")

    py_changed = apply_pair_state_to_python(py_path, state) if py_needs else False
    ipynb_changed = (
        apply_pair_state_to_ipynb(ipynb_path, state, output_policy=output_policy)
        if ipynb_needs
        else False
    )

    py_state = PairState.from_parsed(
        percent.load(py_path), include_cell_ids=state.include_cell_ids
    )
    nb_state = PairState.from_parsed(
        ipynb.load(ipynb_path), include_cell_ids=state.include_cell_ids
    )
    if py_state != state or nb_state != state:
        raise RuntimeError("pair writeback did not converge to the target state")

    baseline_written = False
    if persist_baseline and should_persist:
        baseline_written = _persist_pair_baseline(
            py_path, state.canonical_text(), strict=strict_baseline
        )
    return PairSyncResult(
        state=state,
        drift=drift,
        py_changed=py_changed,
        ipynb_changed=ipynb_changed,
        baseline_written=baseline_written,
    )


def _ensure_valid_kernelspec(parsed: ParsedFile) -> bool:
    kernelspec = parsed.notebook.metadata.get("kernelspec")
    if kernelspec is None:
        return False
    if not isinstance(kernelspec, dict):
        raise TypeError("metadata.kernelspec must be a mapping")
    name = kernelspec.get("name")
    if name is None:
        raise ValueError("metadata.kernelspec.name is required")
    if "display_name" in kernelspec:
        return False
    kernelspec["display_name"] = str(name)
    return True


def _persist_pair_baseline(py_path: Path, text: str, *, strict: bool) -> bool:
    from jupyter_jcli import pair_baseline
    from jupyter_jcli.gitutil import resolve_git_root

    root_result = resolve_git_root(py_path.parent)
    if root_result.error is not None:
        if strict:
            raise RuntimeError(f"baseline Git lookup failed: {root_result.error}")
        return False
    if root_result.root is None:
        return False
    written = pair_baseline.write_baseline(py_path, text)
    if strict and not written:
        raise RuntimeError(
            "pair synchronized but baseline persistence failed; baseline was not advanced"
        )
    return written


def python_text_for_state(template: ParsedFile, state: PairState) -> str:
    """Apply shared state while retaining Python-local header metadata."""
    metadata = metadata_with_local_fields(template.notebook.metadata, state.metadata)
    front_matter = percent.update_front_matter_metadata(
        template.front_matter_raw, metadata
    )
    parsed = ParsedFile(
        cells=[deepcopy(cell) for cell in state.cells],
        source_path=template.source_path,
        front_matter_raw=front_matter,
    )
    parsed.notebook.metadata = metadata
    return percent.dumps(
        parsed,
        include_cell_ids=state.include_cell_ids,
        assign_missing_ids=state.include_cell_ids,
    )


def apply_pair_state_to_python(py_path: Path, state: PairState) -> bool:
    """Apply state to a Python file and report whether bytes changed."""
    current = py_path.read_text(encoding="utf-8") if py_path.exists() else ""
    template = (
        percent.loads(current, source_path=str(py_path))
        if current
        else ParsedFile(source_path=str(py_path))
    )
    updated = python_text_for_state(template, state)
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
        nb.metadata = metadata_with_local_fields(nb.metadata, state.metadata)
        nbformat.write(nb, str(ipynb_path))
    else:
        parsed = ParsedFile(cells=[deepcopy(cell) for cell in state.cells])
        parsed.notebook.metadata = deepcopy(state.metadata)
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
