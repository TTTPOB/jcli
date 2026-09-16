"""Cell-level diff and three-way merge for py:percent / .ipynb pairs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, TypeAlias

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


def _get_git_base_text_strict(path: Path) -> str | None:
    """Return a baseline while propagating Git failures to hook callers."""
    return pair_baseline.read_baseline(path, strict=True)


class _DriftResult:
    """Common read-only status API for concrete drift results."""

    _status: ClassVar[DriftStatus]

    @property
    def status(self) -> DriftStatus:
        """Return the status fixed by the concrete result type."""
        return self._status


@dataclass(frozen=True)
class InSync(_DriftResult):
    """The canonical Python and notebook sources are synchronized."""

    baseline_seed_text: str | None = None
    """Canonical Python text to bootstrap a missing baseline, when applicable."""

    _status: ClassVar[DriftStatus] = DriftStatus.IN_SYNC


@dataclass(frozen=True)
class Merged(_DriftResult):
    """The two sources were reconciled by a conflict-free three-way merge."""

    merged_cells: list[Cell]
    py_needs_update: bool
    ipynb_needs_update: bool
    merge_mode: MergeMode = MergeMode.THREE_WAY

    _status: ClassVar[DriftStatus] = DriftStatus.MERGED

    def __post_init__(self) -> None:
        object.__setattr__(self, "merge_mode", MergeMode(self.merge_mode))


@dataclass(frozen=True)
class Conflict(_DriftResult):
    """A three-way merge found cell-level conflicts."""

    conflict_indices: list[int]
    diff_text: str

    _status: ClassVar[DriftStatus] = DriftStatus.CONFLICT


@dataclass(frozen=True)
class DriftOnly(_DriftResult):
    """Sources differ but no Git baseline is available for a merge."""

    diff_text: str

    _status: ClassVar[DriftStatus] = DriftStatus.DRIFT_ONLY


DriftResult: TypeAlias = InSync | Merged | Conflict | DriftOnly


# ---------------------------------------------------------------------------
# check_drift
# ---------------------------------------------------------------------------


def check_drift(
    py_path: Path, ipynb_path: Path, *, strict_baseline: bool = False
) -> DriftResult:
    """Check whether a py/ipynb pair has drifted and attempt auto-merge.

    Strategy:
    - Both sides are normalized to py:percent text via canonicalize + emit.
    - ``.py`` tracked in git → `git merge-file` three-way text merge:
      base=py_HEAD (canonicalized), ours=py_now (canonicalized),
      theirs=ipynb_now (emitted). Myers diff handles insertions and deletions.
    - ``.py`` untracked (no HEAD blob) → 2-way comparison only; any difference
      is DRIFT_ONLY with a unified diff — no side wins automatically.

    Note: ``.ipynb`` is by design gitignored and never has a HEAD blob; only
    ``.py`` is used as the merge baseline. Hook callers can set
    ``strict_baseline=True`` to propagate Git errors instead of treating them
    as an absent baseline; the default preserves the regular CLI behavior.

    Raises any exception encountered; callers decide how to report it.
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

    base_raw = (
        _get_git_base_text_strict(py_path)
        if strict_baseline
        else _get_git_base_text(py_path)
    )

    if base_raw is None:
        if ours_text == theirs_text:
            return InSync(baseline_seed_text=ours_text)
        return DriftOnly(diff_text=render_no_baseline_diff(ours_text, theirs_text))

    base_text = percent.canonicalize(
        base_raw,
        include_cell_ids=None if include_cell_ids else False,
    )
    merge = merge_three_way(base_text, ours_text, theirs_text)

    py_needs = merge.text != ours_text or py_ids_need_writeback
    ipynb_needs = merge.text != theirs_text

    if not merge.has_conflict:
        if not py_needs and not ipynb_needs:
            return InSync()
        merged_cells = percent.loads(merge.text).cells
        return Merged(
            merge_mode=MergeMode.THREE_WAY,
            merged_cells=merged_cells,
            py_needs_update=py_needs,
            ipynb_needs_update=ipynb_needs,
        )

    return Conflict(
        diff_text=merge.text,
        conflict_indices=locate_conflict_cells(merge.text),
    )
