"""Tests for jupyter_jcli.diff.drift."""

from pathlib import Path
from unittest.mock import patch

import nbformat

from jupyter_jcli.diff import (
    BaselineAvailable,
    BaselineMissing,
    Conflict,
    DriftOnly,
    InSync,
    Merged,
    check_drift,
)
from jupyter_jcli.formats import percent
from tests.helpers import make_ipynb_text, make_py_text

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_py_text_with_ids(cells: list[tuple[str | None, str]]) -> str:
    lines = [
        "# ---\n",
        "# jupyter:\n",
        "#   kernelspec:\n",
        "#     display_name: python3\n",
        "#     language: python\n",
        "#     name: python3\n",
        "# ---\n\n",
    ]
    for cell_id, source in cells:
        id_option = f' id="{cell_id}"' if cell_id is not None else ""
        lines.append(f"# %%{id_option}\n{source}\n\n")
    return "".join(lines)


def _py_with_metadata(metadata: dict, source: str = "x = 1") -> str:
    parsed = percent.loads(f"# %%\n{source}\n")
    parsed.notebook.metadata = metadata
    return percent.dumps(parsed, include_cell_ids=False, assign_missing_ids=False)


def _write_pair(
    tmp_path: Path, py_src: list[str], ipynb_src: list[str]
) -> tuple[Path, Path]:
    py = tmp_path / "nb.py"
    ipynb = tmp_path / "nb.ipynb"
    py.write_text(make_py_text(*py_src), encoding="utf-8")
    ipynb.write_text(make_ipynb_text(*ipynb_src), encoding="utf-8")
    return py, ipynb


# ---------------------------------------------------------------------------
# check_drift — with mocked git
# ---------------------------------------------------------------------------


class TestCheckDrift:
    """Tests for check_drift() using mocked _get_git_base_text."""

    def _patch_git(self, py_base: str | None):
        def _side_effect(path: Path) -> str | None:
            if path.suffix == ".py":
                return py_base
            return None

        return patch(
            "jupyter_jcli.diff.drift._get_git_base_text", side_effect=_side_effect
        )

    def test_in_sync_no_drift(self, tmp_path):
        py, ipynb = _write_pair(tmp_path, ["x = 1", "y = 2"], ["x = 1", "y = 2"])
        base_py = make_py_text("x = 1", "y = 2")
        with self._patch_git(base_py):
            result = check_drift(py, ipynb)
        assert isinstance(result, InSync)
        assert isinstance(result.baseline, BaselineAvailable)

    def test_new_cell_gets_id_written_to_both_sides(self, tmp_path):
        notebook = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell("x = 1")])
        notebook.metadata["kernelspec"] = {
            "name": "python3",
            "display_name": "python3",
            "language": "python",
        }
        existing_id = notebook.cells[0].id
        py = tmp_path / "nb.py"
        ipynb = tmp_path / "nb.ipynb"
        base = make_py_text_with_ids([(existing_id, "x = 1")])
        py.write_text(
            make_py_text_with_ids([(existing_id, "x = 1"), (None, "y = 2")]),
            encoding="utf-8",
        )
        nbformat.write(notebook, str(ipynb))

        with self._patch_git(base):
            result = check_drift(py, ipynb)

        assert isinstance(result, Merged)
        assert result.py_needs_update is True
        assert result.ipynb_needs_update is True
        assert result.target_state.cells[0].cell_id == existing_id
        assert result.target_state.cells[1].cell_id is not None

    def test_commented_magic_is_in_sync_with_notebook_magic(self, tmp_path):
        py, ipynb = _write_pair(
            tmp_path,
            ["# %load_ext autoreload\n# %autoreload 2"],
            ["%load_ext autoreload\n%autoreload 2"],
        )
        base_py = make_py_text("# %load_ext autoreload\n# %autoreload 2")

        with self._patch_git(base_py):
            result = check_drift(py, ipynb)

        assert isinstance(result, InSync)

    def test_py_only_changed(self, tmp_path):
        py, ipynb = _write_pair(tmp_path, ["x = 10", "y = 2"], ["x = 1", "y = 2"])
        base_py = make_py_text("x = 1", "y = 2")
        with self._patch_git(base_py):
            result = check_drift(py, ipynb)
        assert isinstance(result, Merged)
        assert result.ipynb_needs_update is True
        assert result.py_needs_update is False
        assert result.target_state.cells[0].source == "x = 10"

    def test_ipynb_only_changed(self, tmp_path):
        py, ipynb = _write_pair(tmp_path, ["x = 1", "y = 2"], ["x = 1", "y = 99"])
        base_py = make_py_text("x = 1", "y = 2")
        with self._patch_git(base_py):
            result = check_drift(py, ipynb)
        assert isinstance(result, Merged)
        assert result.py_needs_update is True
        assert result.ipynb_needs_update is False
        assert result.target_state.cells[1].source == "y = 99"

    def test_both_changed_same_cell_conflict(self, tmp_path):
        py, ipynb = _write_pair(tmp_path, ["x = 10"], ["x = 99"])
        base_py = make_py_text("x = 1")
        with self._patch_git(base_py):
            result = check_drift(py, ipynb)
        assert isinstance(result, Conflict)
        assert 0 in result.conflict_indices
        assert not hasattr(result, "baseline")

    def test_ours_insert_cell_auto_merges(self, tmp_path):
        """ours (py) adds a cell; theirs (ipynb) unchanged from base -> MERGED."""
        py, ipynb = _write_pair(tmp_path, ["x = 1", "y = 2"], ["x = 1"])
        base_py = make_py_text("x = 1")
        with self._patch_git(base_py):
            result = check_drift(py, ipynb)
        assert isinstance(result, Merged)
        assert result.ipynb_needs_update is True
        assert any(c.source == "y = 2" for c in result.target_state.cells)

    def test_theirs_insert_cell_auto_merges(self, tmp_path):
        """theirs (ipynb) adds a cell; ours (py) unchanged from base -> MERGED."""
        py, ipynb = _write_pair(tmp_path, ["x = 1"], ["x = 1", "z = 3"])
        base_py = make_py_text("x = 1")
        with self._patch_git(base_py):
            result = check_drift(py, ipynb)
        assert isinstance(result, Merged)
        assert result.py_needs_update is True
        assert any(c.source == "z = 3" for c in result.target_state.cells)

    def test_no_git_base_sources_equal_is_in_sync(self, tmp_path):
        """No git base + equal content -> in_sync with a canonical seed."""
        py, ipynb = _write_pair(tmp_path, ["x = 1"], ["x = 1"])
        with self._patch_git(None):
            result = check_drift(py, ipynb)
        assert isinstance(result, InSync)
        assert isinstance(result.baseline, BaselineMissing)
        assert result.baseline.seed_text == percent.canonicalize(
            py.read_text(encoding="utf-8"), include_cell_ids=False
        )

    def test_no_git_base_different_content_is_drift_only(self, tmp_path):
        """No git base + any content difference -> DRIFT_ONLY (no side wins)."""
        py, ipynb = _write_pair(tmp_path, ["x = 1"], ["x = 99"])
        with self._patch_git(None):
            result = check_drift(py, ipynb)
        assert isinstance(result, DriftOnly)
        assert result.diff_text != ""
        assert not hasattr(result, "baseline")

    def test_no_git_base_count_mismatch_drift_only(self, tmp_path):
        """No git base + cell count mismatch -> DRIFT_ONLY."""
        py, ipynb = _write_pair(tmp_path, ["x = 1", "y = 2", "z = 3"], ["x = 99"])
        with self._patch_git(None):
            result = check_drift(py, ipynb)
        assert isinstance(result, DriftOnly)
        assert result.diff_text != ""

    def test_both_changed_different_cells_merged(self, tmp_path):
        """Both sides changed different cells -> merged, both files need update."""
        py, ipynb = _write_pair(tmp_path, ["x = 10", "y = 2"], ["x = 1", "y = 20"])
        base_py = make_py_text("x = 1", "y = 2")
        with self._patch_git(base_py):
            result = check_drift(py, ipynb)
        assert isinstance(result, Merged)
        assert result.target_state.cells[0].source == "x = 10"
        assert result.target_state.cells[1].source == "y = 20"

    def test_ipynb_head_never_consulted(self, tmp_path):
        """.ipynb is gitignored by design; check_drift must never query its HEAD."""
        py, ipynb = _write_pair(tmp_path, ["x = 1"], ["x = 99"])
        base_py = make_py_text("x = 1")

        calls_by_suffix: dict[str, int] = {".py": 0, ".ipynb": 0}

        def _side_effect(path: Path) -> str | None:
            calls_by_suffix[path.suffix] = calls_by_suffix.get(path.suffix, 0) + 1
            return base_py if path.suffix == ".py" else None

        with patch(
            "jupyter_jcli.diff.drift._get_git_base_text", side_effect=_side_effect
        ):
            check_drift(py, ipynb)

        assert calls_by_suffix[".ipynb"] == 0, (
            "check_drift must not query the .ipynb git HEAD — "
            ".ipynb is always gitignored in jcli projects"
        )
        assert calls_by_suffix[".py"] >= 1

    def test_py_untracked_sources_equal_is_in_sync(self, tmp_path):
        """With py untracked and equal sources -> IN_SYNC."""
        py, ipynb = _write_pair(tmp_path, ["x = 1", "y = 2"], ["x = 1", "y = 2"])
        with self._patch_git(None):
            result = check_drift(py, ipynb)
        assert isinstance(result, InSync)

    def test_no_git_base_trailing_empty_cell_difference_is_drift(self, tmp_path):
        py, ipynb = _write_pair(tmp_path, ["x = 1", ""], ["x = 1"])
        with self._patch_git(None):
            result = check_drift(py, ipynb)
        assert isinstance(result, DriftOnly)

    def test_no_git_base_matching_empty_cells_are_in_sync(self, tmp_path):
        py, ipynb = _write_pair(tmp_path, ["", "x = 1", ""], ["", "x = 1", ""])
        with self._patch_git(None):
            result = check_drift(py, ipynb)
        assert isinstance(result, InSync)

    def test_formatter_removing_eof_blank_lines_is_in_sync(self, tmp_path):
        py, ipynb = _write_pair(tmp_path, ["x = 1"], ["x = 1\n"])
        py.write_text(py.read_text(encoding="utf-8").rstrip("\n"), encoding="utf-8")

        with self._patch_git(make_py_text("x = 1")):
            result = check_drift(py, ipynb)

        assert isinstance(result, InSync)

    def test_in_sync_has_no_merge_or_diff_fields(self, tmp_path):
        """IN_SYNC only carries its explicit baseline state."""
        py, ipynb = _write_pair(tmp_path, ["x = 1"], ["x = 1"])
        with self._patch_git(None):
            result = check_drift(py, ipynb)
        assert isinstance(result, InSync)
        assert not hasattr(result, "merged_cells")
        assert not hasattr(result, "diff_text")

    def test_diff_text_nonempty_in_conflict(self, tmp_path):
        """CONFLICT result has diff_text containing conflict markers."""
        py, ipynb = _write_pair(tmp_path, ["x = 10"], ["x = 99"])
        base_py = make_py_text("x = 1")
        with self._patch_git(base_py):
            result = check_drift(py, ipynb)
        assert isinstance(result, Conflict)
        assert "<<<<<<<" in result.diff_text
        assert "=======" in result.diff_text
        assert ">>>>>>>" in result.diff_text

    def test_diff_text_nonempty_in_drift_only(self, tmp_path):
        """DRIFT_ONLY result has diff_text with unified diff lines."""
        py, ipynb = _write_pair(tmp_path, ["x = 1"], ["x = 99"])
        with self._patch_git(None):
            result = check_drift(py, ipynb)
        assert isinstance(result, DriftOnly)
        assert result.diff_text != ""
        assert "-" in result.diff_text or "+" in result.diff_text

    def test_merged_has_only_merge_fields(self, tmp_path):
        """MERGED only carries merge output and update decisions."""
        py, ipynb = _write_pair(tmp_path, ["x = 10"], ["x = 1"])
        base_py = make_py_text("x = 1")
        with self._patch_git(base_py):
            result = check_drift(py, ipynb)
        assert isinstance(result, Merged)
        assert not hasattr(result, "diff_text")
        assert not hasattr(result, "conflict_indices")
        assert not hasattr(result, "baseline")

    def test_same_name_language_and_display_changes_are_shared(self, tmp_path):
        base_metadata = {
            "kernelspec": {
                "name": "env",
                "display_name": "Old",
                "language": "python",
            }
        }
        py = tmp_path / "nb.py"
        ipynb_path = tmp_path / "nb.ipynb"
        base = _py_with_metadata(base_metadata)
        py.write_text(base, encoding="utf-8")
        notebook = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell("x = 1")])
        notebook.metadata["kernelspec"] = {
            "name": "env",
            "display_name": "New",
            "language": "julia",
        }
        nbformat.write(notebook, str(ipynb_path))

        with self._patch_git(base):
            result = check_drift(py, ipynb_path)

        assert isinstance(result, Merged)
        assert result.py_needs_update is True
        assert (
            result.target_state.metadata["kernelspec"]
            == notebook.metadata["kernelspec"]
        )

    def test_metadata_same_path_conflict_is_structured(self, tmp_path):
        base = _py_with_metadata({"custom": {"value": "base"}})
        py = tmp_path / "nb.py"
        ipynb_path = tmp_path / "nb.ipynb"
        py.write_text(_py_with_metadata({"custom": {"value": "py"}}), encoding="utf-8")
        notebook = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell("x = 1")])
        notebook.metadata["custom"] = {"value": "notebook"}
        nbformat.write(notebook, str(ipynb_path))

        with self._patch_git(base):
            result = check_drift(py, ipynb_path)

        assert isinstance(result, Conflict)
        assert result.conflict_indices == []
        assert result.metadata_conflicts[0].display_path == "metadata.custom.value"
        assert "base: 'base'" in result.diff_text
        assert "py: 'py'" in result.diff_text
        assert "notebook: 'notebook'" in result.diff_text

    def test_excluded_metadata_changes_do_not_drift(self, tmp_path):
        metadata = {"kernelspec": {"name": "env", "display_name": "Env"}}
        base = _py_with_metadata(metadata)
        py = tmp_path / "nb.py"
        ipynb_path = tmp_path / "nb.ipynb"
        py.write_text(base, encoding="utf-8")
        notebook = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell("x = 1")])
        notebook.metadata.update(metadata)
        notebook.metadata["widgets"] = {"state": "local"}
        notebook.metadata["language_info"] = {"version": "3.12"}
        nbformat.write(notebook, str(ipynb_path))

        with self._patch_git(base):
            result = check_drift(py, ipynb_path)

        assert isinstance(result, InSync)
