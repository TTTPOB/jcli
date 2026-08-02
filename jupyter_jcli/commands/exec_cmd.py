"""jcli exec — execute code or cells from files."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import click

from jupyter_jcli._enums import ResponseStatus
from jupyter_jcli.cli import CliContext, pass_ctx
from jupyter_jcli.executor import format_outputs_human, process_outputs
from jupyter_jcli.notebook_writer import write_outputs_to_notebook
from jupyter_jcli.output import emit, emit_error
from jupyter_jcli.session_selector import SessionSelectorError

if TYPE_CHECKING:
    from jupyter_jcli.file_execution import FileCellEvent


@click.command("exec")
@click.argument("session_selector", metavar="SESSION_SELECTOR")
@click.option("--code", "-c", default=None, help="Code to execute directly")
@click.option(
    "--file", "-f", "file_path", default=None, help="Path to .py or .ipynb file"
)
@click.option("--cell", default=None, help="Cell spec: 3, 3:7, 3:, :5 (0-indexed)")
@click.option(
    "--display-mode",
    type=click.Choice(["last_expr", "all", "last_expr_or_assign", "last", "none"]),
    default="last_expr",
    show_default=True,
    help="IPython expression display mode for code and file execution",
)
@click.option(
    "--timeout",
    default=None,
    type=int,
    help=(
        "Total execution deadline in seconds. At the deadline, j-cli interrupts "
        "the current execution and waits for kernel idle before returning TIMEOUT. "
        "A failed interrupt returns INTERRUPT_FAILED (default: 10s per cell)."
    ),
)
@pass_ctx
def exec_cmd(
    ctx: CliContext,
    session_selector: str,
    code: str | None,
    file_path: str | None,
    cell: str | None,
    display_mode: str,
    timeout: int | None,
):
    """Execute code in a session selected by ID, short ID, or name.

    Either --code or --file (with --cell) must be provided.
    When using --file, outputs are automatically written back to the paired .ipynb.
    """
    if not code and not file_path:
        emit_error(
            "PARSE_ERROR", "Either --code or --file must be provided", ctx.use_json
        )

    try:
        _, kernel_id = ctx.server.resolve_kernel(session_selector)
    except SessionSelectorError as e:
        emit_error(e.code, str(e), ctx.use_json)
        return
    except Exception as e:  # noqa: BLE001 - normalize command failures for CLI output
        emit_error("SESSION_NOT_FOUND", str(e), ctx.use_json)
        return  # unreachable but helps type checker

    # Direct code execution
    if code:
        _exec_code(ctx, kernel_id, code, display_mode, timeout)
        return

    # File-based execution
    _exec_file(ctx, kernel_id, file_path, cell, display_mode, timeout)


def _exec_code(
    ctx: CliContext, kernel_id: str, code: str, display_mode: str, timeout: int | None
):
    """Execute inline code."""
    try:
        from jupyter_jcli.kernel import execute_code

        result = execute_code(
            ctx.config.server_url,
            ctx.config.token,
            kernel_id,
            code,
            timeout if timeout is not None else 10,
            display_mode,
        )
        raw_outputs = result.get("outputs", [])
        outputs = process_outputs(raw_outputs)

        if ctx.use_json:
            emit({"status": ResponseStatus.OK, "outputs": outputs}, use_json=True)
        else:
            text = format_outputs_human(outputs)
            if text:
                emit({"_human": text}, use_json=False)

    except Exception as e:  # noqa: BLE001 - normalize execution failures for CLI output
        _emit_execution_error(ctx, e)


def _exec_file(
    ctx: CliContext,
    kernel_id: str,
    file_path: str,
    cell_spec: str | None,
    display_mode: str,
    timeout: int | None,
):
    """Execute cells from a file.

    If *timeout* is None, each cell gets a 10s per-cell timeout with no
    overall limit.  If specified, *timeout* is the total wall-clock budget
    shared across all cells.
    """
    try:
        from jupyter_jcli.file_execution import execute_file

        summary = execute_file(
            ctx.config.server_url,
            ctx.config.token,
            kernel_id,
            file_path,
            cell_spec,
            display_mode,
            timeout,
            on_cell=lambda event: _emit_file_cell_result(ctx, event),
            writeback=write_outputs_to_notebook,
        )
        if ctx.use_json:
            summary_data = {"cells_executed": summary.cells_executed}
            if summary.notebook_updated:
                summary_data["notebook_updated"] = summary.notebook_updated
            _emit_jsonl({"status": ResponseStatus.OK, "summary": summary_data})

    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 - normalize execution failures for CLI output
        _emit_execution_error(ctx, e)


def _emit_execution_error(ctx: CliContext, error: Exception) -> None:
    from jupyter_jcli.file_execution import (
        CellExecutionFailed,
        NoCodeCellsError,
        TotalExecutionTimeout,
    )
    from jupyter_jcli.kernel import ExecutionTimeout, KernelInterruptFailed

    if isinstance(error, NoCodeCellsError):
        emit_error("PARSE_ERROR", str(error), ctx.use_json)
    if isinstance(error, ExecutionTimeout):
        emit_error("TIMEOUT", str(error), ctx.use_json)
    if isinstance(error, TotalExecutionTimeout):
        emit_error("TIMEOUT", str(error), ctx.use_json)
    if isinstance(error, KernelInterruptFailed):
        emit_error("INTERRUPT_FAILED", str(error), ctx.use_json)
    if isinstance(error, CellExecutionFailed):
        emit_error("EXECUTION_ERROR", str(error), ctx.use_json)
    emit_error("EXECUTION_ERROR", str(error), ctx.use_json)


def _emit_file_cell_result(ctx: CliContext, event: FileCellEvent) -> None:
    if ctx.use_json:
        cell_payload = {
            "cell_index": event.cell_index,
            "source_preview": event.source_preview,
            "outputs": event.outputs,
            "execution_count": event.execution_count,
        }
        data = {"status": event.status, "cell": cell_payload}
        if event.notebook_created:
            data["notebook_created"] = event.notebook_created
        if event.notebook_updated:
            data["notebook_updated"] = event.notebook_updated
        _emit_jsonl(data)
        return

    parts = [f"--- cell {event.cell_index} ---"]
    text = format_outputs_human(event.outputs)
    if text:
        parts.append(text)
    if event.notebook_created:
        parts.append(f"Notebook created: {event.notebook_created}")
    if event.notebook_updated:
        parts.append(f"Notebook updated: {event.notebook_updated}")
    emit({"_human": "\n".join(parts)}, use_json=False)


def _emit_jsonl(data: dict) -> None:
    click.echo(json.dumps(data, ensure_ascii=False))
