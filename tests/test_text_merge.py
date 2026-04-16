"""Tests for jupyter_jcli.text_merge."""

from __future__ import annotations

from unittest.mock import patch

from jupyter_jcli.text_merge import MergeResult, merge_three_way


def _simple_py(*cell_sources: str, kernel: str = "python3") -> str:
    lines = [
        "# ---\n", "# jupyter:\n", "#   kernelspec:\n",
        f"#     name: {kernel}\n", "# ---\n\n",
    ]
    for src in cell_sources:
        lines.append(f"# %%\n{src}\n\n")
    return "".join(lines)


class TestMergeThreeWay:
    def test_clean_no_changes(self):
        base = _simple_py("x = 1", "y = 2")
        result = merge_three_way(base, base, base)
        assert not result.has_conflict
        assert result.conflict_count == 0

    def test_clean_ours_only_changed(self):
        base = _simple_py("x = 1")
        ours = _simple_py("x = 10")
        result = merge_three_way(base, ours, base)
        assert not result.has_conflict
        assert "x = 10" in result.text

    def test_clean_theirs_only_changed(self):
        base = _simple_py("x = 1")
        theirs = _simple_py("x = 99")
        result = merge_three_way(base, base, theirs)
        assert not result.has_conflict
        assert "x = 99" in result.text

    def test_clean_different_cells_both_changed(self):
        base = _simple_py("x = 1", "y = 2")
        ours = _simple_py("x = 10", "y = 2")
        theirs = _simple_py("x = 1", "y = 20")
        result = merge_three_way(base, ours, theirs)
        assert not result.has_conflict
        assert "x = 10" in result.text
        assert "y = 20" in result.text

    def test_conflict_same_cell_both_changed(self):
        base = _simple_py("x = 1")
        ours = _simple_py("x = 10")
        theirs = _simple_py("x = 99")
        result = merge_three_way(base, ours, theirs)
        assert result.has_conflict
        assert result.conflict_count > 0
        assert "<<<<<<< py (current)" in result.text
        assert ">>>>>>> ipynb (current)" in result.text
        assert "||||||| py (HEAD)" in result.text

    def test_custom_labels_appear_in_conflict(self):
        base = "x = 1\n"
        ours = "x = 10\n"
        theirs = "x = 99\n"
        result = merge_three_way(base, ours, theirs, ours_label="mine", base_label="orig", theirs_label="yours")
        assert result.has_conflict
        assert "<<<<<<< mine" in result.text
        assert "||||||| orig" in result.text
        assert ">>>>>>> yours" in result.text

    def test_git_binary_missing_fallback(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("no git")):
            result = merge_three_way("x = 1\n", "x = 10\n", "x = 99\n")
        assert result.has_conflict
        assert "<<<<<<" in result.text
        assert "=======" in result.text
        assert ">>>>>>>" in result.text

    def test_ours_insert_cell_clean_merge(self):
        base = _simple_py("x = 1")
        ours = _simple_py("x = 1", "y = 2")
        theirs = _simple_py("x = 1")
        result = merge_three_way(base, ours, theirs)
        assert not result.has_conflict
        assert "y = 2" in result.text

    def test_theirs_insert_cell_clean_merge(self):
        base = _simple_py("x = 1")
        ours = _simple_py("x = 1")
        theirs = _simple_py("x = 1", "z = 3")
        result = merge_three_way(base, ours, theirs)
        assert not result.has_conflict
        assert "z = 3" in result.text

    def test_returns_merge_result_dataclass(self):
        base = "a = 1\n"
        result = merge_three_way(base, base, base)
        assert isinstance(result, MergeResult)
        assert isinstance(result.text, str)
        assert isinstance(result.has_conflict, bool)
        assert isinstance(result.conflict_count, int)
