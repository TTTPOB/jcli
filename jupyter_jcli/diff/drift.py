"""Cell-level diff and three-way merge for py:percent / .ipynb pairs."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, TypeAlias

from jupyter_jcli import pair_baseline
from jupyter_jcli._enums import DriftStatus, MergeMode
from jupyter_jcli._pair_prepared import (
    PreparedPair,
    PreparedState,
    prepare_baseline,
    prepare_merged,
    prepare_pair,
)
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


@dataclass(frozen=True)
class CheckOutcome:
    """The selected target and write requirements, not byte-change reports."""

    drift: DriftResult
    target: PreparedState | None = None
    py_needs_update: bool = False
    ipynb_needs_update: bool = False
    should_persist: bool = False


# ---------------------------------------------------------------------------
# check_drift
# ---------------------------------------------------------------------------


def check_drift(
    py_path: Path, ipynb_path: Path, *, strict_baseline: bool = False
) -> DriftResult:
    """Check whether a py/ipynb pair has drifted and attempt auto-merge.

    Strategy:
    - Both sides are normalized to canonical py:percent projections.
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
    return prepare_and_check_pair(py_path, ipynb_path, strict_baseline=strict_baseline)[
        1
    ].drift


def prepare_and_check_pair(
    py_path: Path, ipynb_path: Path, *, strict_baseline: bool = False
) -> tuple[PreparedPair, CheckOutcome]:
    """Prepare canonical inputs for check and retain them for subsequent apply."""
    pair = prepare_pair(py_path, ipynb_path)
    base_raw = (
        _get_git_base_text_strict(py_path)
        if strict_baseline
        else _get_git_base_text(py_path)
    )
    return pair, check_prepared(pair, base_raw)


def check_prepared(pair: PreparedPair, base_raw: str | None) -> CheckOutcome:
    """Run the shared merge and conflict decision on canonical projections."""
    ours, theirs = pair.ours, pair.theirs
    if base_raw is None:
        if ours.matches(theirs):
            return CheckOutcome(
                InSync(baseline=BaselineMissing(seed_text=ours.canonical_text())),
                target=ours,
                should_persist=True,
            )
        return CheckOutcome(
            DriftOnly(
                diff_text=render_no_baseline_diff(
                    ours.canonical_text(), theirs.canonical_text()
                )
            )
        )

    base = prepare_baseline(base_raw, pair)
    cell_merge = merge_three_way(base.cell_text, ours.cell_text, theirs.cell_text)
    merged_metadata, metadata_conflicts = merge_metadata(
        base.state.metadata, ours.state.metadata, theirs.state.metadata
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
        return CheckOutcome(
            Conflict(
                diff_text="\n".join(diff_parts),
                conflict_indices=(
                    locate_conflict_cells(cell_merge.text)
                    if cell_merge.has_conflict
                    else []
                ),
                metadata_conflicts=metadata_conflicts,
            )
        )

    assert merged_metadata is not None
    merged = prepare_merged(cell_merge.text, merged_metadata, pair, base)
    py_needs = not merged.matches(ours) or pair.py_ids_need_writeback
    ipynb_needs = not merged.matches(theirs)
    if not py_needs and not ipynb_needs:
        return CheckOutcome(InSync(baseline=BaselineAvailable()), target=ours)
    return CheckOutcome(
        Merged(
            merge_mode=MergeMode.THREE_WAY,
            target_state=merged.state,
            py_needs_update=py_needs,
            ipynb_needs_update=ipynb_needs,
        ),
        target=merged,
        py_needs_update=py_needs,
        ipynb_needs_update=ipynb_needs,
        should_persist=True,
    )
