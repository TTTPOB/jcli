"""jcli vars — inspect kernel variables."""

import click

from jupyter_jcli.cli import Context, pass_ctx
from jupyter_jcli.output import emit, emit_error


_ORDERING_NOTE = (
    "NOTE: variables are returned in first-definition order (CPython dict insertion "
    "order). Re-assigning a variable does NOT move it to the end; only "
    "'del x; x = ...' does. Do not infer recency from position in the list."
)
_MTIME_NOTE = (
    "NOTE: the Jupyter debug protocol does not expose per-variable last-modified "
    "timestamps. No 'mtime' or 'last_execution_count' field is available."
)


@click.command("vars")
@click.argument("session_id")
@click.option(
    "--name", "-n", default=None,
    help="Inspect a single named variable instead of listing all.",
)
@click.option(
    "--rich", is_flag=True, default=False,
    help=(
        "Use richInspectVariables (DAP) to get MIME-typed data for a single variable. "
        "Requires --name. Falls back silently if the kernel does not support it."
    ),
)
@click.option(
    "--timeout", default=10, type=float,
    help="Per-request timeout in seconds (default: 10).",
)
@pass_ctx
def vars_cmd(ctx: Context, session_id: str, name: str | None, rich: bool, timeout: float):
    """Inspect variables in a kernel session.

    Lists all global variables in the kernel namespace as a NAME / TYPE / VALUE
    table, or inspects a single variable with --name.

    \b
    Ordering caveat
    ---------------
    Variables are returned in first-definition order (CPython dict insertion
    order).  Re-assigning a variable does NOT move it to the end; only
    "del x; x = ..." does.  Do not infer recency from position.

    \b
    No mtime
    --------
    The Jupyter debug protocol does not expose per-variable last-modified
    timestamps.  No "mtime" or "last_execution_count" field is available in
    the protocol.

    \b
    Source
    ------
    When the kernel advertises debugger support the DAP inspectVariables
    command is used (source="dap").  Otherwise a shell-channel code snippet
    is executed as a fallback (source="fallback").
    """
    if rich and not name:
        emit_error("PARSE_ERROR", "--rich requires --name", ctx.use_json)
        return

    # Resolve kernel
    try:
        from jupyter_jcli.server import get_kernel_id_for_session

        kernel_id = get_kernel_id_for_session(ctx.server_url, session_id, ctx.token)
    except Exception as e:
        emit_error("SESSION_NOT_FOUND", str(e), ctx.use_json)
        return

    # Open connection and inspect
    try:
        from jupyter_jcli.kernel import kernel_connection
        from jupyter_jcli.variables import (
            VariablesUnavailable,
            inspect_variable,
            list_variables,
        )

        with kernel_connection(ctx.server_url, ctx.token, kernel_id) as kernel:
            if name:
                result = inspect_variable(kernel, name, rich=rich, timeout=timeout)
            else:
                result = list_variables(kernel, timeout=timeout)

    except VariablesUnavailable as e:
        emit_error("VARS_UNSUPPORTED", str(e), ctx.use_json)
        return
    except Exception as e:
        emit_error("CONNECTION_FAILED", str(e), ctx.use_json)
        return

    try:
        if name:
            _emit_single(ctx, result, session_id)
        else:
            _emit_list(ctx, result, session_id)
    except Exception as e:
        emit_error("INTERNAL_ERROR", str(e), ctx.use_json)


def _emit_list(ctx: Context, result: dict, session_id: str) -> None:
    variables = result["variables"]
    source = result["source"]

    if ctx.use_json:
        emit(
            {
                "session_id": session_id,
                "source": source,
                "variables": variables,
            },
            use_json=True,
        )
        return

    if not variables:
        emit({"_human": f"No variables found (source: {source})"}, use_json=False)
        return

    lines = [f"{'NAME':<24} {'TYPE':<20} {'VALUE':<40}"]
    lines.append("-" * 86)
    for v in variables:
        name = str(v["name"])[:24]
        typ = str(v["type"])[:20]
        value = str(v["value"])
        # Truncate long values for display
        if len(value) > 40:
            value = value[:37] + "..."
        lines.append(f"{name:<24} {typ:<20} {value:<40}")

    lines.append("")
    lines.append(f"source: {source}  |  {len(variables)} variable(s)")
    lines.append(f"hint: {_ORDERING_NOTE}")
    emit({"_human": "\n".join(lines)}, use_json=False)


def _emit_single(ctx: Context, result: dict, session_id: str) -> None:
    if ctx.use_json:
        emit(result, use_json=True)
        return

    lines = [
        f"name:  {str(result['name'])}",
        f"type:  {str(result['type'])}",
        f"value: {str(result['value'])}",
        f"source: {result['source']}",
    ]
    if "data" in result:
        mimetypes = list(result["data"].keys())
        lines.append(f"mimetypes: {', '.join(mimetypes)}")
    emit({"_human": "\n".join(lines)}, use_json=False)
