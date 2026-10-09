"""Render structured notebook summaries for human readers."""

from __future__ import annotations

from jupyter_jcli._enums import CellType


def format_summary_human(data: dict) -> str:
    """Format notebook metadata and cell summaries for inspection."""
    kernel = data["kernel"] if data["kernel"] is not None else "None"
    lines = [f"path={data['path']} cells={data['cell_count']} kernel={kernel}"]
    lines.extend(_format_summary_cell_human(cell) for cell in data["cells"])
    return "\n".join(lines)


def _format_summary_cell_human(cell: dict) -> str:
    cell_type = CellType(cell["type"])
    line_count = cell["line_count"]
    line_label = "line" if line_count == 1 else "lines"
    parts = [f"{cell['index']} [{cell_type.value}] [{line_count} {line_label}]"]
    if "source_start_line" in cell:
        parts.append(f"[L{cell['source_start_line']}-{cell['source_end_line']}]")
    if "full_text" in cell:
        parts.append(f"full_text={cell['full_text']!r}")
        return " ".join(parts)
    if cell_type == CellType.CODE:
        for field in ("imports", "defines", "writes", "calls"):
            if not cell[field]:
                continue
            values = ", ".join(cell[field])
            if cell[f"{field}_truncated"]:
                values += " [truncated]"
            parts.append(f"{field}={values}")
    preview = repr(cell["preview"])
    if cell["preview_truncated"]:
        preview += " [truncated]"
    parts.append(f"preview={preview}")
    return " ".join(parts)
