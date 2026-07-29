"""jcli notebook -- inspect notebook cell structure without execution."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import click

from jupyter_jcli import pair_baseline
from jupyter_jcli._enums import ResponseStatus
from jupyter_jcli.diff import CellChange, align_cells
from jupyter_jcli.formats import percent
from jupyter_jcli.formats.model import Cell, ParsedFile
from jupyter_jcli.output import emit, emit_error
from jupyter_jcli.parser import find_pair, parse_cell_spec, parse_file
from jupyter_jcli.summ import build_summary_data, format_summary_human

if TYPE_CHECKING:
    from jupyter_jcli.cli import CliContext


@click.group("notebook")
def notebook():
    """Inspect notebook cells."""


@notebook.command("summary")
@click.argument(
    "file_path", metavar="FILE", type=click.Path(exists=True, dir_okay=False)
)
@click.pass_obj
def summary(ctx: CliContext, file_path: str) -> None:
    """Print a deterministic structural summary for each cell in FILE."""
    parsed = _parse_or_error(ctx, file_path)
    data = build_summary_data(parsed)
    if ctx.use_json:
        emit(data, use_json=True)
        return
    emit({"_human": format_summary_human(data)}, use_json=False)


@notebook.command("map")
@click.argument(
    "file_path", metavar="FILE", type=click.Path(exists=True, dir_okay=False)
)
@click.pass_obj
def map_cells(ctx: CliContext, file_path: str) -> None:
    """Map cells between a paired Python file and notebook."""
    source_path = Path(file_path)
    paired_path = find_pair(source_path)
    if paired_path is None:
        emit_error(
            "PAIR_NOT_FOUND", f"No paired file found for: {file_path}", ctx.use_json
        )
        return

    py_path, ipynb_path = (
        (paired_path, source_path)
        if source_path.suffix == ".ipynb"
        else (source_path, paired_path)
    )
    try:
        py_parsed = parse_file(str(py_path))
        ipynb_parsed = parse_file(str(ipynb_path))
        baseline_text = pair_baseline.read_baseline(py_path)
        baseline = percent.loads(baseline_text) if baseline_text is not None else None
    except Exception as error:  # noqa: BLE001 - report mapping failures uniformly
        emit_error("NOTEBOOK_MAP_FAILED", str(error), ctx.use_json)
        return

    data = _build_map_data(py_path, ipynb_path, py_parsed, ipynb_parsed, baseline)
    if ctx.use_json:
        emit(data, use_json=True)
        return
    emit({"_human": _format_map_human(data)}, use_json=False)


@notebook.command("show")
@click.argument(
    "file_path", metavar="FILE", type=click.Path(exists=True, dir_okay=False)
)
@click.option(
    "--cell", "cell_spec", required=True, help="Cell spec: 3, 3:7, 3:, :5 (0-indexed)"
)
@click.pass_obj
def show(ctx: CliContext, file_path: str, cell_spec: str) -> None:
    """Print complete source for selected cells in FILE."""
    parsed = _parse_or_error(ctx, file_path)
    try:
        indices = parse_cell_spec(cell_spec, len(parsed.cells))
    except ValueError:
        emit_error("PARSE_ERROR", f"Invalid cell spec: {cell_spec}", ctx.use_json)
        return

    cells = [cell for cell in parsed.cells if cell.index in indices]
    if not cells:
        emit_error("CELL_NOT_FOUND", f"No cells matched: {cell_spec}", ctx.use_json)
        return

    cell_data = [
        {"index": cell.index, "type": cell.cell_type, "source": cell.source}
        for cell in cells
    ]
    data = _notebook_data(parsed, cell_data)
    if ctx.use_json:
        emit(data, use_json=True)
        return
    emit({"_human": _format_show_human(cells)}, use_json=False)


def _parse_or_error(ctx: CliContext, file_path: str) -> ParsedFile:
    try:
        return parse_file(file_path)
    except Exception as error:  # noqa: BLE001 - report parser failures uniformly
        emit_error("NOTEBOOK_PARSE_FAILED", str(error), ctx.use_json)
        raise AssertionError("emit_error should exit")


def _notebook_data(
    parsed: ParsedFile, cells: list[dict], changes: list[dict] | None = None
) -> dict:
    data = {
        "status": ResponseStatus.OK,
        "path": parsed.source_path,
        "cell_count": len(parsed.cells),
        "kernel": parsed.kernel_name,
        "cells": cells,
    }
    if changes is not None:
        data["changes"] = changes
    return data


def _build_map_data(
    py_path: Path,
    ipynb_path: Path,
    py_parsed: ParsedFile,
    ipynb_parsed: ParsedFile,
    baseline: ParsedFile | None,
) -> dict:
    py_baseline = _changes_by_current_index(baseline, py_parsed)
    ipynb_baseline = _changes_by_current_index(baseline, ipynb_parsed)
    cells = [
        _serialize_mapping(change, py_baseline, ipynb_baseline)
        for change in align_cells(py_parsed, ipynb_parsed)
    ]
    return {
        "status": ResponseStatus.OK,
        "python_path": str(py_path.resolve()),
        "notebook_path": str(ipynb_path.resolve()),
        "baseline_available": baseline is not None,
        "cells": cells,
    }


def _changes_by_current_index(
    baseline: ParsedFile | None, current: ParsedFile
) -> dict[int, CellChange]:
    if baseline is None:
        return {}
    return {
        change.new_index: change
        for change in align_cells(baseline, current)
        if change.new_index is not None
    }


def _serialize_mapping(
    change: CellChange,
    py_baseline: dict[int, CellChange],
    ipynb_baseline: dict[int, CellChange],
) -> dict:
    py_cell = change.old_cell
    ipynb_cell = change.new_cell
    py_change = (
        py_baseline.get(change.old_index) if change.old_index is not None else None
    )
    ipynb_change = (
        ipynb_baseline.get(change.new_index) if change.new_index is not None else None
    )
    return {
        "python_index": change.old_index,
        "notebook_index": change.new_index,
        "cell_id": py_cell.cell_id if py_cell is not None else None,
        "notebook_cell_id": ipynb_cell.cell_id if ipynb_cell is not None else None,
        "type": (py_cell or ipynb_cell).cell_type,
        "source_start_line": (
            py_cell.source_start_line if py_cell is not None else None
        ),
        "source_end_line": py_cell.source_end_line if py_cell is not None else None,
        "alignment": change.alignment,
        "change": change.kind,
        "python_baseline_index": py_change.old_index if py_change is not None else None,
        "notebook_baseline_index": (
            ipynb_change.old_index if ipynb_change is not None else None
        ),
        "python_change": py_change.kind if py_change is not None else None,
        "notebook_change": ipynb_change.kind if ipynb_change is not None else None,
    }


def _format_map_human(data: dict) -> str:
    lines = [
        f"python={data['python_path']}",
        f"notebook={data['notebook_path']}",
        f"baseline={'yes' if data['baseline_available'] else 'no'}",
    ]
    for cell in data["cells"]:
        lines.append(
            " ".join(
                (
                    f"py={cell['python_index']}",
                    f"ipynb={cell['notebook_index']}",
                    f"id={cell['cell_id']}",
                    f"lines={cell['source_start_line']}-{cell['source_end_line']}",
                    f"alignment={cell['alignment']}",
                    f"change={cell['change']}",
                    f"baseline={cell['python_change']}/{cell['notebook_change']}",
                )
            )
        )
    return "\n".join(lines)


def _format_show_human(cells: list[Cell]) -> str:
    return "\n".join(
        f"--- cell {cell.index} [{cell.cell_type.value}] ---\n{cell.source}"
        for cell in cells
    )
