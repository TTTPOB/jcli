"""Render structured notebook summaries for human readers."""

from __future__ import annotations

from jupyter_jcli._enums import CellType

_MAX_CHANGE_OVERVIEW_ITEMS = 12
_MAX_BOUNDED_METADATA_FIELD_CHARS = 1000
_MAX_BOUNDED_CELL_LINE_CHARS = 1000
_CHANGE_MARKERS = {"edited": "~", "inserted": "+", "deleted": "-"}


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
    if "source_start_line" in cell:
        parts.append(f"[L{cell['source_start_line']}-{cell['source_end_line']}]")
    if "source" in cell:
        parts.append(f"source={cell['source']!r}")
        return " ".join(parts)
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
