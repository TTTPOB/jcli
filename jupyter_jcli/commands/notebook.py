"""jcli notebook -- inspect notebook cell structure without execution."""

import ast
from dataclasses import dataclass
from difflib import SequenceMatcher

import click

from jupyter_jcli._enums import CellType, ResponseStatus
from jupyter_jcli.cli import Context, pass_ctx
from jupyter_jcli.output import emit, emit_error
from jupyter_jcli.parser import Cell, ParsedFile, parse_cell_spec, parse_file


_MAX_ANALYSIS_ITEMS = 8
_MAX_SOURCE_PREVIEW_CHARS = 120
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
@click.argument("file_path", metavar="FILE", type=click.Path(exists=True, dir_okay=False))
@pass_ctx
def summary(ctx: Context, file_path: str) -> None:
    """Print a deterministic structural summary for each cell in FILE."""
    parsed = _parse_or_error(ctx, file_path)
    data = build_summary_data(parsed)
    if ctx.use_json:
        emit(data, use_json=True)
        return
    emit({"_human": format_summary_human(data)}, use_json=False)


@notebook.command("show")
@click.argument("file_path", metavar="FILE", type=click.Path(exists=True, dir_okay=False))
@click.option("--cell", "cell_spec", required=True, help="Cell spec: 3, 3:7, 3:, :5 (0-indexed)")
@pass_ctx
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


def _notebook_data(parsed: ParsedFile, cells: list[dict], changes: list[dict] | None = None) -> dict:
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
    matcher = SequenceMatcher(
        None,
        [(cell.cell_type.value, cell.source) for cell in old_cells],
        [(cell.cell_type.value, cell.source) for cell in current_cells],
        autojunk=False,
    )
    changes: list[CellChange] = []
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert":
            for cell in current_cells[new_start:new_end]:
                changes.append(CellChange(
                    kind="inserted",
                    old_index=None,
                    new_index=cell.index,
                    old_cell=None,
                    new_cell=cell,
                    current_insertion_index=cell.index,
                ))
            continue
        if tag == "delete":
            insertion_index = _current_insertion_index(current_cells, new_start)
            for cell in old_cells[old_start:old_end]:
                changes.append(CellChange(
                    kind="deleted",
                    old_index=cell.index,
                    new_index=None,
                    old_cell=cell,
                    new_cell=None,
                    current_insertion_index=insertion_index,
                ))
            continue

        changes.extend(_diff_replaced_cells(
            old_cells[old_start:old_end],
            current_cells[new_start:new_end],
            current_cells,
            new_start,
        ))
    return changes


def _diff_replaced_cells(
    old_cells: list[Cell],
    new_cells: list[Cell],
    all_current_cells: list[Cell],
    new_start: int,
) -> list[CellChange]:
    """Align a replace block so nearby source revisions remain edits."""
    old_count = len(old_cells)
    new_count = len(new_cells)
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
            aligned.append(CellChange(
                kind="edited",
                old_index=old_cell.index,
                new_index=new_cell.index,
                old_cell=old_cell,
                new_cell=new_cell,
                current_insertion_index=new_cell.index,
            ))
            old_pos -= 1
            new_pos -= 1
        elif step == "deleted":
            old_cell = old_cells[old_pos - 1]
            insertion_index = _current_insertion_index(all_current_cells, new_start + new_pos)
            aligned.append(CellChange(
                kind="deleted",
                old_index=old_cell.index,
                new_index=None,
                old_cell=old_cell,
                new_cell=None,
                current_insertion_index=insertion_index,
            ))
            old_pos -= 1
        else:
            new_cell = new_cells[new_pos - 1]
            aligned.append(CellChange(
                kind="inserted",
                old_index=None,
                new_index=new_cell.index,
                old_cell=None,
                new_cell=new_cell,
                current_insertion_index=new_cell.index,
            ))
            new_pos -= 1

    aligned.reverse()
    return aligned


def _cell_edit_cost(old_cell: Cell, new_cell: Cell) -> float:
    similarity = SequenceMatcher(
        None,
        old_cell.source,
        new_cell.source,
        autojunk=False,
    ).ratio()
    type_penalty = 0.25 if old_cell.cell_type != new_cell.cell_type else 0.0
    return 1.0 - similarity + type_penalty


def build_summary_data(parsed: ParsedFile, changes: list[CellChange] | None = None) -> dict:
    """Build structured cell summaries, optionally annotated with cell changes."""
    changes = changes or []
    changed_current = {
        change.new_index: change
        for change in changes
        if change.new_index is not None
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
        data[field], data[f"{field}_truncated"] = _truncate_items(values)
    return data


class _PythonSummaryCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.imports: list[str] = []
        self.defines: list[str] = []
        self.writes: list[str] = []
        self.calls: list[str] = []

    def values(self) -> dict[str, list[str]]:
        return {
            "imports": self.imports,
            "defines": self.defines,
            "writes": self.writes,
            "calls": self.calls,
        }

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._append("imports", _format_alias(alias.name, alias.asname))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = "." * node.level + (node.module or "")
        for alias in node.names:
            name = f"{module}.{alias.name}" if module else alias.name
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
        if value not in values:
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


def _truncate_items(values: list[str]) -> tuple[list[str], bool]:
    return values[:_MAX_ANALYSIS_ITEMS], len(values) > _MAX_ANALYSIS_ITEMS


def _truncate_text(value: str, limit: int) -> tuple[str, bool]:
    return value[:limit], len(value) > limit


def _first_nonempty_line(source: str) -> str | None:
    return next((line for line in source.splitlines() if line.strip()), None)


def format_summary_human(data: dict) -> str:
    kernel = data["kernel"] if data["kernel"] is not None else "None"
    lines = [f"path={data['path']} cells={data['cell_count']} kernel={kernel}"]
    changes = data.get("changes", [])
    if changes:
        kinds = [kind for kind in _CHANGE_MARKERS if any(change["kind"] == kind for change in changes)]
        lines.append("changes: " + _format_change_overview(changes, kinds))
        lines.append("legend: " + " | ".join(f"{_CHANGE_MARKERS[kind]} {kind}" for kind in kinds))
    deleted_by_position: dict[int, list[dict]] = {}
    for change in changes:
        if change["kind"] == "deleted":
            deleted_by_position.setdefault(change["current_insertion_index"], []).append(change)
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
        if kind == "deleted":
            locations = ", ".join(
                f"old:{change['old_index']} at current:{change['current_insertion_index']}"
                for change in matching
            )
            parts.append(f"deleted [{locations}]")
        else:
            indices = ",".join(str(change["new_index"]) for change in matching)
            parts.append(f"{kind} current[{indices}]")
    return "; ".join(parts)


def _format_summary_cell_human(cell: dict, heading: str) -> str:
    cell_type = CellType(cell["type"])
    parts = [f"{heading} [{cell_type.value}] [{cell['line_count']}L]"]
    if cell_type == CellType.CODE:
        for field in ("imports", "defines", "writes", "calls"):
            values = ", ".join(cell[field]) or "-"
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
