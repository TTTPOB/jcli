"""Notebook document wrapper with format-specific source context."""

from __future__ import annotations

from dataclasses import dataclass

import nbformat

from jupyter_jcli._enums import CellType


@dataclass(frozen=True)
class SourceSpan:
    """Source line range for a cell in a text representation."""

    start_line: int
    end_line: int


class Cell:
    """Indexed view over an nbformat cell with optional source context."""

    def __init__(
        self,
        index: int,
        cell_type: CellType | str,
        source: str,
        source_start_line: int | None = None,
        source_end_line: int | None = None,
        *,
        node: nbformat.NotebookNode | None = None,
    ) -> None:
        self.index = index
        self.node = node if node is not None else _new_cell(cell_type, source)
        self.source_start_line = source_start_line
        self.source_end_line = source_end_line

    @classmethod
    def from_node(
        cls,
        index: int,
        node: nbformat.NotebookNode,
        span: SourceSpan | None = None,
    ) -> Cell:
        """Create a view over an existing nbformat cell."""
        return cls(
            index=index,
            cell_type=node.cell_type,
            source=node.source,
            source_start_line=span.start_line if span else None,
            source_end_line=span.end_line if span else None,
            node=node,
        )

    @property
    def cell_type(self) -> CellType:
        return CellType(self.node.cell_type)

    @property
    def source(self) -> str:
        return self.node.source


class ParsedFile:
    """An nbformat notebook plus context from its source representation."""

    def __init__(
        self,
        kernel_name: str | None = None,
        cells: list[Cell] | None = None,
        source_path: str = "",
        paired_ipynb: str | None = None,
        front_matter_raw: str | None = None,
        is_py_percent: bool = False,
        kernel_display_name: str | None = None,
        kernel_language: str | None = None,
        *,
        notebook: nbformat.NotebookNode | None = None,
        cell_spans: dict[str, SourceSpan] | None = None,
    ) -> None:
        if notebook is not None and cells is not None:
            raise ValueError("notebook and cells cannot both be provided")

        if notebook is None:
            cells = cells or []
            notebook = nbformat.v4.new_notebook(cells=[cell.node for cell in cells])
        self.notebook = notebook
        self.source_path = source_path
        self.paired_ipynb = paired_ipynb
        self.front_matter_raw = front_matter_raw
        self.is_py_percent = is_py_percent
        self.cell_spans = dict(cell_spans or {})

        if cells is not None:
            for cell in cells:
                if (
                    cell.source_start_line is not None
                    and cell.source_end_line is not None
                ):
                    self.cell_spans[cell.node.id] = SourceSpan(
                        cell.source_start_line, cell.source_end_line
                    )

        if kernel_name is not None:
            self.kernel_name = kernel_name
        if kernel_display_name is not None:
            self.kernel_display_name = kernel_display_name
        if kernel_language is not None:
            self.kernel_language = kernel_language

    @property
    def cells(self) -> list[Cell]:
        return [
            Cell.from_node(index, node, self.cell_spans.get(node.id))
            for index, node in enumerate(self.notebook.cells)
        ]

    @property
    def kernel_name(self) -> str | None:
        return self._kernelspec_value("name")

    @kernel_name.setter
    def kernel_name(self, value: str | None) -> None:
        self._set_kernelspec_value("name", value)

    @property
    def kernel_display_name(self) -> str | None:
        return self._kernelspec_value("display_name")

    @kernel_display_name.setter
    def kernel_display_name(self, value: str | None) -> None:
        self._set_kernelspec_value("display_name", value)

    @property
    def kernel_language(self) -> str | None:
        return self._kernelspec_value("language")

    @kernel_language.setter
    def kernel_language(self, value: str | None) -> None:
        self._set_kernelspec_value("language", value)

    def _kernelspec_value(self, key: str) -> str | None:
        value = self.notebook.metadata.get("kernelspec", {}).get(key)
        return str(value) if value is not None else None

    def _set_kernelspec_value(self, key: str, value: str | None) -> None:
        kernelspec = self.notebook.metadata.setdefault("kernelspec", {})
        if value is None:
            kernelspec.pop(key, None)
            if not kernelspec:
                self.notebook.metadata.pop("kernelspec", None)
            return
        kernelspec[key] = value


def _new_cell(cell_type: CellType | str, source: str) -> nbformat.NotebookNode:
    normalized_type = CellType(cell_type)
    if normalized_type == CellType.CODE:
        return nbformat.v4.new_code_cell(source)
    if normalized_type == CellType.MARKDOWN:
        return nbformat.v4.new_markdown_cell(source)
    return nbformat.v4.new_raw_cell(source)
