"""Normalize py:percent text through our parser+emitter round-trip."""

from __future__ import annotations

from jupyter_jcli.pair_io import emit_py_percent
from jupyter_jcli.parser import parse_py_percent_text


def canonicalize_py_text(text: str) -> str:
    """Round-trip through parser + emitter to normalize py:percent formatting.

    Required so all three merge sides use identical formatting, avoiding
    spurious diffs from whitespace or marker-style differences. Non-py:percent
    files (no front matter and no # %% markers) are returned unchanged.
    """
    parsed = parse_py_percent_text(text)
    if not parsed.is_py_percent:
        return text
    return emit_py_percent(parsed)
