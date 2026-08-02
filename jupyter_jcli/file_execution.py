"""Application orchestration for executing cells from a file."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from jupyter_jcli._enums import CellType, ResponseStatus
from jupyter_jcli.executor import process_outputs
from jupyter_jcli.notebook_writer import write_outputs_to_notebook
from jupyter_jcli.parser import ipynb_path_for_py, parse_cell_spec, parse_file


class NoCodeCellsError(ValueError):
    """Raised when a file selection contains no executable code cells."""


class TotalExecutionTimeout(TimeoutError):
    """Raised when a file's total execution deadline expires between cells."""


class CellExecutionFailed(RuntimeError):
    """Raised after an error result has been delivered to the event sink."""

    def __init__(self, cell_index: int) -> None:
        super().__init__(f"Cell {cell_index} execution failed")
        self.cell_index = cell_index


@dataclass(frozen=True)
class FileCellEvent:
    """Raw and processed results plus persistence metadata for one cell."""

    cell_index: int
    source_preview: str
    raw_outputs: list[dict]
    outputs: list[dict]
    execution_count: int | None
    status: ResponseStatus
    notebook_created: str | None = None
    notebook_updated: str | None = None


@dataclass(frozen=True)
class FileExecutionSummary:
    """Summary returned after every selected cell succeeds."""

    cells_executed: int
    notebook_updated: str | None = None


CellEventSink = Callable[[FileCellEvent], None]
NotebookWriteback = Callable[[str, list[dict]], str | None]


def _select_cells(parsed, cell_spec: str | None):
    if cell_spec:
        indices = parse_cell_spec(cell_spec, len(parsed.cells))
        selected = [
            cell
            for cell in parsed.cells
            if cell.index in indices and cell.cell_type == CellType.CODE
        ]
    else:
        selected = [cell for cell in parsed.cells if cell.cell_type == CellType.CODE]

    if not selected:
        raise NoCodeCellsError("No code cells found to execute")
    return selected


def _prepare_notebook(parsed, file_path: str) -> tuple[str | None, str | None]:
    ipynb_path = parsed.paired_ipynb
    notebook_created = None
    if ipynb_path is None and parsed.is_py_percent and file_path.endswith(".py"):
        from jupyter_jcli.formats import ipynb

        target = ipynb_path_for_py(Path(file_path))
        ipynb.dump(parsed, target)
        parsed.paired_ipynb = str(target)
        ipynb_path = str(target)
        notebook_created = str(target)
    return ipynb_path, notebook_created


def execute_file(
    server_url: str,
    token: str | None,
    kernel_id: str,
    file_path: str,
    cell_spec: str | None,
    display_mode: str,
    timeout: int | None,
    *,
    on_cell: CellEventSink | None = None,
    writeback: NotebookWriteback | None = None,
) -> FileExecutionSummary:
    """Execute selected file cells while keeping the kernel context open.

    The event sink receives raw kernel outputs after each cell is written back.
    A failed cell still reaches the sink before this function raises, allowing
    the context managers to restore the kernel state before the caller maps the
    error to a CLI response.
    """
    from jupyter_jcli.kernel import (
        execute_with_timeout,
        expression_display_mode,
        kernel_connection,
    )

    parsed = parse_file(file_path)
    selected = _select_cells(parsed, cell_spec)
    ipynb_path, notebook_created = _prepare_notebook(parsed, file_path)

    if writeback is None:
        writeback = write_outputs_to_notebook

    cells_executed = 0
    last_notebook_updated = None

    with (
        kernel_connection(server_url, token, kernel_id) as kernel,
        expression_display_mode(kernel, display_mode, timeout=10),
    ):
        deadline = time.monotonic() + timeout if timeout is not None else None
        for cell in selected:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TotalExecutionTimeout(
                        f"Total timeout {timeout}s exceeded at cell {cell.index}"
                    )
            else:
                remaining = 10

            result = execute_with_timeout(kernel, cell.source, timeout=remaining)
            execution_status = (
                ResponseStatus.OK
                if result.get("status") == "ok"
                else ResponseStatus.ERROR
            )
            raw_outputs = result.get("outputs", [])
            outputs = process_outputs(raw_outputs)

            cell_result = {
                "cell_index": cell.index,
                "source_preview": cell.source[:80].replace("\n", " "),
                "outputs": outputs,
                "raw_outputs": raw_outputs,
                "execution_count": result.get("execution_count"),
            }

            notebook_updated = None
            if ipynb_path:
                notebook_updated = writeback(ipynb_path, [cell_result])
                if notebook_updated is None:
                    raise RuntimeError(f"Notebook writeback failed: {ipynb_path}")
                last_notebook_updated = notebook_updated

            event = FileCellEvent(
                cell_index=cell.index,
                source_preview=cell.source[:80].replace("\n", " "),
                raw_outputs=raw_outputs,
                outputs=outputs,
                execution_count=result.get("execution_count"),
                status=execution_status,
                notebook_created=notebook_created,
                notebook_updated=notebook_updated,
            )
            cells_executed += 1
            if on_cell is not None:
                on_cell(event)
            notebook_created = None

            if execution_status != ResponseStatus.OK:
                raise CellExecutionFailed(cell.index)

    return FileExecutionSummary(
        cells_executed=cells_executed,
        notebook_updated=last_notebook_updated,
    )
