"""Two-way notebook cell alignment."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher

from jupyter_jcli.parser import Cell, ParsedFile

_MAX_REPLACE_DP_PRODUCT = 2_500
_MAX_SEQUENCE_MATCHER_PRODUCT = 10_000
_MAX_LARGE_POSITIONAL_CHANGES = 64
_MIN_REPEATED_CELL_FRACTION = 0.5
_FALLBACK_SOURCE_COMPARE_CHARS = 512
_FALLBACK_SHIFT_LOOKAHEAD = 8
_FALLBACK_SHIFT_MIN_SIMILARITY = 0.5
_FALLBACK_SHIFT_ADVANTAGE = 0.2


@dataclass(frozen=True)
class CellChange:
    """An aligned cell pair or one-sided cell change."""

    kind: str
    old_index: int | None
    new_index: int | None
    old_cell: Cell | None
    new_cell: Cell | None
    current_insertion_index: int


def align_cells(
    old: ParsedFile | list[Cell],
    current: ParsedFile | list[Cell],
) -> list[CellChange]:
    """Align old and current cells, including unchanged pairs."""
    return _align_cells(old, current, include_equal=True)


def diff_cells(
    old: ParsedFile | list[Cell],
    current: ParsedFile | list[Cell],
) -> list[CellChange]:
    """Classify cell changes while preserving unchanged-cell alignment."""
    return _align_cells(old, current, include_equal=False)


def _align_cells(
    old: ParsedFile | list[Cell],
    current: ParsedFile | list[Cell],
    *,
    include_equal: bool,
) -> list[CellChange]:
    old_cells = old.cells if isinstance(old, ParsedFile) else old
    current_cells = current.cells if isinstance(current, ParsedFile) else current
    old_keys = [(cell.cell_type.value, cell.source) for cell in old_cells]
    current_keys = [(cell.cell_type.value, cell.source) for cell in current_cells]

    if old_keys == current_keys:
        if not include_equal:
            return []
        return [
            _paired_change("equal", old_cell, new_cell)
            for old_cell, new_cell in zip(old_cells, current_cells)
        ]

    product = len(old_cells) * len(current_cells)
    if (
        product > _MAX_SEQUENCE_MATCHER_PRODUCT
        and len(old_cells) == len(current_cells)
        and _is_highly_repetitive(old_keys)
        and _is_highly_repetitive(current_keys)
        and not _has_shifted_unique_anchor(old_keys, current_keys)
    ):
        changed_positions = {
            index
            for index, (old_key, current_key) in enumerate(zip(old_keys, current_keys))
            if old_key != current_key
        }
        if len(changed_positions) <= _MAX_LARGE_POSITIONAL_CHANGES:
            return [
                _paired_change(
                    "edited" if index in changed_positions else "equal",
                    old_cell,
                    new_cell,
                )
                for index, (old_cell, new_cell) in enumerate(
                    zip(old_cells, current_cells)
                )
                if include_equal or index in changed_positions
            ]

    matcher = SequenceMatcher(
        None,
        old_keys,
        current_keys,
        autojunk=product > _MAX_SEQUENCE_MATCHER_PRODUCT,
    )
    alignments: list[CellChange] = []
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            if include_equal:
                alignments.extend(
                    _paired_change("equal", old_cell, new_cell)
                    for old_cell, new_cell in zip(
                        old_cells[old_start:old_end], current_cells[new_start:new_end]
                    )
                )
            continue
        if tag == "insert":
            alignments.extend(
                CellChange(
                    kind="inserted",
                    old_index=None,
                    new_index=cell.index,
                    old_cell=None,
                    new_cell=cell,
                    current_insertion_index=cell.index,
                )
                for cell in current_cells[new_start:new_end]
            )
            continue
        if tag == "delete":
            insertion_index = _current_insertion_index(current_cells, new_start)
            alignments.extend(
                CellChange(
                    kind="deleted",
                    old_index=cell.index,
                    new_index=None,
                    old_cell=cell,
                    new_cell=None,
                    current_insertion_index=insertion_index,
                )
                for cell in old_cells[old_start:old_end]
            )
            continue

        alignments.extend(
            _align_replaced_cells(
                old_cells[old_start:old_end],
                current_cells[new_start:new_end],
                current_cells,
                new_start,
            )
        )
    if include_equal:
        return alignments
    return [alignment for alignment in alignments if alignment.kind != "equal"]


def _paired_change(kind: str, old_cell: Cell, new_cell: Cell) -> CellChange:
    return CellChange(
        kind=kind,
        old_index=old_cell.index,
        new_index=new_cell.index,
        old_cell=old_cell,
        new_cell=new_cell,
        current_insertion_index=new_cell.index,
    )


def _is_highly_repetitive(keys: list[tuple[str, str]]) -> bool:
    if not keys:
        return False
    repeated_count = len(keys) - len(set(keys))
    return repeated_count / len(keys) >= _MIN_REPEATED_CELL_FRACTION


def _has_shifted_unique_anchor(
    old_keys: list[tuple[str, str]], current_keys: list[tuple[str, str]]
) -> bool:
    old_counts = Counter(old_keys)
    current_counts = Counter(current_keys)
    current_positions = {
        key: index
        for index, key in enumerate(current_keys)
        if old_counts[key] == 1 and current_counts[key] == 1
    }
    return any(
        key in current_positions and current_positions[key] != old_index
        for old_index, key in enumerate(old_keys)
        if old_counts[key] == 1
    )


def _align_replaced_cells(
    old_cells: list[Cell],
    new_cells: list[Cell],
    all_current_cells: list[Cell],
    new_start: int,
) -> list[CellChange]:
    """Align a replace block so nearby source revisions remain edits."""
    old_count = len(old_cells)
    new_count = len(new_cells)
    if old_count * new_count > _MAX_REPLACE_DP_PRODUCT:
        return _align_replaced_cells_by_position(
            old_cells,
            new_cells,
            all_current_cells,
            new_start,
        )

    costs = [[0.0] * (new_count + 1) for _ in range(old_count + 1)]
    steps = [[""] * (new_count + 1) for _ in range(old_count + 1)]

    for old_pos in range(1, old_count + 1):
        costs[old_pos][0] = float(old_pos)
        steps[old_pos][0] = "deleted"
    for new_pos in range(1, new_count + 1):
        costs[0][new_pos] = float(new_pos)
        steps[0][new_pos] = "inserted"

    for old_pos in range(1, old_count + 1):
        for new_pos in range(1, new_count + 1):
            candidates = (
                (
                    costs[old_pos - 1][new_pos - 1]
                    + _cell_edit_cost(old_cells[old_pos - 1], new_cells[new_pos - 1]),
                    0,
                    "edited",
                ),
                (costs[old_pos - 1][new_pos] + 1.0, 1, "deleted"),
                (costs[old_pos][new_pos - 1] + 1.0, 2, "inserted"),
            )
            cost, _, step = min(candidates)
            costs[old_pos][new_pos] = cost
            steps[old_pos][new_pos] = step

    aligned: list[CellChange] = []
    old_pos = old_count
    new_pos = new_count
    while old_pos or new_pos:
        step = steps[old_pos][new_pos]
        if step == "edited":
            old_cell = old_cells[old_pos - 1]
            new_cell = new_cells[new_pos - 1]
            aligned.append(
                _paired_change(_paired_kind(old_cell, new_cell), old_cell, new_cell)
            )
            old_pos -= 1
            new_pos -= 1
        elif step == "deleted":
            old_cell = old_cells[old_pos - 1]
            aligned.append(
                CellChange(
                    kind="deleted",
                    old_index=old_cell.index,
                    new_index=None,
                    old_cell=old_cell,
                    new_cell=None,
                    current_insertion_index=_current_insertion_index(
                        all_current_cells, new_start + new_pos
                    ),
                )
            )
            old_pos -= 1
        else:
            new_cell = new_cells[new_pos - 1]
            aligned.append(
                CellChange(
                    kind="inserted",
                    old_index=None,
                    new_index=new_cell.index,
                    old_cell=None,
                    new_cell=new_cell,
                    current_insertion_index=new_cell.index,
                )
            )
            new_pos -= 1

    aligned.reverse()
    return aligned


def _align_replaced_cells_by_position(
    old_cells: list[Cell],
    new_cells: list[Cell],
    all_current_cells: list[Cell],
    new_start: int,
) -> list[CellChange]:
    """Classify a large replace block with bounded one-cell lookahead."""
    changes: list[CellChange] = []
    old_pos = 0
    new_pos = 0
    while old_pos < len(old_cells) and new_pos < len(new_cells):
        old_cell = old_cells[old_pos]
        new_cell = new_cells[new_pos]
        direct_similarity = _fallback_cell_similarity(old_cell, new_cell)
        insertion_offset, insertion_similarity = _best_forward_match(
            old_cell, new_cells, new_pos
        )
        deletion_offset, deletion_similarity = _best_forward_match(
            new_cell, old_cells, old_pos
        )

        if (
            insertion_similarity >= _FALLBACK_SHIFT_MIN_SIMILARITY
            and insertion_similarity >= deletion_similarity
            and insertion_similarity >= direct_similarity + _FALLBACK_SHIFT_ADVANTAGE
        ):
            changes.extend(
                CellChange(
                    kind="inserted",
                    old_index=None,
                    new_index=inserted_cell.index,
                    old_cell=None,
                    new_cell=inserted_cell,
                    current_insertion_index=inserted_cell.index,
                )
                for inserted_cell in new_cells[new_pos : new_pos + insertion_offset]
            )
            new_pos += insertion_offset
            continue
        if (
            deletion_similarity >= _FALLBACK_SHIFT_MIN_SIMILARITY
            and deletion_similarity > insertion_similarity
            and deletion_similarity >= direct_similarity + _FALLBACK_SHIFT_ADVANTAGE
        ):
            changes.extend(
                CellChange(
                    kind="deleted",
                    old_index=deleted_cell.index,
                    new_index=None,
                    old_cell=deleted_cell,
                    new_cell=None,
                    current_insertion_index=new_cell.index,
                )
                for deleted_cell in old_cells[old_pos : old_pos + deletion_offset]
            )
            old_pos += deletion_offset
            continue

        changes.append(
            _paired_change(_paired_kind(old_cell, new_cell), old_cell, new_cell)
        )
        old_pos += 1
        new_pos += 1

    insertion_index = _current_insertion_index(all_current_cells, new_start + new_pos)
    for old_cell in old_cells[old_pos:]:
        changes.append(
            CellChange(
                kind="deleted",
                old_index=old_cell.index,
                new_index=None,
                old_cell=old_cell,
                new_cell=None,
                current_insertion_index=insertion_index,
            )
        )
    for new_cell in new_cells[new_pos:]:
        changes.append(
            CellChange(
                kind="inserted",
                old_index=None,
                new_index=new_cell.index,
                old_cell=None,
                new_cell=new_cell,
                current_insertion_index=new_cell.index,
            )
        )
    return changes


def _fallback_cell_similarity(old_cell: Cell, new_cell: Cell) -> float:
    if old_cell.cell_type != new_cell.cell_type:
        return 0.0
    return SequenceMatcher(
        None,
        old_cell.source[:_FALLBACK_SOURCE_COMPARE_CHARS],
        new_cell.source[:_FALLBACK_SOURCE_COMPARE_CHARS],
        autojunk=True,
    ).ratio()


def _best_forward_match(
    reference: Cell, candidates: list[Cell], position: int
) -> tuple[int, float]:
    max_offset = min(_FALLBACK_SHIFT_LOOKAHEAD, len(candidates) - position - 1)
    if max_offset <= 0:
        return 0, -1.0
    return max(
        (
            (
                offset,
                _fallback_cell_similarity(reference, candidates[position + offset]),
            )
            for offset in range(1, max_offset + 1)
        ),
        key=lambda item: (item[1], -item[0]),
    )


def _paired_kind(old_cell: Cell, new_cell: Cell) -> str:
    if old_cell.cell_type == new_cell.cell_type and old_cell.source == new_cell.source:
        return "equal"
    return "edited"


def _cell_edit_cost(old_cell: Cell, new_cell: Cell) -> float:
    similarity = SequenceMatcher(
        None,
        old_cell.source,
        new_cell.source,
        autojunk=False,
    ).ratio()
    type_penalty = 0.25 if old_cell.cell_type != new_cell.cell_type else 0.0
    return 1.0 - similarity + type_penalty


def _current_insertion_index(cells: list[Cell], position: int) -> int:
    return cells[position].index if position < len(cells) else len(cells)
