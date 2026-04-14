"""Cell-level diff and three-way merge for py:percent / .ipynb pairs."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import nbformat

from jupyter_jcli._enums import DriftStatus
from jupyter_jcli.parser import Cell, ParsedFile, parse_py_percent_text


# ---------------------------------------------------------------------------
# Three-way merge
# ---------------------------------------------------------------------------

def three_way_merge(
    base: list[Cell],
    ours: list[Cell],
    theirs: list[Cell],
) -> tuple[list[Cell], list[int]]:
    """Per-cell three-way merge.

    Returns (merged_cells, conflict_indices).
    If conflict_indices is non-empty, merged_cells contains placeholders at
    those positions (base cell).

    Cell count mismatch → all indices are treated as conflicting.
    """
    if len(base) != len(ours) or len(base) != len(theirs):
        n = max(len(base), len(ours), len(theirs), 1)
        return [], list(range(n))

    merged: list[Cell] = []
    conflicts: list[int] = []

    for i, (b, o, t) in enumerate(zip(base, ours, theirs)):
        ours_changed = o.source != b.source
        theirs_changed = t.source != b.source

        if not ours_changed and not theirs_changed:
            merged.append(Cell(index=i, cell_type=b.cell_type, source=b.source))
        elif ours_changed and not theirs_changed:
            merged.append(Cell(index=i, cell_type=o.cell_type, source=o.source))
        elif not ours_changed and theirs_changed:
            merged.append(Cell(index=i, cell_type=t.cell_type, source=t.source))
        else:
            # Both changed — conflict
            conflicts.append(i)
            merged.append(Cell(index=i, cell_type=b.cell_type, source=b.source))

    return merged, conflicts


# ---------------------------------------------------------------------------
# Git base helpers
# ---------------------------------------------------------------------------

def _get_git_base_text(path: Path) -> str | None:
    """Return the git HEAD content of *path* as a string, or None.

    Returns None if the file is untracked, the repo has no HEAD, git is not
    available, or any other error occurs.
    """
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=False,
            cwd=str(path.parent),
        )
        if top.returncode != 0:
            return None
        git_root = Path(top.stdout.strip())

        try:
            rel = path.resolve().relative_to(git_root.resolve())
        except ValueError:
            return None

        show = subprocess.run(
            ["git", "show", f"HEAD:{rel.as_posix()}"],
            capture_output=True, check=False,
            cwd=str(git_root),
        )
        if show.returncode != 0:
            return None
        return show.stdout.decode("utf-8")
    except (OSError, FileNotFoundError):
        return None


def _cells_from_ipynb_text(text: str) -> list[Cell]:
    """Parse cells from ipynb JSON text."""
    nb = nbformat.reads(text, as_version=4)
    return [
        Cell(index=i, cell_type=c.cell_type, source=c.source)
        for i, c in enumerate(nb.cells)
        if c.source.strip()
    ]


def _cells_from_py_text(text: str) -> list[Cell]:
    """Parse cells from py:percent text."""
    return parse_py_percent_text(text).cells


# ---------------------------------------------------------------------------
# DriftResult
# ---------------------------------------------------------------------------

@dataclass
class DriftResult:
    """Result of a drift check and optional three-way merge attempt."""

    status: DriftStatus
    """One of: DriftStatus.IN_SYNC | MERGED | CONFLICT | DRIFT_ONLY."""

    py_needs_update: bool = False
    """True when the .py file should be rewritten with merged_py_cells."""

    ipynb_needs_update: bool = False
    """True when the .ipynb file should be updated with merged_ipynb_cells."""

    merged_cells: list[Cell] = field(default_factory=list)
    """Merged cell list (common to both sides after merge)."""

    conflict_indices: list[int] = field(default_factory=list)
    """Cell indices with conflicts (non-empty iff status == DriftStatus.CONFLICT)."""

    def __post_init__(self) -> None:
        self.status = DriftStatus(self.status)


# ---------------------------------------------------------------------------
# check_drift
# ---------------------------------------------------------------------------

def check_drift(py_path: Path, ipynb_path: Path) -> DriftResult:
    """Check whether a py/ipynb pair has drifted and attempt auto-merge.

    Strategy:
    - Both files tracked in git → per-cell 3-way merge against HEAD blobs.
    - Either file untracked (no HEAD blob) → drift-only: compare current cells;
      equal → in_sync, unequal → drift_only (treated as conflict by caller).

    Raises any exception encountered (caller is responsible for fail-open).
    """
    from jupyter_jcli.parser import parse_ipynb, parse_py_percent

    py_now = parse_py_percent(str(py_path)).cells
    ipynb_now = [c for c in parse_ipynb(str(ipynb_path)).cells if c.source.strip()]

    base_py_text = _get_git_base_text(py_path)
    base_ipynb_text = _get_git_base_text(ipynb_path)

    if base_py_text is None or base_ipynb_text is None:
        # Drift-only mode: compare sources
        py_sources = [c.source for c in py_now]
        ipynb_sources = [c.source for c in ipynb_now]
        if py_sources == ipynb_sources:
            return DriftResult(status=DriftStatus.IN_SYNC)
        return DriftResult(
            status=DriftStatus.DRIFT_ONLY,
            conflict_indices=list(range(max(len(py_now), len(ipynb_now), 1))),
        )

    base_py_cells = _cells_from_py_text(base_py_text)
    base_ipynb_cells = _cells_from_ipynb_text(base_ipynb_text)

    # Determine which side changed relative to base
    py_changed = [c.source for c in py_now] != [c.source for c in base_py_cells]
    ipynb_changed = [c.source for c in ipynb_now] != [c.source for c in base_ipynb_cells]

    if not py_changed and not ipynb_changed:
        return DriftResult(status=DriftStatus.IN_SYNC)

    # Use py base as the reference for the 3-way merge
    merged, conflicts = three_way_merge(base_py_cells, py_now, ipynb_now)

    if conflicts:
        return DriftResult(status=DriftStatus.CONFLICT, conflict_indices=conflicts)

    # Determine which files need updating
    py_needs = [c.source for c in merged] != [c.source for c in py_now]
    ipynb_needs = [c.source for c in merged] != [c.source for c in ipynb_now]

    return DriftResult(
        status=DriftStatus.MERGED,
        py_needs_update=py_needs,
        ipynb_needs_update=ipynb_needs,
        merged_cells=merged,
    )
