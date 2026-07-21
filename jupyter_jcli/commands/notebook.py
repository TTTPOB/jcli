"""jcli notebook -- inspect notebook cell structure without execution."""

import ast

import click

from jupyter_jcli._enums import CellType, ResponseStatus
from jupyter_jcli.cli import Context, pass_ctx
from jupyter_jcli.output import emit, emit_error
from jupyter_jcli.parser import Cell, ParsedFile, parse_cell_spec, parse_file


_MAX_ANALYSIS_ITEMS = 8
_MAX_SOURCE_PREVIEW_CHARS = 120


@click.group("notebook")
def notebook():
    """Inspect notebook cells."""


@notebook.command("summary")
@click.argument("file_path", metavar="FILE", type=click.Path(exists=True, dir_okay=False))
@pass_ctx
def summary(ctx: Context, file_path: str) -> None:
    """Print a deterministic structural summary for each cell in FILE."""
    parsed = _parse_or_error(ctx, file_path)
    cells = [_summarize_cell(cell) for cell in parsed.cells]
    data = _notebook_data(parsed, cells)
    if ctx.use_json:
        emit(data, use_json=True)
        return
    emit({"_human": _format_summary_human(data)}, use_json=False)


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


def _notebook_data(parsed: ParsedFile, cells: list[dict]) -> dict:
    return {
        "status": ResponseStatus.OK,
        "path": parsed.source_path,
        "cell_count": len(parsed.cells),
        "kernel": parsed.kernel_name,
        "cells": cells,
    }


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


def _format_summary_human(data: dict) -> str:
    kernel = data["kernel"] if data["kernel"] is not None else "None"
    lines = [f"path={data['path']} cells={data['cell_count']} kernel={kernel}"]
    for cell in data["cells"]:
        cell_type = CellType(cell["type"])
        parts = [f"{cell['index']} [{cell_type.value}] [{cell['line_count']}L]"]
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
        lines.append(" ".join(parts))
    return "\n".join(lines)


def _format_show_human(cells: list[Cell]) -> str:
    return "\n".join(
        f"--- cell {cell.index} [{cell.cell_type.value}] ---\n{cell.source}"
        for cell in cells
    )
