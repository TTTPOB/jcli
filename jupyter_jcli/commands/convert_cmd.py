"""jcli convert — convert between .ipynb and py:percent formats."""

from pathlib import Path

import click
import nbformat

from jupyter_jcli.pair_io import create_ipynb_from_parsed, emit_py_percent, update_ipynb_sources
from jupyter_jcli.parser import parse_ipynb, parse_py_percent


@click.group()
def convert():
    """Convert between .ipynb and py:percent (.py) formats."""


@convert.command("ipynb-to-py")
@click.argument("in_ipynb", metavar="<in.ipynb>", type=click.Path(exists=True, dir_okay=False))
@click.argument("out_py", metavar="<out.py>", type=click.Path(dir_okay=False))
def ipynb_to_py(in_ipynb: str, out_py: str) -> None:
    """Convert a .ipynb file to py:percent format."""
    parsed = parse_ipynb(in_ipynb)
    text = emit_py_percent(parsed)
    Path(out_py).write_text(text, encoding="utf-8")
    click.echo(f"Wrote {out_py}")


@convert.command("py-to-ipynb")
@click.argument("in_py", metavar="<in.py>", type=click.Path(exists=True, dir_okay=False))
@click.argument("out_ipynb", metavar="[out.ipynb]", required=False, default=None,
                type=click.Path(dir_okay=False))
def py_to_ipynb(in_py: str, out_ipynb: str | None) -> None:
    """Convert a py:percent file to .ipynb format.

    If out.ipynb already exists, only cell sources are updated
    (outputs and metadata are preserved). Otherwise a new notebook is created.
    """
    parsed = parse_py_percent(in_py)

    # Determine output path
    if out_ipynb is None:
        py_path = Path(in_py)
        stem = py_path.stem
        if stem.endswith(".dummy"):
            stem = stem[: -len(".dummy")]
        out_ipynb = str(py_path.parent / f"{stem}.ipynb")

    out_path = Path(out_ipynb)

    if out_path.exists():
        # Update existing notebook sources only
        update_ipynb_sources(out_path, parsed.cells)
        click.echo(f"Updated {out_ipynb}")
    else:
        # Create a new notebook
        nb = create_ipynb_from_parsed(parsed)
        nbformat.write(nb, str(out_path))
        click.echo(f"Wrote {out_ipynb}")
