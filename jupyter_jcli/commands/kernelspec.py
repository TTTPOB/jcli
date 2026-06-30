"""jcli kernelspec — kernel spec management."""

import click

from jupyter_jcli.cli import Context, pass_ctx
from jupyter_jcli.output import emit, emit_error


@click.group()
def kernelspec():
    """Manage kernel specifications."""


@kernelspec.command("list")
@pass_ctx
def list_specs(ctx: Context):
    """List available kernel specs."""
    try:
        from jupyter_jcli.server import list_kernelspecs

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


@kernelspec.command("inspect-file")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
@pass_ctx
def inspect_file(ctx: Context, path: str):
    """Inspect kernel metadata declared by a .py or .ipynb file."""
    try:
        from jupyter_jcli.parser import parse_file

        parsed = parse_file(path)
        data = {
            "path": path,
            "kernel_name": parsed.kernel_name,
            "kernel_display_name": parsed.kernel_display_name,
            "kernel_language": parsed.kernel_language,
        }
        if ctx.use_json:
            emit(data, use_json=True)
            return

        kernel_name = parsed.kernel_name or "None"
        emit({"_human": kernel_name}, use_json=False)
    except Exception as e:
        emit_error("INSPECT_FILE_FAILED", str(e), ctx.use_json)
