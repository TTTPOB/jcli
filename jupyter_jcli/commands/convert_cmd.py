"""jcli convert — convert between .ipynb and py:percent formats."""

from pathlib import Path

import click

from jupyter_jcli import pair_baseline
from jupyter_jcli._enums import OutputPolicy
from jupyter_jcli.formats import ipynb, percent
from jupyter_jcli.formats.model import ParsedFile
from jupyter_jcli.pairing import update_ipynb_sources
from jupyter_jcli.parser import ipynb_path_for_py


@click.group()
def convert():
    """Convert between .ipynb and py:percent (.py) formats."""


def _is_canonical_pair(py_path: Path, ipynb_path: Path) -> bool:
    """Return True when *py_path* and *ipynb_path* are the managed pair."""
    return ipynb_path_for_py(py_path).resolve(strict=False) == ipynb_path.resolve(
        strict=False
    )


def _refresh_pair_baseline(py_path: Path) -> None:
    """Best-effort baseline refresh after a successful canonical pair sync."""
    try:
        canonical_text = percent.canonicalize(py_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return
    pair_baseline.write_baseline(py_path, canonical_text)


def _reject_mixed_cell_ids(parsed: ParsedFile) -> None:
    with_ids = [cell.index for cell in parsed.cells if cell.cell_id is not None]
    without_ids = [cell.index for cell in parsed.cells if cell.cell_id is None]
    if with_ids and without_ids:
        raise click.ClickException(
            "Mixed cell ID state: "
            f"{len(with_ids)} of {len(parsed.cells)} cells have persistent IDs.\n"
            f"Cells with IDs ({len(with_ids)}): "
            f"{', '.join(map(str, with_ids))}\n"
            f"Cells without IDs ({len(without_ids)}): "
            f"{', '.join(map(str, without_ids))}\n"
            "Add IDs to all cells or remove them from all cells before py-to-ipynb."
        )


@convert.command("ipynb-to-py")
@click.argument(
    "in_ipynb", metavar="<in.ipynb>", type=click.Path(exists=True, dir_okay=False)
)
@click.argument("out_py", metavar="<out.py>", type=click.Path(dir_okay=False))
def ipynb_to_py(in_ipynb: str, out_py: str) -> None:
    """Convert a .ipynb file to py:percent format."""
    parsed = ipynb.load(in_ipynb)
    text = percent.dumps(parsed)
    in_ipynb_path = Path(in_ipynb)
    out_py_path = Path(out_py)
    out_py_path.write_text(text, encoding="utf-8")
    if _is_canonical_pair(out_py_path, in_ipynb_path):
        _refresh_pair_baseline(out_py_path)
    click.echo(f"Wrote {out_py}")


@convert.command("py-to-ipynb")
@click.argument(
    "in_py", metavar="<in.py>", type=click.Path(exists=True, dir_okay=False)
)
@click.argument(
    "out_ipynb",
    metavar="[out.ipynb]",
    required=False,
    default=None,
    type=click.Path(dir_okay=False),
)
@click.option(
    "--outputs",
    "output_policy",
    type=click.Choice([policy.value for policy in OutputPolicy]),
    default=OutputPolicy.PRESERVE.value,
    show_default=True,
    help="How to handle existing code cell outputs.",
)
@click.option(
    "--allow-mixed-cell-ids",
    is_flag=True,
    default=False,
    help="Allow mixed cell ID states (i.e., some cells have IDs, others don't).",
)
def py_to_ipynb(
    in_py: str, out_ipynb: str | None, output_policy: str, allow_mixed_cell_ids: bool
) -> None:
    """Convert a py:percent file to .ipynb format.

    If out.ipynb already exists, only cell sources are updated. Outputs are
    handled according to --outputs. Otherwise a new notebook is created.
    """
    parsed = percent.load(in_py)
    if not allow_mixed_cell_ids:
        _reject_mixed_cell_ids(parsed)
    in_py_path = Path(in_py)

    # Determine output path
    if out_ipynb is None:
        stem = in_py_path.stem
        stem = stem.removesuffix(".dummy")
        out_ipynb = str(in_py_path.parent / f"{stem}.ipynb")

    out_path = Path(out_ipynb)

    if out_path.exists():
        # Update existing notebook sources only
        update_ipynb_sources(
            out_path, parsed.cells, output_policy=OutputPolicy(output_policy)
        )
        if _is_canonical_pair(in_py_path, out_path):
            _refresh_pair_baseline(in_py_path)
        click.echo(f"Updated {out_ipynb}")
    else:
        # Create a new notebook
        ipynb.dump(parsed, out_path)
        if _is_canonical_pair(in_py_path, out_path):
            _refresh_pair_baseline(in_py_path)
        click.echo(f"Wrote {out_ipynb}")
