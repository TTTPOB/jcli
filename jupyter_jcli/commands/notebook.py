"""jcli notebook -- inspect notebook cell structure without execution."""

from __future__ import annotations

from typing import TYPE_CHECKING

import click

from jupyter_jcli._enums import ResponseStatus
from jupyter_jcli.formats.model import Cell, ParsedFile
from jupyter_jcli.output import emit, emit_error
from jupyter_jcli.parser import parse_cell_spec, parse_file
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


def _format_show_human(cells: list[Cell]) -> str:
    return "\n".join(
        f"--- cell {cell.index} [{cell.cell_type.value}] ---\n{cell.source}"
        for cell in cells
    )
