"""Tests for jupyter_jcli.canonicalize."""

from __future__ import annotations

from jupyter_jcli.canonicalize import canonicalize_py_text
from jupyter_jcli.parser import parse_py_percent_text


def _py_text(*cell_sources: str, kernel: str = "python3") -> str:
    lines = [
        "# ---\n", "# jupyter:\n", "#   kernelspec:\n",
        f"#     name: {kernel}\n", "# ---\n\n",
    ]
    for src in cell_sources:
        lines.append(f"# %%\n{src}\n\n")
    return "".join(lines)


class TestCanonicalizePyText:
    def test_idempotent_basic(self):
        text = _py_text("x = 1")
        r1 = canonicalize_py_text(text)
        r2 = canonicalize_py_text(r1)
        assert r1 == r2

    def test_idempotent_markdown_cell(self):
        text = (
            "# ---\n# jupyter:\n#   kernelspec:\n#     name: python3\n# ---\n\n"
            "# %%\nx = 1\n\n"
            "# %% [markdown]\n# ## Title\n\n"
        )
        r1 = canonicalize_py_text(text)
        r2 = canonicalize_py_text(r1)
        assert r1 == r2

    def test_idempotent_multiple_cells(self):
        text = _py_text("x = 1\ny = 2", "z = 3")
        r1 = canonicalize_py_text(text)
        r2 = canonicalize_py_text(r1)
        assert r1 == r2

    def test_non_py_percent_returned_as_is(self):
        text = "import os\n\ndef main():\n    pass\n"
        assert canonicalize_py_text(text) == text

    def test_preserves_cell_content(self):
        text = _py_text("x = 1\ny = 2", "z = 3")
        result = canonicalize_py_text(text)
        parsed = parse_py_percent_text(result)
        assert len(parsed.cells) == 2
        assert parsed.cells[0].source == "x = 1\ny = 2"
        assert parsed.cells[1].source == "z = 3"

    def test_kernel_name_preserved(self):
        text = (
            "# ---\n# jupyter:\n#   kernelspec:\n#     name: ir\n# ---\n\n"
            "# %%\n1 + 1\n\n"
        )
        result = canonicalize_py_text(text)
        parsed = parse_py_percent_text(result)
        assert parsed.kernel_name == "ir"

    def test_normalizes_trailing_spaces_on_marker(self):
        text = (
            "# ---\n# jupyter:\n#   kernelspec:\n#     name: python3\n# ---\n\n"
            "# %%  \nx = 1\n\n"
        )
        result = canonicalize_py_text(text)
        assert "x = 1" in result

    def test_empty_cells_dropped(self):
        text = (
            "# ---\n# jupyter:\n#   kernelspec:\n#     name: python3\n# ---\n\n"
            "# %%\n\n"
            "# %%\nx = 1\n\n"
        )
        result = canonicalize_py_text(text)
        parsed = parse_py_percent_text(result)
        assert len(parsed.cells) == 1
        assert parsed.cells[0].source == "x = 1"

    def test_py_percent_marker_only_is_py_percent(self):
        text = "# %%\nx = 1\n\n"
        result = canonicalize_py_text(text)
        assert "# %%" in result
        assert "x = 1" in result

    def test_jupytext_frontmatter_normalized_to_name_only(self):
        """Jupytext-style frontmatter (display_name, language, text_representation)
        is stripped down to kernel_name only so .py and .ipynb sides compare equal."""
        jupytext_py = (
            "# ---\n"
            "# jupyter:\n"
            "#   jupytext:\n"
            "#     text_representation:\n"
            "#       extension: .py\n"
            "#       format_name: percent\n"
            "#       format_version: '1.3'\n"
            "#       jupytext_version: 1.19.1\n"
            "#   kernelspec:\n"
            "#     display_name: Python 3\n"
            "#     language: python\n"
            "#     name: python3\n"
            "# ---\n\n"
            "# %%\nx = 1\n\n"
        )
        minimal_py = (
            "# ---\n# jupyter:\n#   kernelspec:\n#     name: python3\n# ---\n\n"
            "# %%\nx = 1\n\n"
        )
        assert canonicalize_py_text(jupytext_py) == canonicalize_py_text(minimal_py)

    def test_display_name_and_language_stripped_for_comparison(self):
        """display_name and language are excluded from canonical form."""
        text = (
            "# ---\n# jupyter:\n#   kernelspec:\n"
            "#     display_name: Python 3\n"
            "#     language: python\n"
            "#     name: python3\n"
            "# ---\n\n# %%\nx = 1\n\n"
        )
        result = canonicalize_py_text(text)
        assert "display_name" not in result
        assert "language" not in result
        assert "python3" in result
