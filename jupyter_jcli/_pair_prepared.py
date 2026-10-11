"""Operation-local inputs and canonical projections for pair synchronization."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

import nbformat

from jupyter_jcli._enums import CellType
from jupyter_jcli.formats import ipynb, percent
from jupyter_jcli.formats.model import Cell, ParsedFile
from jupyter_jcli.metadata import json_compatible_copy, json_equal
from jupyter_jcli.pair_state import PairState, project_shared_metadata

CellTextSnapshot = tuple[bool, tuple[tuple[CellType, str, str | None], ...]]


@dataclass(frozen=True)
class PythonInput:
    """The original text and formatting template for one Python input."""

    text: str
    document: ParsedFile


@dataclass(frozen=True)
class NotebookInput:
    """The original bytes and decoded notebook owned by this operation."""

    content: bytes
    document: ParsedFile


@dataclass(frozen=True)
class PreparedState:
    """A canonical projection, treated as read-only until synchronization ends."""

    state: PairState
    cell_text: str
    snapshot: CellTextSnapshot

    @classmethod
    def from_state(
        cls, state: PairState, *, reuse: tuple[PreparedState, ...] = ()
    ) -> PreparedState:
        # These are exactly the inputs to emit_cell_text, in emission order.
        snapshot = (
            state.include_cell_ids,
            tuple((cell.cell_type, cell.source, cell.cell_id) for cell in state.cells),
        )
        for candidate in reuse:
            if snapshot == candidate.snapshot:
                return cls(state, candidate.cell_text, snapshot)
        return cls(
            state,
            percent.emit_cell_text(
                state.cells, include_cell_ids=state.include_cell_ids
            ),
            snapshot,
        )

    @classmethod
    def from_document(
        cls,
        document: ParsedFile,
        *,
        include_cell_ids: bool,
        reuse: tuple[PreparedState, ...] = (),
    ) -> PreparedState:
        """Project a fresh verification read without copying its saved outputs."""
        return cls.from_state(
            PairState(
                cells=document.cells,
                metadata=project_shared_metadata(document.notebook.metadata),
                include_cell_ids=include_cell_ids,
            ),
            reuse=reuse,
        )

    def matches(self, other: PreparedState) -> bool:
        return self.cell_text == other.cell_text and json_equal(
            self.state.metadata, other.state.metadata
        )

    def canonical_text(self) -> str:
        """Combine the normal shared metadata header with prepared cell text."""
        header = PairState([], self.state.metadata).canonical_text()
        separator = "\n" if header and self.state.cells else ""
        return header + separator + self.cell_text


@dataclass(frozen=True)
class PreparedPair:
    python: PythonInput
    notebook: NotebookInput
    ours: PreparedState
    theirs: PreparedState
    py_ids_need_writeback: bool


def read_python(path: Path, *, missing_ok: bool = False) -> PythonInput:
    text = path.read_text(encoding="utf-8") if not missing_ok or path.exists() else ""
    document = (
        percent.loads(text, source_path=str(path))
        if text
        else ParsedFile(source_path=str(path))
    )
    return PythonInput(text, document)


def read_notebook(path: Path) -> NotebookInput:
    # Retain the existing nbformat text reader; bytes are only the write comparator.
    content = path.read_bytes()
    return NotebookInput(content, ipynb.load(path))


def _canonical_code_projection(
    document: ParsedFile, *, include_ids: bool, original_text: str | None = None
) -> PairState | None:
    cells = document.cells
    if not include_ids or not percent.can_reuse_canonical_code_cells(cells):
        return None
    if original_text is not None and (
        not original_text.endswith("\n")
        or original_text.endswith("\n\n")
        or any(line.rstrip() != line for line in original_text.splitlines())
    ):
        return None
    # Normalization validates all header values, even representation-local ones.
    metadata = project_shared_metadata(json_compatible_copy(document.notebook.metadata))
    # Shared states have source-only independent nodes, just like percent re-parsing.
    # Do not clone the notebook's possibly large saved outputs to make this view.
    projected = [
        Cell.from_node(
            index,
            nbformat.NotebookNode(
                cell_type="code",
                source=cell.source,
                id=cell.cell_id,
                metadata=nbformat.NotebookNode(),
                execution_count=None,
                outputs=[],
            ),
        )
        for index, cell in enumerate(cells)
    ]
    return PairState(projected, metadata, include_cell_ids=include_ids)


def prepare_pair(py_path: Path, ipynb_path: Path) -> PreparedPair:
    python = read_python(py_path)
    include_ids = bool(python.document.stable_cell_ids)
    ours_state = _canonical_code_projection(
        python.document, include_ids=include_ids, original_text=python.text
    )
    py_ids_need_writeback = False
    if ours_state is None:
        preserved = percent.canonicalize(
            python.text, include_cell_ids=None if include_ids else False
        )
        ours_text = percent.canonicalize(python.text, include_cell_ids=include_ids)
        ours_state = PairState.from_parsed(
            percent.loads(ours_text), include_cell_ids=include_ids
        )
        py_ids_need_writeback = ours_text != preserved
    ours = PreparedState.from_state(ours_state)

    notebook = read_notebook(ipynb_path)
    theirs_state = _canonical_code_projection(
        notebook.document, include_ids=include_ids
    )
    if theirs_state is None:
        # ID assignment during emission must not change the writeback input.
        theirs_text = percent.canonicalize(
            percent.dumps(deepcopy(notebook.document), include_cell_ids=include_ids),
            include_cell_ids=None if include_ids else False,
        )
        theirs_state = PairState.from_parsed(
            percent.loads(theirs_text), include_cell_ids=include_ids
        )
    theirs = PreparedState.from_state(theirs_state, reuse=(ours,))
    return PreparedPair(python, notebook, ours, theirs, py_ids_need_writeback)


def prepare_baseline(text: str, pair: PreparedPair) -> PreparedState:
    if text == pair.python.text and not pair.py_ids_need_writeback:
        # Baseline preparation preserves missing IDs; the current side may assign them.
        return pair.ours
    include_ids = pair.ours.state.include_cell_ids
    document = percent.loads(text)
    state = _canonical_code_projection(
        document, include_ids=include_ids, original_text=text
    )
    if state is None:
        canonical = percent._canonicalize_parsed(
            text, document, include_cell_ids=None if include_ids else False
        )
        state = PairState.from_parsed(
            percent.loads(canonical), include_cell_ids=include_ids
        )
    return PreparedState.from_state(state, reuse=(pair.ours, pair.theirs))


def prepare_merged(
    text: str, metadata: dict, pair: PreparedPair, base: PreparedState
) -> PreparedState:
    candidates = (pair.ours, pair.theirs, base)
    for candidate in candidates:
        if text == candidate.cell_text:
            cells = list(candidate.state.cells)
            break
    else:
        # Complementary edits create genuinely new text and must still be parsed.
        cells = percent.loads(text).cells
    return PreparedState.from_state(
        PairState(
            cells=cells,
            metadata=metadata,
            include_cell_ids=pair.ours.state.include_cell_ids,
        ),
        reuse=candidates,
    )
