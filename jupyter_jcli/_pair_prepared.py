"""Operation-local inputs and canonical projections for pair synchronization."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from jupyter_jcli.formats import ipynb, percent
from jupyter_jcli.formats.model import ParsedFile
from jupyter_jcli.metadata import json_equal
from jupyter_jcli.pair_state import PairState


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

    @classmethod
    def from_state(cls, state: PairState) -> PreparedState:
        return cls(state, state.cell_text())

    def matches(self, other: PreparedState) -> bool:
        return self.cell_text == other.cell_text and json_equal(
            self.state.metadata, other.state.metadata
        )


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


def prepare_pair(py_path: Path, ipynb_path: Path) -> PreparedPair:
    python = read_python(py_path)
    include_ids = bool(python.document.stable_cell_ids)
    preserved = percent.canonicalize(
        python.text, include_cell_ids=None if include_ids else False
    )
    ours_text = percent.canonicalize(python.text, include_cell_ids=include_ids)
    notebook = read_notebook(ipynb_path)
    # ID assignment during emission must not change the writeback input.
    theirs_text = percent.canonicalize(
        percent.dumps(deepcopy(notebook.document), include_cell_ids=include_ids),
        include_cell_ids=None if include_ids else False,
    )
    ours = PairState.from_parsed(percent.loads(ours_text), include_cell_ids=include_ids)
    theirs = PairState.from_parsed(
        percent.loads(theirs_text), include_cell_ids=include_ids
    )
    return PreparedPair(
        python,
        notebook,
        PreparedState.from_state(ours),
        PreparedState.from_state(theirs),
        ours_text != preserved,
    )


def prepare_baseline(text: str, pair: PreparedPair) -> PreparedState:
    include_ids = pair.ours.state.include_cell_ids
    canonical = percent.canonicalize(
        text, include_cell_ids=None if include_ids else False
    )
    return PreparedState.from_state(
        PairState.from_parsed(percent.loads(canonical), include_cell_ids=include_ids)
    )


def prepare_merged(
    text: str, metadata: dict, pair: PreparedPair, base: PreparedState
) -> PreparedState:
    return PreparedState.from_state(
        PairState(
            cells=percent.loads(text).cells,
            metadata=metadata,
            include_cell_ids=pair.ours.state.include_cell_ids,
        )
    )
