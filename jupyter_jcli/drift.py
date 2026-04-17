"""Cell-level diff and three-way merge for py:percent / .ipynb pairs."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import nbformat

from jupyter_jcli._enums import DriftStatus, MergeMode
from jupyter_jcli import pair_baseline
from jupyter_jcli.parser import Cell, ParsedFile, parse_py_percent_text


# ---------------------------------------------------------------------------
# Three-way merge (kept for backward compatibility)
# ---------------------------------------------------------------------------

def three_way_merge(
    base: list[Cell],
    ours: list[Cell],
    theirs: list[Cell],
) -> tuple[list[Cell], list[int]]:
    """Per-cell three-way merge (position-aligned, cell count must match).

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
            conflicts.append(i)
            merged.append(Cell(index=i, cell_type=b.cell_type, source=b.source))

    return merged, conflicts


# ---------------------------------------------------------------------------
# Git base helpers
# ---------------------------------------------------------------------------

def _get_git_base_text(path: Path) -> str | None:
    """Return the freshest available git-backed baseline for *path*."""
    return pair_baseline.read_baseline(path)


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

    merge_mode: MergeMode = MergeMode.THREE_WAY
    """How the merge was produced (only meaningful when status == MERGED)."""

    diff_text: str = ""
    """Diff content for agent consumption.

    For CONFLICT: git merge-file output with <<<<<<< / ======= / >>>>>>> markers.
    For DRIFT_ONLY: unified diff between py and ipynb (no common baseline).
    Empty for IN_SYNC and MERGED.
    """

    def __post_init__(self) -> None:
        self.status = DriftStatus(self.status)
        self.merge_mode = MergeMode(self.merge_mode)


# ---------------------------------------------------------------------------
# check_drift
# ---------------------------------------------------------------------------

def check_drift(py_path: Path, ipynb_path: Path) -> DriftResult:
    """Check whether a py/ipynb pair has drifted and attempt auto-merge.

    Strategy:
    - Both sides are normalized to py:percent text via canonicalize + emit.
    - ``.py`` tracked in git → `git merge-file` three-way text merge:
      base=py_HEAD (canonicalized), ours=py_now (canonicalized),
      theirs=ipynb_now (emitted). Myers diff handles insertions and deletions.
    - ``.py`` untracked (no HEAD blob) → 2-way comparison only; any difference
      is DRIFT_ONLY with a unified diff — no side wins automatically.

    Note: ``.ipynb`` is by design gitignored and never has a HEAD blob; only
    ``.py`` is used as the merge baseline.

    Raises any exception encountered (caller is responsible for fail-open).
    """
    from jupyter_jcli.canonicalize import canonicalize_py_text
    from jupyter_jcli.diff_render import locate_conflict_cells, render_no_baseline_diff
    from jupyter_jcli.pair_io import emit_py_percent
    from jupyter_jcli.parser import parse_ipynb
    from jupyter_jcli.text_merge import merge_three_way

    ours_text = canonicalize_py_text(py_path.read_text(encoding="utf-8"))
    theirs_text = canonicalize_py_text(emit_py_percent(parse_ipynb(str(ipynb_path))))

    base_raw = _get_git_base_text(py_path)

    if base_raw is None:
        if ours_text == theirs_text:
            return DriftResult(status=DriftStatus.IN_SYNC)
        return DriftResult(
            status=DriftStatus.DRIFT_ONLY,
            diff_text=render_no_baseline_diff(ours_text, theirs_text),
        )

    base_text = canonicalize_py_text(base_raw)
    merge = merge_three_way(base_text, ours_text, theirs_text)

    py_needs = merge.text != ours_text
    ipynb_needs = merge.text != theirs_text

    if not merge.has_conflict:
        if not py_needs and not ipynb_needs:
            return DriftResult(status=DriftStatus.IN_SYNC)
        merged_cells = parse_py_percent_text(merge.text).cells
        return DriftResult(
            status=DriftStatus.MERGED,
            merge_mode=MergeMode.THREE_WAY,
            merged_cells=merged_cells,
            py_needs_update=py_needs,
            ipynb_needs_update=ipynb_needs,
        )

    return DriftResult(
        status=DriftStatus.CONFLICT,
        diff_text=merge.text,
        conflict_indices=locate_conflict_cells(merge.text),
    )
