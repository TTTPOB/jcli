"""Cell-level diff and three-way merge for py:percent / .ipynb pairs."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from jupyter_jcli import pair_baseline
from jupyter_jcli._enums import DriftStatus, MergeMode
from jupyter_jcli.formats import ipynb, percent
from jupyter_jcli.formats.model import Cell

from .merge import merge_three_way
from .render import locate_conflict_cells, render_no_baseline_diff

# ---------------------------------------------------------------------------
# Git base helpers
# ---------------------------------------------------------------------------


def _get_git_base_text(path: Path) -> str | None:
    """Return the freshest available git-backed baseline for *path*."""
    return pair_baseline.read_baseline(path)


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
    ours_raw = py_path.read_text(encoding="utf-8")
    ours_parsed = percent.loads(ours_raw)
    include_cell_ids = bool(ours_parsed.stable_cell_ids)
    ours_preserved = percent.canonicalize(
        ours_raw,
        include_cell_ids=None if include_cell_ids else False,
    )
    ours_text = percent.canonicalize(
        ours_raw,
        include_cell_ids=include_cell_ids,
    )
    py_ids_need_writeback = ours_text != ours_preserved
    theirs_text = percent.canonicalize(
        percent.dumps(ipynb.load(ipynb_path), include_cell_ids=include_cell_ids),
        include_cell_ids=None if include_cell_ids else False,
    )

    base_raw = _get_git_base_text(py_path)

    if base_raw is None:
        if ours_text == theirs_text:
            return DriftResult(status=DriftStatus.IN_SYNC)
        return DriftResult(
            status=DriftStatus.DRIFT_ONLY,
            diff_text=render_no_baseline_diff(ours_text, theirs_text),
        )

    base_text = percent.canonicalize(
        base_raw,
        include_cell_ids=None if include_cell_ids else False,
    )
    merge = merge_three_way(base_text, ours_text, theirs_text)

    py_needs = merge.text != ours_text or py_ids_need_writeback
    ipynb_needs = merge.text != theirs_text

    if not merge.has_conflict:
        if not py_needs and not ipynb_needs:
            return DriftResult(status=DriftStatus.IN_SYNC)
        merged_cells = percent.loads(merge.text).cells
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
