"""Cell-level diff and three-way merge for py:percent / .ipynb pairs."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, TypeAlias

from jupyter_jcli import pair_baseline
from jupyter_jcli._enums import DriftStatus, MergeMode
from jupyter_jcli.formats import ipynb, percent
from jupyter_jcli.pair_state import MetadataConflict, PairState, merge_metadata

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
class BaselineAvailable:
    """A usable baseline was read from HEAD or the sticky baseline."""


@dataclass(frozen=True)
class BaselineMissing:
    """No baseline was obtained under the current baseline-reading strategy.

    Strict hook reads ensure lookup errors are raised instead of represented as
    a missing baseline.
    """

    seed_text: str
    """Canonical Python text to use when bootstrapping the baseline."""


@dataclass(frozen=True)
class InSync(_DriftResult):
    """The canonical Python and notebook sources are synchronized."""

    baseline: BaselineAvailable | BaselineMissing
    """Whether a usable baseline was available, or the seed to bootstrap one."""

    _status: ClassVar[DriftStatus] = DriftStatus.IN_SYNC


@dataclass(frozen=True)
class Merged(_DriftResult):
    """The two sources were reconciled to one complete shared state."""

    target_state: PairState
    py_needs_update: bool
    ipynb_needs_update: bool
    merge_mode: MergeMode = MergeMode.THREE_WAY

    _status: ClassVar[DriftStatus] = DriftStatus.MERGED

    def __post_init__(self) -> None:
        object.__setattr__(self, "merge_mode", MergeMode(self.merge_mode))


@dataclass(frozen=True)
class Conflict(_DriftResult):
    """A three-way merge found cell and/or kernel conflicts."""

    conflict_indices: list[int]
    diff_text: str
    metadata_conflicts: list[MetadataConflict] = field(default_factory=list)

    _status: ClassVar[DriftStatus] = DriftStatus.CONFLICT

    @property
    def kernel_conflict(self) -> MetadataConflict | None:
        """Return the atomic kernel conflict, when present."""
        return next(
            (
                conflict
                for conflict in self.metadata_conflicts
                if conflict.path == ("kernel",)
            ),
            None,
        )


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
    ours_source = percent.loads(ours_raw)
    include_cell_ids = bool(ours_source.stable_cell_ids)
    ours_preserved = percent.canonicalize(
        ours_raw,
        include_cell_ids=None if include_cell_ids else False,
    )
    ours_text = percent.canonicalize(
        ours_raw,
        include_cell_ids=include_cell_ids,
    )
    py_ids_need_writeback = ours_text != ours_preserved
    notebook_source = ipynb.load(ipynb_path)
    theirs_text = percent.canonicalize(
        percent.dumps(notebook_source, include_cell_ids=include_cell_ids),
        include_cell_ids=None if include_cell_ids else False,
    )
    ours = PairState.from_parsed(
        percent.loads(ours_text), include_cell_ids=include_cell_ids
    )
    theirs = PairState.from_parsed(
        percent.loads(theirs_text), include_cell_ids=include_cell_ids
    )

    base_raw = (
        _get_git_base_text_strict(py_path)
        if strict_baseline
        else _get_git_base_text(py_path)
    )

    if base_raw is None:
        if ours == theirs:
            return InSync(baseline=BaselineMissing(seed_text=ours.canonical_text()))
        return DriftOnly(
            diff_text=render_no_baseline_diff(
                ours.canonical_text(), theirs.canonical_text()
            )
        )

    base_text = percent.canonicalize(
        base_raw,
        include_cell_ids=None if include_cell_ids else False,
    )
    base = PairState.from_parsed(
        percent.loads(base_text), include_cell_ids=include_cell_ids
    )
    cell_merge = merge_three_way(base.cell_text(), ours.cell_text(), theirs.cell_text())
    merged_metadata, metadata_conflicts = merge_metadata(
        base.metadata, ours.metadata, theirs.metadata
    )

    if cell_merge.has_conflict or metadata_conflicts:
        diff_parts = [
            "Metadata conflict at "
            f"{conflict.display_path}:\n"
            f"  base: {conflict.base!r}\n"
            f"  py: {conflict.py!r}\n"
            f"  notebook: {conflict.notebook!r}\n"
            for conflict in metadata_conflicts
        ]
        if cell_merge.has_conflict:
            diff_parts.append(cell_merge.text)
        return Conflict(
            diff_text="\n".join(diff_parts),
            conflict_indices=(
                locate_conflict_cells(cell_merge.text)
                if cell_merge.has_conflict
                else []
            ),
            metadata_conflicts=metadata_conflicts,
        )

    assert merged_metadata is not None
    merged = PairState(
        cells=percent.loads(cell_merge.text).cells,
        metadata=merged_metadata,
        include_cell_ids=include_cell_ids,
    )
    py_needs = merged != ours or py_ids_need_writeback
    ipynb_needs = merged != theirs
    if not py_needs and not ipynb_needs:
        return InSync(baseline=BaselineAvailable())
    return Merged(
        merge_mode=MergeMode.THREE_WAY,
        target_state=merged,
        py_needs_update=py_needs,
        ipynb_needs_update=ipynb_needs,
    )
