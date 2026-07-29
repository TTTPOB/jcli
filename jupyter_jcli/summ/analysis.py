"""Build structured notebook cell summaries."""

from __future__ import annotations

import ast

from jupyter_jcli._enums import CellType, ResponseStatus
from jupyter_jcli.diff.alignment import CellChange
from jupyter_jcli.formats.model import Cell, ParsedFile

_MAX_ANALYSIS_ITEMS = 8
_MAX_SOURCE_PREVIEW_CHARS = 120


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
    return {
        "status": ResponseStatus.OK,
        "path": parsed.source_path,
        "cell_count": len(parsed.cells),
        "kernel": parsed.kernel_name,
        "cells": cells,
        "changes": [_serialize_change(change) for change in changes],
    }


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
    if cell.source_start_line is not None and cell.source_end_line is not None:
        data["source_start_line"] = cell.source_start_line
        data["source_end_line"] = cell.source_end_line
    if len(cell.source) <= _MAX_SOURCE_PREVIEW_CHARS:
        data["source"] = cell.source
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
        except Exception:  # noqa: BLE001 - ast.unparse may reject unknown nodes
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
    if isinstance(node, ast.Attribute) and (parent := _qualified_name(node.value)):
        return f"{parent}.{node.attr}"
    return None


def _truncate_text(value: str, limit: int) -> tuple[str, bool]:
    return value[:limit], len(value) > limit


def _first_nonempty_line(source: str) -> str | None:
    return next((line for line in source.splitlines() if line.strip()), None)
