"""jcli session — session management."""

import click

from jcli.cli import Context, pass_ctx
from jcli.output import emit, emit_error


@click.group()
def session():
    """Manage Jupyter sessions."""


@session.command("create")
@click.option("--kernel", "-k", required=True, help="Kernel spec name")
@click.option("--name", "-n", default=None, help="Session name")
@pass_ctx
def create(ctx: Context, kernel: str, name: str | None):
    """Create a new session with the given kernel."""
    try:
        from jcli.server import create_session

        info = create_session(ctx.server_url, kernel, name, ctx.token)
        emit(
            {
                **info,
                "_human": f"Created session {info['session_id']} (kernel: {info['kernel_id']}, spec: {info['kernel_name']})",
            },
            use_json=ctx.use_json,
        )
    except Exception as e:
        emit_error("SESSION_CREATE_FAILED", str(e), ctx.use_json)


@session.command("list")
@pass_ctx
def list_sessions(ctx: Context):
    """List active sessions."""
    try:
        from jcli.server import list_sessions

        sessions = list_sessions(ctx.server_url, ctx.token)

        if ctx.use_json:
            emit({"sessions": sessions}, use_json=True)
        else:
            if not sessions:
                emit({"_human": "No active sessions"}, use_json=False)
                return
            lines = [f"{'SESSION_ID':<40} {'KERNEL_ID':<40} {'KERNEL':<20} {'STATE':<10} {'NAME':<20}"]
            for s in sessions:
                lines.append(
                    f"{s['session_id']:<40} {s['kernel_id']:<40} "
                    f"{s['kernel_name']:<20} {s['kernel_state']:<10} {s['name']:<20}"
                )
            emit({"_human": "\n".join(lines)}, use_json=False)

    except Exception as e:
        emit_error("CONNECTION_FAILED", str(e), ctx.use_json)


@session.command("kill")
@click.argument("session_id")
@pass_ctx
def kill(ctx: Context, session_id: str):
    """Kill (delete) a session."""
    try:
        from jcli.server import delete_session

        delete_session(ctx.server_url, session_id, ctx.token)
        emit(
            {"status": "ok", "_human": f"Killed session {session_id}"},
            use_json=ctx.use_json,
        )
    except Exception as e:
        emit_error("SESSION_NOT_FOUND", str(e), ctx.use_json)
