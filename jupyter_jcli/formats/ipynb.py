"""Serializer and deserializer for Jupyter notebook files."""

from pathlib import Path

import nbformat

from jupyter_jcli._enums import CellType
from jupyter_jcli.formats.model import Cell, ParsedFile


def from_node(nb: "nbformat.NotebookNode", *, source_path: str = "") -> ParsedFile:
    """Convert a NotebookNode to the shared document model."""
    kernelspec = nb.metadata.get("kernelspec", {})
    return ParsedFile(
        kernel_name=kernelspec.get("name"),
        cells=[
            Cell(index=index, cell_type=cell.cell_type, source=cell.source)
            for index, cell in enumerate(nb.cells)
        ],
        source_path=source_path,
        paired_ipynb=source_path or None,
        kernel_display_name=kernelspec.get("display_name") or None,
        kernel_language=kernelspec.get("language") or None,
    )


def load(path: str | Path) -> ParsedFile:
    """Read a Jupyter notebook into the shared document model."""
    source_path = str(path)
    return from_node(nbformat.read(source_path, as_version=4), source_path=source_path)


def loads(text: str) -> ParsedFile:
    """Parse Jupyter notebook JSON into the shared document model."""
    return from_node(nbformat.reads(text, as_version=4))


def to_node(parsed: ParsedFile) -> "nbformat.NotebookNode":
    """Convert the shared document model to a NotebookNode."""
    nb = nbformat.v4.new_notebook()
    if parsed.kernel_name:
        nb.metadata["kernelspec"] = {
            "name": parsed.kernel_name,
            "display_name": parsed.kernel_display_name or parsed.kernel_name,
            "language": parsed.kernel_language or "python",
        }
    for cell in parsed.cells:
        if not cell.source.strip():
            continue
        if cell.cell_type == CellType.CODE:
            nb.cells.append(nbformat.v4.new_code_cell(cell.source))
        elif cell.cell_type == CellType.MARKDOWN:
            nb.cells.append(nbformat.v4.new_markdown_cell(cell.source))
        else:
            nb.cells.append(nbformat.v4.new_raw_cell(cell.source))
    return nb


def dump(parsed: ParsedFile, path: str | Path) -> None:
    """Serialize the shared document model to a Jupyter notebook file."""
    nbformat.write(to_node(parsed), str(path))
