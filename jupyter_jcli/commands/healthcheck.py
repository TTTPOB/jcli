"""jcli healthcheck — check if Jupyter server is reachable."""

import click

from jupyter_jcli._enums import ResponseStatus
from jupyter_jcli.cli import Context, pass_ctx
from jupyter_jcli.output import emit, emit_error


@click.command()
@pass_ctx
def healthcheck(ctx: Context):
    """Check if the Jupyter server is reachable."""
    try:
        from jupyter_jcli.server import healthcheck as do_healthcheck

        info = do_healthcheck(ctx.server_url, ctx.token)
        emit(
            {
                "status": ResponseStatus.OK,
                "version": info["version"],
                "kernels_running": info["kernels_running"],
                "_human": f"OK  Jupyter server v{info['version']}  {info['kernels_running']} kernel(s) running",
            },
            use_json=ctx.use_json,
        )
    except Exception as e:
        emit_error("CONNECTION_FAILED", f"Cannot reach Jupyter server at {ctx.server_url}: {e}", ctx.use_json)
