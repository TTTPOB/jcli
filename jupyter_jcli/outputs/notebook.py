"""Read saved notebook outputs without modifying paired files."""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jupyter_jcli._enums import AlignmentMethod, CellChangeKind
from jupyter_jcli.diff import align_cells
from jupyter_jcli.formats.model import Cell, ParsedFile
from jupyter_jcli.outputs.contracts import (
    DEFAULT_TEXT_LIMIT,
    MAX_TRANSPORT_BYTES,
    OutputProtocolError,
)
from jupyter_jcli.outputs.core import list_outputs, read_output
from jupyter_jcli.parser import find_pair, parse_file


@dataclass(frozen=True)
class ResolvedNotebookCell:
    """A requested cell mapped to its saved notebook cell."""

    requested_path: Path
    requested_cell_index: int
    notebook_path: Path
    notebook_cell_index: int
    mapping: str
    requested_cell: Cell
    notebook_cell: Cell

    @property
    def source(self) -> dict[str, Any]:
        """Return stable provenance included in output protocol responses."""
        data: dict[str, Any] = {
            "kind": "notebook",
            "path": str(self.notebook_path),
            "cell_index": self.notebook_cell_index,
            "requested_path": str(self.requested_path),
            "requested_cell_index": self.requested_cell_index,
            "mapping": self.mapping,
        }
        if self.notebook_cell.cell_id is not None:
            data["cell_id"] = self.notebook_cell.cell_id
        if self.requested_cell.source_start_line is not None:
            data["source_start_line"] = self.requested_cell.source_start_line
            data["source_end_line"] = self.requested_cell.source_end_line
        execution_count = self.notebook_cell.node.get("execution_count")
        if execution_count is not None:
            data["execution_count"] = execution_count
        return data

    @property
    def outputs(self) -> list[Mapping[str, Any]]:
        """Return the physical saved outputs for the resolved cell."""
        outputs = self.notebook_cell.node.get("outputs", [])
        if not isinstance(outputs, list):
            raise OutputProtocolError(
                "OUTPUT_DATA_INVALID", "Notebook cell outputs must be a list"
            )
        return outputs


def resolve_notebook_cell(
    file_path: str | Path, cell_index: int
) -> ResolvedNotebookCell:
    """Resolve an ipynb cell directly or a py:percent cell without guessing."""
    requested_path = Path(file_path).expanduser().resolve()
    if not requested_path.is_file():
        raise OutputProtocolError("FILE_NOT_FOUND", f"File not found: {requested_path}")
    if cell_index < 0:
        raise OutputProtocolError(
            "CELL_NOT_FOUND", f"Cell index must be non-negative: {cell_index}"
        )
    if requested_path.suffix not in (".py", ".ipynb"):
        raise OutputProtocolError(
            "FILE_TYPE_UNSUPPORTED",
            f"Expected a .py or .ipynb file: {requested_path}",
        )

    requested = _parse_notebook_file(requested_path)
    requested_cell = _cell_at(requested, cell_index, requested_path)
    if requested_path.suffix == ".ipynb":
        return ResolvedNotebookCell(
            requested_path=requested_path,
            requested_cell_index=cell_index,
            notebook_path=requested_path,
            notebook_cell_index=cell_index,
            mapping="direct",
            requested_cell=requested_cell,
            notebook_cell=requested_cell,
        )

    paired_path = find_pair(requested_path)
    if paired_path is None:
        raise OutputProtocolError(
            "NOTEBOOK_PAIR_NOT_FOUND",
            f"No paired notebook found for: {requested_path}",
        )
    notebook_path = paired_path.resolve()
    notebook = _parse_notebook_file(notebook_path)
    mapping = _reliable_mapping(requested, notebook, cell_index)
    notebook_cell = _cell_at(notebook, mapping.new_index, notebook_path)
    return ResolvedNotebookCell(
        requested_path=requested_path,
        requested_cell_index=cell_index,
        notebook_path=notebook_path,
        notebook_cell_index=mapping.new_index,
        mapping=mapping.method,
        requested_cell=requested_cell,
        notebook_cell=notebook_cell,
    )


def list_notebook_outputs(
    file_path: str | Path,
    cell_index: int,
    *,
    max_transport_bytes: int = MAX_TRANSPORT_BYTES,
) -> dict[str, Any]:
    """List saved outputs for one reliably resolved notebook cell."""
    resolved = resolve_notebook_cell(file_path, cell_index)
    return list_outputs(
        resolved.outputs,
        source=resolved.source,
        max_transport_bytes=max_transport_bytes,
    )


def read_notebook_output(
    file_path: str | Path,
    cell_index: int,
    output_index: int,
    *,
    mime_type: str | None = None,
    supported_mime_types: Collection[str] | None = None,
    offset: int = 0,
    limit: int | None = DEFAULT_TEXT_LIMIT,
    max_transport_bytes: int = MAX_TRANSPORT_BYTES,
) -> dict[str, Any]:
    """Read one physical saved output from a reliably resolved cell."""
    resolved = resolve_notebook_cell(file_path, cell_index)
    return read_output(
        resolved.outputs,
        output_index,
        source=resolved.source,
        mime_type=mime_type,
        supported_mime_types=supported_mime_types,
        offset=offset,
        limit=limit,
        max_transport_bytes=max_transport_bytes,
    )


@dataclass(frozen=True)
class _Mapping:
    new_index: int
    method: str


def _reliable_mapping(
    requested: ParsedFile, notebook: ParsedFile, cell_index: int
) -> _Mapping:
    requested_cell = requested.cells[cell_index]
    alignments = [
        change
        for change in align_cells(requested, notebook)
        if change.old_index == cell_index
    ]
    if len(alignments) != 1 or alignments[0].new_index is None:
        raise _unreliable_mapping(cell_index, "the cell has no unique notebook match")
    alignment = alignments[0]
    notebook_cell = alignment.new_cell
    assert notebook_cell is not None

    if alignment.alignment == AlignmentMethod.ID:
        cell_id = requested_cell.cell_id
        if (
            cell_id is not None
            and cell_id == notebook_cell.cell_id
            and _id_count(requested, cell_id) == 1
            and _id_count(notebook, cell_id) == 1
        ):
            return _Mapping(alignment.new_index, "id")
        raise _unreliable_mapping(cell_index, "the stable cell ID is not unique")

    key = (requested_cell.cell_type.value, requested_cell.source)
    conflicting_ids = (
        requested_cell.cell_id is not None
        and notebook_cell.cell_id is not None
        and requested_cell.cell_id != notebook_cell.cell_id
    )
    if (
        alignment.alignment == AlignmentMethod.CONTENT
        and alignment.kind == CellChangeKind.EQUAL
        and not conflicting_ids
        and _content_counts(requested)[key] == 1
        and _content_counts(notebook)[key] == 1
    ):
        return _Mapping(alignment.new_index, "content")

    reason = (
        "the matching sources have conflicting cell IDs"
        if conflicting_ids
        else "only positional or ambiguous source alignment is available"
    )
    raise _unreliable_mapping(cell_index, reason)


def _parse_notebook_file(path: Path) -> ParsedFile:
    try:
        return parse_file(str(path))
    except Exception as error:
        raise OutputProtocolError(
            "NOTEBOOK_READ_FAILED", f"Could not parse {path}: {error}"
        ) from error


def _cell_at(parsed: ParsedFile, cell_index: int, path: Path) -> Cell:
    if cell_index < 0 or cell_index >= len(parsed.cells):
        raise OutputProtocolError(
            "CELL_NOT_FOUND", f"Cell index out of range for {path}: {cell_index}"
        )
    return parsed.cells[cell_index]


def _id_count(parsed: ParsedFile, cell_id: str) -> int:
    return sum(cell.cell_id == cell_id for cell in parsed.cells)


def _content_counts(parsed: ParsedFile) -> Counter[tuple[str, str]]:
    return Counter((cell.cell_type.value, cell.source) for cell in parsed.cells)


def _unreliable_mapping(cell_index: int, reason: str) -> OutputProtocolError:
    return OutputProtocolError(
        "CELL_MAPPING_UNRELIABLE",
        f"Cannot reliably map Python cell {cell_index}: {reason}",
    )
