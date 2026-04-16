"""Diff rendering utilities for drift output."""

from __future__ import annotations

import difflib

from jupyter_jcli.parser import parse_py_percent_text


def render_no_baseline_diff(
    ours_text: str,
    theirs_text: str,
    ours_label: str = "py",
    theirs_label: str = "ipynb",
    max_chars: int = 6000,
) -> str:
    """Unified diff between ours and theirs for no-baseline DRIFT_ONLY cases."""
    lines_ours = ours_text.splitlines(keepends=True)
    lines_theirs = theirs_text.splitlines(keepends=True)
    diff = "".join(difflib.unified_diff(
        lines_ours, lines_theirs,
        fromfile=ours_label, tofile=theirs_label,
    ))
    if len(diff) > max_chars:
        diff = diff[:max_chars] + "\n... (truncated)\n"
    return diff


def locate_conflict_cells(merged_text: str) -> list[int]:
    """Return cell indices whose source contains '<<<<<<<' conflict markers."""
    try:
        parsed = parse_py_percent_text(merged_text)
    except Exception:  # noqa: BLE001
        return []
    return [cell.index for cell in parsed.cells if "<<<<<<<" in cell.source]
