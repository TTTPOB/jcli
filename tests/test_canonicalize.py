"""Tests for py:percent canonicalization."""

from __future__ import annotations

import pytest

from jupyter_jcli.formats.percent import canonicalize as canonicalize_py_text
from jupyter_jcli.formats.percent import loads as parse_py_percent_text


def _py_text(*cell_sources: str, kernel: str = "python3") -> str:
    lines = [
        "# ---\n",
        "# jupyter:\n",
        "#   kernelspec:\n",
        f"#     name: {kernel}\n",
        "# ---\n\n",
    ]
    for src in cell_sources:
        lines.append(f"# %%\n{src}\n\n")
    return "".join(lines)


_CANONICAL_FRONT_MATTER = (
    "# ---\n# jupyter:\n#   kernelspec:\n#     name: python3\n# ---\n\n"
)


class TestCanonicalizePyText:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param(
                _CANONICAL_FRONT_MATTER + "# %%  \nx = 1\n\n\n",
                _CANONICAL_FRONT_MATTER + "# %%\nx = 1\n",
                id="basic-trailing-marker-and-eof-whitespace",
            ),
            pytest.param(
                _CANONICAL_FRONT_MATTER
                + "# %%\nx = 1\n\n"
                + "# %% [markdown]\n# ## Title\n\n",
                _CANONICAL_FRONT_MATTER
                + "# %%\nx = 1\n\n# %% [markdown]\n# ## Title\n",
                id="markdown",
            ),
            pytest.param(
                _py_text("x = 1\ny = 2", "z = 3"),
                _CANONICAL_FRONT_MATTER + "# %%\nx = 1\ny = 2\n\n# %%\nz = 3\n",
                id="multiple",
            ),
            pytest.param(
                "# %%\nx = 1", "# %%\nx = 1\n", id="marker-only-no-eof-newline"
            ),
            pytest.param("# %%\nx = 1\n\n\n", "# %%\nx = 1\n", id="eof-blank-lines"),
        ],
    )
    def test_canonical_form_is_explicit_and_stable(self, text, expected):
        result = canonicalize_py_text(text)
        assert result == expected
        assert canonicalize_py_text(result) == expected

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
        # R metadata tests non-default kernelspec preservation, not R execution support.
        text = (
            "# ---\n# jupyter:\n#   kernelspec:\n#     name: ir\n# ---\n\n"
            "# %%\n1 + 1\n\n"
        )
        result = canonicalize_py_text(text)
        parsed = parse_py_percent_text(result)
        assert parsed.kernel_name == "ir"

    def test_empty_cells_preserved(self):
        text = (
            "# ---\n# jupyter:\n#   kernelspec:\n#     name: python3\n# ---\n\n"
            "# %%\n\n"
            "# %%\nx = 1\n\n"
        )
        result = canonicalize_py_text(text)
        parsed = parse_py_percent_text(result)
        assert [cell.source for cell in parsed.cells] == ["", "x = 1"]

    def test_py_percent_marker_only_is_py_percent(self):
        text = "# %%\nx = 1\n\n"
        result = canonicalize_py_text(text)
        assert "# %%" in result
        assert "x = 1" in result

    def test_jupytext_frontmatter_preserves_structured_metadata(self):
        """Canonical formatting retains metadata; PairState owns exclusions."""
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
        result = canonicalize_py_text(jupytext_py)
        assert "jupytext:" in result
        assert "display_name: Python 3" in result
        assert canonicalize_py_text(result) == result

    def test_display_name_and_language_are_canonicalized(self):
        """Complete kernelspec configuration remains in canonical form."""
        text = (
            "# ---\n# jupyter:\n#   kernelspec:\n"
            "#     display_name: Python 3\n"
            "#     language: python\n"
            "#     name: python3\n"
            "# ---\n\n# %%\nx = 1\n\n"
        )
        result = canonicalize_py_text(text)
        assert "display_name: Python 3" in result
        assert "language: python" in result
        assert "name: python3" in result
