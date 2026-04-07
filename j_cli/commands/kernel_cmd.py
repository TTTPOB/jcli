"""jcli kernel — kernel interrupt/restart."""

import click

from j_cli.cli import Context, pass_ctx
from j_cli.output import emit, emit_error


@click.group()
def kernel():
    """Manage kernels (interrupt, restart)."""


@kernel.command("interrupt")
@click.argument("session_id")
@pass_ctx
def interrupt(ctx: Context, session_id: str):
    """Interrupt a running kernel by session ID."""
    try:
        from j_cli.server import get_kernel_id_for_session, interrupt_kernel

        kernel_id = get_kernel_id_for_session(ctx.server_url, session_id, ctx.token)
        interrupt_kernel(ctx.server_url, kernel_id, ctx.token)
        emit(
            {"status": "ok", "_human": f"Interrupted kernel {kernel_id} (session {session_id})"},
            use_json=ctx.use_json,
        )
    except Exception as e:
        emit_error("KERNEL_NOT_FOUND", str(e), ctx.use_json)


@kernel.command("restart")
@click.argument("session_id")
@pass_ctx
def restart(ctx: Context, session_id: str):
    """Restart a kernel by session ID."""
    try:
        from j_cli.server import get_kernel_id_for_session, restart_kernel

        kernel_id = get_kernel_id_for_session(ctx.server_url, session_id, ctx.token)
        restart_kernel(ctx.server_url, kernel_id, ctx.token)
        emit(
            {"status": "ok", "_human": f"Restarted kernel {kernel_id} (session {session_id})"},
            use_json=ctx.use_json,
        )
    except Exception as e:
        emit_error("KERNEL_NOT_FOUND", str(e), ctx.use_json)
