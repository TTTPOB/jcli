"""Notebook diff, merge, and pair drift operations."""

from jupyter_jcli.diff.alignment import CellChange, align_cells, diff_cells
from jupyter_jcli.diff.drift import (
    BaselineAvailable,
    BaselineMissing,
    Conflict,
    DriftOnly,
    DriftResult,
    InSync,
    Merged,
    check_drift,
)

__all__ = [
    "BaselineAvailable",
    "BaselineMissing",
    "CellChange",
    "Conflict",
    "DriftOnly",
    "DriftResult",
    "InSync",
    "Merged",
    "align_cells",
    "check_drift",
    "diff_cells",
]
