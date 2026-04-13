"""Tests for jupyter_jcli.drift."""

from pathlib import Path
from unittest.mock import patch

import nbformat
import pytest

from jupyter_jcli.drift import DriftResult, check_drift, three_way_merge
from jupyter_jcli.parser import Cell


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cells(*sources: str, cell_type: str = "code") -> list[Cell]:
    return [Cell(index=i, cell_type=cell_type, source=s) for i, s in enumerate(sources)]


def _make_py_text(*sources: str, kernel: str = "python3") -> str:
    lines = [
        "# ---\n",
        "# jupyter:\n",
        "#   kernelspec:\n",
        f"#     name: {kernel}\n",
        "# ---\n",
        "\n",
    ]
    for src in sources:
        lines.append("# %%\n")
        lines.append(src + "\n")
        lines.append("\n")
    return "".join(lines)


def _make_ipynb_text(*sources: str, kernel: str = "python3") -> str:
    nb = nbformat.v4.new_notebook()
    nb.metadata["kernelspec"] = {"name": kernel, "display_name": kernel, "language": "python"}
    for src in sources:
        nb.cells.append(nbformat.v4.new_code_cell(src))
    return nbformat.writes(nb)


def _write_pair(tmp_path: Path, py_src: list[str], ipynb_src: list[str]) -> tuple[Path, Path]:
    py = tmp_path / "nb.py"
    ipynb = tmp_path / "nb.ipynb"
    py.write_text(_make_py_text(*py_src), encoding="utf-8")
    ipynb.write_text(_make_ipynb_text(*ipynb_src), encoding="utf-8")
    return py, ipynb


# ---------------------------------------------------------------------------
# three_way_merge — unit tests (no file I/O)
# ---------------------------------------------------------------------------

class TestThreeWayMerge:
    def test_no_changes(self):
        cells = _cells("x = 1", "y = 2")
        merged, conflicts = three_way_merge(cells, cells, cells)
        assert conflicts == []
        assert [c.source for c in merged] == ["x = 1", "y = 2"]

    def test_ours_changed_only(self):
        base = _cells("x = 1", "y = 2")
        ours = _cells("x = 10", "y = 2")
        theirs = _cells("x = 1", "y = 2")
        merged, conflicts = three_way_merge(base, ours, theirs)
        assert conflicts == []
        assert merged[0].source == "x = 10"
        assert merged[1].source == "y = 2"

    def test_theirs_changed_only(self):
        base = _cells("x = 1", "y = 2")
        ours = _cells("x = 1", "y = 2")
        theirs = _cells("x = 1", "y = 20")
        merged, conflicts = three_way_merge(base, ours, theirs)
        assert conflicts == []
        assert merged[1].source == "y = 20"

    def test_both_changed_same_cell_is_conflict(self):
        base = _cells("x = 1")
        ours = _cells("x = 10")
        theirs = _cells("x = 99")
        merged, conflicts = three_way_merge(base, ours, theirs)
        assert conflicts == [0]

    def test_both_changed_different_cells_no_conflict(self):
        base = _cells("x = 1", "y = 2")
        ours = _cells("x = 10", "y = 2")
        theirs = _cells("x = 1", "y = 20")
        merged, conflicts = three_way_merge(base, ours, theirs)
        assert conflicts == []
        assert merged[0].source == "x = 10"
        assert merged[1].source == "y = 20"

    def test_cell_count_mismatch_all_conflict(self):
        base = _cells("x = 1")
        ours = _cells("x = 1", "y = 2")  # extra cell
        theirs = _cells("x = 1")
        _, conflicts = three_way_merge(base, ours, theirs)
        assert len(conflicts) >= 1

    def test_empty_base_and_ours(self):
        merged, conflicts = three_way_merge([], [], [])
        assert merged == []
        assert conflicts == []

    def test_conflict_indices_returned(self):
        base = _cells("a", "b", "c")
        ours = _cells("a", "B", "c")
        theirs = _cells("a", "b", "C")
        # cell 1 (b/B) changed by ours, cell 2 (c/C) changed by theirs — not conflict
        # but if both changed the same cell:
        ours2 = _cells("a", "B", "C")
        theirs2 = _cells("a", "b", "X")
        _, conflicts = three_way_merge(base, ours2, theirs2)
        # cell 2: ours2[2]="C" vs theirs2[2]="X" vs base[2]="c" -> conflict
        assert 2 in conflicts

    def test_md_code_type_mismatch_same_index(self):
        """Cell type difference at same index is detected via source comparison."""
        base = _cells("text", cell_type="code")
        ours = [Cell(0, "markdown", "text")]
        theirs = [Cell(0, "code", "other")]
        # ours changed type/source, theirs changed source -> both changed -> conflict
        base[0] = Cell(0, "code", "text")
        ours[0] = Cell(0, "markdown", "## Header")
        theirs[0] = Cell(0, "code", "x = 1")
        _, conflicts = three_way_merge(base, ours, theirs)
        assert 0 in conflicts


# ---------------------------------------------------------------------------
# check_drift — with mocked git
# ---------------------------------------------------------------------------

class TestCheckDrift:
    """Tests for check_drift() using mocked _get_git_base_text."""

    def _patch_git(self, py_base: str | None, ipynb_base: str | None):
        """Return a patch context for _get_git_base_text."""
        def _side_effect(path: Path) -> str | None:
            if path.suffix == ".py":
                return py_base
            return ipynb_base
        return patch("jupyter_jcli.drift._get_git_base_text", side_effect=_side_effect)

    def test_in_sync_no_drift(self, tmp_path):
        py, ipynb = _write_pair(tmp_path, ["x = 1", "y = 2"], ["x = 1", "y = 2"])
        base_py = _make_py_text("x = 1", "y = 2")
        base_ipynb = _make_ipynb_text("x = 1", "y = 2")
        with self._patch_git(base_py, base_ipynb):
            result = check_drift(py, ipynb)
        assert result.status == "in_sync"

    def test_py_only_changed(self, tmp_path):
        py, ipynb = _write_pair(tmp_path, ["x = 10", "y = 2"], ["x = 1", "y = 2"])
        base_py = _make_py_text("x = 1", "y = 2")
        base_ipynb = _make_ipynb_text("x = 1", "y = 2")
        with self._patch_git(base_py, base_ipynb):
            result = check_drift(py, ipynb)
        assert result.status == "merged"
        assert result.ipynb_needs_update is True
        assert result.py_needs_update is False
        assert result.merged_cells[0].source == "x = 10"

    def test_ipynb_only_changed(self, tmp_path):
        py, ipynb = _write_pair(tmp_path, ["x = 1", "y = 2"], ["x = 1", "y = 99"])
        base_py = _make_py_text("x = 1", "y = 2")
        base_ipynb = _make_ipynb_text("x = 1", "y = 2")
        with self._patch_git(base_py, base_ipynb):
            result = check_drift(py, ipynb)
        assert result.status == "merged"
        assert result.py_needs_update is True
        assert result.ipynb_needs_update is False
        assert result.merged_cells[1].source == "y = 99"

    def test_both_changed_same_cell_conflict(self, tmp_path):
        py, ipynb = _write_pair(tmp_path, ["x = 10"], ["x = 99"])
        base_py = _make_py_text("x = 1")
        base_ipynb = _make_ipynb_text("x = 1")
        with self._patch_git(base_py, base_ipynb):
            result = check_drift(py, ipynb)
        assert result.status == "conflict"
        assert 0 in result.conflict_indices

    def test_cell_count_mismatch_conflict(self, tmp_path):
        py, ipynb = _write_pair(tmp_path, ["x = 1", "y = 2"], ["x = 1"])
        base_py = _make_py_text("x = 1")
        base_ipynb = _make_ipynb_text("x = 1")
        with self._patch_git(base_py, base_ipynb):
            result = check_drift(py, ipynb)
        assert result.status == "conflict"
        assert len(result.conflict_indices) >= 1

    def test_no_git_base_drift_only_equal(self, tmp_path):
        """No git base + equal cells -> in_sync."""
        py, ipynb = _write_pair(tmp_path, ["x = 1"], ["x = 1"])
        with self._patch_git(None, None):
            result = check_drift(py, ipynb)
        assert result.status == "in_sync"

    def test_no_git_base_drift_only_unequal(self, tmp_path):
        """No git base + unequal cells -> drift_only."""
        py, ipynb = _write_pair(tmp_path, ["x = 1"], ["x = 99"])
        with self._patch_git(None, None):
            result = check_drift(py, ipynb)
        assert result.status == "drift_only"
        assert len(result.conflict_indices) >= 1

    def test_both_changed_different_cells_merged(self, tmp_path):
        """Both sides changed different cells -> merged, both files need update."""
        py, ipynb = _write_pair(tmp_path, ["x = 10", "y = 2"], ["x = 1", "y = 20"])
        base_py = _make_py_text("x = 1", "y = 2")
        base_ipynb = _make_ipynb_text("x = 1", "y = 2")
        with self._patch_git(base_py, base_ipynb):
            result = check_drift(py, ipynb)
        assert result.status == "merged"
        assert result.merged_cells[0].source == "x = 10"
        assert result.merged_cells[1].source == "y = 20"
