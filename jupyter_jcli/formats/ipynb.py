"""Serializer and deserializer for Jupyter notebook files."""

from copy import deepcopy
from pathlib import Path

import nbformat

from jupyter_jcli.formats.model import ParsedFile


def from_node(nb: nbformat.NotebookNode, *, source_path: str = "") -> ParsedFile:
    """Wrap a NotebookNode in the shared document model."""
    return ParsedFile(
        notebook=nb,
        source_path=source_path,
        paired_ipynb=source_path or None,
    )


def load(path: str | Path) -> ParsedFile:
    """Read a Jupyter notebook into the shared document model."""
    source_path = str(path)
    return from_node(nbformat.read(source_path, as_version=4), source_path=source_path)


def loads(text: str) -> ParsedFile:
    """Parse Jupyter notebook JSON into the shared document model."""
    return from_node(nbformat.reads(text, as_version=4))


def to_node(parsed: ParsedFile) -> nbformat.NotebookNode:
    """Return a writable NotebookNode without mutating the wrapped notebook."""
    notebook = deepcopy(parsed.notebook)
    if parsed.kernel_name:
        kernelspec = notebook.metadata.setdefault("kernelspec", {})
        kernelspec.setdefault("display_name", parsed.kernel_name)
        kernelspec.setdefault("language", "python")
    return notebook


def dump(parsed: ParsedFile, path: str | Path) -> None:
    """Serialize the shared document model to a Jupyter notebook file."""
    nbformat.write(to_node(parsed), str(path))
