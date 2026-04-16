"""Tests for jupyter_jcli.diff_render."""

from __future__ import annotations

from jupyter_jcli.diff_render import locate_conflict_cells, render_no_baseline_diff


def _py_text(*sources: str, kernel: str = "python3") -> str:
    lines = [
        "# ---\n", "# jupyter:\n", "#   kernelspec:\n",
        f"#     name: {kernel}\n", "# ---\n\n",
    ]
    for src in sources:
        lines.append(f"# %%\n{src}\n\n")
    return "".join(lines)


class TestRenderNoBaselineDiff:
    def test_contains_plus_minus_lines(self):
        ours = _py_text("x = 1")
        theirs = _py_text("x = 99")
        diff = render_no_baseline_diff(ours, theirs)
        assert "-" in diff
        assert "+" in diff

    def test_includes_labels(self):
        ours = _py_text("x = 1")
        theirs = _py_text("x = 2")
        diff = render_no_baseline_diff(ours, theirs, ours_label="py", theirs_label="ipynb")
        assert "py" in diff
        assert "ipynb" in diff

    def test_empty_when_identical(self):
        text = _py_text("x = 1")
        diff = render_no_baseline_diff(text, text)
        assert diff == ""

    def test_truncation(self):
        ours = _py_text("x = " + "a" * 3000)
        theirs = _py_text("x = " + "b" * 3000)
        diff = render_no_baseline_diff(ours, theirs, max_chars=100)
        assert len(diff) <= 100 + 50  # a bit of slack for the truncation suffix
        assert "truncated" in diff


class TestLocateConflictCells:
    def test_no_conflicts_returns_empty(self):
        text = _py_text("x = 1", "y = 2")
        assert locate_conflict_cells(text) == []

    def test_finds_conflict_cell_index(self):
        # Simulate a merged text with conflict markers in cell 0
        text = (
            "# ---\n# jupyter:\n#   kernelspec:\n#     name: python3\n# ---\n\n"
            "# %%\n<<<<<<< py (current)\nx = 10\n||||||| py (HEAD)\nx = 1\n=======\nx = 99\n>>>>>>> ipynb (current)\n\n"
            "# %%\ny = 2\n\n"
        )
        indices = locate_conflict_cells(text)
        assert 0 in indices
        assert 1 not in indices

    def test_finds_multiple_conflict_cells(self):
        text = (
            "# ---\n# jupyter:\n#   kernelspec:\n#     name: python3\n# ---\n\n"
            "# %%\n<<<<<<< py (current)\nx = 10\n=======\nx = 99\n>>>>>>> ipynb (current)\n\n"
            "# %%\n<<<<<<< py (current)\ny = 20\n=======\ny = 99\n>>>>>>> ipynb (current)\n\n"
        )
        indices = locate_conflict_cells(text)
        assert 0 in indices
        assert 1 in indices

    def test_only_conflict_cells_returned(self):
        text = (
            "# ---\n# jupyter:\n#   kernelspec:\n#     name: python3\n# ---\n\n"
            "# %%\nx = 1\n\n"  # no conflict
            "# %%\n<<<<<<< py (current)\ny = 10\n=======\ny = 99\n>>>>>>> ipynb (current)\n\n"
        )
        indices = locate_conflict_cells(text)
        assert indices == [1]

    def test_malformed_text_returns_empty(self):
        assert locate_conflict_cells("") == []
