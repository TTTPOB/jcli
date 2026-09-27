"""Application orchestration for executing cells from a file."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from jupyter_jcli._enums import CellType, ResponseStatus
from jupyter_jcli.executor import summarize_outputs
from jupyter_jcli.notebook_writer import write_outputs_to_notebook
from jupyter_jcli.outputs.notebook import resolve_execution_notebook_cell
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


class OutputSummaryError(RuntimeError):
    """Raised when saved notebook outputs cannot be summarized for transport."""

    def __init__(
        self,
        cell_index: int,
        notebook_path: str,
        notebook_cell_index: int,
        error: Exception,
    ) -> None:
        super().__init__(
            "Cell execution completed and raw outputs were saved to "
            f"{notebook_path} cell {notebook_cell_index}, but the response summary "
            f"failed: {error}"
        )
        self.cell_index = cell_index
        self.notebook_path = notebook_path
        self.notebook_cell_index = notebook_cell_index


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
    notebook_cell_index: int | None = None
    output_manifest: str | None = None


@dataclass(frozen=True)
class FileExecutionSummary:
    """Summary returned after every selected cell succeeds."""

    cells_executed: int
    notebook_updated: str | None = None


CellEventSink = Callable[[FileCellEvent], None]
CellStartSink = Callable[[int], None]
CellExecutionObserver = Callable[
    [int, dict | None, list[dict], set[int], int | None], None
]
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


def _notebook_cell_targets(
    file_path: str,
    selected: list,
    ipynb_path: str | None,
    notebook_created: str | None,
) -> dict[int, tuple[int, str | None, bool]]:
    """Validate selected sources and resolve write targets before execution."""
    if ipynb_path is None:
        return {}
    source_path = Path(file_path)
    if source_path.suffix == ".ipynb" or notebook_created is not None:
        return {
            cell.index: (
                cell.index,
                cell.node.id,
                bool(cell.node.get("outputs", [])),
            )
            for cell in selected
        }
    targets = {}
    for cell in selected:
        resolved = resolve_execution_notebook_cell(source_path, cell.index)
        targets[cell.index] = (
            resolved.notebook_cell_index,
            resolved.notebook_cell.node.id,
            bool(resolved.notebook_cell.node.get("outputs", [])),
        )
    return targets


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
    on_cell_start: CellStartSink | None = None,
    observer: CellExecutionObserver | None = None,
    writeback: NotebookWriteback | None = None,
    periodic_writeback: bool = True,
) -> FileExecutionSummary:
    """Execute selected file cells while keeping the kernel context open.

    The event sink receives raw kernel outputs after each cell is written back.
    A failed cell still reaches the sink before this function raises, allowing
    the context managers to restore the kernel state before the caller maps the
    error to a CLI response.
    """
    from jupyter_jcli.kernel import (
        ExecutionObserverError,
        ExecutionTimeout,
        KernelInterruptFailed,
        execute_with_timeout,
        expression_display_mode,
        kernel_connection,
    )
    from jupyter_jcli.streaming import NotebookCheckpoint

    parsed = parse_file(file_path)
    selected = _select_cells(parsed, cell_spec)
    ipynb_path, notebook_created = _prepare_notebook(parsed, file_path)
    notebook_cell_targets = _notebook_cell_targets(
        file_path, selected, ipynb_path, notebook_created
    )

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
            target = notebook_cell_targets.get(cell.index, (cell.index, None, False))
            notebook_cell_index, expected_cell_id, had_saved_outputs = target
            checkpoint = None
            if ipynb_path and periodic_writeback:
                checkpoint = NotebookCheckpoint(
                    writeback,
                    ipynb_path,
                    notebook_cell_index,
                    expected_cell_id,
                    cell.source,
                    had_saved_outputs=had_saved_outputs,
                )

            execute_observer = None
            if checkpoint is not None or observer is not None:

                def observe_update(
                    message: dict | None,
                    outputs: list[dict],
                    changed_indices: set[int],
                    execution_count: int | None,
                    *,
                    current_checkpoint=checkpoint,
                    cell_index=cell.index,
                ) -> None:
                    if current_checkpoint is not None:
                        current_checkpoint.observe(
                            message, outputs, changed_indices, execution_count
                        )
                    if observer is not None:
                        observer(
                            cell_index,
                            message,
                            outputs,
                            changed_indices,
                            execution_count,
                        )

                execute_observer = observe_update

            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TotalExecutionTimeout(
                        f"Total timeout {timeout}s exceeded at cell {cell.index}"
                    )
            else:
                remaining = 10

            if on_cell_start is not None:
                on_cell_start(cell.index)

            execution_error = None
            observer_failure = None
            try:
                result = execute_with_timeout(
                    kernel,
                    cell.source,
                    timeout=remaining,
                    observer=execute_observer,
                )
            except ExecutionObserverError as error:
                if error.result is None:
                    raise
                observer_failure = error
                result = error.result
                if isinstance(
                    error.kernel_error, (ExecutionTimeout, KernelInterruptFailed)
                ):
                    execution_error = error.kernel_error
            except (ExecutionTimeout, KernelInterruptFailed) as error:
                if error.partial_result is None:
                    raise
                execution_error = error
                result = error.partial_result
            execution_status = (
                ResponseStatus.OK
                if result.get("status") == "ok"
                else ResponseStatus.ERROR
            )
            raw_outputs = result.get("outputs", [])
            failure = observer_failure or execution_error
            if failure is not None and not any(
                output.get("output_type") == "error" for output in raw_outputs
            ):
                raw_outputs.append(
                    {
                        "output_type": "error",
                        "ename": type(failure).__name__,
                        "evalue": str(failure),
                        "traceback": [],
                    }
                )

            cell_result = {
                "cell_index": notebook_cell_index,
                "expected_cell_id": expected_cell_id,
                "expected_source": cell.source,
                "source_preview": cell.source[:80].replace("\n", " "),
                "raw_outputs": raw_outputs,
                "execution_count": result.get("execution_count"),
            }

            notebook_updated = None
            if ipynb_path:
                try:
                    notebook_updated = writeback(ipynb_path, [cell_result])
                    if notebook_updated is None:
                        raise RuntimeError(f"Notebook writeback failed: {ipynb_path}")
                except Exception as error:
                    if observer_failure is not None:
                        raise observer_failure from error
                    raise
                last_notebook_updated = notebook_updated

            output_manifest = None
            notebook_cell_index = (
                notebook_cell_targets[cell.index][0] if ipynb_path else None
            )
            if ipynb_path is None:
                from jupyter_jcli.outputs.store import persist_inline_outputs

                stored = persist_inline_outputs(raw_outputs)
                if stored is not None:
                    outputs = stored.outputs
                    output_manifest = str(stored.manifest_path)
                else:
                    outputs = summarize_outputs(raw_outputs)
            else:
                assert notebook_cell_index is not None
                location = f"{ipynb_path}#cell={notebook_cell_index}"
                try:
                    outputs = summarize_outputs(
                        raw_outputs, full_output_location=location
                    )
                except Exception as error:
                    raise OutputSummaryError(
                        cell.index, ipynb_path, notebook_cell_index, error
                    ) from error
            event = FileCellEvent(
                cell_index=cell.index,
                source_preview=cell.source[:80].replace("\n", " "),
                raw_outputs=raw_outputs,
                outputs=outputs,
                execution_count=result.get("execution_count"),
                status=execution_status,
                notebook_created=notebook_created,
                notebook_updated=notebook_updated,
                notebook_cell_index=notebook_cell_index,
                output_manifest=output_manifest,
            )
            cells_executed += 1
            if on_cell is not None:
                on_cell(event)
            notebook_created = None

            if observer_failure is not None:
                if execution_error is not None:
                    raise observer_failure from execution_error
                if execution_status != ResponseStatus.OK:
                    raise observer_failure from CellExecutionFailed(cell.index)
                raise observer_failure
            if execution_error is not None:
                raise execution_error
            if execution_status != ResponseStatus.OK:
                raise CellExecutionFailed(cell.index)

    return FileExecutionSummary(
        cells_executed=cells_executed,
        notebook_updated=last_notebook_updated,
    )
