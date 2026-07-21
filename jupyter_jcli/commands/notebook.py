"""jcli notebook -- inspect notebook cell structure without execution."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

import click

from jupyter_jcli._enums import CellType, ResponseStatus
from jupyter_jcli.output import emit, emit_error
from jupyter_jcli.parser import Cell, ParsedFile, parse_cell_spec, parse_file

if TYPE_CHECKING:
    from jupyter_jcli.cli import Context


_MAX_ANALYSIS_ITEMS = 8
_MAX_SOURCE_PREVIEW_CHARS = 120
_MAX_REPLACE_DP_PRODUCT = 10_000
_MAX_SEQUENCE_MATCHER_PRODUCT = 10_000
_MAX_LARGE_POSITIONAL_CHANGES = 64
_MIN_REPEATED_CELL_FRACTION = 0.5
_FALLBACK_SOURCE_COMPARE_CHARS = 512
_FALLBACK_SHIFT_MIN_SIMILARITY = 0.5
_FALLBACK_SHIFT_ADVANTAGE = 0.2
_MAX_CHANGE_OVERVIEW_ITEMS = 12
_MAX_BOUNDED_METADATA_FIELD_CHARS = 1000
_MAX_BOUNDED_CELL_LINE_CHARS = 1000
_CHANGE_MARKERS = {"edited": "~", "inserted": "+", "deleted": "-"}


@dataclass(frozen=True)
class CellChange:
    """A cell-level change between two parsed notebook versions."""

    kind: str
    old_index: int | None
    new_index: int | None
    old_cell: Cell | None
    new_cell: Cell | None
    current_insertion_index: int


@click.group("notebook")
def notebook():
    """Inspect notebook cells."""


@notebook.command("summary")
@click.argument(
    "file_path", metavar="FILE", type=click.Path(exists=True, dir_okay=False)
)
@click.pass_obj
def summary(ctx: Context, file_path: str) -> None:
    """Print a deterministic structural summary for each cell in FILE."""
    parsed = _parse_or_error(ctx, file_path)
    data = build_summary_data(parsed)
    if ctx.use_json:
        emit(data, use_json=True)
        return
    emit({"_human": format_summary_human(data)}, use_json=False)


@notebook.command("show")
@click.argument(
    "file_path", metavar="FILE", type=click.Path(exists=True, dir_okay=False)
)
@click.option(
    "--cell", "cell_spec", required=True, help="Cell spec: 3, 3:7, 3:, :5 (0-indexed)"
)
@click.pass_obj
def show(ctx: Context, file_path: str, cell_spec: str) -> None:
    """Print complete source for selected cells in FILE."""
    parsed = _parse_or_error(ctx, file_path)
    try:
        indices = parse_cell_spec(cell_spec, len(parsed.cells))
    except ValueError:
        emit_error("PARSE_ERROR", f"Invalid cell spec: {cell_spec}", ctx.use_json)
        return

    cells = [cell for cell in parsed.cells if cell.index in indices]
    if not cells:
        emit_error("CELL_NOT_FOUND", f"No cells matched: {cell_spec}", ctx.use_json)
        return

    cell_data = [
        {"index": cell.index, "type": cell.cell_type, "source": cell.source}
        for cell in cells
    ]
    data = _notebook_data(parsed, cell_data)
    if ctx.use_json:
        emit(data, use_json=True)
        return
    emit({"_human": _format_show_human(cells)}, use_json=False)


def _parse_or_error(ctx: Context, file_path: str) -> ParsedFile:
    try:
        return parse_file(file_path)
    except Exception as error:
        emit_error("NOTEBOOK_PARSE_FAILED", str(error), ctx.use_json)
        raise AssertionError("emit_error should exit")


def _notebook_data(
    parsed: ParsedFile, cells: list[dict], changes: list[dict] | None = None
) -> dict:
    data = {
        "status": ResponseStatus.OK,
        "path": parsed.source_path,
        "cell_count": len(parsed.cells),
        "kernel": parsed.kernel_name,
        "cells": cells,
    }
    if changes is not None:
        data["changes"] = changes
    return data


def diff_cells(
    old: ParsedFile | list[Cell],
    current: ParsedFile | list[Cell],
) -> list[CellChange]:
    """Classify exact cell changes while preserving unchanged-cell alignment."""
    old_cells = old.cells if isinstance(old, ParsedFile) else old
    current_cells = current.cells if isinstance(current, ParsedFile) else current
    old_keys = [(cell.cell_type.value, cell.source) for cell in old_cells]
    current_keys = [(cell.cell_type.value, cell.source) for cell in current_cells]
    if old_keys == current_keys:
        return []

    product = len(old_cells) * len(current_cells)
    if (
        product > _MAX_SEQUENCE_MATCHER_PRODUCT
        and len(old_cells) == len(current_cells)
        and _is_highly_repetitive(old_keys)
        and _is_highly_repetitive(current_keys)
    ):
        changed_positions = [
            index
            for index, (old_key, current_key) in enumerate(zip(old_keys, current_keys))
            if old_key != current_key
        ]
        if len(changed_positions) <= _MAX_LARGE_POSITIONAL_CHANGES:
            return [
                CellChange(
                    kind="edited",
                    old_index=old_cells[index].index,
                    new_index=current_cells[index].index,
                    old_cell=old_cells[index],
                    new_cell=current_cells[index],
                    current_insertion_index=current_cells[index].index,
                )
                for index in changed_positions
            ]

    matcher = SequenceMatcher(
        None,
        old_keys,
        current_keys,
        autojunk=product > _MAX_SEQUENCE_MATCHER_PRODUCT,
    )
    changes: list[CellChange] = []
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert":
            for cell in current_cells[new_start:new_end]:
                changes.append(
                    CellChange(
                        kind="inserted",
                        old_index=None,
                        new_index=cell.index,
                        old_cell=None,
                        new_cell=cell,
                        current_insertion_index=cell.index,
                    )
                )
            continue
        if tag == "delete":
            insertion_index = _current_insertion_index(current_cells, new_start)
            for cell in old_cells[old_start:old_end]:
                changes.append(
                    CellChange(
                        kind="deleted",
                        old_index=cell.index,
                        new_index=None,
                        old_cell=cell,
                        new_cell=None,
                        current_insertion_index=insertion_index,
                    )
                )
            continue

        changes.extend(
            _diff_replaced_cells(
                old_cells[old_start:old_end],
                current_cells[new_start:new_end],
                current_cells,
                new_start,
            )
        )
    return changes


def _is_highly_repetitive(keys: list[tuple[str, str]]) -> bool:
    if not keys:
        return False
    repeated_count = len(keys) - len(set(keys))
    return repeated_count / len(keys) >= _MIN_REPEATED_CELL_FRACTION


def _diff_replaced_cells(
    old_cells: list[Cell],
    new_cells: list[Cell],
    all_current_cells: list[Cell],
    new_start: int,
) -> list[CellChange]:
    """Align a replace block so nearby source revisions remain edits."""
    old_count = len(old_cells)
    new_count = len(new_cells)
    if old_count * new_count > _MAX_REPLACE_DP_PRODUCT:
        return _diff_replaced_cells_by_position(
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
                CellChange(
                    kind="edited",
                    old_index=old_cell.index,
                    new_index=new_cell.index,
                    old_cell=old_cell,
                    new_cell=new_cell,
                    current_insertion_index=new_cell.index,
                )
            )
            old_pos -= 1
            new_pos -= 1
        elif step == "deleted":
            old_cell = old_cells[old_pos - 1]
            insertion_index = _current_insertion_index(
                all_current_cells, new_start + new_pos
            )
            aligned.append(
                CellChange(
                    kind="deleted",
                    old_index=old_cell.index,
                    new_index=None,
                    old_cell=old_cell,
                    new_cell=None,
                    current_insertion_index=insertion_index,
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


def _diff_replaced_cells_by_position(
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
        insertion_similarity = (
            _fallback_cell_similarity(old_cell, new_cells[new_pos + 1])
            if new_pos + 1 < len(new_cells)
            else -1.0
        )
        deletion_similarity = (
            _fallback_cell_similarity(old_cells[old_pos + 1], new_cell)
            if old_pos + 1 < len(old_cells)
            else -1.0
        )

        if (
            insertion_similarity >= _FALLBACK_SHIFT_MIN_SIMILARITY
            and insertion_similarity >= deletion_similarity
            and insertion_similarity >= direct_similarity + _FALLBACK_SHIFT_ADVANTAGE
        ):
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
            new_pos += 1
            continue
        if (
            deletion_similarity >= _FALLBACK_SHIFT_MIN_SIMILARITY
            and deletion_similarity > insertion_similarity
            and deletion_similarity >= direct_similarity + _FALLBACK_SHIFT_ADVANTAGE
        ):
            changes.append(
                CellChange(
                    kind="deleted",
                    old_index=old_cell.index,
                    new_index=None,
                    old_cell=old_cell,
                    new_cell=None,
                    current_insertion_index=new_cell.index,
                )
            )
            old_pos += 1
            continue

        changes.append(
            CellChange(
                kind="edited",
                old_index=old_cell.index,
                new_index=new_cell.index,
                old_cell=old_cell,
                new_cell=new_cell,
                current_insertion_index=new_cell.index,
            )
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


def _cell_edit_cost(old_cell: Cell, new_cell: Cell) -> float:
    similarity = SequenceMatcher(
        None,
        old_cell.source,
        new_cell.source,
        autojunk=False,
    ).ratio()
    type_penalty = 0.25 if old_cell.cell_type != new_cell.cell_type else 0.0
    return 1.0 - similarity + type_penalty


def build_summary_data(
    parsed: ParsedFile, changes: list[CellChange] | None = None
) -> dict:
    """Build structured cell summaries, optionally annotated with cell changes."""
    changes = changes or []
    changed_current = {
        change.new_index: change for change in changes if change.new_index is not None
    }
    cells = []
    for cell in parsed.cells:
        cell_data = _summarize_cell(cell)
        if change := changed_current.get(cell.index):
            cell_data["change"] = change.kind
            if change.old_index is not None:
                cell_data["old_index"] = change.old_index
        cells.append(cell_data)
    return _notebook_data(
        parsed,
        cells,
        changes=[_serialize_change(change) for change in changes],
    )


def _current_insertion_index(cells: list[Cell], position: int) -> int:
    return cells[position].index if position < len(cells) else len(cells)


def _serialize_change(change: CellChange) -> dict:
    data = {
        "kind": change.kind,
        "old_index": change.old_index,
        "new_index": change.new_index,
        "current_insertion_index": change.current_insertion_index,
    }
    if change.old_cell is not None:
        data["old_cell"] = _summarize_cell(change.old_cell)
    return data


def _summarize_cell(cell: Cell) -> dict:
    preview, preview_truncated = _truncate_text(
        _first_nonempty_line(cell.source) or "",
        _MAX_SOURCE_PREVIEW_CHARS,
    )
    data = {
        "index": cell.index,
        "type": cell.cell_type,
        "line_count": len(cell.source.splitlines()),
        "source_preview": preview,
        "source_preview_truncated": preview_truncated,
    }
    if cell.cell_type == CellType.CODE:
        data.update(_summarize_python(cell.source))
    elif cell.cell_type == CellType.MARKDOWN:
        data["first_nonempty_line"] = preview
    return data


def _summarize_python(source: str) -> dict:
    data = {
        "ast_parsed": False,
        "imports": [],
        "imports_truncated": False,
        "defines": [],
        "defines_truncated": False,
        "writes": [],
        "writes_truncated": False,
        "calls": [],
        "calls_truncated": False,
    }
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return data

    collector = _PythonSummaryCollector()
    collector.visit(tree)
    data["ast_parsed"] = True
    for field, values in collector.values().items():
        data[field] = values
        data[f"{field}_truncated"] = collector.truncated(field)
    return data


class _PythonSummaryCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.imports: list[str] = []
        self.defines: list[str] = []
        self.writes: list[str] = []
        self.calls: list[str] = []
        self._seen: dict[str, set[str]] = {
            "imports": set(),
            "defines": set(),
            "writes": set(),
            "calls": set(),
        }
        self._overflow: set[str] = set()

    def values(self) -> dict[str, list[str]]:
        return {
            "imports": self.imports,
            "defines": self.defines,
            "writes": self.writes,
            "calls": self.calls,
        }

    def truncated(self, field: str) -> bool:
        return field in self._overflow

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._append("imports", _format_alias(alias.name, alias.asname))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = "." * node.level + (node.module or "")
        for alias in node.names:
            separator = "" if module.endswith(".") else "."
            name = f"{module}{separator}{alias.name}" if module else alias.name
            self._append("imports", _format_alias(name, alias.asname))
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._append("defines", node.name)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._append("defines", node.name)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        self._add_targets(node.targets)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._add_target(node.target)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._add_target(node.target)
        self.generic_visit(node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self._add_target(node.target)
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        self._add_target(node.target)
        self.generic_visit(node)

    visit_AsyncFor = visit_For

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            if item.optional_vars:
                self._add_target(item.optional_vars)
        self.generic_visit(node)

    visit_AsyncWith = visit_With

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self._append("writes", node.name)
        self.generic_visit(node)

    def visit_comprehension(self, node: ast.comprehension) -> None:
        self._add_target(node.target)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if name := _qualified_name(node.func):
            self._append("calls", name)
        self.generic_visit(node)

    def _add_targets(self, targets: list[ast.expr]) -> None:
        for target in targets:
            self._add_target(target)

    def _add_target(self, target: ast.expr) -> None:
        if isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._add_target(element)
            return
        if isinstance(target, ast.Starred):
            self._add_target(target.value)
            return
        try:
            self._append("writes", ast.unparse(target))
        except Exception:
            return

    def _append(self, field: str, value: str) -> None:
        values = getattr(self, field)
        seen = self._seen[field]
        if value in seen:
            return
        if len(values) >= _MAX_ANALYSIS_ITEMS:
            self._overflow.add(field)
            return
        seen.add(value)
        values.append(value)


def _format_alias(name: str, alias: str | None) -> str:
    return f"{name} as {alias}" if alias else name


def _qualified_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        if parent := _qualified_name(node.value):
            return f"{parent}.{node.attr}"
    return None


def _truncate_text(value: str, limit: int) -> tuple[str, bool]:
    return value[:limit], len(value) > limit


def _first_nonempty_line(source: str) -> str | None:
    return next((line for line in source.splitlines() if line.strip()), None)


def format_summary_human(
    data: dict,
    *,
    max_cells: int | None = None,
    max_chars: int | None = None,
    neighbor_cells: int = 1,
) -> str:
    """Format a summary, optionally prioritizing changes within hook budgets."""
    if max_cells is not None or max_chars is not None:
        return _format_summary_human_bounded(
            data,
            max_cells=max_cells,
            max_chars=max_chars,
            neighbor_cells=neighbor_cells,
        )

    kernel = data["kernel"] if data["kernel"] is not None else "None"
    lines = [f"path={data['path']} cells={data['cell_count']} kernel={kernel}"]
    changes = data.get("changes", [])
    if changes:
        kinds = [
            kind
            for kind in _CHANGE_MARKERS
            if any(change["kind"] == kind for change in changes)
        ]
        lines.append("changes: " + _format_change_overview(changes, kinds))
        lines.append(
            "legend: " + " | ".join(f"{_CHANGE_MARKERS[kind]} {kind}" for kind in kinds)
        )
    deleted_by_position: dict[int, list[dict]] = {}
    for change in changes:
        if change["kind"] == "deleted":
            deleted_by_position.setdefault(
                change["current_insertion_index"], []
            ).append(change)
    for cell in data["cells"]:
        for change in deleted_by_position.pop(cell["index"], []):
            heading = f"- old:{change['old_index']} at current:{change['current_insertion_index']}"
            lines.append(_format_summary_cell_human(change["old_cell"], heading))
        marker = _CHANGE_MARKERS.get(cell.get("change"), "")
        prefix = f"{marker} " if marker else ""
        lines.append(_format_summary_cell_human(cell, f"{prefix}{cell['index']}"))
    for position in sorted(deleted_by_position):
        for change in deleted_by_position[position]:
            heading = f"- old:{change['old_index']} at current:{change['current_insertion_index']}"
            lines.append(_format_summary_cell_human(change["old_cell"], heading))
    return "\n".join(lines)


def _format_change_overview(changes: list[dict], kinds: list[str]) -> str:
    parts = []
    for kind in kinds:
        matching = [change for change in changes if change["kind"] == kind]
        visible = matching[:_MAX_CHANGE_OVERVIEW_ITEMS]
        omitted = len(matching) - len(visible)
        if kind == "deleted":
            locations = ", ".join(
                f"old:{change['old_index']} at current:{change['current_insertion_index']}"
                for change in visible
            )
            if omitted:
                locations += f", ... +{omitted} more"
            parts.append(f"deleted [{locations}]")
        else:
            indices = ",".join(str(change["new_index"]) for change in visible)
            if omitted:
                indices += f",...+{omitted} more"
            parts.append(f"{kind} current[{indices}]")
    return "; ".join(parts)


def _format_summary_human_bounded(
    data: dict,
    *,
    max_cells: int | None,
    max_chars: int | None,
    neighbor_cells: int,
) -> str:
    cells = data["cells"]
    changes = data.get("changes", [])
    deleted = [change for change in changes if change["kind"] == "deleted"]
    limit = max(0, max_cells) if max_cells is not None else len(cells) + len(deleted)

    current_by_index = {cell["index"]: cell for cell in cells}
    changed_indices = [cell["index"] for cell in cells if cell.get("change")]
    candidates: list[tuple[str, dict]] = []
    seen_current: set[int] = set()

    def add_current(index: int) -> None:
        if index in current_by_index and index not in seen_current:
            seen_current.add(index)
            candidates.append(("current", current_by_index[index]))

    if len(cells) + len(deleted) <= limit:
        for cell in cells:
            add_current(cell["index"])
        candidates.extend(("deleted", change) for change in deleted)
    else:
        for index in changed_indices:
            add_current(index)
        candidates.extend(("deleted", change) for change in deleted)
        anchors = changed_indices + [
            change["current_insertion_index"] for change in deleted
        ]
        for distance in range(1, max(0, neighbor_cells) + 1):
            for anchor in anchors:
                add_current(anchor - distance)
                add_current(anchor + distance)

    selected = candidates[:limit]
    kinds = [
        kind
        for kind in _CHANGE_MARKERS
        if any(change["kind"] == kind for change in changes)
    ]
    kernel = data["kernel"] if data["kernel"] is not None else "None"
    path = _truncate_bounded_field(str(data["path"]))
    kernel = _truncate_bounded_field(str(kernel))
    lines = [f"path={path} cells={data['cell_count']} kernel={kernel}"]
    if changes:
        lines.append("changes: " + _format_change_overview(changes, kinds))
        lines.append(
            "legend: " + " | ".join(f"{_CHANGE_MARKERS[kind]} {kind}" for kind in kinds)
        )

    shown_current = 0
    shown_deleted = 0
    details_truncated = path != str(data["path"]) or kernel != str(
        data["kernel"] if data["kernel"] is not None else "None"
    )

    for item_kind, item in selected:
        if item_kind == "deleted":
            heading = f"- old:{item['old_index']} at current:{item['current_insertion_index']}"
            raw_line = _format_summary_cell_human(item["old_cell"], heading)
        else:
            marker = _CHANGE_MARKERS.get(item.get("change"), "")
            heading = f"{marker} {item['index']}" if marker else str(item["index"])
            raw_line = _format_summary_cell_human(item, heading)
        line = _truncate_bounded_line(raw_line, _MAX_BOUNDED_CELL_LINE_CHARS)
        line_truncated = line != raw_line

        next_current = shown_current + (item_kind == "current")
        next_deleted = shown_deleted + (item_kind == "deleted")
        suffix = _format_bounded_omission(
            data,
            len(cells) - next_current,
            len(deleted) - next_deleted,
            details_truncated or line_truncated,
        )
        tentative = "\n".join([*lines, line, suffix])
        if max_chars is not None and len(tentative) > max_chars:
            break
        lines.append(line)
        shown_current = next_current
        shown_deleted = next_deleted
        details_truncated = details_truncated or line_truncated

    omitted_current = len(cells) - shown_current
    omitted_deleted = len(deleted) - shown_deleted
    suffix = _format_bounded_omission(
        data,
        omitted_current,
        omitted_deleted,
        details_truncated,
    )
    if suffix:
        lines.append(suffix)

    text = "\n".join(lines)
    if max_chars is not None and len(text) > max_chars:
        # Metadata and change lines have fixed caps; this only handles very small
        # caller-provided budgets while preserving the command hint at the end.
        hint = _format_bounded_omission(data, len(cells), len(deleted), True)
        available = max(0, max_chars - len(hint) - 1)
        prefix = "\n".join(lines[:3])[:available]
        return f"{prefix}\n{hint}"[-max_chars:] if max_chars else ""
    return text


def _truncate_bounded_field(value: str) -> str:
    return _truncate_bounded_line(value, _MAX_BOUNDED_METADATA_FIELD_CHARS)


def _truncate_bounded_line(value: str, limit: int) -> str:
    suffix = "... [truncated]"
    if len(value) <= limit:
        return value
    return value[: max(0, limit - len(suffix))] + suffix


def _format_bounded_omission(
    data: dict,
    omitted_current: int,
    omitted_deleted: int,
    details_truncated: bool,
) -> str:
    if not omitted_current and not omitted_deleted and not details_truncated:
        return ""
    path = _truncate_bounded_field(str(data["path"]))
    detail = "; cell details truncated" if details_truncated else ""
    return (
        f"omitted: {omitted_current} current cells, {omitted_deleted} deleted tombstones"
        f"{detail}; run: j-cli notebook summary {path}"
    )


def _format_summary_cell_human(cell: dict, heading: str) -> str:
    cell_type = CellType(cell["type"])
    parts = [f"{heading} [{cell_type.value}] [{cell['line_count']}L]"]
    if cell_type == CellType.CODE:
        for field in ("imports", "defines", "writes", "calls"):
            if not cell[field]:
                continue
            values = ", ".join(cell[field])
            if cell[f"{field}_truncated"]:
                values += " [truncated]"
            parts.append(f"{field}={values}")
    elif cell_type == CellType.MARKDOWN:
        parts.append(f"first_line={cell['first_nonempty_line']!r}")
    preview = repr(cell["source_preview"])
    if cell["source_preview_truncated"]:
        preview += " [truncated]"
    parts.append(f"preview={preview}")
    return " ".join(parts)


def _format_show_human(cells: list[Cell]) -> str:
    return "\n".join(
        f"--- cell {cell.index} [{cell.cell_type.value}] ---\n{cell.source}"
        for cell in cells
    )
