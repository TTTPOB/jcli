"""jcli exec — execute code or cells from files."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

import click

from jupyter_jcli._enums import ResponseStatus
from jupyter_jcli.cli import CliContext, pass_ctx
from jupyter_jcli.commands._display import display_path
from jupyter_jcli.commands._server_errors import server_error_code
from jupyter_jcli.executor import (
    format_outputs_human,
    process_outputs,
    summarize_outputs,
)
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
    "--stream/--no-stream",
    default=True,
    show_default=True,
    help="Show human output live and periodically save notebook output",
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
    stream: bool,
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
        emit_error(server_error_code(e, "SESSION_NOT_FOUND"), str(e), ctx.use_json)
        return  # unreachable but helps type checker

    # Direct code execution
    if code:
        _exec_code(ctx, kernel_id, code, display_mode, timeout, stream)
        return

    # File-based execution
    _exec_file(ctx, kernel_id, file_path, cell, display_mode, timeout, stream)


def _exec_code(
    ctx: CliContext,
    kernel_id: str,
    code: str,
    display_mode: str,
    timeout: int | None,
    stream: bool,
):
    """Execute inline code."""
    try:
        from jupyter_jcli.kernel import (
            ExecutionObserverError,
            ExecutionTimeout,
            KernelInterruptFailed,
            execute_code,
        )

        live_output = None
        if stream and not ctx.use_json:
            from jupyter_jcli.streaming import HumanOutputStreamer

            live_output = HumanOutputStreamer()

        observer = None
        if live_output is not None:
            observer = lambda message, outputs, changed, count: live_output.observe(
                None, message, outputs, changed, count
            )

        execution_error = None
        observer_failure = None
        try:
            result = execute_code(
                ctx.config.server_url,
                ctx.config.token,
                kernel_id,
                code,
                timeout if timeout is not None else 10,
                display_mode,
                observer=observer,
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
        raw_outputs = result.get("outputs", [])
        from jupyter_jcli.outputs.store import persist_inline_outputs

        stored = persist_inline_outputs(raw_outputs)
        if stored is not None:
            outputs = stored.outputs
        elif live_output is not None:
            outputs = summarize_outputs(raw_outputs)
        else:
            outputs = process_outputs(raw_outputs)
        status = (
            ResponseStatus.OK if result.get("status") == "ok" else ResponseStatus.ERROR
        )
        response = {"status": status, "outputs": outputs}
        if stored is not None:
            response["output_manifest"] = str(stored.manifest_path)

        if ctx.use_json:
            emit(response, use_json=True)
        elif live_output is not None:
            live_output.finish_inline(
                raw_outputs,
                outputs,
                output_manifest=str(stored.manifest_path)
                if stored is not None
                else None,
            )
            if live_output.truncated and stored is None:
                stored = persist_inline_outputs(raw_outputs, text_budget=0)
                if stored is not None:
                    response["output_manifest"] = str(stored.manifest_path)
                    live_output.report_outputs_saved(str(stored.manifest_path))
        else:
            text = format_outputs_human(outputs)
            if stored is not None:
                saved = f"Outputs saved: {stored.manifest_path}"
                text = f"{text}\n{saved}" if text else saved
            if text:
                emit({"_human": text}, use_json=False)

        if observer_failure is not None:
            _emit_execution_error(ctx, observer_failure)
        if execution_error is not None:
            _emit_execution_error(ctx, execution_error)
        if status != ResponseStatus.OK:
            emit_error("EXECUTION_ERROR", "Code execution failed", ctx.use_json)

    except Exception as e:  # noqa: BLE001 - normalize execution failures for CLI output
        _emit_execution_error(ctx, e)


def _exec_file(
    ctx: CliContext,
    kernel_id: str,
    file_path: str,
    cell_spec: str | None,
    display_mode: str,
    timeout: int | None,
    stream: bool,
):
    """Execute cells from a file.

    If *timeout* is None, each cell gets a 10s per-cell timeout with no
    overall limit.  If specified, *timeout* is the total wall-clock budget
    shared across all cells.
    """
    try:
        from jupyter_jcli.file_execution import execute_file
        from jupyter_jcli.streaming import HumanOutputStreamer

        live_output = HumanOutputStreamer() if stream and not ctx.use_json else None
        observer = None
        if live_output is not None:

            def observe_cell(
                cell_index, message, outputs, changed_indices, execution_count
            ):
                live_output.observe(
                    cell_index, message, outputs, changed_indices, execution_count
                )

            observer = observe_cell

        def emit_cell(event):
            _emit_file_cell_result(ctx, event, live_output=live_output)

        summary = execute_file(
            ctx.config.server_url,
            ctx.config.token,
            kernel_id,
            file_path,
            cell_spec,
            display_mode,
            timeout,
            on_cell=emit_cell,
            on_cell_start=(live_output.start_cell if live_output is not None else None),
            observer=observer,
            writeback=write_outputs_to_notebook,
            periodic_writeback=stream,
        )
        if ctx.use_json:
            summary_data = {"cells_executed": summary.cells_executed}
            if summary.notebook_updated:
                summary_data["notebook_updated"] = True
            _emit_jsonl({"status": ResponseStatus.OK, "summary": summary_data})

    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 - normalize execution failures for CLI output
        _emit_execution_error(ctx, e)


def _emit_execution_error(ctx: CliContext, error: Exception) -> None:
    from jupyter_jcli.file_execution import (
        CellExecutionFailed,
        NoCodeCellsError,
        OutputSummaryError,
        TotalExecutionTimeout,
    )
    from jupyter_jcli.kernel import (
        ExecutionObserverError,
        ExecutionTimeout,
        KernelInterruptFailed,
    )
    from jupyter_jcli.outputs.store import OutputStoreError

    if isinstance(error, OutputStoreError):
        data = {
            "status": ResponseStatus.ERROR,
            "code": "OUTPUT_SAVE_FAILED",
            "message": str(error),
            "outputs": error.outputs,
        }
        if ctx.use_json:
            emit(data, use_json=True)
        else:
            diagnostic = format_outputs_human(error.outputs)
            message = f"ERROR [OUTPUT_SAVE_FAILED]: {error}"
            if diagnostic:
                message += f"\n{diagnostic}"
            emit({"_human": message}, use_json=False)
        raise SystemExit(1)
    if isinstance(error, OutputSummaryError):
        emit_error("OUTPUT_SUMMARY_FAILED", str(error), ctx.use_json)
    if isinstance(error, NoCodeCellsError):
        emit_error("PARSE_ERROR", str(error), ctx.use_json)
    if isinstance(error, ExecutionObserverError):
        if isinstance(error.kernel_error, ExecutionTimeout):
            emit_error("TIMEOUT", str(error), ctx.use_json)
        if isinstance(error.kernel_error, KernelInterruptFailed):
            emit_error("INTERRUPT_FAILED", str(error), ctx.use_json)
        emit_error("EXECUTION_ERROR", str(error), ctx.use_json)
    if isinstance(error, ExecutionTimeout):
        emit_error("TIMEOUT", str(error), ctx.use_json)
    if isinstance(error, TotalExecutionTimeout):
        emit_error("TIMEOUT", str(error), ctx.use_json)
    if isinstance(error, KernelInterruptFailed):
        emit_error("INTERRUPT_FAILED", str(error), ctx.use_json)
    if isinstance(error, CellExecutionFailed):
        emit_error("EXECUTION_ERROR", str(error), ctx.use_json)
    emit_error("EXECUTION_ERROR", str(error), ctx.use_json)


def _emit_file_cell_result(
    ctx: CliContext,
    event: FileCellEvent,
    *,
    live_output=None,
) -> None:
    if ctx.use_json:
        cell_payload = {
            "cell_index": event.cell_index,
            "source_preview": event.source_preview,
            "outputs": event.outputs,
            "execution_count": event.execution_count,
        }
        if (
            event.notebook_cell_index is not None
            and event.notebook_cell_index != event.cell_index
        ):
            cell_payload["notebook_cell_index"] = event.notebook_cell_index
        data = {"status": event.status, "cell": cell_payload}
        if event.notebook_created:
            data["notebook_created"] = event.notebook_created
        if event.notebook_updated:
            data["notebook_updated"] = True
        if event.output_manifest:
            data["output_manifest"] = event.output_manifest
        _emit_jsonl(data)
        return

    if live_output is not None:
        location = event.output_manifest
        if event.notebook_updated:
            notebook_cell_index = (
                event.notebook_cell_index
                if event.notebook_cell_index is not None
                else event.cell_index
            )
            location = f"{event.notebook_updated}#cell={notebook_cell_index}"
        live_output.finish_cell(
            replace(
                event,
                notebook_created=(
                    display_path(event.notebook_created)
                    if event.notebook_created
                    else None
                ),
            ),
            location=location,
        )
        if live_output.truncated and location is None:
            from jupyter_jcli.outputs.store import persist_inline_outputs

            stored = persist_inline_outputs(event.raw_outputs, text_budget=0)
            if stored is not None:
                live_output.report_outputs_saved(str(stored.manifest_path))
        return

    parts = [f"--- cell {event.cell_index} ---"]
    text = format_outputs_human(event.outputs)
    if text:
        parts.append(text)
    if event.notebook_created:
        parts.append(f"Notebook created: {display_path(event.notebook_created)}")
    if event.notebook_updated:
        parts.append("Notebook updated")
    if (
        event.notebook_cell_index is not None
        and event.notebook_cell_index != event.cell_index
    ):
        parts.append(f"Notebook cell: {event.notebook_cell_index}")
    if event.output_manifest:
        parts.append(f"Outputs saved: {event.output_manifest}")
    emit({"_human": "\n".join(parts)}, use_json=False)


def _emit_jsonl(data: dict) -> None:
    click.echo(json.dumps(data, ensure_ascii=False))
