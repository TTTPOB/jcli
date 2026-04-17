"""Tests for `j-cli convert`."""

import subprocess
from pathlib import Path

import nbformat
import pytest
from click.testing import CliRunner

from jupyter_jcli import pair_baseline
from jupyter_jcli.canonicalize import canonicalize_py_text
from jupyter_jcli.cli import main
from jupyter_jcli.parser import parse_py_percent, parse_py_percent_text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _invoke(*args: str):
    runner = CliRunner()
    return runner.invoke(main, list(args), catch_exceptions=False)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    return tmp_path


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )


def _has_ref(repo: Path, rel_py_path: str) -> bool:
    ref_name = pair_baseline._ref_name(Path(rel_py_path).as_posix())
    result = subprocess.run(
        ["git", "for-each-ref", ref_name, "--format=%(refname)"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() == ref_name


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

    def test_canonical_ipynb_to_py_refreshes_baseline(self, git_repo):
        nb = _make_ipynb([("code", "x = 1", [])])
        ipynb = git_repo / "nb.ipynb"
        nbformat.write(nb, str(ipynb))
        py = git_repo / "nb.py"

        result = _invoke("convert", "ipynb-to-py", str(ipynb), str(py))

        assert result.exit_code == 0
        assert _has_ref(git_repo, "nb.py")
        assert pair_baseline.read_baseline(py) == canonicalize_py_text(py.read_text(encoding="utf-8"))

    def test_dummy_ipynb_to_py_refreshes_baseline(self, git_repo):
        nb = _make_ipynb([("code", "x = 1", [])])
        ipynb = git_repo / "nb.ipynb"
        nbformat.write(nb, str(ipynb))
        py = git_repo / "nb.dummy.py"

        result = _invoke("convert", "ipynb-to-py", str(ipynb), str(py))

        assert result.exit_code == 0
        assert _has_ref(git_repo, "nb.dummy.py")

    def test_noncanonical_ipynb_to_py_does_not_refresh_baseline(self, git_repo):
        nb = _make_ipynb([("code", "x = 1", [])])
        ipynb = git_repo / "nb.ipynb"
        nbformat.write(nb, str(ipynb))
        py = git_repo / "custom.py"

        result = _invoke("convert", "ipynb-to-py", str(ipynb), str(py))

        assert result.exit_code == 0
        assert not _has_ref(git_repo, "custom.py")

    def test_ipynb_to_py_non_git_still_succeeds(self, tmp_path):
        nb = _make_ipynb([("code", "x = 1", [])])
        ipynb = tmp_path / "nb.ipynb"
        nbformat.write(nb, str(ipynb))
        py = tmp_path / "nb.py"

        result = _invoke("convert", "ipynb-to-py", str(ipynb), str(py))

        assert result.exit_code == 0
        assert py.exists()


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

    def test_canonical_py_to_ipynb_refreshes_baseline(self, git_repo):
        py = git_repo / "script.py"
        py.write_text("# %%\nx = 1\n", encoding="utf-8")

        result = _invoke("convert", "py-to-ipynb", str(py))

        assert result.exit_code == 0
        assert (git_repo / "script.ipynb").exists()
        assert _has_ref(git_repo, "script.py")
        assert pair_baseline.read_baseline(py) == canonicalize_py_text(py.read_text(encoding="utf-8"))

    def test_noncanonical_py_to_ipynb_does_not_refresh_baseline(self, git_repo):
        py = git_repo / "script.py"
        py.write_text("# %%\nx = 1\n", encoding="utf-8")
        out = git_repo / "custom.ipynb"

        result = _invoke("convert", "py-to-ipynb", str(py), str(out))

        assert result.exit_code == 0
        assert out.exists()
        assert not _has_ref(git_repo, "script.py")

    def test_py_to_ipynb_non_git_still_succeeds(self, tmp_path):
        py = tmp_path / "script.py"
        py.write_text("# %%\nx = 1\n", encoding="utf-8")

        result = _invoke("convert", "py-to-ipynb", str(py))

        assert result.exit_code == 0
        assert (tmp_path / "script.ipynb").exists()


# ---------------------------------------------------------------------------
# py-to-ipynb — in-place update (preserve outputs)
# ---------------------------------------------------------------------------

class TestPyToIpynbUpdate:
    def test_update_preserves_outputs_for_unchanged_cells(self, tmp_path):
        """Cells whose source is unchanged keep outputs; changed cells lose them."""
        nb = _make_ipynb([
            ("code", "x = 1", ["1\n"]),
            ("code", "y = 2", ["2\n"]),
        ])
        ipynb = tmp_path / "script.ipynb"
        nbformat.write(nb, str(ipynb))

        py = tmp_path / "script.py"
        # x = 1 is unchanged (hash matches -> outputs preserved)
        # y = 20 is changed (hash mismatch -> outputs lost)
        py.write_text("# %%\nx = 1\n\n# %%\ny = 20\n", encoding="utf-8")

        result = _invoke("convert", "py-to-ipynb", str(py), str(ipynb))
        assert result.exit_code == 0

        nb2 = nbformat.read(str(ipynb), as_version=4)
        assert nb2.cells[0].source == "x = 1"
        assert nb2.cells[1].source == "y = 20"
        # Unchanged cell: outputs preserved
        assert nb2.cells[0].outputs[0]["text"] == "1\n"
        assert nb2.cells[0].execution_count == 1
        # Changed cell: outputs lost (user should re-run)
        assert nb2.cells[1].outputs == []

    def test_update_outputs_updated_message(self, tmp_path, capsys):
        nb = _make_ipynb([("code", "x = 1", [])])
        ipynb = tmp_path / "nb.ipynb"
        nbformat.write(nb, str(ipynb))
        py = tmp_path / "nb.py"
        py.write_text("# %%\nx = 10\n", encoding="utf-8")

        result = _invoke("convert", "py-to-ipynb", str(py), str(ipynb))
        assert result.exit_code == 0
        assert "Updated" in result.output
