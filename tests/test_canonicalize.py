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
