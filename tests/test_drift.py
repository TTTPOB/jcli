"""Tests for jupyter_jcli.drift."""

from pathlib import Path
from unittest.mock import call, patch

import nbformat
import pytest

from jupyter_jcli._enums import MergeMode
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

    def _patch_git(self, py_base: str | None, ipynb_base: str | None = None):
        """Return a patch context for _get_git_base_text.

        Only the py_base is used by check_drift; ipynb_base is accepted for
        backward compatibility in tests but is never consulted by the new logic.
        """
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
        """No git base + same count but different sources -> merged (py wins)."""
        py, ipynb = _write_pair(tmp_path, ["x = 1"], ["x = 99"])
        with self._patch_git(None, None):
            result = check_drift(py, ipynb)
        assert result.status == "merged"
        assert result.merge_mode == MergeMode.PY_WINS_NO_BASE
        assert result.ipynb_needs_update is True
        assert result.py_needs_update is False
        assert result.merged_cells[0].source == "x = 1"

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

    def test_ipynb_head_never_consulted(self, tmp_path):
        """.ipynb is gitignored by design; check_drift must never query its HEAD."""
        py, ipynb = _write_pair(tmp_path, ["x = 1"], ["x = 99"])
        base_py = _make_py_text("x = 1")

        calls_by_suffix: dict[str, int] = {".py": 0, ".ipynb": 0}

        def _side_effect(path: Path) -> str | None:
            calls_by_suffix[path.suffix] = calls_by_suffix.get(path.suffix, 0) + 1
            return base_py if path.suffix == ".py" else None

        with patch("jupyter_jcli.drift._get_git_base_text", side_effect=_side_effect):
            check_drift(py, ipynb)

        assert calls_by_suffix[".ipynb"] == 0, (
            "check_drift must not query the .ipynb git HEAD — "
            ".ipynb is always gitignored in jcli projects"
        )
        assert calls_by_suffix[".py"] >= 1

    def test_py_untracked_ipynb_only_exists(self, tmp_path):
        """With py untracked and same cell count, different sources -> py wins (MERGED)."""
        py, ipynb = _write_pair(tmp_path, ["x = 1"], ["x = 99"])
        with self._patch_git(None):
            result = check_drift(py, ipynb)
        assert result.status == "merged"
        assert result.merge_mode == MergeMode.PY_WINS_NO_BASE

    def test_py_untracked_sources_equal_is_in_sync(self, tmp_path):
        """With py untracked and equal sources -> IN_SYNC."""
        py, ipynb = _write_pair(tmp_path, ["x = 1", "y = 2"], ["x = 1", "y = 2"])
        with self._patch_git(None):
            result = check_drift(py, ipynb)
        assert result.status == "in_sync"

    def test_no_git_base_py_wins_py_canonical(self, tmp_path):
        """No git base + 2 non-empty cells each, different sources -> MERGED, py wins."""
        py, ipynb = _write_pair(tmp_path, ["x = 1", "y = 2"], ["x = 99", "y = 88"])
        with self._patch_git(None):
            result = check_drift(py, ipynb)
        assert result.status == "merged"
        assert result.merge_mode == MergeMode.PY_WINS_NO_BASE
        assert result.py_needs_update is False
        assert result.ipynb_needs_update is True
        assert [c.source for c in result.merged_cells] == ["x = 1", "y = 2"]

    def test_no_git_base_structural_mismatch_stays_drift_only(self, tmp_path):
        """No git base + cell count mismatch -> DRIFT_ONLY (structural divergence)."""
        py, ipynb = _write_pair(tmp_path, ["x = 1", "y = 2", "z = 3"], ["x = 99"])
        with self._patch_git(None):
            result = check_drift(py, ipynb)
        assert result.status == "drift_only"
        assert len(result.conflict_indices) >= 1

    def test_no_git_base_py_trailing_empty_cell_is_in_sync(self, tmp_path):
        """No git base + py has trailing empty cell -> filtered out, still IN_SYNC."""
        # _write_pair writes cells as-is; empty string -> empty cell in py
        py, ipynb = _write_pair(tmp_path, ["x = 1", ""], ["x = 1"])
        with self._patch_git(None):
            result = check_drift(py, ipynb)
        assert result.status == "in_sync"
