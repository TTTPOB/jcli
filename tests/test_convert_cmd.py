"""Tests for `j-cli convert`."""

from pathlib import Path

import nbformat
import pytest
from click.testing import CliRunner

from jupyter_jcli.cli import main
from jupyter_jcli.parser import parse_py_percent, parse_py_percent_text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _invoke(*args: str):
    runner = CliRunner()
    return runner.invoke(main, list(args), catch_exceptions=False)


def _make_ipynb(cells: list[tuple[str, str, list]], kernel: str = "python3") -> nbformat.NotebookNode:
    nb = nbformat.v4.new_notebook()
    nb.metadata["kernelspec"] = {"name": kernel, "display_name": kernel, "language": "python"}
    for cell_type, source, outputs in cells:
        if cell_type == "code":
            cell = nbformat.v4.new_code_cell(source)
            cell.outputs = [
                nbformat.v4.new_output(output_type="stream", name="stdout", text=o)
                for o in outputs
            ]
            cell.execution_count = len(outputs) or None
        else:
            cell = nbformat.v4.new_markdown_cell(source)
        nb.cells.append(cell)
    return nb


# ---------------------------------------------------------------------------
# ipynb-to-py
# ---------------------------------------------------------------------------

class TestIpynbToPy:
    def test_produces_parseable_py(self, tmp_path):
        nb = _make_ipynb([
            ("code", "import numpy as np", []),
            ("markdown", "## Analysis", []),
            ("code", "x = np.array([1,2,3])", []),
        ])
        ipynb = tmp_path / "nb.ipynb"
        nbformat.write(nb, str(ipynb))
        py = tmp_path / "nb.py"

        result = _invoke("convert", "ipynb-to-py", str(ipynb), str(py))
        assert result.exit_code == 0
        assert py.exists()

        parsed = parse_py_percent(str(py))
        assert len(parsed.cells) == 3
        assert parsed.cells[0].cell_type == "code"
        assert "import numpy" in parsed.cells[0].source
        assert parsed.cells[1].cell_type == "markdown"
        assert parsed.cells[2].cell_type == "code"

    def test_kernel_name_in_output(self, tmp_path):
        nb = _make_ipynb([("code", "x = 1", [])], kernel="ir")
        ipynb = tmp_path / "nb.ipynb"
        nbformat.write(nb, str(ipynb))
        py = tmp_path / "nb.py"

        _invoke("convert", "ipynb-to-py", str(ipynb), str(py))
        parsed = parse_py_percent(str(py))
        assert parsed.kernel_name == "ir"

    def test_roundtrip_content(self, tmp_path):
        """ipynb -> py -> parse should give same sources."""
        nb = _make_ipynb([
            ("code", "a = 1", []),
            ("code", "b = 2", []),
        ])
        ipynb = tmp_path / "nb.ipynb"
        nbformat.write(nb, str(ipynb))
        py = tmp_path / "nb.py"

        _invoke("convert", "ipynb-to-py", str(ipynb), str(py))
        parsed = parse_py_percent(str(py))
        assert parsed.cells[0].source == "a = 1"
        assert parsed.cells[1].source == "b = 2"


# ---------------------------------------------------------------------------
# py-to-ipynb — new file creation
# ---------------------------------------------------------------------------

class TestPyToIpynbCreate:
    def test_creates_new_ipynb(self, tmp_path):
        py = tmp_path / "script.py"
        py.write_text(
            "# ---\n# jupyter:\n#   kernelspec:\n#     name: python3\n# ---\n\n"
            "# %%\nx = 1\n\n# %%\ny = 2\n",
            encoding="utf-8",
        )
        ipynb = tmp_path / "script.ipynb"

        result = _invoke("convert", "py-to-ipynb", str(py))
        assert result.exit_code == 0
        assert ipynb.exists()

        nb = nbformat.read(str(ipynb), as_version=4)
        sources = [c.source for c in nb.cells]
        assert "x = 1" in sources
        assert "y = 2" in sources

    def test_explicit_out_path(self, tmp_path):
        py = tmp_path / "script.py"
        py.write_text("# %%\nx = 1\n", encoding="utf-8")
        out = tmp_path / "custom.ipynb"

        result = _invoke("convert", "py-to-ipynb", str(py), str(out))
        assert result.exit_code == 0
        assert out.exists()

    def test_dummy_py_default_output(self, tmp_path):
        """foo.dummy.py -> foo.ipynb by default."""
        py = tmp_path / "foo.dummy.py"
        py.write_text("# %%\nx = 1\n", encoding="utf-8")

        result = _invoke("convert", "py-to-ipynb", str(py))
        assert result.exit_code == 0
        assert (tmp_path / "foo.ipynb").exists()


# ---------------------------------------------------------------------------
# py-to-ipynb — in-place update (preserve outputs)
# ---------------------------------------------------------------------------

class TestPyToIpynbUpdate:
    def test_update_preserves_outputs(self, tmp_path):
        nb = _make_ipynb([
            ("code", "x = 1", ["1\n"]),
            ("code", "y = 2", ["2\n"]),
        ])
        ipynb = tmp_path / "script.ipynb"
        nbformat.write(nb, str(ipynb))

        py = tmp_path / "script.py"
        py.write_text("# %%\nx = 10\n\n# %%\ny = 20\n", encoding="utf-8")

        result = _invoke("convert", "py-to-ipynb", str(py), str(ipynb))
        assert result.exit_code == 0

        nb2 = nbformat.read(str(ipynb), as_version=4)
        assert nb2.cells[0].source == "x = 10"
        assert nb2.cells[1].source == "y = 20"
        # Outputs preserved
        assert nb2.cells[0].outputs[0]["text"] == "1\n"
        assert nb2.cells[1].outputs[0]["text"] == "2\n"
        assert nb2.cells[0].execution_count == 1
        assert nb2.cells[1].execution_count == 1

    def test_update_outputs_updated_message(self, tmp_path, capsys):
        nb = _make_ipynb([("code", "x = 1", [])])
        ipynb = tmp_path / "nb.ipynb"
        nbformat.write(nb, str(ipynb))
        py = tmp_path / "nb.py"
        py.write_text("# %%\nx = 10\n", encoding="utf-8")

        result = _invoke("convert", "py-to-ipynb", str(py), str(ipynb))
        assert result.exit_code == 0
        assert "Updated" in result.output
