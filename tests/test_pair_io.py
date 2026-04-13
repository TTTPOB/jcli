"""Tests for jupyter_jcli.pair_io."""

from pathlib import Path

import nbformat
import pytest

from jupyter_jcli.pair_io import (
    create_ipynb_from_parsed,
    emit_py_percent,
    update_ipynb_sources,
)
from jupyter_jcli.parser import Cell, ParsedFile, parse_py_percent_text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cells(*pairs: tuple[str, str]) -> list[Cell]:
    """Build a Cell list from (cell_type, source) pairs."""
    return [Cell(index=i, cell_type=t, source=s) for i, (t, s) in enumerate(pairs)]


def _parsed(kernel: str | None, *pairs: tuple[str, str], fmr: str | None = None) -> ParsedFile:
    return ParsedFile(
        kernel_name=kernel,
        cells=_cells(*pairs),
        front_matter_raw=fmr,
    )


# ---------------------------------------------------------------------------
# emit_py_percent — round-trip stability
# ---------------------------------------------------------------------------

class TestEmitPyPercent:
    def test_roundtrip_code_cell(self):
        source = "x = 1\ny = 2"
        parsed = _parsed("python3", ("code", source))
        text = emit_py_percent(parsed)
        parsed2 = parse_py_percent_text(text)
        assert len(parsed2.cells) == 1
        assert parsed2.cells[0].cell_type == "code"
        assert parsed2.cells[0].source == source

    def test_roundtrip_markdown_cell(self):
        source = "## Title\n\nSome text"
        parsed = _parsed("python3", ("markdown", source))
        text = emit_py_percent(parsed)
        parsed2 = parse_py_percent_text(text)
        assert len(parsed2.cells) == 1
        assert parsed2.cells[0].cell_type == "markdown"
        assert parsed2.cells[0].source == source

    def test_roundtrip_mixed_cells(self):
        cells = [
            ("code", "import numpy as np"),
            ("markdown", "## Analysis"),
            ("code", "np.array([1, 2, 3])"),
        ]
        parsed = _parsed("python3", *cells)
        text = emit_py_percent(parsed)
        parsed2 = parse_py_percent_text(text)
        assert len(parsed2.cells) == 3
        for orig, reparsed in zip(cells, parsed2.cells):
            assert reparsed.cell_type == orig[0]
            assert reparsed.source == orig[1]

    def test_markdown_line_prefix(self):
        source = "## Title\nSome text"
        parsed = _parsed(None, ("markdown", source))
        text = emit_py_percent(parsed)
        lines = [l for l in text.splitlines() if l.startswith("#")]
        # All markdown body lines should be prefixed with "# "
        body_lines = [l for l in lines if not l.startswith("# %%")]
        for line in body_lines:
            assert line.startswith("# ") or line == "#"

    def test_empty_markdown_line_becomes_bare_hash(self):
        # Empty line in markdown source should emit "#" (not "# ")
        source = "line1\n\nline2"
        parsed = _parsed(None, ("markdown", source))
        text = emit_py_percent(parsed)
        parsed2 = parse_py_percent_text(text)
        assert parsed2.cells[0].source == source

    def test_front_matter_preserved(self):
        # front_matter_raw always ends with \n (splitlines keepends=True)
        fmr = "# ---\n# jupyter:\n#   kernelspec:\n#     name: ir\n# ---\n"
        parsed = _parsed("ir", ("code", "print(1)"), fmr=fmr)
        text = emit_py_percent(parsed)
        assert text.startswith(fmr)
        parsed2 = parse_py_percent_text(text)
        assert parsed2.front_matter_raw == fmr
        assert parsed2.kernel_name == "ir"

    def test_synthesized_header_from_kernel_name(self):
        parsed = _parsed("python3", ("code", "x = 1"))
        text = emit_py_percent(parsed)
        assert "# ---" in text
        assert "python3" in text
        parsed2 = parse_py_percent_text(text)
        assert parsed2.kernel_name == "python3"

    def test_no_header_when_no_kernel(self):
        parsed = _parsed(None, ("code", "x = 1"))
        text = emit_py_percent(parsed)
        assert not text.startswith("# ---")
        parsed2 = parse_py_percent_text(text)
        assert len(parsed2.cells) == 1

    def test_skips_empty_cells(self):
        parsed = _parsed("python3", ("code", ""), ("code", "x = 1"))
        text = emit_py_percent(parsed)
        parsed2 = parse_py_percent_text(text)
        assert len(parsed2.cells) == 1
        assert parsed2.cells[0].source == "x = 1"

    def test_raw_cell_roundtrip(self):
        source = "raw content"
        parsed = _parsed(None, ("raw", source))
        text = emit_py_percent(parsed)
        assert "# %% [raw]" in text
        parsed2 = parse_py_percent_text(text)
        assert parsed2.cells[0].cell_type == "raw"
        assert parsed2.cells[0].source == source


# ---------------------------------------------------------------------------
# update_ipynb_sources — source-only update, outputs/metadata preserved
# ---------------------------------------------------------------------------

def _make_ipynb(cells: list[tuple[str, str, list]]) -> nbformat.NotebookNode:
    """Create a notebook from (cell_type, source, outputs) triples."""
    nb = nbformat.v4.new_notebook()
    for cell_type, source, outputs in cells:
        if cell_type == "code":
            cell = nbformat.v4.new_code_cell(source)
            cell.outputs = [
                nbformat.v4.new_output(output_type="stream", name="stdout", text=o)
                for o in outputs
            ]
            cell.execution_count = 1 if outputs else None
        else:
            cell = nbformat.v4.new_markdown_cell(source)
        nb.cells.append(cell)
    return nb


class TestUpdateIpynbSources:
    def test_updates_source_preserves_outputs(self, tmp_path):
        nb = _make_ipynb([
            ("code", "x = 1", ["1\n"]),
            ("code", "y = 2", ["2\n"]),
        ])
        p = tmp_path / "nb.ipynb"
        nbformat.write(nb, str(p))

        new_cells = [
            Cell(0, "code", "x = 10"),
            Cell(1, "code", "y = 20"),
        ]
        update_ipynb_sources(p, new_cells)

        nb2 = nbformat.read(str(p), as_version=4)
        assert nb2.cells[0].source == "x = 10"
        assert nb2.cells[1].source == "y = 20"
        # Outputs/execution_count unchanged
        assert nb2.cells[0].execution_count == 1
        assert nb2.cells[1].execution_count == 1
        assert nb2.cells[0].outputs[0]["text"] == "1\n"
        assert nb2.cells[1].outputs[0]["text"] == "2\n"

    def test_raises_on_count_mismatch(self, tmp_path):
        nb = _make_ipynb([
            ("code", "x = 1", []),
            ("code", "y = 2", []),
        ])
        p = tmp_path / "nb.ipynb"
        nbformat.write(nb, str(p))

        with pytest.raises(ValueError, match="Cell count mismatch"):
            update_ipynb_sources(p, [Cell(0, "code", "x = 10")])

    def test_skips_empty_ipynb_cells(self, tmp_path):
        """Empty cells in ipynb are skipped; non-empty ones are updated positionally."""
        nb = nbformat.v4.new_notebook()
        nb.cells.append(nbformat.v4.new_code_cell("x = 1"))
        nb.cells.append(nbformat.v4.new_code_cell(""))  # empty
        nb.cells.append(nbformat.v4.new_code_cell("y = 2"))
        p = tmp_path / "nb.ipynb"
        nbformat.write(nb, str(p))

        new_cells = [
            Cell(0, "code", "x = 10"),
            Cell(1, "code", "y = 20"),
        ]
        update_ipynb_sources(p, new_cells)

        nb2 = nbformat.read(str(p), as_version=4)
        assert nb2.cells[0].source == "x = 10"
        assert nb2.cells[1].source == ""  # empty cell unchanged
        assert nb2.cells[2].source == "y = 20"


# ---------------------------------------------------------------------------
# create_ipynb_from_parsed
# ---------------------------------------------------------------------------

class TestCreateIpynbFromParsed:
    def test_creates_notebook_with_cells(self, tmp_path):
        parsed = _parsed("python3", ("code", "x = 1"), ("markdown", "## Title"))
        nb = create_ipynb_from_parsed(parsed)
        assert len(nb.cells) == 2
        assert nb.cells[0].cell_type == "code"
        assert nb.cells[0].source == "x = 1"
        assert nb.cells[1].cell_type == "markdown"
        assert nb.cells[1].source == "## Title"

    def test_sets_kernelspec_metadata(self):
        parsed = _parsed("ir", ("code", "1 + 1"))
        nb = create_ipynb_from_parsed(parsed)
        assert nb.metadata["kernelspec"]["name"] == "ir"

    def test_no_kernelspec_when_kernel_is_none(self):
        parsed = _parsed(None, ("code", "x = 1"))
        nb = create_ipynb_from_parsed(parsed)
        assert "kernelspec" not in nb.metadata

    def test_skips_empty_cells(self):
        parsed = _parsed(None, ("code", ""), ("code", "x = 1"))
        nb = create_ipynb_from_parsed(parsed)
        assert len(nb.cells) == 1
        assert nb.cells[0].source == "x = 1"

    def test_writable_with_nbformat(self, tmp_path):
        parsed = _parsed("python3", ("code", "x = 1"))
        nb = create_ipynb_from_parsed(parsed)
        p = tmp_path / "out.ipynb"
        nbformat.write(nb, str(p))
        nb2 = nbformat.read(str(p), as_version=4)
        assert nb2.cells[0].source == "x = 1"
