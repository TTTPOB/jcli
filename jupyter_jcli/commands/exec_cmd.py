"""jcli exec — execute code or cells from files."""

from pathlib import Path

import click

from jupyter_jcli._enums import ResponseStatus
from jupyter_jcli.cli import Context, pass_ctx
from jupyter_jcli.output import emit, emit_error
from jupyter_jcli.executor import process_outputs, format_outputs_human
from jupyter_jcli.notebook_writer import write_outputs_to_notebook


@click.command("exec")
@click.argument("session_id")
@click.option("--code", "-c", default=None, help="Code to execute directly")
@click.option("--file", "-f", "file_path", default=None, help="Path to .py or .ipynb file")
@click.option("--cell", default=None, help="Cell spec: 3, 3:7, 3:, :5 (0-indexed)")
@click.option("--timeout", default=None, type=int, help="Total execution timeout in seconds (default: 10s per cell)")
@pass_ctx
def exec_cmd(ctx: Context, session_id: str, code: str | None, file_path: str | None, cell: str | None, timeout: int | None):
    """Execute code in a kernel session.

    Either --code or --file (with --cell) must be provided.
    When using --file, outputs are automatically written back to the paired .ipynb.
    """
    if not code and not file_path:
        emit_error("PARSE_ERROR", "Either --code or --file must be provided", ctx.use_json)

    try:
        from jupyter_jcli.server import get_kernel_id_for_session

        kernel_id = get_kernel_id_for_session(ctx.server_url, session_id, ctx.token)
    except Exception as e:
        emit_error("SESSION_NOT_FOUND", str(e), ctx.use_json)
        return  # unreachable but helps type checker

    # Direct code execution
    if code:
        _exec_code(ctx, kernel_id, code, timeout)
        return

    # File-based execution
    _exec_file(ctx, kernel_id, file_path, cell, timeout)


def _exec_code(ctx: Context, kernel_id: str, code: str, timeout: int | None):
    """Execute inline code."""
    try:
        from jupyter_jcli.kernel import execute_code

        result = execute_code(ctx.server_url, ctx.token, kernel_id, code, timeout if timeout is not None else 10)
        raw_outputs = result.get("outputs", [])
        outputs = process_outputs(raw_outputs)

        if ctx.use_json:
            emit({"status": ResponseStatus.OK, "outputs": outputs}, use_json=True)
        else:
            text = format_outputs_human(outputs)
            if text:
                emit({"_human": text}, use_json=False)

    except Exception as e:
        emit_error("EXECUTION_ERROR", str(e), ctx.use_json)


def _exec_file(ctx: Context, kernel_id: str, file_path: str, cell_spec: str | None, timeout: int | None):
    """Execute cells from a file.

    If *timeout* is None, each cell gets a 10s per-cell timeout with no
    overall limit.  If specified, *timeout* is the total wall-clock budget
    shared across all cells.
    """
    try:
        import time as _time

        from jupyter_jcli.parser import parse_file, parse_cell_spec
        from jupyter_jcli.kernel import kernel_connection

        parsed = parse_file(file_path)

        from jupyter_jcli._enums import CellType

        # Determine which cells to execute
        code_cells = [c for c in parsed.cells if c.cell_type == CellType.CODE]
        if cell_spec:
            indices = parse_cell_spec(cell_spec, len(parsed.cells))
            selected = [c for c in parsed.cells if c.index in indices and c.cell_type == CellType.CODE]
        else:
            selected = code_cells

        if not selected:
            emit_error("PARSE_ERROR", "No code cells found to execute", ctx.use_json)

        cell_results = []
        all_outputs_human = []
        deadline = _time.monotonic() + timeout if timeout is not None else None

        with kernel_connection(ctx.server_url, ctx.token, kernel_id) as kernel:
            for cell in selected:
                if deadline is not None:
                    remaining = deadline - _time.monotonic()
                    if remaining <= 0:
                        emit_error("TIMEOUT", f"Total timeout {timeout}s exceeded at cell {cell.index}", ctx.use_json)
                        break
                else:
                    remaining = 10

                result = kernel.execute(cell.source, timeout=remaining)
                raw_outputs = result.get("outputs", [])
                outputs = process_outputs(raw_outputs)

                cell_results.append({
                    "cell_index": cell.index,
                    "source_preview": cell.source[:80].replace("\n", " "),
                    "outputs": outputs,
                    "raw_outputs": raw_outputs,
                    "execution_count": result.get("execution_count"),
                })

                if not ctx.use_json:
                    all_outputs_human.append(f"--- cell {cell.index} ---")
                    text = format_outputs_human(outputs)
                    if text:
                        all_outputs_human.append(text)

        # Auto-create paired .ipynb for py:percent files that have no pair yet
        notebook_created = None
        if parsed.paired_ipynb is None and parsed.is_py_percent and file_path.endswith(".py"):
            from jupyter_jcli.parser import ipynb_path_for_py
            from jupyter_jcli.pair_io import create_ipynb_from_parsed
            import nbformat as _nbformat

            target = ipynb_path_for_py(Path(file_path))
            nb = create_ipynb_from_parsed(parsed)
            _nbformat.write(nb, str(target))
            parsed.paired_ipynb = str(target)
            notebook_created = str(target)

        # Write back to notebook
        notebook_updated = None
        ipynb_path = parsed.paired_ipynb
        if ipynb_path:
            notebook_updated = write_outputs_to_notebook(ipynb_path, cell_results)

        if ctx.use_json:
            # Remove raw_outputs from JSON output (they're internal)
            for cr in cell_results:
                del cr["raw_outputs"]
            data = {"status": ResponseStatus.OK, "cells": cell_results}
            if notebook_created:
                data["notebook_created"] = notebook_created
            if notebook_updated:
                data["notebook_updated"] = notebook_updated
            emit(data, use_json=True)
        else:
            if notebook_created:
                all_outputs_human.append(f"\nNotebook created: {notebook_created}")
            if notebook_updated:
                all_outputs_human.append(f"\nNotebook updated: {notebook_updated}")
            emit({"_human": "\n".join(all_outputs_human)}, use_json=False)

    except SystemExit:
        raise
    except Exception as e:
        emit_error("EXECUTION_ERROR", str(e), ctx.use_json)
