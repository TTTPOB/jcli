"""Read and manage persisted inline execution outputs."""

from __future__ import annotations

from typing import TYPE_CHECKING

import click

from jupyter_jcli.output import emit, emit_error
from jupyter_jcli.outputs import DEFAULT_TEXT_LIMIT, OutputProtocolError
from jupyter_jcli.outputs.store import list_stored_outputs, read_stored_output

if TYPE_CHECKING:
    from jupyter_jcli.cli import CliContext


@click.group("output")
def output():
    """Read persisted inline execution outputs."""


@output.command("show")
@click.argument("manifest", type=click.Path(dir_okay=False))
@click.option("--output", "output_index", type=int, default=None)
@click.option("--mime", "mime_type", default=None, help="Exact MIME representation")
@click.option("--offset", default=0, type=int, show_default=True, help="Text offset")
@click.option(
    "--limit",
    default=DEFAULT_TEXT_LIMIT,
    type=int,
    show_default=True,
    help="Maximum text characters",
)
@click.pass_obj
def show(
    ctx: CliContext,
    manifest: str,
    output_index: int | None,
    mime_type: str | None,
    offset: int,
    limit: int,
) -> None:
    """List a run or read one complete stored output."""
    try:
        if output_index is None:
            if mime_type is not None or offset != 0 or limit != DEFAULT_TEXT_LIMIT:
                raise OutputProtocolError(
                    "INVALID_WINDOW", "--mime, --offset, and --limit require --output"
                )
            data = list_stored_outputs(manifest)
        else:
            data = read_stored_output(
                manifest,
                output_index,
                mime_type=mime_type,
                offset=offset,
                limit=limit,
            )
    except OutputProtocolError as error:
        emit_error(error.code, error.message, ctx.use_json)
    except Exception as error:  # noqa: BLE001 - normalize reader failures for CLI output
        emit_error("OUTPUT_READ_FAILED", str(error), ctx.use_json)
    emit(data, use_json=ctx.use_json)
