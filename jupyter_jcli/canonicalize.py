"""Normalize py:percent text through our parser+emitter round-trip."""

from __future__ import annotations

from jupyter_jcli.pair_io import emit_py_percent
from jupyter_jcli.parser import parse_py_percent_text


def canonicalize_py_text(text: str) -> str:
    """Round-trip through parser + emitter to normalize py:percent formatting.

    Required so all three merge sides use identical formatting, avoiding
    spurious diffs from whitespace or marker-style differences. Non-py:percent
    files (no front matter and no # %% markers) are returned unchanged.

    Frontmatter is re-synthesized from kernel_name only (display_name and
    language are stripped). This ensures both the .py side (which may have
    jupytext-style frontmatter) and the .ipynb side (which has full kernelspec
    metadata) produce the same canonical text when kernel_name matches.
    """
    parsed = parse_py_percent_text(text)
    if not parsed.is_py_percent:
        return text
    # Normalize frontmatter: drop raw block and extra kernelspec fields so
    # both sides always emit identical frontmatter for drift comparison.
    parsed.front_matter_raw = None
    parsed.kernel_display_name = None
    parsed.kernel_language = None
    return emit_py_percent(parsed)
