"""Shared semantic state for py:percent / notebook pairs."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field

from jupyter_jcli.formats import percent
from jupyter_jcli.formats.model import Cell, ParsedFile


@dataclass(frozen=True)
class KernelInfo:
    """Kernel identity plus optional descriptive fields from its source."""

    name: str
    display_name: str | None = None
    language: str | None = None


@dataclass(eq=False)
class PairState:
    """The state shared by a Python file and its paired notebook.

    Cell metadata, outputs, execution counts, and descriptive kernelspec fields are
    deliberately excluded from equality. ``kernel_info`` only supplies a template
    when a changed kernel must be written to the other representation.
    """

    cells: list[Cell]
    kernel_name: str | None
    kernel_info: KernelInfo | None = field(default=None, compare=False)
    include_cell_ids: bool = True

    @classmethod
    def from_parsed(
        cls, parsed: ParsedFile, *, include_cell_ids: bool = True
    ) -> PairState:
        """Project a parsed representation onto pair-shared semantics."""
        info = (
            KernelInfo(
                parsed.kernel_name,
                parsed.kernel_display_name,
                parsed.kernel_language,
            )
            if parsed.kernel_name is not None
            else None
        )
        return cls(
            cells=[deepcopy(cell) for cell in parsed.cells],
            kernel_name=parsed.kernel_name,
            kernel_info=info,
            include_cell_ids=include_cell_ids,
        )

    def cell_text(self) -> str:
        """Return deterministic kernel-free canonical text for cell merging."""
        parsed = ParsedFile(cells=[deepcopy(cell) for cell in self.cells])
        return percent.dumps(
            parsed,
            include_cell_ids=self.include_cell_ids,
            assign_missing_ids=False,
        )

    def canonical_text(self) -> str:
        """Return deterministic baseline text for the complete shared state."""
        parsed = ParsedFile(
            kernel_name=self.kernel_name,
            cells=[deepcopy(cell) for cell in self.cells],
        )
        return percent.dumps(
            parsed,
            include_cell_ids=self.include_cell_ids,
            assign_missing_ids=False,
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PairState):
            return NotImplemented
        return (
            self.kernel_name == other.kernel_name
            and self.cell_text() == other.cell_text()
        )


@dataclass(frozen=True)
class KernelConflict:
    """A three-way conflict between kernel identities."""

    base: str | None
    py: str | None
    notebook: str | None


def merge_kernel(
    base: PairState, py: PairState, notebook: PairState
) -> tuple[str | None, KernelInfo | None] | KernelConflict:
    """Merge kernel identity by value, preserving the winning description."""
    ours = py.kernel_name
    theirs = notebook.kernel_name
    ancestor = base.kernel_name

    if ours == theirs:
        return ours, py.kernel_info or notebook.kernel_info
    if ours == ancestor:
        return theirs, notebook.kernel_info
    if theirs == ancestor:
        return ours, py.kernel_info
    return KernelConflict(base=ancestor, py=ours, notebook=theirs)
