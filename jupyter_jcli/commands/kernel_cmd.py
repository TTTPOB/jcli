"""jcli kernel — kernel interrupt/restart."""

import click

from jupyter_jcli._enums import ResponseStatus
from jupyter_jcli.cli import CliContext, pass_ctx
from jupyter_jcli.output import emit, emit_error
from jupyter_jcli.session_selector import SessionSelectorError


@click.group()
def kernel():
    """Manage kernels (interrupt, restart)."""


@kernel.command("interrupt")
@click.argument("session_selector", metavar="SESSION_SELECTOR")
@pass_ctx
def interrupt(ctx: CliContext, session_selector: str):
    """Interrupt a kernel selected by ID, short ID, or name."""
    try:
        session_id, kernel_id = ctx.server.resolve_kernel(session_selector)
    except SessionSelectorError as e:
        emit_error(e.code, str(e), ctx.use_json)
        return
    except Exception as e:  # noqa: BLE001 - normalize command failures for CLI output
        emit_error("KERNEL_NOT_FOUND", str(e), ctx.use_json)
        return

    try:
        ctx.server.interrupt_kernel(kernel_id)
        emit(
            {
                "status": ResponseStatus.OK,
                "_human": f"Interrupted kernel {kernel_id} (session {session_id})",
            },
            use_json=ctx.use_json,
        )
    except Exception as e:  # noqa: BLE001 - normalize server failures for CLI output
        emit_error("KERNEL_NOT_FOUND", str(e), ctx.use_json)


@kernel.command("restart")
@click.argument("session_selector", metavar="SESSION_SELECTOR")
@pass_ctx
def restart(ctx: CliContext, session_selector: str):
    """Restart a kernel selected by ID, short ID, or name."""
    try:
        session_id, kernel_id = ctx.server.resolve_kernel(session_selector)
    except SessionSelectorError as e:
        emit_error(e.code, str(e), ctx.use_json)
        return
    except Exception as e:  # noqa: BLE001 - normalize command failures for CLI output
        emit_error("KERNEL_NOT_FOUND", str(e), ctx.use_json)
        return

    try:
        ctx.server.restart_kernel(kernel_id)
        emit(
            {
                "status": ResponseStatus.OK,
                "_human": f"Restarted kernel {kernel_id} (session {session_id})",
            },
            use_json=ctx.use_json,
        )
    except Exception as e:  # noqa: BLE001 - normalize server failures for CLI output
        emit_error("KERNEL_NOT_FOUND", str(e), ctx.use_json)
