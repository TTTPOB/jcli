"""jcli kernelspec — kernel spec management."""

import click

from j_cli.cli import Context, pass_ctx
from j_cli.output import emit, emit_error


@click.group()
def kernelspec():
    """Manage kernel specifications."""


@kernelspec.command("list")
@pass_ctx
def list_specs(ctx: Context):
    """List available kernel specs."""
    try:
        from j_cli.server import list_kernelspecs

        specs = list_kernelspecs(ctx.server_url, ctx.token)

        if ctx.use_json:
            emit({"kernelspecs": specs}, use_json=True)
        else:
            # Table format
            lines = [f"{'NAME':<20} {'DISPLAY_NAME':<20} {'LANGUAGE':<10}"]
            for s in specs:
                lines.append(f"{s['name']:<20} {s['display_name']:<20} {s['language']:<10}")
            emit({"_human": "\n".join(lines)}, use_json=False)

    except Exception as e:
        emit_error("CONNECTION_FAILED", str(e), ctx.use_json)
